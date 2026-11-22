"""Fake LC1020E for the tests: builds every answer from the records documented in PROTOCOL.md of
fnirsi-lc1020e-firmware-rewrite (the section numbers in the comments). Two doors to the same
FakeMeter:

  FakeLink    a double of link.port.SerialLink (request / exclusive / close / signals), used by
              the device tests; raises StatusError for an answer status != 0.
  FakeSerial  a serial-level fake (read / write / in_waiting / dtr / close) usable as the
              serial_factory result of SerialLink; it speaks the frames of link/frames.py, which
              is imported when the fake is used.

All UIDs, copies and serial numbers here are invented.
"""

from __future__ import annotations

import struct
import zlib
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal

from fnirsi_companion.devices import base
from fnirsi_companion.devices import lc1020e as lc
from fnirsi_companion.link.frames import Frame

StatusError = base.StatusError
PortLost = base.PortLost

INVENTED_UID = bytes.fromhex("A1B2C3D4E5F60718293A4B5C")
OTHER_UID = bytes.fromhex("0F1E2D3C4B5A69788796A5B4")
ALL_CAPS = 0x000FFFFF                       # SL 14: bits 0..19 (old firmware; the default)
DIRECT_CAPS = 0x002FFFFF                    # SL 14: bits 0..19 and 21 (new firmware, direct set)


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ----------------------------------------------------------------------------------------------
# Record builders (the inverse of the parsers; layouts from PROTOCOL.md section 6, 8, 5.6, 9)
# ----------------------------------------------------------------------------------------------

def build_info(caps=ALL_CAPS, text="LC1020E V1.1 OWN", uid=INVENTED_UID, slot=1, slot_version=2,
               app_size=0x1F000, update_result=0, protocol=1) -> bytes:
    """INFO, 46 bytes (6.4)."""
    return struct.pack("<BI16s12sBIII", protocol, caps, text.encode("ascii"), uid, slot,
                       slot_version, app_size, update_result)


def build_state(screen=1, flags=0x0004, func=2, main_row=2, secondary=2, frequency=1000,
                level=0.6, bias=0.0, rng=0, speed=1, circuit=0, cursor=0, dialog=0, menu=0xFF,
                language=1, dropped=0, save_status=0, time=1234) -> bytes:
    """STATE, 32 bytes (6.1)."""
    return struct.pack("<BHBBBIffBBBBBBBHBI", screen, flags, func, main_row, secondary, frequency,
                       level, bias, rng, speed, circuit, cursor, dialog, menu, language, dropped,
                       save_status, time)


def build_value(value: float, unit: int, decimals: int, validity: int, text: str) -> bytes:
    """Value block, 15 bytes (6.2)."""
    return struct.pack("<fBBB8s", value, unit, decimals, validity, text.encode("ascii"))


def build_measure(result=7, time=5000, main_row=2, main=None, secondary_kind=2, secondary=None,
                  parallel=0, z=1591.5, theta=-89.9, frequency=1000, level=0.6, bias=0.0, rng=0,
                  speed=1, circuit=0) -> bytes:
    """MEASURE, 64 bytes (6.2). Default: C = 100 nF (the value block carries nF), D = 0.0012."""
    main = main if main is not None else build_value(100.0, 5, 2, 0, "100.00")
    secondary = secondary if secondary is not None else build_value(0.0012, 0, 4, 0, "0.0012")
    return (struct.pack("<IIB", result, time, main_row) + main + bytes([secondary_kind]) + secondary
            + struct.pack("<BffIffBBB", parallel, z, theta, frequency, level, bias, rng, speed,
                          circuit))


def build_compare(quantity=2, nominal=100.0, tolerance=5.0, deviation=1.5, ok=1, passed=3,
                  failed=1, value=101.5, warn_type=1, warn_sound=0, warn_led=1) -> bytes:
    """COMPARE, 29 bytes (6.3)."""
    return struct.pack("<BfffBIIfBBB", quantity, nominal, tolerance, deviation, ok, passed,
                       failed, value, warn_type, warn_sound, warn_led)


