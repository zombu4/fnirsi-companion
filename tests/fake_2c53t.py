"""Fake 2C53T for the tests: builds every answer from the records documented in PROTOCOL.md of
fnirsi-2c53t-firmware-rewrite (the section numbers in the comments). Two doors to the same
FakeMeter:

  FakeLink    a double of link.port.SerialLink (request / exclusive / close / signals), used by
              the device tests; raises StatusError for an answer status != 0 and AnswerTimeout
              for an answer the meter "loses" (FakeMeter.lose).
  FakeSerial  a serial-level fake (read / write / in_waiting / dtr / close) usable as the
              serial_factory result of SerialLink; it speaks the frames of link/frames.py.

The meter side knows nothing of the device module: its own capability map, its own checks (those
of the firmware: usb_ctl.s, usb_remote.s, slot_11.s). All UIDs, pages and files here are invented.
"""

from __future__ import annotations

import struct
import zlib
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal

from fnirsi_companion.devices import base
from fnirsi_companion.link.frames import Frame

StatusError = base.StatusError
PortLost = base.PortLost
AnswerTimeout = base.AnswerTimeout

INVENTED_UID = bytes.fromhex("5A4B3C2D1E0F9A8B7C6D5E4F")
OTHER_UID = bytes.fromhex("0102030405060708090A0B0C")
ALL_CAPS = 0x7FFFFF                          # S2 4.1: firmware 1.4.12, bits 0..22

# command -> capability bit (S2 4.1 list), independent of the module under test
CAP_OF = {0x10: 0, 0x11: 1, 0x12: 2, 0x13: 3, 0x40: 4, 0x53: 5, 0x74: 6, 0x80: 7, 0x81: 7,
          0x82: 7, 0x83: 7, 0x03: 8, 0x04: 9, 0xF0: 10, 0x20: 11, 0x21: 11, 0x22: 11,
          0x30: 12, 0x31: 12, 0x32: 12, 0x33: 12, 0x34: 12, 0x35: 12, 0x37: 12, 0x36: 13,
          0x50: 14, 0x51: 14, 0x52: 14, 0x60: 15, 0x70: 16, 0x71: 16, 0x72: 16, 0x73: 16,
          0xF2: 18, 0xF3: 19, 0xF4: 20, 0xF5: 21, 0xF6: 22}


class Lost(Exception):
    """The meter processed the request but its answer is lost (the host sees a timeout)."""


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ----------------------------------------------------------------------------------------------
# Record builders (the inverse of the parsers; layouts from PROTOCOL.md 4.1, 4.4, 4.6)
# ----------------------------------------------------------------------------------------------

def build_info(caps=ALL_CAPS, text="1.4.17", uid=INVENTED_UID, slot=1, boot_version=2,
               app_size=0x2F000, update_result=0, protocol=1) -> bytes:
    """INFO, 46 bytes (4.1)."""
    return struct.pack("<BI16s12sBIII", protocol, caps, text.encode("ascii"), uid, slot,
                       boot_version, app_size, update_result)


def build_state(page=1, flags=0, battery=80, function=5, value=4.7, unit=3, decimals=2,
                timebase=10, ch1=(1, 5, 0, 1, 0, 0), ch2=(0, 5, 0, 1, 0, 0),
                trigger=(0, 0, 20, 1), gen=(0, 0, 1000, 50, 30), time=123456,
                status=0, shown_unit=3, exponent=1, unit_text=3, prefix=1, length=49) -> bytes:
    """STATE record (4.6). length 44 or 49 (or any 44..49: the bytes after 43 are cut)."""
    rec = struct.pack("<BHBBfBB", page, flags, battery, function, value, unit, decimals)
    rec += b"\x00" + bytes([timebase])
    for ch in (ch1, ch2):
        rec += struct.pack("<BBh", ch[0], ch[1], ch[2])
    rec += struct.pack("<BBhB", *trigger)
    rec += struct.pack("<BBIBB", *gen)
    rec += struct.pack("<I", time)
    for ch in (ch1, ch2):
        rec += bytes([ch[3], ch[4], ch[5]])
    assert len(rec) == 44, len(rec)
    rec += struct.pack("<BBbBB", status, shown_unit, exponent, unit_text, prefix)
    return rec[:length]


