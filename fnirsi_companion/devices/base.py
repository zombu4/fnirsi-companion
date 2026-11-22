"""Common device interface of the FNIRSI companion (API.md, "devices/base.py").

Holds what both meter modules share: the Reading model (the record of one measurement that Live,
Trend and Log use), the Command table entry, the INFO record (the 46-byte layout is the same on
the LC1020E and the 2C53T; LC1020E PROTOCOL.md section 6.4), the Device base class (capability
check, command call with the time limit of the table, live stream plumbing) and identify(), which
reads INFO and applies the identification rules of DESIGN.md section 5 and 13.21.

Cancellation (DESIGN.md 3: long jobs "can be cancelled between requests"): the job runner puts a
CancelToken in place for the thread of a job (cancel_scope). A device method opens a cancellable
phase with Device._cancellable() only around steps that may stop between two requests without
harm (reading a backup, a screenshot, the 2C53T staging before FW_INSTALL, waiting for an update
drive); it tests Device._cancel_point() between the requests. Outside such a phase a request to
cancel is refused, so a save, an install or a copy always runs to its end. Leaving a phase raises
Cancelled when a cancel was accepted after the last test, so the step after the phase is not sent.

The serial link is imported here only for its three exception classes; every call goes through
the link object that is handed to the device (request / exclusive / signals).
"""

from __future__ import annotations

import re
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..link.port import AnswerTimeout, PortLost, StatusError

__all__ = [
    "AnswerTimeout", "PortLost", "StatusError", "Reading", "Command", "Info", "Device",
    "DeviceError", "CapabilityError", "UnknownCommand", "SafetyRefused", "IdentifyError",
    "identify", "INFO_SIZE", "CONFIRM_WORD", "Cancelled", "CancelToken", "cancel_scope",
    "cancel_token", "payload_bytes",
]

INFO_SIZE = 46
CONFIRM_WORD = "OK"        # the typed confirmation the UI collects (DESIGN 9, 13.8)


class DeviceError(Exception):
    """Base of the errors the device modules raise themselves."""


class CapabilityError(DeviceError):
    """The command's capability bit is clear in the meter's INFO (DESIGN 13.5)."""


class UnknownCommand(DeviceError):
    """The command code is not in the module's command table."""


class SafetyRefused(DeviceError):
    """A save / destroy method was refused: missing confirmation, no backup folder, or the
    backup does not belong to this meter (DESIGN 13.8, 13.9)."""


class Cancelled(DeviceError):
    """The user cancelled the job at a point between two requests where it may stop."""


class CancelToken:
    """The cancel state of one job, shared by the job's thread and the GUI thread.

    allowed: the job is in a cancellable phase now; why_not: what the job does while it is not;
    request() is the GUI's call (True when accepted); changed(allowed) is called on every change
    of `allowed` (from the job's thread)."""

    NOT_ALLOWED = "the job is at a step that must run to its end"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self._allowed = False
        self.why_not = self.NOT_ALLOWED
        self.changed = None

    @property
    def allowed(self) -> bool:
        return self._allowed

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> bool:
        """Asks the job to stop at its next cancel point; refused (False) outside a cancellable
        phase."""
        with self._lock:
            if not self._allowed:
                return False
            self._requested = True
            return True

    def _set(self, allowed: bool, why_not: str) -> None:
        with self._lock:
            if not allowed and self._requested:
                raise Cancelled("cancelled by the user")
            changed = allowed != self._allowed
            self._allowed, self.why_not = allowed, why_not
        if changed and self.changed is not None:
            self.changed(allowed)

    def check(self) -> None:
        if self._requested:
            raise Cancelled("cancelled by the user")


_scope = threading.local()


@contextmanager
def cancel_scope(token: CancelToken):
    """Makes `token` the cancel token of the calling thread for the duration of the block."""
    prev = getattr(_scope, "token", None)
    _scope.token = token
    try:
        yield token
    finally:
        _scope.token = prev


