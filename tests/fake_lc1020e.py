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

import math
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
NEW_CAPS = 0x01EFFFFF                       # bits 0..19, 21 .. 24 (60 Hz, tolerance modes, markers)
OCAL_CAPS = NEW_CAPS | 1 << lc.CAP_OCAL     # + bit 26: the own calibration area (OCD 4.3)
# IEC 60062 letters: (lower, upper) in % and, for a C nominal below 10 pF, B C D in pF
LETTER_LIMITS = {"B": (-0.1, 0.1), "C": (-0.25, 0.25), "D": (-0.5, 0.5), "F": (-1.0, 1.0),
                 "G": (-2.0, 2.0), "J": (-5.0, 5.0), "K": (-10.0, 10.0), "M": (-20.0, 20.0),
                 "Z": (-20.0, 80.0)}


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


F32_1E6 = struct.unpack("<f", struct.pack("<f", 1e-6))[0]     # the meter's f32 lower bound of a kV


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
                  speed=1, circuit=0, markers=None, ocal=None) -> bytes:
    """MEASURE, 64 bytes (6.2); 65 bytes with the markers byte when `markers` is given; 66 bytes
    with the OCAL flags byte (OCD 2.8) when `ocal` is given (the markers byte is then 0 unless
    given). Default: C = 100 nF (the value block carries nF), D = 0.0012."""
    if ocal is not None and markers is None:
        markers = 0
    main = main if main is not None else build_value(100.0, 5, 2, 0, "100.00")
    secondary = secondary if secondary is not None else build_value(0.0012, 0, 4, 0, "0.0012")
    return (struct.pack("<IIB", result, time, main_row) + main + bytes([secondary_kind]) + secondary
            + struct.pack("<BffIffBBB", parallel, z, theta, frequency, level, bias, rng, speed,
                          circuit)
            + (b"" if markers is None else bytes([markers]))
            + (b"" if ocal is None else bytes([ocal])))


def build_compare(quantity=2, nominal=100.0, tolerance=5.0, deviation=1.5, ok=1, passed=3,
                  failed=1, value=101.5, warn_type=1, warn_sound=0, warn_led=1,
                  tail=None) -> bytes:
    """COMPARE, 29 bytes (6.3); 50 bytes when `tail` (a dict of the tail fields: mode_in_force,
    mode_set, letter, lower, upper, result, signed_deviation, secondary_kind, secondary_value)
    is given."""
    head = struct.pack("<BfffBIIfBBB", quantity, nominal, tolerance, deviation, ok, passed,
                       failed, value, warn_type, warn_sound, warn_led)
    if tail is None:
        return head
    t = dict(mode_in_force=0, mode_set=0, letter="", lower=-5.0, upper=5.0, result=1,
             signed_deviation=1.5, secondary_kind=0, secondary_value=0.0)
    t.update(tail)
    return head + struct.pack("<BBBffBfBf", t["mode_in_force"], t["mode_set"],
                              ord(t["letter"]) if t["letter"] else 0, t["lower"], t["upper"],
                              t["result"], t["signed_deviation"], t["secondary_kind"],
                              t["secondary_value"])


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


# ----------------------------------------------------------------------------------------------
# Own calibration area: record builders (OCD 1.2, 4.2) written independently of devices/lc1020e.py
# ----------------------------------------------------------------------------------------------

def ocal_section(sid: int, data: bytes) -> bytes:
    return struct.pack("<HH", sid, len(data)) + data


def ocal_gain_entry(zeta=None, nu=None, masks=(0, 0, 0, 0)) -> bytes:
    """One 332-byte GAIN entry: zeta mag / phase masks, nu mag / phase masks, pad, 32 zeta and
    8 nu as (re, im) f32 (default 1 + 0j)."""
    zeta = zeta or [1 + 0j] * 32
    nu = nu or [1 + 0j] * 8
    out = struct.pack("<IIBBH", masks[0], masks[1], masks[2], masks[3], 0)
    for z in list(zeta) + list(nu):
        out += struct.pack("<ff", z.real, z.imag)
    assert len(out) == 332
    return out


def ocal_body(gain=None, clip=None, settle=None, monitor=None, bias=None, meta=None) -> bytes:
    """A record body: each argument is the section's bytes or None (absent); sections ascending."""
    out = b""
    for sid, data in enumerate((meta, gain, clip, settle, monitor, bias)):
        if data is not None:
            out += ocal_section(sid, data)
    return out