def build_wave(channel=0, timebase=10, vdiv=5, probe=0, coupling=1, offset=0, enabled=1,
               running=1, start=0, count=301, samples=None) -> bytes:
    """SCOPE_WAVE answer (4.4): 12 header bytes and 301 samples."""
    samples = samples if samples is not None else bytes((128 + (i % 50)) & 0xFF for i in range(301))
    return struct.pack("<BBBBBbBBHH", channel, timebase, vdiv, probe, coupling, offset, enabled,
                       running, start, count) + samples


def build_tables() -> bytes:
    """SCOPE_TABLES answer (4.4): 29 timebase texts, 13 V/div texts."""
    tb = ["TB%d" % i for i in range(29)]
    vd = ["V%d" % i for i in range(13)]
    return bytes([29, 13]) + b"".join(t.encode() + b"\0" for t in tb + vd)


def build_app_image(length=0x400, base_addr=0x08007000, sp=0x20030000, vec_offset=0x101) -> bytes:
    """A vector table of a valid image: word 0 stack pointer, word 1 odd reset vector."""
    body = bytearray((i * 13 + 5) & 0xFF for i in range(length))
    struct.pack_into("<II", body, 0, sp, base_addr + vec_offset)
    return bytes(body)


def build_uf2(blocks=4) -> bytes:
    """A UF2 file: the start of each 512-byte block is the magic 0x0A324655 (UF2 specification)."""
    return b"".join(struct.pack("<IIIIIIII", 0x0A324655, 0x9E5D5157, 0, 0x08007000 + 256 * i, 256,
                                i, blocks, 0xC6B3602B) + bytes(476) + struct.pack("<I", 0x0AB16F30)
                    for i in range(blocks))


def invented_page(seed=1) -> bytes:
    """2048 bytes standing for the settings / calibration page."""
    return bytes((i * 31 + seed * 17 + (i >> 8)) & 0xFF for i in range(0x800))


def pixel(x: int, y: int) -> int:
    return ((x * 3 + y) & 0x1F) << 11 | ((x + y * 2) & 0x3F) << 5 | ((x ^ y) & 0x1F)


# ----------------------------------------------------------------------------------------------
# The meter
# ----------------------------------------------------------------------------------------------