def build_lcr_list(lid: int, vtype: int, name: str, unit: str, entries) -> bytes:
    """LCR_TABLES list (8.1): entries = [(value, text)] with value an int or, for type 2, a float."""
    out = bytes([lid, len(entries), vtype]) + name.encode() + b"\0" + unit.encode() + b"\0"
    for v, text in entries:
        raw = struct.unpack("<I", struct.pack("<f", v))[0] if vtype == 2 else v
        out += struct.pack("<I", raw) + text.encode() + b"\0"
    return out


def build_setting(sid, kind, cur, lo, hi, step, name, choices) -> bytes:
    """SETTINGS_TABLES setting (8.2)."""
    out = struct.pack("<BBIIII", sid, kind, cur, lo, hi, step) + name.encode() + b"\0"
    out += bytes([len(choices)])
    for c in choices:
        out += c.encode() + b"\0"
    return out


def build_boot_status(slots, newest_slot, newest_state, newest_seq, nxt, free) -> bytes:
    """BOOT_STATUS, 47 bytes (5.6): slots = [(version, length, crc, ok)] for F, A, B."""
    out = b"".join(struct.pack("<IIIB", *s) for s in slots)
    return out + struct.pack("<BBIBB", newest_slot, newest_state, newest_seq, nxt, free)


def build_copy(seq: int, seed: int) -> bytes:
    """A valid settings copy (9): markers at 0 and 0xC10, content bytes from `seed` (invented),
    trailer sequence at 0xFF8 and CRC-32 over the block followed by the sequence at 0xFFC."""
    block = bytearray((seed * 31 + i * 7) & 0xFF for i in range(lc.BLOCK_SIZE))
    struct.pack_into("<I", block, 0, lc.BLOCK_MARKER)
    struct.pack_into("<I", block, 0xC10, lc.BLOCK_MARKER)
    return copy_from_block(bytes(block), seq)


def copy_from_block(block: bytes, seq: int) -> bytes:
    body = bytes(block) + b"\xFF" * (0xFF8 - lc.BLOCK_SIZE)
    sq = struct.pack("<I", seq)
    crc = zlib.crc32(sq, zlib.crc32(block)) & 0xFFFFFFFF
    return body + sq + struct.pack("<I", crc)


def build_uf2(blocks=4, family=lc.UF2_FAMILY, base_addr=lc.APP_BASE) -> bytes:
    """An application UF2 file (13.1 step 1): 256-byte payloads, family flag and ID."""
    out = b""
    for i in range(blocks):
        payload = bytes((i + j) & 0xFF for j in range(256)) + b"\0" * 220
        out += struct.pack("<8I", lc.UF2_MAGIC0, lc.UF2_MAGIC1, lc.UF2_FLAG_FAMILY,
                           base_addr + 256 * i, 256, i, blocks, family)
        out += payload + struct.pack("<I", lc.UF2_MAGIC_END)
    return out


def build_slot_file(base: int, version=2, length=0x40) -> bytes:
    """A slot file (13.2): 0x200-byte header (magic, length, image CRC, version, link address,
    reserved, header CRC at +28) and the image (initial SP, odd reset vector inside the image)."""
    image = struct.pack("<II", 0x20010000, (base + 0x200 + 8) | 1) + bytes(i & 0xFF for i in range(length - 8))
    head = struct.pack("<5I", lc.HDR_MAGIC, length, crc32(image), version, base + 0x200)
    head += b"\0" * (28 - len(head))
    head += struct.pack("<I", crc32(head))
    return head + b"\xFF" * (0x200 - len(head)) + image


def pixel(x: int, y: int) -> int:
    """The invented screen content: an RGB565 value that depends on the position."""
    return (x * 7 + y * 131) & 0xFFFF


# ----------------------------------------------------------------------------------------------
# The meter
# ----------------------------------------------------------------------------------------------

