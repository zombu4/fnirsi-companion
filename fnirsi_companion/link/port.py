"""SerialLink: the serial control channel of one meter, owned by one worker thread.

Rules of the link (DESIGN.md section 4 and 13.11 / 13.12; LC1020E PROTOCOL.md section 4.1):

- One request in flight. The next request is written only after the answer of the previous one
  or after its time limit. The answer is the frame of type 2 with the request's seq and cmd; its
  first payload byte is the status, the bytes after it are the answer data.
- Stream frames (type 3) and event frames (type 4) are emitted as the signals `stream` and
  `event_frame` as they arrive, at any time, also while a request waits for its answer.
- After a time limit the request fails with AnswerTimeout and the link resynchronises: answers
  with another seq (a late answer of the timed-out request) are dropped, and a PING (cmd 1, four
  bytes) must be answered with the same four bytes before the next request is written. The
  meter takes one request at a time in the order it receives them, so once the PING is answered
  every earlier answer has left it. When PING_ATTEMPTS PINGs in a row get no answer the link is
  declared lost.
- exclusive(): a job thread holds the port; requests of other threads wait in the queue until
  it lets go. Requests of the holding thread go first.
- A read or write error of the port, or mark_lost() (called by the program when the port has
  left the system's port list), ends the link: every open and queued request fails with
  PortLost and the signal `lost` is emitted once with the reason.

Threads: the worker thread is a plain Python thread; it alone touches the serial object. The
signals are emitted in the worker thread; receivers in the GUI thread connect them with
Qt.QueuedConnection (or rely on AutoConnection, which queues across threads). Callbacks added to
the futures of request_async() also run in the worker thread.
"""

from __future__ import annotations

import collections
import concurrent.futures
import struct
import threading
import time
from contextlib import contextmanager
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .frames import ANSWER, EVENT, MAX_PAYLOAD, REQUEST, STREAM, FrameParser, encode

BAUD = 115200
READ_TIMEOUT = 0.02             # seconds one read waits for the first byte (worker loop period)
WRITE_TIMEOUT = 2.0             # a write blocked longer than this ends the link
PING = 0x01
PING_TIMEOUT = 1.5              # LC1020E PROTOCOL.md 4.3: commands answered in the pass, 1.5 s
PING_ATTEMPTS = 3


class PortLost(Exception):
    """The link is dead: the port failed, left the system, or the link was closed."""


class AnswerTimeout(Exception):
    """No answer within the request's time limit."""


class StatusError(Exception):
    """An answer with a status other than 0. data: the answer bytes after the status byte."""

    def __init__(self, cmd: int, status: int, data: bytes = b""):
        self.cmd, self.status, self.data = cmd, status, bytes(data)
        super().__init__("command 0x%02X: status %d" % (cmd, status))


class _Lost(Exception):
    """Inside the worker: ends the link with the reason given."""


def _pyserial(port: str):
    """The port opened with pyserial. On macOS and Linux a port can be opened by several programs
    at once (measured 2026-11-21: a second program opened the LC1020E's port while the companion
    had it, and both read the meter's frames); exclusive=True (TIOCEXCL plus flock) refuses a
    second exclusive opener, so two companions never share a meter. Windows opens a COM port for
    one program only by itself; pyserial takes the parameter on POSIX only."""
    import os
    import serial
    extra = {"exclusive": True} if os.name == "posix" else {}
    return serial.Serial(port, BAUD, timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT, **extra)


class _Job:
    __slots__ = ("cmd", "payload", "timeout", "future", "owner", "ping", "seq")

    def __init__(self, cmd, payload, timeout, future, owner, ping=False):
        self.cmd, self.payload, self.timeout = cmd, payload, timeout
        self.future, self.owner, self.ping = future, owner, ping
        self.seq = None