class FakeMeter:
    """The state of a 2C53T and its answers. handle(cmd, payload) -> (status, data)."""

    def __init__(self, uid=INVENTED_UID, caps=ALL_CAPS, text="1.4.17", boot_version=2):
        self.uid, self.caps, self.text, self.boot_version = uid, caps, text, boot_version
        self.update_result = 0
        self.state_kw: dict = {}
        self.state_length = 49
        self.page = invented_page(1)
        self.settings = [80, 5, 1, 0]
        self.saved_settings = None
        self.log: list[tuple[int, bytes]] = []
        self.pending: list[tuple[int, bytes]] = []
        self.restarts = 0
        self.stream_period = 0
        self.keys: list[int] = []
        self.powered_off = False
        self.lose = lambda cmd, payload: False         # True: process, then lose the answer
        self.shot_drop_first: set[int] = set()
        self.shot_calls = 0
        self.list_per_answer = 2
        self.files: dict[str, bytes] = {
            "2:/README.TXT": b"hello", "2:/DATA/A.CSV": b"1,2,3\n", "2:/UPDATE/APP.BIN": b"old",
            "2:/BOOTINST.TXT": b"x", "3:/System file/9999.bin": b"\x01\x02\x03",
            "3:/System file/5.jpg": b"\xff\xd8\xff\xc0old-picture",
            "3:/System file/6.jpg": b"\xff\xd8\xff\xc0second",
        }
        self.written: list[str] = []
        self.deleted: list[str] = []
        self.fw = None
        self.install_result = 1
        self.install_text = "1.4.18"
        self.installed = None
        self.meter_log = None
        self.ram_fault = struct.pack("<12I", 0xFA017EC0, 3, 0x08010001, 0x08010203, 0x01000000,
                                     0x400, 0x40000000, 0, 0, 0x20030000, 0x20001000, 2)
        self.sent_to_chip: list[tuple[int, int]] = []
        self.text_colour = None

    # -- helpers ------------------------------------------------------------------------------
    def info(self) -> bytes:
        return build_info(self.caps, self.text, self.uid, 1, self.boot_version, 0x2F000,
                          self.update_result)

    def state(self, **kw) -> bytes:
        args = dict(self.state_kw)
        args.update(kw)
        args.setdefault("length", self.state_length)
        return build_state(**args)

    def cmds(self, code=None):
        return [c for c, _ in self.log if code is None or c == code]

    # -- the command engine -------------------------------------------------------------------
    def handle(self, cmd: int, payload: bytes):
        payload = bytes(payload)
        self.log.append((cmd, payload))
        bit = CAP_OF.get(cmd)
        if bit is not None and not self.caps >> bit & 1:
            return 1, b""
        h = getattr(self, "_c_%02x" % cmd, None)
        if h is None:
            return 1, b""
        res = h(payload)
        if self.lose(cmd, payload):
            raise Lost()
        return res

    def _st(self, **kw):
        return 0, self.state(**kw)

    def _c_00(self, p):
        return 0, self.info()

    def _c_01(self, p):
        return 0, p

    def _c_02(self, p):
        self.restarts += 1
        return 0, b""

    def _c_03(self, p):
        return 0, bytes([self.state_kw.get("battery", 80), 1])

    def _c_04(self, p):
        return (0, b"") if len(p) == 1 else (2, b"")

    def _c_10(self, p):
        return self._st()

    def _c_11(self, p):
        if len(p) != 2:
            return 2, b""
        self.stream_period = struct.unpack("<H", p)[0]
        return 0, b""

    def _c_12(self, p):
        if len(p) != 1 or p[0] > 3:
            return 2, b""
        self.state_kw["page"] = p[0]
        return self._st()

    def _c_13(self, p):
        if len(p) != 1 or not 1 <= p[0] <= 23:
            return 2, b""
        self.keys.append(p[0])
        if p[0] == 1:
            self.powered_off = True
            self.saved_settings = list(self.settings)
        return self._st()

    def _c_20(self, p):
        if len(p) != 1 or p[0] > 12:
            return 2, b""
        self.state_kw["function"] = p[0]
        return self._st()

    def _c_21(self, p):
        self.state_kw["flags"] = (self.state_kw.get("flags", 0) & ~1) | (p[0] & 1)
        return self._st()

    def _c_22(self, p):
        self.state_kw["flags"] = (self.state_kw.get("flags", 0) & ~2) | (p[0] & 1) << 1
        return self._st()

    def _c_30(self, p):
        if len(p) != 1 or p[0] > 28:
            return 2, b""
        self.state_kw["timebase"] = p[0]
        return self._st()

    def _c_31(self, p):
        if len(p) != 7:
            return 2, b""
        ch, en, vdiv, off, cpl, probe, fft = struct.unpack("<BBBbBBB", p)
        self.state_kw["ch2" if ch else "ch1"] = (en, vdiv, off, cpl, probe, fft)
        return self._st()

    def _c_32(self, p):
        if len(p) != 5:
            return 2, b""
        self.state_kw["trigger"] = struct.unpack("<BBhB", p)
        return self._st()

    def _c_33(self, p):
        return 0, build_tables()

    def _c_34(self, p):
        return self._st()

    def _c_35(self, p):
        return self._st()

    def _c_36(self, p):
        if len(p) != 1 or p[0] > 1:
            return 2, b""
        return 0, build_wave(channel=p[0])

    def _c_37(self, p):
        return self._st()

    def _c_40(self, p):
        if len(p) != 8:
            return 2, b""
        self.state_kw["gen"] = struct.unpack("<BBIBB", p)
        return self._st()

    def _c_50(self, p):
        return 0, bytes(self.settings)

    def _c_51(self, p):
        if len(p) != 4:
            return 2, b""
        self.settings = list(p)
        return 0, bytes(self.settings)

    def _c_52(self, p):
        self.saved_settings = list(self.settings)
        self.restarts += 1
        return 0, b""

    def _c_53(self, p):
        if len(p) != 4:
            return 2, b""
        off = struct.unpack("<I", p)[0]
        return 0, struct.pack("<I", len(self.page)) + self.page[off:off + 256]

    def _c_60(self, p):
        self.shot_calls += 1
        for i in range(480):
            if self.shot_calls == 1 and i in self.shot_drop_first:
                continue
            row, half = divmod(i, 2)
            data = b"".join(struct.pack("<H", pixel(x, row)) for x in range(half * 160, half * 160 + 160))
            self.pending.append((0x60, struct.pack("<I", i * 320) + data))
        return 0, struct.pack("<IHHB", 153600, 320, 240, 0)

    # files (4.10)
    def _c_70(self, p):
        if len(p) < 3:
            return 2, b""
        first = struct.unpack_from("<H", p)[0]
        d = p[2:].decode()
        parent = d if d.endswith(":/") else d.rstrip("/")
        names = sorted((path, data) for path, data in self.files.items()
                       if (path.rsplit("/", 1)[0] + "/" if parent.endswith(":/")
                           and path.count("/") == 1 else path.rsplit("/", 1)[0]) == parent)
        chunk = names[first:first + self.list_per_answer]
        nxt = first + len(chunk) if first + len(chunk) < len(names) else 0xFFFF
        out = struct.pack("<H", nxt)
        for path, data in chunk:
            name = path.rsplit("/", 1)[1].encode()
            out += struct.pack("<IBB", len(data), 0x20, len(name)) + name
        return 0, out

    def _c_71(self, p):
        if len(p) < 5:
            return 2, b""
        off = struct.unpack_from("<I", p)[0]
        path = p[4:].decode()
        if path not in self.files:
            return 5, b""
        data = self.files[path]
        return 0, struct.pack("<I", len(data)) + data[off:off + 500]

    def _writable(self, path):
        import re
        if path.startswith("2:"):
            return True
        m = re.fullmatch(r"3:/System file/([1-9][0-9]{0,2})\.jpg", path)
        return bool(m) and 1 <= int(m.group(1)) <= 199

    def _c_72(self, p):
        if len(p) < 9:
            return 2, b""
        off, total = struct.unpack_from("<II", p)
        end = p.index(b"\0", 8)
        path = p[8:end].decode()
        data = p[end + 1:]
        if not self._writable(path):
            return 6, b""
        if off == 0:
            self.files[path] = b""
        elif path not in self.files or len(self.files[path]) != off:
            return 2, b""
        self.files[path] += data
        self.written.append(path)
        return 0, b""

    def _c_73(self, p):
        path = p.decode()
        if not self._writable(path):
            return 6, b""
        if path not in self.files:
            return 5, b""
        del self.files[path]
        self.deleted.append(path)
        return 0, b""

    def _c_74(self, p):
        return (0, b"") if len(p) == 1 else (2, b"")

    # firmware update (4.11; usb_ctl.s cmd_fw_*)
    def _c_80(self, p):
        if len(p) != 8:
            return 2, b""
        total, crc = struct.unpack("<II", p)
        if total < 8 or total > 0xE0000 or total & 3:
            return 2, b""
        self.fw = {"total": total, "crc": crc, "data": bytearray(), "ok": False}
        return 0, b""

    def _c_81(self, p):
        fw = self.fw
        if fw is None:
            return 3, b""
        if len(p) < 5 or struct.unpack_from("<I", p)[0] != len(fw["data"]) \
                or len(fw["data"]) + len(p) - 4 > fw["total"]:
            return 2, b""
        fw["data"] += p[4:]
        return 0, b""

    def _c_82(self, p):
        fw = self.fw
        if fw is None:
            return 3, b""
        if len(fw["data"]) != fw["total"] or crc32(bytes(fw["data"])) != fw["crc"]:
            self.fw = None
            return 7, b""
        fw["ok"] = True
        return 0, b""

    def _c_83(self, p):
        if self.fw is None or not self.fw["ok"]:
            return 3, b""
        if self.boot_version < 2:
            return 6, b""
        self.installed = bytes(self.fw["data"])
        self.update_result = self.install_result
        if self.install_result == 1:
            self.text = self.install_text
        self.restarts += 1
        return 0, b""

    # diagnostics (4.13)
    def _c_f0(self, p):
        if len(p) != 6:
            return 2, b""
        addr, n = struct.unpack("<IH", p)
        if n > 256:
            return 2, b""
        if 0x08000000 <= addr and addr + n <= 0x08100000:
            return 0, bytes((addr + i) * 7 & 0xFF for i in range(n))
        if 0x20037FB0 <= addr and addr + n <= 0x20038000:
            ram = self.ram_fault + bytes(0x50 - len(self.ram_fault))
            return 0, ram[addr - 0x20037FB0:addr - 0x20037FB0 + n]
        return 6, b""

    def _c_f2(self, p):
        if len(p) != 6:
            return 2, b""
        addr, n = struct.unpack("<IH", p)
        if not 1 <= n <= 256 or addr + n > 16 << 20:
            return 2, b""
        return 0, bytes((addr + i) & 0xFF for i in range(n))

    def _c_f3(self, p):
        if len(p) == 1 and p[0] in (0, 1):
            self.meter_log = bytearray(range(40)) if p[0] else self.meter_log
            return 0, b""
        if len(p) == 3 and p[0] == 2:
            if self.meter_log is None:
                return 3, b""
            off = struct.unpack_from("<H", p, 1)[0]
            return 0, struct.pack("<H", len(self.meter_log)) + bytes(self.meter_log[off:off + 16])
        return 2, b""

    def _c_f4(self, p):
        return 0, bytes([0xEF, 0x40, 0x17])

    def _c_f5(self, p):
        if len(p) != 2:
            return 2, b""
        self.sent_to_chip.append((p[0], p[1]))
        return 0, b""

    def _c_f6(self, p):
        if len(p) != 2:
            return 2, b""
        self.text_colour = struct.unpack("<H", p)[0]
        return 0, b""