def ocal_meta(time=1700000000, note="") -> bytes:
    return struct.pack("<I28s", time, note.encode())


def ocal_record(body: bytes, seq: int, uid=INVENTED_UID, fmt=1, shopen=0, revoked=False,
                length=None, magic=0x4C41434F) -> bytes:
    """A whole record (OCD 1.2): header 0x40 + body; CRC-32 at +0x3C over bytes 0..0x37 then
    0x40..length-1; the revoke word at +0x38 is 0xFFFFFFFF (in force) or 0."""
    length = 0x40 + len(body) if length is None else length
    h = struct.pack("<IHHII", magic, fmt, 0x40, length, seq) + uid + struct.pack("<I", shopen)
    h += b"\0" * (0x38 - len(h)) + struct.pack("<I", 0 if revoked else 0xFFFFFFFF)
    rec = h + b"\0\0\0\0" + body
    crc = zlib.crc32(rec[:0x38] + rec[0x40:length]) & 0xFFFFFFFF
    return rec[:0x3C] + struct.pack("<I", crc) + rec[0x40:]


def ocal_slot(record: bytes) -> bytearray:
    """A 0x2000-byte slot image: the record then erased flash."""
    return bytearray(record + b"\xFF" * (0x2000 - len(record)))


def build_ocal_raw(window=1, c5=2, c6=2, c8=1, lock=0, frequency=1000, level=0.6, bias=0.0, n=30,
                   cls=0, flags=0, z=1000 + 0j, a_v=2000.0, a_i=1500.0, s2v=1.0, s2i=1.0,
                   mm=(3000, 1000, 2000, 2900, 1100, 2000)) -> bytes:
    """OCAL_RAW, 64 bytes (OCD 4.1 0x35)."""
    return struct.pack("<IBBBBIffHBBffffffHHHHHH", window, c5, c6, c8, lock, frequency, level,
                       bias, n, cls, flags, z.real, z.imag, a_v, a_i, s2v, s2i, *mm) + b"\0" * 4


def build_ocal_trace(state, kind, tick, count, entries) -> bytes:
    """OCAL_TRACE answer: entries = [(tick, re, im, flags, c5, c6, c8)]."""
    return struct.pack("<BBIBB", state, kind, tick, count, len(entries)) + b"".join(
        struct.pack("<IffBBBB", *e) for e in entries)