class FakeMeter:
    """The state of an LC1020E and its answers. handle(cmd, payload) -> (status, data)."""

    def __init__(self, uid=INVENTED_UID, caps=ALL_CAPS, text="LC1020E V1.1 OWN"):
        self.uid, self.caps, self.text = uid, caps, text
        self.running_slot, self.slot_version = 1, 2
        self.update_result = 0
        self.screen = 1                        # current screen (STATE, SCREEN, SETTINGS_SET)
        self.update_mode_status6 = False       # next slot below version 2 (UPDATE_MODE status 6)
        self.copies = [build_copy(5, 1), build_copy(4, 1)]      # A, B
        self.saves_held = False
        self.factory = 0
        self.log: list[tuple[int, bytes]] = []
        self.pending: list[tuple[int, bytes]] = []   # stream frames to deliver after the answer
        self.in_update_mode = False
        self.restarts = 0
        self.on_update_mode = lambda: None
        self.measure_stream = None
        self.block_fail_finals = 0            # WRITE_BLOCK final chunks to answer with status 7
        self.screenshot_bad = {}              # band -> end result for its first capture
        self._shot_seen = set()
        self._stage = None
        self._boot = None
        self.free_slot, self.newest = 1, (0xFF, 0, 0)
        self.slot_files = {}
        self.written_blocks = []

    # -- helpers ------------------------------------------------------------------------------
    def info(self) -> bytes:
        return build_info(self.caps, self.text, self.uid, self.running_slot, self.slot_version,
                          0x1F000, self.update_result)

    def cmds(self, code=None):
        return [c for c, _ in self.log if code is None or c == code]

    def _save_answer(self, ok=True, flags=0):
        if self.saves_held:
            return 3, struct.pack("<II", 1, 0x01)                 # result 1: held
        return 0, struct.pack("<II", 0 | 0x44 << 8 | 1 << 16, flags)

    # -- the command engine -------------------------------------------------------------------
    def handle(self, cmd: int, payload: bytes):
        self.log.append((cmd, bytes(payload)))
        bit = {lc.BATTERY: 15, lc.POWER_OFF: 16, lc.FACTORY_MODE: 19, lc.SCREENSHOT: 10,
               lc.MEM_READ: 11, lc.CAL_READ: 9, lc.UPDATE_MODE: 13}.get(cmd)
        if bit is not None and not self.caps >> bit & 1:
            return 1, b""
        h = getattr(self, "_c_%02x" % cmd, None)
        if h is None:
            return 1, b""
        return h(bytes(payload))

    def _confirmed(self, p, at=0, word=lc.CONFIRM_OK):
        return len(p) >= at + 2 and struct.unpack_from("<H", p, at)[0] == word

    def _c_00(self, p):
        return 0, self.info()

    def _c_01(self, p):
        return (2, b"") if len(p) > 511 else (0, p)

    def _c_02(self, p):
        self.restarts += 1
        return 0, b""

    def _c_03(self, p):
        return 0, struct.pack("<fBB", 3.9, 3, 0)

    def _c_05(self, p):
        return (0, b"") if self._confirmed(p, 0, lc.CONFIRM_POWER_OFF) else (2, b"")

    def _c_06(self, p):
        return 0, bytes([self.factory])

    def _c_10(self, p):
        return 0, build_state(screen=self.screen)

    def _c_11(self, p):
        return 0, b""

    def _c_12(self, p):
        self.screen = p[0]
        return 0, build_state(screen=self.screen)

    def _c_13(self, p):
        return 0, build_state()

    def _c_20(self, p):
        if not p:
            return 0, bytes([8])
        tables = {
            1: build_lcr_list(1, 0, "Secondary", "", [(i, n) for i, n in enumerate(
                ["R", "X", "D", "Q", "theta"])]),
            2: build_lcr_list(2, 1, "FREQ", "Hz", [(100, "100Hz"), (1000, "1kHz")]),
            3: build_lcr_list(3, 2, "AMP", "V", [(0.1, "0.1V"), (0.6, "0.6V")]),
        }
        return (0, tables[p[0]]) if p[0] in tables else (2, b"")

    def _c_21(self, p):
        return 0, build_measure()

    def _c_22(self, p):
        return 0, build_measure()

    def _c_23(self, p):
        self.measure_stream = struct.unpack_from("<H", p)[0]
        return 0, b""

    def _c_24(self, p):
        return 0, build_compare()

    def _c_25(self, p):
        if len(p) < 11:
            return 2, b""
        nominal, tol, wt, ws, wl = struct.unpack_from("<ffBBB", p)
        return 0, build_compare(nominal=nominal, tolerance=tol, warn_type=wt, warn_sound=ws,
                                warn_led=wl)

    def _c_26(self, p):
        return 0, build_state(flags=0x0005 if p[0] else 0x0004)

    def _c_27(self, p):
        if len(p) < 3 or not self._confirmed(p, 1):
            return 2, b""
        t = p[0]
        if t > 11 or (1 <= t <= 10 and self.factory != 2):
            return 2, b""
        if self.saves_held:
            return 3, struct.pack("<BBI", 3, t, 1)
        return 0, struct.pack("<BBI", 2, t, 0 | 0x44 << 8 | 1 << 16)

    def _c_50(self, p):
        if not p:
            return 0, bytes([6])
        if p[0] == 0:
            return 0, build_setting(0, 1, 50, 10, 100, 10, "Brightness", [])
        if p[0] == 2:
            return 0, build_setting(2, 0, 1, 0, 3, 1, "Off Time", ["NO", "5min", "15min", "30min"])
        return 2, b""

    def _c_51(self, p):
        if len(p) < 3:
            return 2, b""
        if not self.caps >> lc.CAP_DIRECT_SET & 1:            # old firmware: key job, ends on screen 3
            self.screen = 3
        return 0, build_state(screen=self.screen)             # direct set: the screen stays

    def _c_52(self, p):
        return self._save_answer() if self._confirmed(p) else (2, b"")

    def _c_53(self, p):
        if len(p) < 5:
            return 2, b""
        copy, off, n = struct.unpack_from("<BHH", p)
        if copy > 1 or not 1 <= n <= 256 or off + n > lc.COPY_SIZE:
            return 2, b""
        return 0, self.copies[copy][off:off + n]

    def _c_54(self, p):
        return self._save_answer() if self._confirmed(p) else (2, b"")

    def _recovery(self, p, flags=0):
        if not self._confirmed(p):
            return 2, b""
        return self._save_answer(flags=flags)

    def _c_55(self, p):
        return self._recovery(p, lc.SF_RESTART)

    def _c_56(self, p):
        return self._recovery(p)

    def _c_59(self, p):
        return self._recovery(p)

    def _c_5a(self, p):
        return self._recovery(p)

    def _c_57(self, p):
        if len(p) < 11 or not self._confirmed(p):
            return 2, b""
        off, total, crc = struct.unpack_from("<HHI", p, 2)
        chunk = p[10:]
        if total != lc.BLOCK_SIZE or not 1 <= len(chunk) <= 502:
            return 2, b""
        if off == 0:
            self._stage = bytearray()
        if self._stage is None or off != len(self._stage):
            return 2, b""
        self._stage += chunk
        if len(self._stage) < total:
            return 0, struct.pack("<H", len(self._stage))
        block, self._stage = bytes(self._stage), None
        ok = (len(block) == total and struct.unpack_from("<I", block, 0)[0] == lc.BLOCK_MARKER
              and struct.unpack_from("<I", block, 0xC10)[0] == lc.BLOCK_MARKER
              and crc32(block) == crc)
        if self.block_fail_finals > 0:
            self.block_fail_finals -= 1
            ok = False
        if not ok:
            return 7, struct.pack("<II", 7 | 0x01 << 8, 0)
        seq = 1 + max(struct.unpack_from("<I", c, 0xFF8)[0] for c in self.copies)
        self.copies = [copy_from_block(block, seq + 1), copy_from_block(block, seq)]
        self.written_blocks.append(block)
        return 0, struct.pack("<II", 0 | 0x44 << 8 | 1 << 16, 0)

    def _c_58(self, p):
        return 1, b""

    def _c_60(self, p):
        band = p[0] if p else 0xFF
        if band > 0xFF or (band != 0xFF and band > 23):
            return 2, b""
        bands = list(range(24)) if band == 0xFF else [band]
        for b in bands:
            first = b not in self._shot_seen
            self._shot_seen.add(b)
            self._emit_band(b, self.screenshot_bad.get(b) if first else None)
        return 0, struct.pack("<HHBB", 320, 240, 0, len(bands))

    def _emit_band(self, b, bad):
        """Pixel frames for the band (12): full-width pieces of at most 240 values, then the end
        frame. `bad` = end result to report (1 no room: the band is cut after two frames)."""
        values = [pixel(x, 10 * b + r) for r in range(10) for x in range(320)]
        pieces = range(0, 3200, 240)
        for n, off in enumerate(pieces):
            if bad and n == 2:
                break
            vals = values[off:off + 240]
            self.pending.append((lc.SCREENSHOT, struct.pack("<BHhhhh", b, off, 0, 10 * b, 319,
                                                           10 * b + 9)
                                 + struct.pack("<%dH" % len(vals), *vals)))
        self.pending.append((lc.SCREENSHOT, struct.pack("<BHhhhhB", b, 0xFFFF, 0, 10 * b, 319,
                                                       10 * b + 9, bad or 0)))

    def _c_f0(self, p):
        a, n = struct.unpack_from("<IH", p)
        return 0, bytes((a + i) & 0xFF for i in range(n))

    def _c_84(self, p):
        if self.update_mode_status6:
            return 6, b""
        self.in_update_mode = True
        self.on_update_mode()
        return 0, struct.pack("<BIB", 1, 2, self.running_slot)

    def _slots(self):
        return [(2, 0x2000, 0xAAAA0000 + i, 1) for i in range(3)]

    def _c_89(self, p):
        return 0, build_boot_status(self._slots(), self.newest[0], self.newest[1], self.newest[2],
                                    self.free_slot, self.free_slot)

    def _c_85(self, p):
        slot, length, crc = struct.unpack_from("<BII", p)
        if slot != self.free_slot:
            return 3, b""
        if not 0x208 <= length <= 0x6000 or length % 4:
            return 2, b""
        self._boot = dict(slot=slot, length=length, crc=crc, data=bytearray(length), ended=False)
        return 0, b""

    def _c_86(self, p):
        if self._boot is None:
            return 3, b""
        off = struct.unpack_from("<I", p)[0]
        chunk = p[4:]
        if off % 4 or not 4 <= len(chunk) <= 256 or off + len(chunk) > self._boot["length"]:
            return 2, b""
        self._boot["data"][off:off + len(chunk)] = chunk
        self._boot["ended"] = False
        return 0, b""

    def _c_87(self, p):
        b = self._boot
        if b is None:
            return 3, b""
        if crc32(bytes(b["data"])) != b["crc"]:
            return 7, b""
        b["ended"] = True
        return 0, struct.pack("<I", struct.unpack_from("<I", b["data"], 12)[0])

    def _c_88(self, p):
        b = self._boot
        if b is None or not b["ended"]:
            return 3, b""
        self.slot_files[b["slot"]] = bytes(b["data"])
        self.running_slot = b["slot"]
        self.newest = (b["slot"], 3, self.newest[2] + 1)
        self.free_slot = 2 if b["slot"] == 1 else 1
        self.restarts += 1
        return 0, b""

    # -- streams --------------------------------------------------------------------------------
    def apply_uf2(self, data: bytes, result: int) -> None:
        """The slot bootloader's work after the file was copied: `result` is what INFO reports
        next (1 written and checked, 0 / 2 / 3 as errors); the meter is out of update mode."""
        self.in_update_mode = False
        self.update_result = result
        self.restarts += 1


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
        status, data = self.meter.handle(cmd, payload)
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
    FakeMeter with link.frames.encode (imported when the object is created)."""

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
            status, ans = self.meter.handle(f.cmd, f.payload)
            self._rx += fr.encode(f.seq, fr.ANSWER, f.cmd, bytes([status]) + ans)
            pending, self.meter.pending = self.meter.pending, []
            for c, p in pending:
                self._seq = (self._seq + 1) & 0xFF
                self._rx += fr.encode(self._seq, fr.STREAM, c, p)
        return len(data)

    def close(self) -> None:
        self.closed = True