# ----------------------------------------------------------------------------------------------
# The link double
# ----------------------------------------------------------------------------------------------

class FakeLink(QObject):
    """Double of link.port.SerialLink for one FakeMeter."""
    stream = Signal(object)
    event_frame = Signal(object)
    lost = Signal(str)

    def __init__(self, meter: FakeMeter):
        super().__init__()
        self.meter = meter
        self.closed = False
        self.requests: list[tuple[int, bytes, float]] = []
        self.exclusive_count = 0
        self.exclusive_depth = 0
        self._seq = 0

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 1.5) -> bytes:
        if self.closed:
            raise PortLost("closed")
        self.requests.append((cmd, bytes(payload), timeout))
        try:
            status, data = self.meter.handle(cmd, payload)
        except Lost:
            self.meter.pending = []
            raise AnswerTimeout("no answer to 0x%02X" % cmd) from None
        pending, self.meter.pending = self.meter.pending, []
        if status:
            raise StatusError(cmd, status, data)
        for c, p in pending:
            self.emit_stream(c, p)
        return data

    @contextmanager
    def exclusive(self):
        self.exclusive_count += 1
        self.exclusive_depth += 1
        try:
            yield
        finally:
            self.exclusive_depth -= 1

    def close(self):
        self.closed = True

    def set_dtr(self, on):
        self.dtr = on

    def emit_stream(self, cmd: int, payload: bytes) -> None:
        self._seq = (self._seq + 1) & 0xFF
        self.stream.emit(Frame(self._seq, 3, cmd, bytes(payload), 12.5))

    def emit_event(self, cmd: int, payload: bytes) -> None:
        self._seq = (self._seq + 1) & 0xFF
        self.event_frame.emit(Frame(self._seq, 4, cmd, bytes(payload), 12.5))