def build_monitor(v=math.nan, i=math.nan, a_v=math.nan, a_i=math.nan, flags=0, mid1=2048,
                  mid2=2047, window=7) -> bytes:
    """MONITOR, 25 bytes (OCD 4.1 0x37)."""
    return struct.pack("<ffffBHHI", v, i, a_v, a_i, flags, mid1, mid2, window)


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
        self.unchanged_saves = False          # saves answer result 9 (both copies already held it)
        self.factory = 0
        self.log: list[tuple[int, bytes]] = []
        self.pending: list[tuple[int, bytes]] = []   # stream frames to deliver after the answer
        self.in_update_mode = False
        self.restarts = 0
        self.on_update_mode = lambda: None
        self.measure_stream = None
        self.measure_markers = 0
        self.old_records = False              # answer MEASURE 64 / COMPARE 29 bytes whatever caps say
        self.compare_quantity = 2             # the compare quantity (2 = C)
        self.compare_nominal = 100.0
        self.compare_tolerance = 5.0
        self.tol = dict(mode_set=0, letter="", lower=-5.0, upper=5.0, in_force=0,
                        secondary_kind=0, secondary_value=0.0)
        self.block_fail_finals = 0            # WRITE_BLOCK final chunks to answer with status 7
        self.screenshot_bad = {}              # band -> end result for its first capture
        self._shot_seen = set()
        self._stage = None
        self._boot = None
        self.free_slot, self.newest = 1, (0xFF, 0, 0)
        self.slot_files = {}
        self.written_blocks = []
        self._ocal_init()

    # -- helpers ------------------------------------------------------------------------------
    def info(self) -> bytes:
        return build_info(self.caps, self.text, self.uid, self.running_slot, self.slot_version,
                          0x1F000, self.update_result)

    def cmds(self, code=None):
        return [c for c, _ in self.log if code is None or c == code]

    def _save_answer(self, ok=True, flags=0):
        if self.saves_held:
            return 3, struct.pack("<II", 1, 0x01)                 # result 1: held
        if self.unchanged_saves:
            return 0, struct.pack("<II", 9, flags)                # result 9: nothing written
        return 0, struct.pack("<II", 0 | 0x44 << 8 | 1 << 16, flags)

    # -- the command engine -------------------------------------------------------------------
    def handle(self, cmd: int, payload: bytes):
        self.log.append((cmd, bytes(payload)))
        bit = {lc.BATTERY: 15, lc.POWER_OFF: 16, lc.FACTORY_MODE: 19, lc.SCREENSHOT: 10,
               lc.MEM_READ: 11, lc.CAL_READ: 9, lc.UPDATE_MODE: 13}.get(cmd)
        if bit is not None and not self.caps >> bit & 1:
            return 1, b""
        if 0x30 <= cmd <= 0x37 and not self.caps >> lc.CAP_OCAL & 1:
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

    def _frequencies(self):
        """Frequency list: 2 entries; with bit 22 six entries, index 5 = 60 Hz."""
        fq = [(100, "100Hz"), (1000, "1kHz")]
        if self.caps >> lc.CAP_FREQ_60HZ & 1:
            fq += [(10000, "10kHz"), (100000, "100kHz"), (120, "120Hz"), (60, "60Hz")]
        return fq

    def _c_20(self, p):
        if not p:
            return 0, bytes([8])
        tables = {
            1: build_lcr_list(1, 0, "Secondary", "", [(i, n) for i, n in enumerate(
                ["R", "X", "D", "Q", "theta"])]),
            2: build_lcr_list(2, 1, "FREQ", "Hz", self._frequencies()),
            3: build_lcr_list(3, 2, "AMP", "V", [(0.1, "0.1V"), (0.6, "0.6V")]),
        }
        return (0, tables[p[0]]) if p[0] in tables else (2, b"")

    def _measure(self):
        """MEASURE: 66 bytes with bit 26 (markers byte, OCAL flags byte), 65 with bit 24, else 64;
        marker bit 1 = secondary unspecified."""
        if self.caps >> lc.CAP_OCAL & 1 and not self.old_records:
            return build_measure(markers=self.measure_markers, ocal=self.ocal_flags)
        if self.caps >> lc.CAP_MEASURE_MARKER & 1 and not self.old_records:
            return build_measure(markers=self.measure_markers)
        return build_measure()

    def _c_21(self, p):
        """LCR_SET: frequency (setting 2) and level (3) are kept for OCAL_RAW; an armed trace
        records the change; the range (5) is refused while a lock is set (OCD 2.2)."""
        if len(p) >= 2:
            if p[0] == 5 and self.lock is not None:
                return 3, b""
            if p[0] == 2 and p[1] < len(self._frequencies()):
                self.freq = self._frequencies()[p[1]][0]
            elif p[0] == 3 and p[1] < 2:
                self.level = (0.1, 0.6)[p[1]]
            self._on_change({2: 1, 3: 2, 4: 3, 6: 4}.get(p[0], 0))
        return 0, self._measure()

    def _c_22(self, p):
        return 0, self._measure()

    def _c_23(self, p):
        self.measure_stream = struct.unpack_from("<H", p)[0]
        return 0, b""

    def _compare(self, **kw):
        """COMPARE: 50 bytes with bit 23 (tolerance state of TOLERANCE_SET), else 29."""
        kw.setdefault("quantity", self.compare_quantity)
        kw.setdefault("nominal", self.compare_nominal)
        kw.setdefault("tolerance", self.compare_tolerance)
        if not self.caps >> lc.CAP_TOLERANCE & 1 or self.old_records:
            return build_compare(**kw)
        t, dev = self.tol, 1.5
        result = 1 if t["lower"] <= dev <= t["upper"] else (2 if dev > t["upper"] else 3)
        kw["ok"] = 1 if result == 1 else 0
        tail = dict(mode_in_force=t["in_force"], mode_set=t["mode_set"], letter=t["letter"],
                    lower=t["lower"], upper=t["upper"], result=result, signed_deviation=dev,
                    secondary_kind=t["secondary_kind"], secondary_value=t["secondary_value"])
        return build_compare(tail=tail, **kw)

    def _c_24(self, p):
        return 0, self._compare()

    def _c_25(self, p):
        if len(p) < 11:
            return 2, b""
        nominal, tol, wt, ws, wl = struct.unpack_from("<ffBBB", p)
        self.compare_nominal, self.compare_tolerance = nominal, tol
        self.tol.update(mode_set=0, letter="", lower=-tol, upper=tol, in_force=0)   # back to mode 0
        return 0, self._compare(warn_type=wt, warn_sound=ws, warn_led=wl)

    def _c_28(self, p):
        """TOLERANCE_SET: 15 bytes; status 2 for bad arguments, 3 for pF limits when the
        compare quantity is not C; the answer carries the record with every status."""
        if len(p) != 15:
            return 2, self._compare()
        mode, lo, hi, letter, kind, value = struct.unpack("<BiiBBf", p)
        letter = chr(letter) if letter else ""
        if mode > 3 or kind > 3 or not math.isfinite(value) or value < 0:
            return 2, self._compare()
        if mode == 1 and not (-9999 <= lo <= hi <= 99990):
            return 2, self._compare()
        if mode == 2 and not (-99999 <= lo <= hi <= 99999):
            return 2, self._compare()
        if mode == 3 and letter not in LETTER_LIMITS:
            return 2, self._compare()
        if mode == 2 and self.compare_quantity != 2:
            return 3, self._compare()
        t = self.tol
        t.update(mode_set=mode, letter=letter if mode == 3 else "", secondary_kind=kind,
                 secondary_value=value)
        if mode == 0:
            t.update(lower=-self.compare_tolerance, upper=self.compare_tolerance, in_force=0)
        elif mode == 1:
            t.update(lower=lo / 100.0, upper=hi / 100.0, in_force=1)
        elif mode == 2:
            t.update(lower=lo / 100.0, upper=hi / 100.0, in_force=2)
        else:
            t.update(lower=LETTER_LIMITS[letter][0], upper=LETTER_LIMITS[letter][1], in_force=1)
        return 0, self._compare()

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

    # -- own calibration area (OCD 1 to 5) --------------------------------------------------------
    def _ocal_init(self):
        self.slots = [bytearray(b"\xFF" * 0x2000), bytearray(b"\xFF" * 0x2000)]
        self.ocal_ops: list[tuple] = []        # ("erase", slot, page) / ("program", slot, words) / ("revoke", slot)
        self.ocal_xfer = None                  # the open OCAL_WRITE transfer
        self.events: list[tuple[int, bytes]] = []    # events to deliver after the answer
        self.off_mode = False                  # powered-off mode (refusal 3)
        self.cal_running = False               # a calibration runs (refusal 6)
        self.job_active = False                # another ctl job runs (refusal 4)
        self.rewrite_a = False                 # rewrite of copy A pending (refusal 4)
        self.ocal_fail_attempts = 0            # commit attempts that fail with flash result 5
        self.shopen_crc = 0x1234ABCD           # CRC-32 of the user table's short / open entries now
        self.lock = None                       # (c5, c6, c8) while locked
        self.window = 100                      # window counter of OCAL_RAW, + window_step per read
        self.window_step = 1
        self.freq, self.level = 1000, 0.6
        self.dut_fn = lambda hz: 1000 + 0j     # the impedance of the part in the terminals
        self.factor_fn = lambda fi, c5, c6, c8: 1 + 0j      # injected zeta * nu per cell
        self.saturated_cells: set = set()
        self.weak_cells: set = set()           # cells with amplitudes below 300 counts
        self.ocal_flags = 0                    # MEASURE byte 65
        self.trace = dict(state=0, kind=0, tick=0, entries=[])
        self.trace_tau = 4.0
        self.monitor_values = dict(v=math.nan, i=math.nan, a_v=math.nan, a_i=math.nan, flags=0)
        self.raw_reads = 0

    @staticmethod
    def _fk_record(d: bytes, uid: bytes) -> dict:
        """OCD 1.3 step 1 on one slot's bytes."""
        r = dict(valid=False, own=False, revoked=False, fmt=0, length=0, seq=0, shopen=0)
        magic, fmt, hsize, length, seq = struct.unpack_from("<IHHII", d)
        if magic != 0x4C41434F or hsize != 0x40 or not 0x40 <= length <= 0x2000 or length % 4 \
                or seq in (0, 0xFFFFFFFF):
            return r
        if zlib.crc32(bytes(d[:0x38]) + bytes(d[0x40:length])) & 0xFFFFFFFF != \
                struct.unpack_from("<I", d, 0x3C)[0]:
            return r
        r.update(valid=True, own=bytes(d[0x10:0x1C]) == uid, fmt=fmt, length=length, seq=seq,
                 revoked=struct.unpack_from("<I", d, 0x38)[0] != 0xFFFFFFFF,
                 shopen=struct.unpack_from("<I", d, 0x1C)[0])
        r["foreign"] = not r["own"]
        return r

    @staticmethod
    def _fk_check(sid: int, d: bytes):
        """OCD 2.4 value rules of one known section of the exact length: None or the entry index."""
        if sid == 1:
            for fi in range(6):
                e = d[fi * 332:(fi + 1) * 332]
                zm, zp, nm, np_, pad = struct.unpack_from("<IIBBH", e)
                if pad:
                    return fi * 41 + 40          # the meter's index (own/ocal.s): 41 fi + 40
                v = struct.unpack_from("<80f", e, 12)
                for k in range(40):
                    used = ((zm | zp) >> k & 1) if k < 32 else ((nm | np_) >> (k - 32) & 1)
                    if not used:
                        continue
                    re, im = v[2 * k], v[2 * k + 1]
                    if not (math.isfinite(re) and math.isfinite(im)):
                        return fi * 41 + k
                    m2 = re * re + im * im
                    if not (0.25 <= m2 <= 4 and re > 0 and 4 * re * re >= m2):
                        return fi * 41 + k
        elif sid == 2:
            for i in range(16):
                lo, hi = struct.unpack_from("<HH", d, 4 * i)
                if hi > 4095 or lo >= hi or hi - lo <= 3600:
                    return i
        elif sid == 3:
            for i, v in enumerate(struct.unpack("<42H", d)):
                if not 1 <= v <= 500:
                    return i
        elif sid == 4:
            mask = struct.unpack_from("<I", d)[0]
            if mask >> 6:
                return 0
            kv = struct.unpack_from("<6f", d, 4)
            for fi in range(6):
                if mask >> fi & 1 and not (math.isfinite(kv[fi]) and F32_1E6 <= kv[fi] <= 1.0):
                    return fi
        elif sid == 5:
            limit, pad = struct.unpack("<HH", d)
            if not 1 <= limit <= 4095 or pad:
                return 0
        return None

    SECTION_LEN = {0: 32, 1: 1992, 2: 64, 3: 84, 4: 28, 5: 4}

    def _ocal_load(self) -> dict:
        """OCD 1.3: the state after a load (active slot, flags, sections, rejected bits...)."""
        recs = [self._fk_record(bytes(s), self.uid) for s in self.slots]
        st = dict(recs=recs, active=None, flags=0, present=0, gain=0, mon=0, rejected=0,
                  tmax=26, sections={}, meta=(0, b""))
        for i in (0, 1):
            if recs[i]["valid"]:
                st["flags"] |= 1 << (1 + i)
                if recs[i]["foreign"]:
                    st["flags"] |= 1 << lc.OF_UID
        cand = [i for i in (0, 1) if recs[i]["own"] and not recs[i]["revoked"]]
        if cand:
            n = cand[0]
            for i in cand[1:]:
                if recs[i]["seq"] > recs[n]["seq"]:
                    n = i
            if recs[n]["fmt"] != 1:
                st["flags"] |= 1 << lc.OF_NEWER
            else:
                st["active"] = n
        if st["active"] is not None:
            n, d, L = st["active"], bytes(self.slots[st["active"]]), recs[st["active"]]["length"]
            st["flags"] |= 1 << lc.OF_ACTIVE
            o, prev = 0x40, -1
            while o + 4 <= L:
                sid, ln = struct.unpack_from("<HH", d, o)
                if ln % 4 or o + 4 + ln > L or sid <= prev:
                    st["flags"] |= 1 << lc.OF_SECT
                    break
                body = d[o + 4:o + 4 + ln]
                if sid in self.SECTION_LEN:
                    if ln != self.SECTION_LEN[sid] or self._fk_check(sid, body) is not None:
                        st["flags"] |= 1 << lc.OF_SECT
                        st["rejected"] |= 1 << sid
                    else:
                        st["sections"][sid] = body
                        st["present"] |= 1 << sid
                prev, o = sid, o + 4 + ln
        sec = st["sections"]
        if 0 in sec:
            st["meta"] = struct.unpack("<I28s", sec[0])
        if 1 in sec:
            for fi in range(6):
                zm, zp, nm, np_, _pad = struct.unpack_from("<IIBBH", sec[1], fi * 332)
                if any(zm >> (8 * c8 + c6) & 1 and zp >> (8 * c8 + c6) & 1 and nm >> c5 & 1
                       and np_ >> c5 & 1 for c8 in range(4) for c6 in range(8) for c5 in range(8)):
                    st["gain"] |= 1 << fi
        if 4 in sec:
            st["mon"] = struct.unpack_from("<I", sec[4])[0]
        if 3 in sec:
            st["tmax"] = max(struct.unpack("<42H", sec[3]))
        if st["active"] is not None and 1 in sec and recs[st["active"]]["shopen"] == self.shopen_crc:
            st["flags"] |= 1 << lc.OF_RETAKE
        return st

    def _ocal_flags(self, st) -> int:
        f = st["flags"]
        if self.lock is not None:
            f |= 1 << lc.OF_LOCK
        if self.ocal_xfer is not None:
            f |= 1 << lc.OF_XFER
        if self.trace["state"] in (1, 2):
            f |= 1 << lc.OF_TRACE
        return f

    def _ocal_event(self, st):
        a = st["active"]
        self.events.append((lc.EV_OCAL_CHANGED, struct.pack(
            "<BII", 0xFF if a is None else a, 0 if a is None else st["recs"][a]["seq"],
            self._ocal_flags(st))))

    def _c_30(self, p):
        st = self._ocal_load()
        a = st["active"]
        other = st["recs"][1 - a]["seq"] if a is not None and st["recs"][1 - a]["valid"] else 0
        length = st["recs"][a]["length"] if a is not None else 0
        seq = st["recs"][a]["seq"] if a is not None else 0
        crc = struct.unpack_from("<I", self.slots[a], 0x3C)[0] if a is not None else 0
        fmt = st["recs"][a]["fmt"] if a is not None else 0
        t, note = st["meta"]
        return 0, struct.pack("<IBBHIIIBBBBHHI28s", self._ocal_flags(st), 0xFF if a is None else a,
                              fmt, length, seq, other, crc, st["present"], st["gain"], st["mon"],
                              st["rejected"], st["tmax"], 0, t, note) + b"\0" * 4

    def _c_31(self, p):
        if len(p) != 5:
            return 2, b""
        slot, off, n = struct.unpack("<BHH", p)
        if slot > 1 or not 1 <= n <= 256 or off + n > 0x2000:
            return 2, b""
        return 0, bytes(self.slots[slot][off:off + n])

    def _ocal_busy(self):
        if self.off_mode:
            return 3
        if self.cal_running:
            return 6
        if self.job_active or self.rewrite_a:
            return 4
        return 0

    def _c_32(self, p):
        if len(p) < 11 or not self._confirmed(p):
            return 2, b""
        off, total, crc = struct.unpack_from("<HHI", p, 2)
        chunk = p[10:]
        if not 8 <= total <= 0x9C0 or total % 4 or not 1 <= len(chunk) <= 502:
            return 2, b""
        x = self.ocal_xfer
        if off == 0:
            x = self.ocal_xfer = dict(total=total, crc=crc, data=bytearray())
        elif x is None or off != len(x["data"]) or total != x["total"] or crc != x["crc"]:
            return 2, b""
        if len(x["data"]) + len(chunk) > total:
            return 2, b""
        if len(x["data"]) + len(chunk) < total:
            x["data"] += chunk
            return 0, struct.pack("<H", len(x["data"]))
        busy = self._ocal_busy()
        if busy:
            return busy, b""                  # the transfer stays open at its offset
        body = bytes(x["data"]) + chunk
        self.ocal_xfer = None
        if zlib.crc32(body) & 0xFFFFFFFF != crc:
            return 7, b""
        bad = self._ocal_validate(body)
        if bad is not None:
            return 2, struct.pack("<BH", *bad)
        return self._ocal_commit(body)

    def _ocal_validate(self, body: bytes):
        """OCD 2.4: None, or (section id, entry index) of the first failure."""
        o, prev = 0, -1
        while o + 4 <= len(body):
            sid, ln = struct.unpack_from("<HH", body, o)
            if sid <= prev or ln % 4 or o + 4 + ln > len(body):
                return sid & 0xFF, 0xFFFF
            if sid in self.SECTION_LEN:
                if ln != self.SECTION_LEN[sid]:
                    return sid, 0xFFFF
                idx = self._fk_check(sid, body[o + 4:o + 4 + ln])
                if idx is not None:
                    return sid, idx
            prev, o = sid, o + 4 + ln
        return None if o == len(body) else (prev & 0xFF, 0xFFFF)

    def _ocal_target(self, recs, st):
        """OCD 1.4 step 1."""
        if st["active"] is not None:
            return 1 - st["active"]
        bad = [i for i in (0, 1) if not recs[i]["valid"]]
        if bad:
            return bad[0]
        cls = [0 if r["foreign"] else (1 if r["revoked"] else 2) for r in recs]
        if cls[0] != cls[1]:
            return 0 if cls[0] < cls[1] else 1
        if recs[0]["seq"] < recs[1]["seq"]:
            return 0
        return 1                              # lower sequence; slot 1 on equal sequences

    def _ocal_commit(self, body: bytes):
        st = self._ocal_load()
        recs = st["recs"]
        t = self._ocal_target(recs, st)
        top = max([r["seq"] for r in recs if r["valid"]] or [0])
        seq = top + 1
        if seq == 0xFFFFFFFF:
            o = recs[1 - t]
            if o["own"] and not o["revoked"] and o["seq"] == 0xFFFFFFFE:
                return 3, b""
            seq = o["seq"] + 1 if o["valid"] and o["seq"] < 0xFFFFFFFE else 1
        if st["active"] is not None and t == st["active"]:
            return 5, struct.pack("<BBI", 1, st["active"], recs[st["active"]]["seq"])
        shopen = self.shopen_crc
        if st["active"] is not None and 1 in st["sections"]:
            new = self._body_sections(body)
            if new.get(1) == st["sections"][1]:
                shopen = recs[st["active"]]["shopen"]
        rec = ocal_record(body, seq, self.uid, 1, shopen)
        for attempt in range(2):
            if self.ocal_fail_attempts > 0:
                self.ocal_fail_attempts -= 1
                self.ocal_ops.append(("erase", t, 0))
                self.slots[t][:0x800] = b"\xFF" * 0x800
                continue
            pages = (len(rec) + 0x7FF) // 0x800
            for pg in range(pages):
                self.slots[t][pg * 0x800:(pg + 1) * 0x800] = b"\xFF" * 0x800
                self.ocal_ops.append(("erase", t, pg))
            self.slots[t][:len(rec)] = rec[:0x3C] + b"\xFF" * 4 + rec[0x40:]      # CRC word last
            self.ocal_ops.append(("program", t, len(rec) // 4 - 1))
            self.slots[t][0x3C:0x40] = rec[0x3C:0x40]
            self.ocal_ops.append(("crc", t, 1))
            break
        else:
            a = st["active"]
            return 5, struct.pack("<BBI", 5, 0xFF if a is None else a,
                                  0 if a is None else recs[a]["seq"])
        new = self._ocal_load()
        self._ocal_event(new)
        if new["active"] != t:
            return 5, struct.pack("<BBI", 1, 0xFF if new["active"] is None else new["active"], 0)
        return 0, struct.pack("<BBI", 0, t, seq)

    @staticmethod
    def _body_sections(body: bytes) -> dict:
        out, o = {}, 0
        while o + 4 <= len(body):
            sid, ln = struct.unpack_from("<HH", body, o)
            out[sid] = body[o + 4:o + 4 + ln]
            o += 4 + ln
        return out

    def _c_33(self, p):
        if len(p) != 3 or not self._confirmed(p) or p[2] > 1:
            return 2, b""
        busy = self._ocal_busy()
        if busy:
            return busy, b""
        st = self._ocal_load()
        recs = st["recs"]
        if p[2] == 0:
            targets = [st["active"]] if st["active"] is not None else []
        else:
            q = [i for i in (0, 1) if recs[i]["own"] and not recs[i]["revoked"] and recs[i]["fmt"] == 1]
            targets = sorted(q, key=lambda i: (recs[i]["seq"], -i))      # older first (OCD 1.5, G2)
        if not targets:
            return 3, b""
        for i in targets:
            self.slots[i][0x38:0x3C] = b"\0\0\0\0"                      # programmed 0: revoked
            self.ocal_ops.append(("revoke", i, 1))
        new = self._ocal_load()
        self._ocal_event(new)
        a = new["active"]
        return 0, struct.pack("<BII", 0xFF if a is None else a, 0 if a is None else new["recs"][a]["seq"],
                              self._ocal_flags(new))

    def _fi(self):
        return lc.OCAL_FREQ.index(self.freq) if self.freq in lc.OCAL_FREQ else None

    def _autorange(self):
        z = abs(self.dut_fn(self.freq))
        c8 = 0 if z < 300 else 1 if z < 3000 else 2 if z < 30000 else 3
        return 2, 2, c8

    def _raw(self):
        self.window += self.window_step
        locked = self.lock is not None
        c5, c6, c8 = self.lock if locked else self._autorange()
        fi = self._fi()
        f = self.factor_fn(fi, c5, c6, c8) if fi is not None else 1 + 0j
        z = self.dut_fn(self.freq) / f
        cell = (c5, c6, c8)
        flags = 1 if cell in self.saturated_cells else 0       # bit 0: saturated acquisitions seen
        amp = 100.0 if cell in self.weak_cells else 2000.0
        mm = (4095, 0, 2048, 4095, 0, 2048) if flags else (3000, 1000, 2000, 2900, 1100, 2000)
        return build_ocal_raw(self.window, c5, c6, c8, 1 if locked else 0, self.freq, self.level,
                              0.0, 30, 0, flags, z, amp, amp, 1.0, 1.0, mm)

    def _c_34(self, p):
        if len(p) != 3:
            return 2, b""
        if self.off_mode:
            return 3, b""
        if self.cal_running:
            return 6, b""
        if p == b"\xFF\xFF\xFF":
            self.lock = None
            return 0, self._raw()
        if p[0] > 7 or p[1] > 7 or p[2] > 3:
            return 2, b""
        if self.screen != 1:
            return 3, b""
        if self.lock != tuple(p):
            self._on_change(6)
        self.lock = tuple(p)
        return 0, self._raw()

    def _c_35(self, p):
        return 0, self._raw()

    def _on_change(self, kind: int):
        """A settle-relevant change: an armed trace records its 32 acquisitions (OCD 2.2)."""
        if self.trace["state"] != 1:
            return
        final = self.dut_fn(self.freq)
        init = final * 1.5
        c5, c6, c8 = self.lock if self.lock is not None else self._autorange()
        entries = []
        for i in range(32):
            z = final + (init - final) * math.exp(-i / self.trace_tau)
            entries.append((1000 + 5 * (i + 1), z.real, z.imag, 1 if i < 2 else 0, c5, c6, c8))
        self.trace = dict(state=3, kind=kind, tick=1000, entries=entries)

    def _c_36(self, p):
        if len(p) != 2 or p[0] > 2:
            return 2, b""
        t = self.trace
        if p[0] == 0:
            self.trace = dict(state=0, kind=0, tick=0, entries=[])
            return 0, build_ocal_trace(0, 0, 0, 0, [])
        if p[0] == 1:
            self.trace = dict(state=1, kind=0, tick=0, entries=[])
            return 0, build_ocal_trace(1, 0, 0, 0, [])
        if p[1] > 31:
            return 2, b""
        return 0, build_ocal_trace(t["state"], t["kind"], t["tick"], len(t["entries"]),
                                   t["entries"][p[1]:p[1] + 31])

    def _c_37(self, p):
        m = self.monitor_values
        return 0, build_monitor(m["v"], m["i"], m["a_v"], m["a_i"], m["flags"], 2048, 2047, self.window)

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
        events, self.meter.events = self.meter.events, []
        for c, p in events:
            self.emit_event(c, p)
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
            events, self.meter.events = self.meter.events, []
            for c, p in events:
                self._seq = (self._seq + 1) & 0xFF
                self._rx += fr.encode(self._seq, fr.EVENT, c, p)
        return len(data)

    def close(self) -> None:
        self.closed = True