class SerialLink(QObject):
    """One open serial port of one meter. See the module docstring for the rules."""

    stream = Signal(object)     # Frame of type 3
    event_frame = Signal(object)    # Frame of type 4 (not "event": that would shadow QObject.event)
    lost = Signal(str)          # reason; after it the link is dead

    def __init__(self, port: str, *, serial_factory: Callable[[str], object] | None = None):
        super().__init__()
        self.port = port
        self._factory = serial_factory or _pyserial
        self._ser = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._owner_free = threading.Condition(self._lock)
        self._pending: collections.deque[_Job] = collections.deque()
        self._controls: collections.deque = collections.deque()
        self._owner: int | None = None
        self._owner_depth = 0
        self._stop = threading.Event()
        self._dead: str | None = None
        self._dtr = False
        # worker-only state
        self._parser = FrameParser()
        self._seq = 0
        self._inflight: _Job | None = None
        self._deadline = 0.0
        self._resync = False
        self._ping_failures = 0
        self._ping_count = 0
        self.stats = {"frames_in": 0, "crc_errors": 0, "skipped": 0, "timeouts": 0, "stale": 0}

    # -- state --------------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """True from open() until the link is closed or lost."""
        with self._lock:
            return self._thread is not None and self._dead is None

    @property
    def dtr(self) -> bool:
        """The DTR state last set on the port."""
        return self._dtr

    # -- open / close -------------------------------------------------------------------------
    def open(self) -> None:
        """Opens the port (115200 8N1), sets DTR and starts the worker thread. An open that fails
        raises PortLost with the system's message (for example "permission denied")."""
        with self._lock:
            if self._thread is not None or self._dead is not None:
                raise RuntimeError("SerialLink.open() may be called once")
        try:
            ser = self._factory(self.port)
        except OSError as e:
            raise PortLost("%s: %s" % (self.port, e)) from e
        try:
            ser.dtr = True
        except OSError as e:
            try:
                ser.close()
            except OSError:
                pass        # the open already failed; the DTR error is the one reported
            raise PortLost("%s: setting DTR failed: %s" % (self.port, e)) from e
        self._ser = ser
        self._dtr = True
        thread = threading.Thread(target=self._run, name="SerialLink %s" % self.port, daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def close(self) -> None:
        """Stops the worker, closes the port; open and queued requests fail with PortLost. The
        signal `lost` is not emitted for a close. Safe to call more than once."""
        with self._lock:
            thread = self._thread
            if thread is None:
                if self._dead is None:
                    self._dead = "link closed"
                return
        self._stop.set()
        if threading.get_ident() != thread.ident:
            thread.join()

    def mark_lost(self, reason: str) -> None:
        """Ends the link from outside, for example when the meter's port has left the system's
        port list without a read error yet. Emits `lost` with reason."""
        def fail():
            raise _Lost(reason)
        self._control(fail, wait=False)

    # -- requests -----------------------------------------------------------------------------
    def request_async(self, cmd: int, payload: bytes = b"",
                      timeout: float = 1.5) -> concurrent.futures.Future:
        """Queues a request and returns a Future of the answer data (bytes after the status).
        The future fails with StatusError, AnswerTimeout or PortLost. A future cancelled before
        its request is written is skipped. Does not block."""
        if not 0 <= cmd <= 0xFF:
            raise ValueError("command %r outside 0..255" % (cmd,))
        payload = bytes(payload)
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("payload of %d bytes: at most %d" % (len(payload), MAX_PAYLOAD))
        if not timeout > 0:
            raise ValueError("timeout must be above 0")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        job = _Job(cmd, payload, float(timeout), fut, threading.get_ident())
        with self._lock:
            if self._thread is None and self._dead is None:
                raise RuntimeError("SerialLink is not open")
            if self._dead is not None:
                fut.set_exception(PortLost(self._dead))
                return fut
            self._pending.append(job)
        return fut

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 1.5) -> bytes:
        """Sends one request and blocks until its answer; returns the answer data after the
        status byte. Raises StatusError, AnswerTimeout, PortLost. It blocks the calling thread
        for up to the time limit (and longer while another thread holds exclusive()), so it is
        meant for worker and job threads; the GUI uses request_async()."""
        self._not_worker("request()")
        return self.request_async(cmd, payload, timeout).result()

    @contextmanager
    def exclusive(self):
        """Holds the port for the calling thread: requests of other threads wait until the block
        ends. Re-entrant. Waits while another thread holds it (call it from a job thread)."""
        self._not_worker("exclusive()")
        me = threading.get_ident()
        with self._owner_free:
            while self._dead is None and self._owner not in (None, me):
                self._owner_free.wait()
            if self._dead is not None:
                raise PortLost(self._dead)
            self._owner = me
            self._owner_depth += 1
        try:
            yield self
        finally:
            with self._owner_free:
                self._owner_depth -= 1
                if self._owner_depth == 0:
                    self._owner = None
                    self._owner_free.notify_all()

    def set_dtr(self, on: bool) -> None:
        """Sets or clears DTR (the worker applies it). Raises PortLost when the link is dead."""
        def apply():
            self._ser.dtr = bool(on)
            self._dtr = bool(on)
        self._control(apply, wait=True)

    # -- calls into the worker ----------------------------------------------------------------
    def _not_worker(self, what: str) -> None:
        thread = self._thread
        if thread is not None and threading.get_ident() == thread.ident:
            raise RuntimeError("%s on the link's own worker thread would wait for itself" % what)

    def _control(self, fn: Callable[[], None], wait: bool) -> None:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if self._thread is None and self._dead is None:
                raise RuntimeError("SerialLink is not open")
            if self._dead is not None:
                if wait:
                    raise PortLost(self._dead)
                return
            self._controls.append((fn, fut))
        thread = self._thread
        if wait and threading.get_ident() != thread.ident:
            fut.result()

    # -- worker -------------------------------------------------------------------------------
    def _run(self) -> None:
        reason, emit = "link closed", False
        try:
            while not self._stop.is_set():
                self._run_controls()
                if self._inflight is None:
                    self._send_next()
                self._read_once()
                self._check_deadline()
        except _Lost as e:
            reason, emit = str(e), True
        except Exception as e:
            # a fault of this module: the link ends so that no request waits forever, and the
            # exception is raised again so that the thread's excepthook reports it
            self._die("link worker failed: %s: %s" % (type(e).__name__, e), True)
            raise
        self._die(reason, emit)

    def _run_controls(self) -> None:
        while True:
            with self._lock:
                if not self._controls:
                    return
                fn, fut = self._controls.popleft()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fn()
            except _Lost as e:
                fut.set_exception(PortLost(str(e)))
                raise
            except OSError as e:
                fut.set_exception(PortLost("port control failed: %s" % e))
                raise _Lost("port control failed: %s" % e) from e
            fut.set_result(None)

    def _send_next(self) -> None:
        if self._resync:
            self._ping_count = (self._ping_count + 1) & 0xFFFFFFFF
            ping = _Job(PING, struct.pack("<I", self._ping_count), PING_TIMEOUT, None, None, True)
            self._write(ping)
            return
        while True:
            with self._lock:
                job = None
                for j in self._pending:
                    if self._owner is None or j.owner == self._owner:
                        job = j
                        break
                if job is None:
                    return
                self._pending.remove(job)
            if job.future.set_running_or_notify_cancel():
                self._write(job)
                return

    def _write(self, job: _Job) -> None:
        self._seq = (self._seq + 1) & 0xFF
        job.seq = self._seq
        frame = encode(job.seq, REQUEST, job.cmd, job.payload)
        self._inflight = job
        self._deadline = time.monotonic() + job.timeout
        try:
            self._ser.write(frame)
        except OSError as e:
            raise _Lost("write failed: %s" % e) from e

    def _read_once(self) -> None:
        ser = self._ser
        try:
            n = ser.in_waiting
            data = ser.read(n if n else 1)
        except OSError as e:
            raise _Lost("read failed: %s" % e) from e
        if not data and not self._parser.waiting:
            return
        # with no new bytes the feed still lets the parser's time limit for an incomplete frame
        # run out (link/frames.py, INCOMPLETE_LIMIT)
        frames = self._parser.feed(data, time.monotonic())
        self.stats["crc_errors"] = self._parser.crc_errors
        self.stats["skipped"] = self._parser.skipped
        for f in frames:
            self.stats["frames_in"] += 1
            if f.ftype == ANSWER:
                job = self._inflight
                if job is not None and f.seq == job.seq and f.cmd == job.cmd and f.payload:
                    self._answer(job, f.payload[0], bytes(f.payload[1:]))
                else:
                    self.stats["stale"] += 1
            elif f.ftype == STREAM:
                self.stream.emit(f)
            elif f.ftype == EVENT:
                self.event_frame.emit(f)
            else:
                self.stats["stale"] += 1

    def _answer(self, job: _Job, status: int, data: bytes) -> None:
        self._inflight = None
        if job.ping:
            if status == 0 and data == job.payload:
                self._resync = False
                self._ping_failures = 0
            else:
                self._ping_failed()
            return
        if status:
            job.future.set_exception(StatusError(job.cmd, status, data))
        else:
            job.future.set_result(data)

    def _check_deadline(self) -> None:
        job = self._inflight
        if job is None or time.monotonic() < self._deadline:
            return
        self._inflight = None
        self.stats["timeouts"] += 1
        self._resync = True
        if job.ping:
            self._ping_failed()
        else:
            job.future.set_exception(AnswerTimeout(
                "command 0x%02X: no answer within %.1f s" % (job.cmd, job.timeout)))

    def _ping_failed(self) -> None:
        self._ping_failures += 1
        if self._ping_failures >= PING_ATTEMPTS:
            raise _Lost("the meter answers no PING (%d tries)" % PING_ATTEMPTS)

    def _die(self, reason: str, emit: bool) -> None:
        with self._owner_free:
            if self._dead is None:
                self._dead = reason
            pending = list(self._pending)
            self._pending.clear()
            controls = list(self._controls)
            self._controls.clear()
            self._owner_free.notify_all()
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except OSError:
                pass    # the port is gone or failed already; the link is dead either way
        exc = PortLost(reason)
        job, self._inflight = self._inflight, None
        if job is not None and not job.ping:
            job.future.set_exception(exc)
        for j in pending:
            if j.future.set_running_or_notify_cancel():
                j.future.set_exception(exc)
        for _fn, fut in controls:
            if fut.set_running_or_notify_cancel():
                fut.set_exception(exc)
        if emit:
            self.lost.emit(reason)