def cancel_token() -> CancelToken | None:
    """The cancel token of the calling thread (None outside a job)."""
    return getattr(_scope, "token", None)


def payload_bytes(payload) -> bytes:
    """A request payload as bytes. bytes, bytearray, memoryview and sequences of byte values are
    taken; an int is refused (bytes(n) would be n zero bytes, not the byte n)."""
    if isinstance(payload, int):
        raise TypeError("a payload is a byte string or a sequence of byte values, not an int")
    return bytes(payload)


class IdentifyError(DeviceError):
    """INFO does not match the expected model. `answers_as` names what the meter answers as."""

    def __init__(self, expected: str, answers_as: str, detail: str):
        self.expected, self.answers_as, self.detail = expected, answers_as, detail
        super().__init__("expected %s, meter answers as %s: %s" % (expected, answers_as, detail))


@dataclass
class Reading:
    """One measurement. value is in the SI base unit of `unit` (F, H, ohm, V, A ...) or None."""
    t: float                    # host monotonic seconds
    wall: float                 # host wall clock seconds (time.time())
    meter_ms: int | None        # the meter's own time of the record
    quantity: str               # "C", "R", "L", "Z", "V DC", ...
    value: float | None
    unit: str                   # SI unit symbol of value
    text: str                   # as the meter shows it, with its unit text
    decimals: int | None
    validity: str               # "valid" | "overload" | "no reading" | "held" | "unknown"
    secondary: "Reading | None"
    key: tuple                  # statistics / trend restart when it changes
    unspecified: bool = False   # outside the manual's accuracy table (LC1020E MEASURE marker)
    gain_flags: int | None = None    # LC1020E MEASURE byte 65 (OCAL flags); None = not sent


@dataclass
class Command:
    """One entry of a module's command table."""
    code: int
    name: str
    cap_bit: int | None         # INFO capability bit; None = always available
    timeout: float              # seconds the host waits for the answer
    dangerous: bool             # saves, destroys or restarts: only through the confirmed methods


@dataclass
class Info:
    """INFO record, 46 bytes (LC1020E PROTOCOL.md 6.4; the 2C53T answers the same layout)."""
    protocol: int
    capabilities: int
    firmware: str               # text field, trailing zero bytes removed
    uid: bytes                  # 12 bytes chip unique ID
    running_slot: int
    slot_version: int
    app_size: int
    update_result: int
    raw: bytes = field(default=b"", repr=False)

    _FORMAT = "<BI16s12sBIII"

    @classmethod
    def parse(cls, data: bytes) -> "Info":
        if len(data) < INFO_SIZE:
            raise DeviceError("INFO answer of %d bytes, expected %d" % (len(data), INFO_SIZE))
        p, caps, fw, uid, slot, ver, size, res = struct.unpack_from(cls._FORMAT, data)
        return cls(p, caps, fw.rstrip(b"\0").decode("ascii", "replace"), bytes(uid), slot, ver,
                   size, res, bytes(data[:INFO_SIZE]))

    @property
    def uid_hex(self) -> str:
        """The UID as 24 upper-case hex digits (the USB serial number, SL section 1)."""
        return self.uid.hex().upper()

    def has(self, bit: int) -> bool:
        return bool(self.capabilities >> bit & 1)