# ----------------------------------------------------------------------------------------------
# The serial-level fake
# ----------------------------------------------------------------------------------------------

class FakeSerial:
    """Usable as the serial_factory result of SerialLink: read / write / in_waiting / dtr /
    close. Requests written are parsed with link.frames.FrameParser and answered from the
    FakeMeter with link.frames.encode (imported when the object is created); a lost answer is
    simply not written."""

    def __init__(self, meter: FakeMeter):
        from fnirsi_companion.link import frames
        self._frames = frames
        self.meter = meter
        self.dtr = False
        self.closed = False
        self._parser = frames.FrameParser()
        self._rx = bytearray()
        self._seq = 0

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, n: int = 1) -> bytes:
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, data: bytes) -> int:
        fr = self._frames
        for f in self._parser.feed(bytes(data), 0.0):
            if f.ftype != fr.REQUEST:
                continue
            try:
                status, ans = self.meter.handle(f.cmd, f.payload)
            except Lost:
                self.meter.pending = []
                continue
            pending, self.meter.pending = self.meter.pending, []
            for c, p in pending:                       # stream frames first (S2 4.9)
                self._seq = (self._seq + 1) & 0xFF
                self._rx += fr.encode(self._seq, fr.STREAM, c, p)
            self._rx += fr.encode(f.seq, fr.ANSWER, f.cmd, bytes([status]) + ans)
        return len(data)

    def close(self) -> None:
        self.closed = True