class Device(QObject):
    """A connected meter. Subclasses set `model`, `COMMANDS` and implement start_live, stop_live
    and backup. `link` is the SerialLink of API.md (or a test double with the same calls)."""

    reading = Signal(object)    # Reading
    state = Signal(object)      # the model's STATE record
    meter_event = Signal(object)      # the model's event record

    model: str = ""
    COMMANDS: dict[int, Command] = {}

    def __init__(self, link, info: Info):
        super().__init__()
        self.link = link
        self.info = info

    # -- command table ------------------------------------------------------------------------
    def command(self, code: int) -> Command:
        try:
            return self.COMMANDS[code]
        except KeyError:
            raise UnknownCommand("command 0x%02X is not in the %s command table"
                                 % (code, self.model)) from None

    def has(self, code: int) -> bool:
        """True when the capability bit of the command is set in INFO (or the command has none)."""
        cmd = self.COMMANDS.get(code)
        if cmd is None:
            return False
        return cmd.cap_bit is None or self.info.has(cmd.cap_bit)

    def timeout_for(self, code: int, payload: bytes = b"") -> float:
        """Seconds to wait for the answer of this request; modules override it where the limit
        depends on the payload."""
        return self.command(code).timeout

    # -- cancellation (module docstring) ------------------------------------------------------
    @staticmethod
    def _cancel_point() -> None:
        """Between two requests of a cancellable phase: raises Cancelled when the user asked."""
        token = cancel_token()
        if token is not None:
            token.check()

    @staticmethod
    @contextmanager
    def _cancellable():
        """A phase in which the job may stop between requests. On leaving it the token returns
        to the state before; when that state forbids cancelling and a cancel was accepted, the
        exit raises Cancelled (the step after the phase is then not sent)."""
        token = cancel_token()
        if token is None:
            yield
            return
        was, why = token.allowed, token.why_not
        token._set(True, "")
        ok = False
        try:
            yield
            ok = True
        finally:
            if not was:
                if ok:
                    token._set(False, why)
                else:
                    with token._lock:
                        token._allowed, token.why_not = False, why
                    if token.changed is not None:
                        token.changed(False)

    def call(self, code: int, payload: bytes = b"", *, allow_dangerous: bool = False) -> bytes:
        """Sends one request after the capability check and returns the answer data (after the
        status byte). Dangerous commands are refused unless the confirmed method that owns them
        passes allow_dangerous=True. The payload is turned into bytes before any check."""
        payload = payload_bytes(payload)
        cmd = self.command(code)
        if not self.has(code):
            raise CapabilityError("%s: capability bit %s is clear in INFO" % (cmd.name, cmd.cap_bit))
        if cmd.dangerous and not allow_dangerous:
            raise SafetyRefused("%s is dangerous: use its confirmed method" % cmd.name)
        return self.link.request(code, payload, self.timeout_for(code, payload))

    # -- to be provided by the model ----------------------------------------------------------
    def start_live(self) -> None:
        raise NotImplementedError("%s does not implement start_live" % type(self).__name__)

    def stop_live(self) -> None:
        raise NotImplementedError("%s does not implement stop_live" % type(self).__name__)

    def backup(self, folder: Path) -> Path:
        """Writes the meter's settings / calibration to `folder`; returns the file (header with
        model, UID, date and CRC-32s)."""
        raise NotImplementedError("%s does not implement backup" % type(self).__name__)


_VERSION_TEXT = re.compile(r"\d+\.\d+\.\d+")


def _answers_as(text: str) -> str:
    if text.startswith("LC1020E"):
        return "LC1020E"
    if _VERSION_TEXT.fullmatch(text):
        return "2C53T"
    return "unknown (%r)" % text


def identify(link, model: str) -> Info:
    """INFO of the meter on `link`, checked against `model`: protocol version 1 and the firmware
    text rule (DESIGN 5; 13.21): LC1020E text begins "LC1020E"; 2C53T text is digits and dots
    "N.N.N" and nothing else. Raises IdentifyError otherwise. The link carries no state."""
    if model not in ("LC1020E", "2C53T"):
        raise ValueError("unknown model %r" % (model,))
    info = Info.parse(link.request(0x00, b"", 1.5))
    if info.protocol != 1:
        raise IdentifyError(model, _answers_as(info.firmware),
                            "protocol version %d, expected 1" % info.protocol)
    answers = _answers_as(info.firmware)
    if answers != model:
        raise IdentifyError(model, answers, "firmware text %r" % info.firmware)
    return info
