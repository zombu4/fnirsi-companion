"""FNIRSI LC1020E device module: every command of the control protocol as a method.

Source of every number: PROTOCOL.md of fnirsi-lc1020e-firmware-rewrite ("SL"), cited by section in
the comments; the records, parsers, file checks and the update flows are the owner's host library
tools/lc1020e.py ("LL") ported into this structure. The parse_* functions and the check_* functions
are pure (testable without a link). The class LC1020E(Device) works on the SerialLink of API.md.

The own calibration area (commands 0x30 to 0x37, event 0x98, capability bit 26) follows OWN_CAL_DESIGN.md of
the LC1020E repository ("OCD"): the record and section layouts, the value checks of OCD 2.4 on the host before a
write, the .ocal backup of both slots before every write (OCD 6.1) and the OCAL_* methods.

Safety (DESIGN.md 9, 13.7 to 13.9): methods that save, restart or overwrite take `confirm` and
refuse unless it is "OK"; those that touch the settings storage (calibration, serial, options) and
the update flows (UPDATE_MODE, BOOT_*) write a backup of both settings copies first. The generic
Device.call refuses dangerous commands.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import struct
import sys
import time
import zlib
from array import array
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt

from .base import (CONFIRM_WORD, AnswerTimeout, Cancelled, Command, Device, DeviceError,
                   Info, PortLost, Reading, SafetyRefused, StatusError, identify)

# ----------------------------------------------------------------------------------------------
# Constants (SL sections cited)
# ----------------------------------------------------------------------------------------------

MODEL = "LC1020E"
CONFIRM_OK = 0x4F4B              # SL 5: bytes 4B 4F
CONFIRM_POWER_OFF = 0x4F46       # SL 5: bytes 46 4F

# Command codes (SL 5.1 .. 5.6)
INFO, PING, RESET, BATTERY, POWER_OFF, FACTORY_MODE = 0x00, 0x01, 0x02, 0x03, 0x05, 0x06
STATE, STREAM, SCREEN, KEY = 0x10, 0x11, 0x12, 0x13
LCR_TABLES, LCR_SET, MEASURE, MEASURE_STREAM = 0x20, 0x21, 0x22, 0x23
COMPARE, COMPARE_SET, HOLD, CALIBRATE = 0x24, 0x25, 0x26, 0x27
TOLERANCE_SET = 0x28
SETTINGS_TABLES, SETTINGS_SET, SETTINGS_SAVE, CAL_READ = 0x50, 0x51, 0x52, 0x53
RESTORE_DEFAULTS, SETTINGS_RESTORE_B, SETTINGS_ACCEPT_A = 0x54, 0x55, 0x56
SETTINGS_WRITE_BLOCK, SETUP_LANGUAGE, SETTINGS_ACCEPT_B, SETTINGS_ACCEPT_DEFAULTS = \
    0x57, 0x58, 0x59, 0x5A
SCREENSHOT, MEM_READ = 0x60, 0xF0
UPDATE_MODE, BOOT_BEGIN, BOOT_DATA, BOOT_END, BOOT_INSTALL, BOOT_STATUS = \
    0x84, 0x85, 0x86, 0x87, 0x88, 0x89
# Own calibration area (OCD = OWN_CAL_DESIGN.md of the LC1020E repository, section 4)
OCAL_INFO, OCAL_READ, OCAL_WRITE, OCAL_ROLLBACK = 0x30, 0x31, 0x32, 0x33
OCAL_LOCK, OCAL_RAW, OCAL_TRACE, MONITOR = 0x34, 0x35, 0x36, 0x37
OCAL_COMMANDS = tuple(range(0x30, 0x38))

# Events (SL 10)
EV_KEY, EV_SCREEN_CHANGED, EV_SETTING_CHANGED, EV_POWERED_OFF = 0x90, 0x91, 0x93, 0x94
EV_MEASURE_DONE, EV_SAVE_RESULT, EV_CALIBRATION = 0x95, 0x96, 0x97
EV_OCAL_CHANGED = 0x98           # OCD 4.3: active slot u8, sequence u32, flags u32
EVENT_NAME = {EV_KEY: "KEY", EV_SCREEN_CHANGED: "SCREEN_CHANGED",
              EV_SETTING_CHANGED: "SETTING_CHANGED", EV_POWERED_OFF: "POWERED_OFF",
              EV_MEASURE_DONE: "MEASURE_DONE", EV_SAVE_RESULT: "SAVE_RESULT",
              EV_CALIBRATION: "CALIBRATION", EV_OCAL_CHANGED: "OCAL_CHANGED"}

# Answer time limits in seconds (SL 4.3: the column "tools/lc1020e.py waits")
T_FAST = 1.5
T_SAVE = 4.0
T_JOB = 23.0
T_COMPARE_SET = 63.0
T_DIRECT = 4.0                   # SL 4.3 / 14: the five set commands when CAP_DIRECT_SET is set
T_CALIBRATE = 126.0              # SL 4.3: the vendor sweep, job limit 120 000 ms
T_CALIBRATE_SWEEP = 366.0        # SL 4.3: with CAP_CAL_SWEEP, job limit 360 000 ms
T_BOOT_BEGIN = 13.0
T_BOOT_INSTALL = 4.0

# INFO capability bits (SL 14)
CAP_STATE, CAP_SCREEN, CAP_KEY, CAP_LCR, CAP_MEASURE, CAP_COMPARE, CAP_HOLD, CAP_CALIBRATE = range(8)
CAP_SETTINGS, CAP_CAL_READ, CAP_SCREENSHOT, CAP_MEM_READ, CAP_EVENTS, CAP_UPDATE_MODE = 8, 9, 10, 11, 12, 13
CAP_BOOT, CAP_BATTERY, CAP_POWER_OFF, CAP_RESTORE_DEFAULTS, CAP_RECOVERY, CAP_FACTORY_MODE = \
    14, 15, 16, 17, 18, 19
CAP_SETUP_LANGUAGE = 20          # clear in this firmware: English only (SL 5.6)
CAP_DIRECT_SET = 21              # SL 4.3 / 14: the five set commands set the value directly, no key job
CAP_FREQ_60HZ = 22               # LCR_TABLES list 2 "frequency" has a sixth entry, 60 Hz
CAP_TOLERANCE = 23               # TOLERANCE_SET and the 50-byte COMPARE record
CAP_MEASURE_MARKER = 24          # MEASURE byte 64 (markers) and save result 9
CAP_CAL_SWEEP = 25               # SL 5.3.3 / 14: the own calibration sweep: CALIBRATE answers 8 bytes
                                 # (failed point, reason), waits up to 360 s; the CALIBRATION step is
                                 # 3 x the points done + the windows held for the point in progress
CAP_OCAL = 26                    # OCD 4.3 (binding decision: bit 26): OCAL_*, MONITOR, MEASURE byte 65

STATUS_NAME = {0: "OK", 1: "unknown command", 2: "bad length or argument", 3: "not allowed now",
               4: "busy", 5: "storage error", 6: "refused for safety",
               7: "CRC or length mismatch of transferred data"}            # SL 3

SCREEN_NAME = {1: "measure", 2: "compare", 3: "settings"}                   # SL 6.1
STATE_FLAGS = ["hold", "compare screen", "DTR", "power off requested", "save failed",
               "backup differs", "calibration running", "factory mode", "saves held",
               "restart pending", "powered-off mode"]                       # SL 6.1 flags, bit 0..10
MAIN_NAME = {1: "R", 2: "C", 3: "L", 4: "Z"}                                # SL 6.2
SECONDARY_NAME = {0: "R", 1: "X", 2: "D", 3: "Q", 4: "theta"}               # SL 6.2
UNIT_TEXT = {0: "", 1: "Ω", 2: "kΩ", 3: "MΩ", 4: "pF", 5: "nF", 6: "µF", 7: "mF",
             8: "µH", 9: "mH", 10: "H", 11: "°"}                            # SL 6.2 value block
VALIDITY_NAME = {0: "valid", 1: "overload", 2: "no reading", 3: "held"}     # SL 6.2 value block
CIRCUIT_NAME = {0: "auto", 1: "series", 2: "parallel"}                      # SL 5.3 table
# Base unit of each quantity in the MEASURE value block (SL 6.2): factor to SI and SI symbol
QUANTITY_SI = {"R": (1.0, "Ω"), "Z": (1.0, "Ω"), "X": (1.0, "Ω"),          # ohm
               "C": (1e-9, "F"),                                            # nF -> F
               "L": (1e-6, "H"),                                            # uH -> H
               "D": (1.0, ""), "Q": (1.0, ""), "theta": (1.0, "°")}

# Settings storage (SL 9)
BLOCK_SIZE = 0xC14
BLOCK_MARKER = 0x12345678
COPY_SIZE = 0x1000
COPY_BASE = {0: 0x0807F000, 1: 0x08066000}
COPY_NAME = {0: "A", 1: "B"}
SF_RESTART = 0x80
SAVE_FLAGS = {0x01: "saves held", 0x02: "backup differs", 0x04: "no valid copy at load",
              0x08: "last save failed", 0x10: "copy B loaded (saves write A only)",
              0x20: "copy A damaged", 0x40: "rewrite of A pending", 0x80: "restart pending"}
SAVE_RESULT = {0: "OK", 1: "held", 2: "refused", 3: "trailer mark failed", 4: "erase failed",
               5: "program failed", 6: "verify failed", 7: "bad block", 8: "source copy invalid",
               9: "unchanged: both copies already held the block, nothing written"}
SAVE_UNCHANGED = 9               # save result 9 (CAP_MEASURE_MARKER); its answer status is 0

# MEASURE byte 64 markers and the COMPARE tail (CAP_MEASURE_MARKER, CAP_TOLERANCE)
MARK_MAIN, MARK_SECONDARY_UNSPECIFIED = 0x01, 0x02     # bit 0 main value, bit 1 secondary value
                                                       # outside the manual's accuracy table
TOLERANCE_MODE_NAME = {0: "vendor +-%", 1: "% limits", 2: "pF limits", 3: "IEC letter"}
COMPARE_MODE_IN_FORCE_NAME = {0: "vendor symmetric %", 1: "% limits", 2: "pF limits"}
COMPARE_RESULT_NAME = {0: "---", 1: "IN", 2: "HI", 3: "LO", 4: "secondary limit failed",
                       5: "OUT of range", 6: "---"}   # 0 no part, 6 not yet stable
SECONDARY_LIMIT_NAME = {0: "none", 1: "D max", 2: "Q min", 3: "ESR max"}
TOL_MODE_VENDOR, TOL_MODE_PERCENT, TOL_MODE_PF, TOL_MODE_LETTER = 0, 1, 2, 3
TOL_PERCENT_RANGE = (-9999, 99990)       # 0.01 % steps
TOL_PF_RANGE = (-99999, 99999)           # 0.01 pF steps
TOL_LETTERS = "BCDFGJKMZ"                # IEC 60062

SAVE_STEP = {0: "", 0x01: "check", 0x41: "mark", 0x42: "erase", 0x43: "program", 0x44: "verify"}
SAVE_COPY = {0: "", 1: "A", 2: "B"}
FLASH_RESULT = {0: "OK", 1: "outside the writable area", 2: "word not erased",
                3: "controller locked", 4: "flash error flag", 5: "read-back differs",
                6: "page not blank"}                                        # SL 9.1
UPDATE_RESULT = {0: "none", 1: "OK", 2: "image check failed", 3: "programming failed"}  # SL 6.4
CAL_STATUS = {0: "start", 1: "busy", 2: "finish", 3: "fail"}                # SL 5.3 CALIBRATE
RECORD_STATE = {0: "none", 1: "INSTALL", 2: "TRIAL", 3: "OK"}               # SL 5.6 BOOT_STATUS
WRITE_BLOCK_RETRIES = 3          # SL 9.4: repeat from offset 0 after status 7

# Slots and the update drive (SL 5.6, 13)
SLOT_BASE = {0: 0x08069000, 1: 0x0806F000, 2: 0x08075000}
SLOT_NAME = {0: "F", 1: "A", 2: "B", 0xFF: "unknown"}
SLOT_SIZE = 0x6000
SLOT_ROOM = 0x5E00
SLOT_FILE_MIN = 0x208
HDR_MAGIC = 0x544F4C53           # "SLOT"
SRAM_LO, SRAM_HI = 0x20000000, 0x20024000
UF2_MAGIC0, UF2_MAGIC1, UF2_MAGIC_END = 0x0A324655, 0x9E5D5157, 0x0AB16F30
UF2_FAMILY = 0xC6B3602B
UF2_FLAG_FAMILY = 0x2000
APP_BASE, APP_END = 0x08006000, 0x08062000
DRIVE_FILE = "LC1020E.UF2"
DRIVE_READY_FILE = "ready.txt"
MOUNT_COMMAND = "udisksctl mount -b %s"      # Linux without automount (DESIGN 13.29)

# Screenshot (SL 12)
SCR_W, SCR_H, SCR_BANDS, SCR_ROWS = 320, 240, 24, 10
SCR_RESULT = {0: "done", 1: "no room", 2: "no refresh"}


def _cmd(code, name, cap, timeout=T_FAST, dangerous=False):
    return Command(code, name, cap, timeout, dangerous)


# The command table: one entry per command of the SL tables 5.1 .. 5.6 (0x80..0x83 are reserved
# and answer status 1, so they are not commands). 0x58 is listed with its clear capability bit
# 20, so it is always refused here.
COMMANDS: dict[int, Command] = {c.code: c for c in [
    _cmd(INFO, "INFO", None),
    _cmd(PING, "PING", None),
    _cmd(RESET, "RESET", None),
    _cmd(BATTERY, "BATTERY", CAP_BATTERY),
    _cmd(POWER_OFF, "POWER_OFF", CAP_POWER_OFF, dangerous=True),
    _cmd(FACTORY_MODE, "FACTORY_MODE", CAP_FACTORY_MODE),
    _cmd(STATE, "STATE", CAP_STATE),
    _cmd(STREAM, "STREAM", CAP_STATE),
    _cmd(SCREEN, "SCREEN", CAP_SCREEN, T_JOB),
    _cmd(KEY, "KEY", CAP_KEY, T_JOB),                      # + hold time (timeout_for)
    _cmd(LCR_TABLES, "LCR_TABLES", CAP_LCR),
    _cmd(LCR_SET, "LCR_SET", CAP_LCR, T_JOB),
    _cmd(MEASURE, "MEASURE", CAP_MEASURE),
    _cmd(MEASURE_STREAM, "MEASURE_STREAM", CAP_MEASURE),
    _cmd(COMPARE, "COMPARE", CAP_COMPARE),
    _cmd(COMPARE_SET, "COMPARE_SET", CAP_COMPARE, T_COMPARE_SET),
    _cmd(TOLERANCE_SET, "TOLERANCE_SET", CAP_TOLERANCE),    # answered at once: fast timeout
    _cmd(HOLD, "HOLD", CAP_HOLD, T_JOB),
    _cmd(CALIBRATE, "CALIBRATE", CAP_CALIBRATE, T_CALIBRATE, dangerous=True),
    _cmd(SETTINGS_TABLES, "SETTINGS_TABLES", CAP_SETTINGS),
    _cmd(SETTINGS_SET, "SETTINGS_SET", CAP_SETTINGS, T_JOB),
    _cmd(SETTINGS_SAVE, "SETTINGS_SAVE", CAP_SETTINGS, T_SAVE, dangerous=True),
    _cmd(CAL_READ, "CAL_READ", CAP_CAL_READ),
    _cmd(RESTORE_DEFAULTS, "RESTORE_DEFAULTS", CAP_RESTORE_DEFAULTS, T_SAVE, dangerous=True),
    _cmd(SETTINGS_RESTORE_B, "SETTINGS_RESTORE_B", CAP_RECOVERY, T_SAVE, dangerous=True),
    _cmd(SETTINGS_ACCEPT_A, "SETTINGS_ACCEPT_A", CAP_RECOVERY, T_SAVE, dangerous=True),
    _cmd(SETTINGS_WRITE_BLOCK, "SETTINGS_WRITE_BLOCK", CAP_RECOVERY, T_SAVE, dangerous=True),
    _cmd(SETUP_LANGUAGE, "SETUP_LANGUAGE", CAP_SETUP_LANGUAGE),
    _cmd(SETTINGS_ACCEPT_B, "SETTINGS_ACCEPT_B", CAP_RECOVERY, T_SAVE, dangerous=True),
    _cmd(SETTINGS_ACCEPT_DEFAULTS, "SETTINGS_ACCEPT_DEFAULTS", CAP_RECOVERY, T_SAVE,
         dangerous=True),
    _cmd(SCREENSHOT, "SCREENSHOT", CAP_SCREENSHOT),
    _cmd(MEM_READ, "MEM_READ", CAP_MEM_READ),
    _cmd(UPDATE_MODE, "UPDATE_MODE", CAP_UPDATE_MODE, dangerous=True),
    _cmd(BOOT_BEGIN, "BOOT_BEGIN", CAP_BOOT, T_BOOT_BEGIN, dangerous=True),
    _cmd(BOOT_DATA, "BOOT_DATA", CAP_BOOT, dangerous=True),
    _cmd(BOOT_END, "BOOT_END", CAP_BOOT, dangerous=True),
    _cmd(BOOT_INSTALL, "BOOT_INSTALL", CAP_BOOT, T_BOOT_INSTALL, dangerous=True),
    _cmd(BOOT_STATUS, "BOOT_STATUS", CAP_BOOT),
    _cmd(OCAL_INFO, "OCAL_INFO", CAP_OCAL),                 # OCD 4.1
    _cmd(OCAL_READ, "OCAL_READ", CAP_OCAL),
    _cmd(OCAL_WRITE, "OCAL_WRITE", CAP_OCAL, T_SAVE, dangerous=True),     # one commit, up to 1 s
    _cmd(OCAL_ROLLBACK, "OCAL_ROLLBACK", CAP_OCAL, T_SAVE, dangerous=True),
    _cmd(OCAL_LOCK, "OCAL_LOCK", CAP_OCAL),                 # RAM only
    _cmd(OCAL_RAW, "OCAL_RAW", CAP_OCAL),
    _cmd(OCAL_TRACE, "OCAL_TRACE", CAP_OCAL),
    _cmd(MONITOR, "MONITOR", CAP_OCAL),
]}


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    """CRC-32 of the firmware (SL 9: reflected, polynomial 0xEDB88320, initial / final XOR
    0xFFFFFFFF = zlib's crc32)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def _f32(u: int) -> float:
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def _zstr(data: bytes, pos: int) -> tuple[bytes, int]:
    """Zero-terminated text at pos -> (bytes, position after the zero); a text cut by the end of
    the answer ends there (SL 8)."""
    end = data.find(b"\0", pos)
    if end < 0:
        return bytes(data[pos:]), len(data)
    return bytes(data[pos:end]), end + 1


def flag_names(value: int, names) -> list[str]:
    if isinstance(names, dict):
        return [n for bit, n in names.items() if value & bit]
    return [n for i, n in enumerate(names) if value >> i & 1]


def _short(name: str, data: bytes, size: int):
    if len(data) < size:
        raise DeviceError("%s answer of %d bytes, expected %d" % (name, len(data), size))


# ----------------------------------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------------------------------

@dataclass
class State:
    """STATE record, 32 bytes (SL 6.1)."""
    screen: int
    flags: int
    func: int
    main_row: int
    secondary: int
    frequency: int
    level: float
    bias: float
    range: int
    speed: int
    circuit: int
    cursor: int
    dialog: int
    menu_item: int
    language: int
    dropped: int
    save_status: int
    time: int
    raw: bytes = field(default=b"", repr=False)
    SIZE = 32

    def flag(self, name: str) -> bool:
        return bool(self.flags >> STATE_FLAGS.index(name) & 1)


def parse_state(d: bytes) -> State:
    _short("STATE", d, State.SIZE)
    v = struct.unpack_from("<BHBBBIffBBBBBBBHBI", d)
    return State(*v, raw=bytes(d[:State.SIZE]))


@dataclass
class ValueBlock:
    """Value block, 15 bytes (SL 6.2). value is in the base unit of the quantity (ohm, nF, uH,
    plain, degrees); unit is the code of the display text."""
    value: float
    unit: int
    decimals: int
    validity: int
    text: str
    SIZE = 15


def parse_value_block(d: bytes, pos: int = 0) -> ValueBlock:
    v, unit, dec, val, txt = struct.unpack_from("<fBBB8s", d, pos)
    return ValueBlock(v, unit, dec, val, txt.rstrip(b"\0").decode("ascii", "replace"))


@dataclass
class Measure:
    """MEASURE record, 65 bytes (SL 6.2); firmware without CAP_MEASURE_MARKER sends 64 bytes and
    byte 64 (markers) then reads as 0."""
    result: int
    time: int
    main_row: int
    main: ValueBlock
    secondary_kind: int
    secondary: ValueBlock
    parallel: int
    z: float
    theta: float
    frequency: int
    level: float
    bias: float
    range: int
    speed: int
    circuit: int
    raw: bytes = field(default=b"", repr=False)
    markers: int = 0                 # byte 64: bit 0 main value, bit 1 secondary "unspecified"
    ocal_flags: int | None = None    # byte 65 (CAP_OCAL, OCD 2.8): OCAL_FLAG_*; None = not sent
    SIZE = 64                        # shortest record accepted
    SIZE_MARKERS = 65
    SIZE_OCAL = 66

    @property
    def gain_applied(self) -> bool:
        return bool((self.ocal_flags or 0) & OCAL_FLAG_GAIN)

    @property
    def gain_missing(self) -> bool:
        """The frequency has GAIN entries but this cell is not calibrated (OCD 2.8 bit 1)."""
        return bool((self.ocal_flags or 0) & OCAL_FLAG_GAIN_MISSING)

    @property
    def main_unspecified(self) -> bool:
        """The main value lies outside the manual's accuracy table (amber on the meter)."""
        return bool(self.markers & MARK_MAIN)

    @property
    def secondary_unspecified(self) -> bool:
        """The secondary value lies outside the manual's accuracy table (amber on the meter)."""
        return bool(self.markers & MARK_SECONDARY_UNSPECIFIED)


def parse_measure(d: bytes, ocal: bool = False) -> Measure:
    """MEASURE record. `ocal`: the meter has CAP_OCAL, so byte 65 (OCD 2.8) is the OCAL flags
    byte; it is read only then and only when the record has 66 bytes."""
    _short("MEASURE", d, Measure.SIZE)
    res, t, mrow = struct.unpack_from("<IIB", d, 0)
    main = parse_value_block(d, 9)
    skind = d[24]
    sec = parse_value_block(d, 25)
    par, z, th, fq, lv, bs, rg, sp, ci = struct.unpack_from("<BffIffBBB", d, 40)
    has_ocal = ocal and len(d) >= Measure.SIZE_OCAL
    size = Measure.SIZE_OCAL if has_ocal else (
        Measure.SIZE_MARKERS if len(d) >= Measure.SIZE_MARKERS else Measure.SIZE)
    return Measure(res, t, mrow, main, skind, sec, par, z, th, fq, lv, bs, rg, sp, ci,
                   bytes(d[:size]), d[64] if size >= Measure.SIZE_MARKERS else 0,
                   d[65] if has_ocal else None)


@dataclass
class Compare:
    """COMPARE record, 50 bytes (SL 6.3); 29 bytes on firmware without CAP_TOLERANCE, then the
    tail fields (from mode_in_force on) are None. `ok` is 1 when `result` is IN."""
    quantity: int
    nominal: float
    tolerance: float
    deviation: float
    ok: int
    passed: int
    failed: int
    value: float
    warn_type: int
    warn_sound: int
    warn_led: int
    raw: bytes = field(default=b"", repr=False)
    mode_in_force: int | None = None     # 29: 0 vendor symmetric %, 1 % limits, 2 pF limits
    mode_set: int | None = None          # 30: 0 vendor +-%, 1 % limits, 2 pF limits, 3 IEC letter
    letter: str | None = None            # 31: ASCII letter of mode 3, "" when none
    lower: float | None = None           # 32: lower limit in force (% or pF)
    upper: float | None = None           # 36: upper limit in force (% or pF)
    result: int | None = None            # 40: COMPARE_RESULT_NAME
    signed_deviation: float | None = None    # 41: % or pF by mode_in_force
    secondary_kind: int | None = None    # 45: SECONDARY_LIMIT_NAME
    secondary_value: float | None = None     # 46
    SIZE = 29                            # shortest record accepted
    SIZE_TOLERANCE = 50

    @property
    def result_name(self) -> str | None:
        return None if self.result is None else COMPARE_RESULT_NAME.get(self.result,
                                                                        "?%d" % self.result)


def parse_compare(d: bytes) -> Compare:
    _short("COMPARE", d, Compare.SIZE)
    head = struct.unpack_from("<BfffBIIfBBB", d)
    if len(d) < Compare.SIZE_TOLERANCE:
        return Compare(*head, raw=bytes(d[:Compare.SIZE]))
    mode_in_force, mode_set, letter, lower, upper, result, dev, kind, value = \
        struct.unpack_from("<BBBffBfBf", d, 29)
    return Compare(*head, bytes(d[:Compare.SIZE_TOLERANCE]), mode_in_force, mode_set,
                   chr(letter) if letter else "", lower, upper, result, dev, kind, value)


@dataclass
class Battery:
    """BATTERY answer, 6 bytes (SL 5.1)."""
    volts: float
    level: int
    low_popup: int


def parse_battery(d: bytes) -> Battery:
    _short("BATTERY", d, 6)
    return Battery(*struct.unpack_from("<fBB", d))


@dataclass
class SaveResult:
    """Answer of the save commands: save status u32 and save flags u32 (SL 5.4, 9.1)."""
    status: int          # answer status (0 OK, 3, 5, 7)
    save_status: int     # result | step << 8 | copy << 16
    flags: int
    restart: bool        # the meter restarts after this answer

    @property
    def result(self) -> int:
        return self.save_status & 0xFF

    def describe(self) -> str:
        r = SAVE_RESULT.get(self.result, "?%d" % self.result)
        step = SAVE_STEP.get(self.save_status >> 8 & 0xFF, "")
        copy = SAVE_COPY.get(self.save_status >> 16 & 0xFF, "")
        s = "save %s" % r
        if step:
            s += ", step %s" % step
        if copy:
            s += ", copy %s" % copy
        fl = flag_names(self.flags, SAVE_FLAGS)
        return s + "; flags 0x%02X %s" % (self.flags, ", ".join(fl) if fl else "(none)") + \
            ("; the meter restarts" if self.restart else "")


def parse_save_answer(status: int, d: bytes, restarts_on_ok: bool = False) -> SaveResult:
    _short("save", d, 8)
    st, flags = struct.unpack_from("<II", d)
    restart = bool(flags & SF_RESTART) or (restarts_on_ok and (st & 0xFF) == 0)
    return SaveResult(status, st, flags, restart)


# SL 5.3.3, answer byte 7 (CAP_CAL_SWEEP): why the sweep ended without a result
CAL_REASON = {0: "none", 1: "no acquisition for 1.5 s", 2: "the point's 20-s limit with fewer than 3 windows",
              3: "an open point below 100 kohm", 4: "the vendor's store check (table written back as it was)",
              5: "the point's limit with 3 windows that do not agree",
              6: "ended by a key, the dialog closed or the screen left"}
CAL_POINTS = 15                  # SL 5.3.3: 100, 120, 1k, 10k, 100k Hz x 0.1, 0.3, 0.6 V


@dataclass
class CalResult:
    """CALIBRATE answer (SL 5.3): 6 bytes, 8 with CAP_CAL_SWEEP (failed point, reason)."""
    status: int          # answer status
    cal_status: int      # 0 start, 1 busy, 2 finish, 3 fail
    cal_type: int
    save_status: int
    point: int | None = None     # 0..14 where the sweep ended without a result, 0xFF none; None: 6 bytes
    reason: int | None = None    # CAL_REASON; None: 6 bytes

    def describe(self) -> str:
        text = "calibration status %s, type %d, save status 0x%08X" % (
            CAL_STATUS.get(self.cal_status, self.cal_status), self.cal_type, self.save_status)
        if self.point is not None and self.point != 0xFF:
            text += ", ended at point %d/%d: %s" % (self.point + 1, CAL_POINTS,
                                                     CAL_REASON.get(self.reason, "reason %s" % self.reason))
        return text


def parse_cal_answer(status: int, d: bytes) -> CalResult:
    _short("CALIBRATE", d, 6)
    st, ty, sv = struct.unpack_from("<BBI", d)
    if len(d) >= 8:
        return CalResult(status, st, ty, sv, d[6], d[7])
    return CalResult(status, st, ty, sv)


def cal_progress(fields: dict, sweep: bool) -> str:
    """CALIBRATION event text (SL 10): with CAP_CAL_SWEEP the step is 3 x the points done + the
    windows held for the point in progress (45 at the store); without it the vendor's seconds."""
    status = CAL_STATUS.get(fields["status"], fields["status"])
    step = fields["step"]
    if sweep:
        if step >= 3 * CAL_POINTS:
            where = "storing"
        else:
            where = "point %d/%d, %d of 3 windows" % (step // 3 + 1, CAL_POINTS, step % 3)
    else:
        where = "step %d s" % step
    return "status %s, %s, type %d" % (status, where, fields["type"])


@dataclass
class UpdateMode:
    """UPDATE_MODE answer, 6 bytes (SL 5.6)."""
    status: int
    next_slot: int
    next_version: int
    running_slot: int


def parse_update_mode(status: int, d: bytes) -> UpdateMode:
    _short("UPDATE_MODE", d, 6)
    return UpdateMode(status, *struct.unpack_from("<BIB", d))


@dataclass
class SlotInfo:
    version: int
    length: int
    crc: int
    ok: int


@dataclass
class BootStatus:
    """BOOT_STATUS answer, 47 bytes (SL 5.6)."""
    slots: list[SlotInfo]        # F, A, B
    newest_slot: int
    newest_state: int
    newest_sequence: int
    next_slot: int
    free_slot: int
    SIZE = 47


def parse_boot_status(d: bytes) -> BootStatus:
    _short("BOOT_STATUS", d, BootStatus.SIZE)
    slots = [SlotInfo(*struct.unpack_from("<IIIB", d, 13 * i)) for i in range(3)]
    ns, st, seq, nxt, free = struct.unpack_from("<BBIBB", d, 39)
    return BootStatus(slots, ns, st, seq, nxt, free)


@dataclass
class LcrList:
    """LCR_TABLES list (SL 8.1)."""
    id: int
    value_type: int              # 0 index, 1 u32, 2 f32
    name: str
    unit: str
    entries: list[tuple[object, str]]
    count: int


def parse_lcr_list(d: bytes) -> LcrList:
    _short("LCR_TABLES", d, 3)
    lid, count, vtype = d[0], d[1], d[2]
    name, pos = _zstr(d, 3)
    unit, pos = _zstr(d, pos)
    entries = []
    while pos + 4 <= len(d) and len(entries) < count:
        (u,) = struct.unpack_from("<I", d, pos)
        pos += 4
        text, pos = _zstr(d, pos)
        entries.append((_f32(u) if vtype == 2 else u, text.decode("utf-8", "replace")))
    return LcrList(lid, vtype, name.decode("utf-8", "replace"), unit.decode("utf-8", "replace"),
                   entries, count)


@dataclass
class Setting:
    """SETTINGS_TABLES setting (SL 8.2)."""
    id: int
    kind: int                    # 0 choices, 1 range
    current: int
    min: int
    max: int
    step: int
    name: str
    choices: list[str]


def parse_setting(d: bytes) -> Setting:
    _short("SETTINGS_TABLES", d, 19)
    sid, kind, cur, lo, hi, step = struct.unpack_from("<BBIIII", d)
    name, pos = _zstr(d, 18)
    count = d[pos] if pos < len(d) else 0
    pos += 1
    choices = []
    while pos < len(d) and len(choices) < count:
        text, pos = _zstr(d, pos)
        choices.append(text.decode("utf-8", "replace"))
    return Setting(sid, kind, cur, lo, hi, step, name.decode("utf-8", "replace"), choices)


@dataclass
class Event:
    """An event frame (SL 10): cmd, the frame's seq, decoded fields."""
    cmd: int
    seq: int
    fields: dict

    @property
    def name(self) -> str:
        return EVENT_NAME.get(self.cmd, "0x%02X" % self.cmd)


def parse_event(cmd: int, payload: bytes, seq: int = 0) -> Event:
    p = payload
    try:
        if cmd == EV_KEY:
            mask, code, src = struct.unpack_from("<HBB", p)
            fl = dict(mask=mask, code=code, source="injected" if src else "physical")
        elif cmd == EV_SCREEN_CHANGED:
            fl = dict(screen=p[0], previous=p[1])
        elif cmd == EV_SETTING_CHANGED:
            sid, val = struct.unpack_from("<HI", p)
            fl = dict(id=sid, value=val)
        elif cmd == EV_POWERED_OFF:
            fl = {}
        elif cmd == EV_MEASURE_DONE:
            fl = dict(result=struct.unpack_from("<I", p)[0])
        elif cmd == EV_SAVE_RESULT:
            st, flags = struct.unpack_from("<II", p)
            fl = dict(save_status=st, flags=flags)
        elif cmd == EV_CALIBRATION:
            fl = dict(status=p[0], step=p[1], type=p[2])
        elif cmd == EV_OCAL_CHANGED:
            slot, seq, flags = struct.unpack_from("<BII", p)
            fl = dict(slot=slot, sequence=seq, flags=flags)
        else:
            fl = dict(payload=bytes(p))
    except (struct.error, IndexError):
        fl = dict(payload=bytes(p), malformed=True)
    return Event(cmd, seq, fl)


@dataclass
class ScreenshotHeader:
    """SCREENSHOT answer, 6 bytes (SL 5.5)."""
    width: int
    height: int
    format: int
    bands: int


def parse_screenshot_header(d: bytes) -> ScreenshotHeader:
    _short("SCREENSHOT", d, 6)
    return ScreenshotHeader(*struct.unpack_from("<HHBB", d))


# ----------------------------------------------------------------------------------------------
# Own calibration area (OCD = OWN_CAL_DESIGN.md of the LC1020E repository: 1.2 record, 2.4 value
# checks, 4 commands). Little-endian, packed.
# ----------------------------------------------------------------------------------------------

OCAL_BASE = 0x08062000           # OCD 1.2: slot 0 at +0, slot 1 at +0x2000
OCAL_SLOT_SIZE = 0x2000
OCAL_FILE_SIZE = 2 * OCAL_SLOT_SIZE      # a .ocal backup file: slot 0 then slot 1 (OCD 6.1)
OCAL_MAGIC = 0x4C41434F          # "OCAL"
OCAL_HEADER = 0x40
OCAL_FORMAT = 1
OCAL_REVOKE = 0x38               # header: revoke word, 0xFFFFFFFF = in force
OCAL_CRC_AT = 0x3C               # header: CRC-32 over bytes 0..0x37 then 0x40..length-1
OCAL_BODY_MIN, OCAL_BODY_MAX = 8, 0x9C0      # OCD 4.1 OCAL_WRITE total
OCAL_CHUNK_MAX = 502             # OCD 4.1 OCAL_WRITE chunk 1..502
OCAL_READ_MAX = 256              # OCD 4.1 OCAL_READ length 1..256
OCAL_NO_SLOT = 0xFF
OCAL_TRACE_ENTRIES = 32
OCAL_SOURCE_NAME = re.compile(r"^LC1020E-([0-9A-F]{24})-.+\.ocal$")

# OCAL_INFO flags (OCD 4.2), bit numbers
OF_ACTIVE, OF_SLOT0_VALID, OF_SLOT1_VALID, OF_NEWER, OF_UID, OF_SECT, OF_RETAKE, OF_LOCK, \
    OF_XFER, OF_TRACE = range(10)
OCAL_INFO_FLAGS = ["record active", "slot 0 valid", "slot 1 valid", "newer format present",
                   "foreign UID present", "section rejected", "retake short/open", "lock",
                   "transfer open", "trace armed"]
# MEASURE byte 65 (OCD 2.8)
OCAL_FLAG_GAIN, OCAL_FLAG_GAIN_MISSING, OCAL_FLAG_LOCK, OCAL_FLAG_RETAKE = 1, 2, 4, 8
# OCAL_RAW flags byte (OCD 4.1 0x35)
RAW_SATURATED, RAW_OVERLOAD = 1, 2
# MONITOR flags (OCD 4.1 0x37)
MON_V_CALIBRATED, MON_GAIN_APPLIED, MON_NO_READING, MON_LOCK = 1, 2, 4, 8
# OCAL_TRACE
TRACE_DISARM, TRACE_ARM, TRACE_READ = 0, 1, 2
TRACE_STATE_NAME = {0: "idle", 1: "armed", 2: "recording", 3: "full"}
TRACE_DISCARDED, TRACE_SATURATED = 1, 2
TRACE_OUT_OF_BAND = 4            # PROTOCOL 5.7 0x36 bit 2: the first or second out-of-band acquisition in a
                                 # row, discarded by the ranging band rule (not ranged)
SETTLE_KIND_NAME = ["restart", "frequency", "level", "bias", "speed", "screen", "range or gain code"]

# Sections (OCD 1.2)
SEC_META, SEC_GAIN, SEC_CLIP, SEC_SETTLE, SEC_MONITOR, SEC_BIAS = range(6)
SECTION_NAME = {SEC_META: "META", SEC_GAIN: "GAIN", SEC_CLIP: "CLIP", SEC_SETTLE: "SETTLE",
                SEC_MONITOR: "MONITOR", SEC_BIAS: "BIAS"}
OCAL_FREQ = (100, 120, 1000, 10000, 100000, 60)      # frequency index fi 0..5 (OCD 1.2 GAIN)
GAIN_ENTRY_SIZE = 332            # 12 + 32 x 8 + 8 x 8
SECTION_SIZE = {SEC_META: 32, SEC_GAIN: 6 * GAIN_ENTRY_SIZE, SEC_CLIP: 64, SEC_SETTLE: 84,
                SEC_MONITOR: 28, SEC_BIAS: 4}
GAIN_ENTRY_INDEX = 41            # validation entry index of GAIN as the meter reports it (firmware
                                 # own/ocal.s ocal_validate_section): 41 * fi + k, k 0..31 zeta,
                                 # 32..39 nu, 40 the pad halfword
GAIN_PAD_K = 40
STRUCTURE_INDEX = 0xFFFF         # entry index the host reports for a structure error
DEFAULT_SETTLE_MS = (26, 26, 26, 26, 26, 5, 5)       # OCD 1.2 SETTLE defaults per kind
SETTLE_MIN, SETTLE_MAX = 1, 500
CLIP_MIN_SPAN = 3600             # OCD 2.4: hi - lo must exceed this
# the vendor tables the OCD formulas use (OCD 5; meas.s meas_win)
OCAL_GAIN_CODES = tuple(1 << i for i in range(8))    # G[code], code 0..7
OCAL_RANGE_OHM = (100, 1000, 10000, 100000)          # R[c8], c8 0..3
KV_MIN, KV_MAX = struct.unpack("<f", struct.pack("<f", 1e-6))[0], 1.0   # as the meter's f32 constant


class OcalValidationError(DeviceError):
    """A body that the meter would refuse (OCD 2.4): the section id and the entry index the meter
    would answer in status 2 with 3 data bytes; STRUCTURE_INDEX for the structure of the body."""

    def __init__(self, section: int, index: int, why: str):
        self.section, self.index, self.why = section, index, why
        super().__init__("%s: %s" % (ocal_where(section, index), why))


def ocal_where(section: int, index: int) -> str:
    name = SECTION_NAME.get(section, "section %d" % section)
    return name if index == STRUCTURE_INDEX else "%s entry %d" % (name, index)


@dataclass
class GainFreq:
    """GAIN entry of one frequency index (OCD 1.2): mask bits (zeta: bit 8 * c8 + c6, nu: bit
    c5) for magnitude and phase, 32 zeta and 8 nu factors. A factor is used by the meter only
    when all four bits of its cell are set (OCD 2.1)."""
    zeta_mag_mask: int = 0
    zeta_phase_mask: int = 0
    nu_mag_mask: int = 0
    nu_phase_mask: int = 0
    pad: int = 0
    zeta: list = field(default_factory=lambda: [1 + 0j] * 32)
    nu: list = field(default_factory=lambda: [1 + 0j] * 8)

    def cell_calibrated(self, c5: int, c6: int, c8: int) -> bool:
        k = 8 * c8 + c6
        return bool(self.zeta_mag_mask >> k & 1 and self.zeta_phase_mask >> k & 1
                    and self.nu_mag_mask >> c5 & 1 and self.nu_phase_mask >> c5 & 1)

    def any_cell(self) -> bool:
        return any(self.cell_calibrated(c5, c6, c8)
                   for c8 in range(4) for c6 in range(8) for c5 in range(8))


def pack_gain_freq(g: GainFreq) -> bytes:
    if len(g.zeta) != 32 or len(g.nu) != 8:
        raise ValueError("GAIN entry: 32 zeta and 8 nu factors")
    out = struct.pack("<IIBBH", g.zeta_mag_mask, g.zeta_phase_mask, g.nu_mag_mask,
                      g.nu_phase_mask, g.pad)
    for z in list(g.zeta) + list(g.nu):
        out += struct.pack("<ff", complex(z).real, complex(z).imag)
    return out


def unpack_gain_freq(d: bytes) -> GainFreq:
    if len(d) != GAIN_ENTRY_SIZE:
        raise DeviceError("GAIN entry of %d bytes, expected %d" % (len(d), GAIN_ENTRY_SIZE))
    zm, zp, nm, np_, pad = struct.unpack_from("<IIBBH", d)
    v = struct.unpack_from("<80f", d, 12)
    z = [complex(v[2 * i], v[2 * i + 1]) for i in range(40)]
    return GainFreq(zm, zp, nm, np_, pad, z[:32], z[32:])


def pack_gain(entries) -> bytes:
    """The GAIN section body: six entries in the frequency-index order of OCAL_FREQ."""
    entries = list(entries)
    if len(entries) != len(OCAL_FREQ):
        raise ValueError("GAIN: %d entries expected" % len(OCAL_FREQ))
    return b"".join(pack_gain_freq(g) for g in entries)


def unpack_gain(d: bytes) -> list:
    if len(d) != SECTION_SIZE[SEC_GAIN]:
        raise DeviceError("GAIN section of %d bytes, expected %d" % (len(d), SECTION_SIZE[SEC_GAIN]))
    return [unpack_gain_freq(d[i * GAIN_ENTRY_SIZE:(i + 1) * GAIN_ENTRY_SIZE])
            for i in range(len(OCAL_FREQ))]


def pack_clip(clip) -> bytes:
    """CLIP section body from 16 (lo, hi) pairs: index 8 * channel + gain code, channel 0 = ADC1
    (V), 1 = ADC2 (I)."""
    clip = list(clip)
    if len(clip) != 16:
        raise ValueError("CLIP: 16 (lo, hi) pairs")
    return b"".join(struct.pack("<HH", lo, hi) for lo, hi in clip)


def unpack_clip(d: bytes) -> list:
    if len(d) != SECTION_SIZE[SEC_CLIP]:
        raise DeviceError("CLIP section of %d bytes" % len(d))
    return [struct.unpack_from("<HH", d, 4 * i) for i in range(16)]


def pack_settle(table) -> bytes:
    """SETTLE section body from 7 rows (kind 0..6) of 6 values (frequency index), milliseconds."""
    rows = [list(r) for r in table]
    if len(rows) != 7 or any(len(r) != 6 for r in rows):
        raise ValueError("SETTLE: 7 kinds x 6 frequencies")
    return struct.pack("<42H", *[v for r in rows for v in r])


def unpack_settle(d: bytes) -> list:
    if len(d) != SECTION_SIZE[SEC_SETTLE]:
        raise DeviceError("SETTLE section of %d bytes" % len(d))
    v = struct.unpack("<42H", d)
    return [list(v[6 * k:6 * k + 6]) for k in range(7)]


def pack_monitor(mask: int, kv) -> bytes:
    kv = list(kv)
    if len(kv) != 6:
        raise ValueError("MONITOR: six kV values")
    return struct.pack("<I6f", mask, *kv)


def unpack_monitor(d: bytes) -> tuple:
    if len(d) != SECTION_SIZE[SEC_MONITOR]:
        raise DeviceError("MONITOR section of %d bytes" % len(d))
    v = struct.unpack("<I6f", d)
    return v[0], list(v[1:])


def pack_bias(limit: int, pad: int = 0) -> bytes:
    return struct.pack("<HH", limit, pad)


def pack_meta(time_s: int, note: str = "") -> bytes:
    """META section body: host time (Unix seconds) and a note of at most 28 ASCII bytes."""
    raw = note.encode("ascii", "replace")[:28]
    return struct.pack("<I28s", int(time_s) & 0xFFFFFFFF, raw)


def unpack_meta(d: bytes) -> tuple:
    if len(d) != SECTION_SIZE[SEC_META]:
        raise DeviceError("META section of %d bytes" % len(d))
    t, note = struct.unpack("<I28s", d)
    return t, note.rstrip(b"\0").decode("ascii", "replace")


def check_gain_factor(z: complex) -> str | None:
    """OCD 2.4 for a factor whose mask bit is set: finite, 0.25 <= re^2 + im^2 <= 4, re > 0 and
    4 re^2 >= re^2 + im^2 (|arg| <= 60 degrees). None when it passes, else the reason."""
    re, im = z.real, z.imag
    if not (math.isfinite(re) and math.isfinite(im)):
        return "not finite"
    m2 = re * re + im * im
    if not 0.25 <= m2 <= 4.0:
        return "magnitude %.4g outside 0.5 .. 2" % math.sqrt(m2)
    if not (re > 0 and 4 * re * re >= m2):
        return "phase beyond 60 degrees"
    return None


def check_gain_entry(g: GainFreq, fi: int = 0) -> tuple[int, str] | None:
    """OCD 2.4 GAIN rules for one entry -> (entry index, reason) of the first violation, or None."""
    base = GAIN_ENTRY_INDEX * fi
    if g.pad != 0:
        return base + GAIN_PAD_K, "pad is not 0"
    if g.zeta_mag_mask >> 32 or g.zeta_phase_mask >> 32 or g.nu_mag_mask >> 8 or g.nu_phase_mask >> 8:
        return base, "mask bit beyond zeta 0..31 / nu 0..7"
    for k in range(32):
        if (g.zeta_mag_mask | g.zeta_phase_mask) >> k & 1:
            why = check_gain_factor(complex(g.zeta[k]))
            if why:
                return base + k, "zeta[%d][%d]: %s" % (k // 8, k % 8, why)
    for c5 in range(8):
        if (g.nu_mag_mask | g.nu_phase_mask) >> c5 & 1:
            why = check_gain_factor(complex(g.nu[c5]))
            if why:
                return base + 32 + c5, "nu[%d]: %s" % (c5, why)
    return None


def validate_section(sid: int, d: bytes) -> None:
    """The value rules of OCD 2.4 for one section body; raises OcalValidationError. Unknown ids
    (a later format's additions) have no value rules; META has none."""
    want = SECTION_SIZE.get(sid)
    if want is not None and len(d) != want:
        raise OcalValidationError(sid, STRUCTURE_INDEX, "length %d, expected %d" % (len(d), want))
    if sid == SEC_GAIN:
        for fi, g in enumerate(unpack_gain(d)):
            bad = check_gain_entry(g, fi)
            if bad:
                raise OcalValidationError(sid, bad[0], bad[1])
    elif sid == SEC_CLIP:
        for i, (lo, hi) in enumerate(unpack_clip(d)):
            if hi > 4095 or lo >= hi or hi - lo <= CLIP_MIN_SPAN:
                raise OcalValidationError(sid, i, "lo %d, hi %d: need hi <= 4095, lo < hi and "
                                          "hi - lo > %d" % (lo, hi, CLIP_MIN_SPAN))
    elif sid == SEC_SETTLE:
        for i, v in enumerate(struct.unpack("<42H", d)):
            if not SETTLE_MIN <= v <= SETTLE_MAX:
                raise OcalValidationError(sid, i, "%d ms outside %d .. %d"
                                          % (v, SETTLE_MIN, SETTLE_MAX))
    elif sid == SEC_MONITOR:
        mask, kv = unpack_monitor(d)
        if mask >> 6:
            raise OcalValidationError(sid, 0, "mask bit beyond 0..5")
        for fi in range(6):
            if mask >> fi & 1 and not (math.isfinite(kv[fi]) and KV_MIN <= kv[fi] <= KV_MAX):
                raise OcalValidationError(sid, fi, "kV %g outside %g .. %g" % (kv[fi], KV_MIN, KV_MAX))
    elif sid == SEC_BIAS:
        limit, pad = struct.unpack("<HH", d)
        if not 1 <= limit <= 4095 or pad != 0:
            raise OcalValidationError(sid, 0, "limit %d (1 .. 4095) / pad %d (0)" % (limit, pad))


def parse_body(body: bytes) -> dict:
    """The sections of a record body {id: bytes} (OCD 1.2): {id u16, length u16} + body in
    strictly ascending ids, every length a multiple of 4 and inside the body. A body that breaks
    that raises OcalValidationError (the walk the meter does at load stops there instead)."""
    body = bytes(body)
    out: dict = {}
    pos, prev = 0, -1
    while pos + 4 <= len(body):
        sid, n = struct.unpack_from("<HH", body, pos)
        if n % 4:
            raise OcalValidationError(sid, STRUCTURE_INDEX, "length %d is not a multiple of 4" % n)
        if pos + 4 + n > len(body):
            raise OcalValidationError(sid, STRUCTURE_INDEX, "runs past the end of the body")
        if sid <= prev:
            raise OcalValidationError(sid, STRUCTURE_INDEX, "ids are not strictly ascending")
        out[sid] = body[pos + 4:pos + 4 + n]
        prev, pos = sid, pos + 4 + n
    if pos != len(body):
        raise OcalValidationError(prev if prev >= 0 else 0, STRUCTURE_INDEX,
                                  "%d bytes after the last section" % (len(body) - pos))
    return out


def build_body(sections: dict, validate: bool = True) -> bytes:
    """A record body from {id: section bytes}, ids ascending (OCD 1.2). With `validate` the
    structure and the value rules of OCD 2.4 are checked here, as the meter does: body length
    8 .. 0x9C0 (multiple of 4), known ids with exactly their length."""
    out = bytearray()
    for sid in sorted(sections):
        data = bytes(sections[sid])
        if not 0 <= sid <= 0xFFFF or len(data) % 4 or len(data) > 0xFFFF:
            raise OcalValidationError(sid, STRUCTURE_INDEX, "length %d is not a multiple of 4" % len(data))
        if validate:
            validate_section(sid, data)
        out += struct.pack("<HH", sid, len(data)) + data
    if validate and not OCAL_BODY_MIN <= len(out) <= OCAL_BODY_MAX:
        raise OcalValidationError(0, STRUCTURE_INDEX, "body of %d bytes, allowed %d .. %d"
                                  % (len(out), OCAL_BODY_MIN, OCAL_BODY_MAX))
    return bytes(out)


def validate_body(body: bytes) -> dict:
    """parse_body and the value rules for every section; returns the sections."""
    body = bytes(body)
    if len(body) % 4 or not OCAL_BODY_MIN <= len(body) <= OCAL_BODY_MAX:
        raise OcalValidationError(0, STRUCTURE_INDEX, "body of %d bytes: multiple of 4, %d .. %d"
                                  % (len(body), OCAL_BODY_MIN, OCAL_BODY_MAX))
    sections = parse_body(body)
    for sid, data in sections.items():
        validate_section(sid, data)
    return sections


@dataclass
class OcalRecord:
    """One slot read as a record (OCD 1.2 header, 1.3 step 1). `valid` = CRC-valid: magic, header
    size, length, sequence and CRC-32 all right."""
    slot: int
    valid: bool
    why: str
    format: int = 0
    length: int = 0
    sequence: int = 0
    uid: bytes = b""
    shopen_crc: int = 0
    revoked: bool = False
    raw: bytes = field(default=b"", repr=False)

    @property
    def body(self) -> bytes:
        return self.raw[OCAL_HEADER:self.length]

    def own(self, uid: bytes) -> bool:
        return self.valid and self.uid == bytes(uid)

    def in_force(self, uid: bytes) -> bool:
        return self.own(uid) and not self.revoked

    def describe(self) -> str:
        if not self.valid:
            return "slot %d: no record (%s)" % (self.slot, self.why)
        return "slot %d: format %d, sequence %d, %d bytes, UID %s%s" % (
            self.slot, self.format, self.sequence, self.length, self.uid.hex().upper(),
            ", revoked" if self.revoked else "")


def ocal_record_crc(rec: bytes, length: int) -> int:
    """CRC-32 of bytes 0..0x37 followed by bytes 0x40..length-1 (OCD 1.2, the word at +0x3C)."""
    return crc32(bytes(rec[:OCAL_REVOKE]) + bytes(rec[OCAL_HEADER:length]))


def parse_ocal_record(slot_bytes: bytes, slot: int = 0) -> OcalRecord:
    """The record at the start of one slot's bytes (OCD 1.3 step 1)."""
    d = bytes(slot_bytes)
    if len(d) < OCAL_HEADER:
        return OcalRecord(slot, False, "fewer than %d bytes" % OCAL_HEADER)
    magic, fmt, hsize, length, seq = struct.unpack_from("<IHHII", d)
    if magic != OCAL_MAGIC:
        return OcalRecord(slot, False, "no magic")
    if hsize != OCAL_HEADER:
        return OcalRecord(slot, False, "header size %d" % hsize)
    if not OCAL_HEADER <= length <= OCAL_SLOT_SIZE or length % 4 or length > len(d):
        return OcalRecord(slot, False, "length %d" % length)
    if seq in (0, 0xFFFFFFFF):
        return OcalRecord(slot, False, "sequence 0x%08X" % seq)
    (stored,) = struct.unpack_from("<I", d, OCAL_CRC_AT)
    if ocal_record_crc(d, length) != stored:
        return OcalRecord(slot, False, "CRC-32 mismatch")
    (shopen,) = struct.unpack_from("<I", d, 0x1C)
    (revoke,) = struct.unpack_from("<I", d, OCAL_REVOKE)
    return OcalRecord(slot, True, "ok", fmt, length, seq, d[0x10:0x1C], shopen,
                      revoke != 0xFFFFFFFF, d[:length])


def parse_ocal_file(data: bytes) -> list:
    """The two records of a .ocal backup file / of both slots read (0x4000 bytes)."""
    if len(data) != OCAL_FILE_SIZE:
        raise DeviceError("OCAL image of %d bytes, expected %d" % (len(data), OCAL_FILE_SIZE))
    return [parse_ocal_record(data[i * OCAL_SLOT_SIZE:(i + 1) * OCAL_SLOT_SIZE], i) for i in range(2)]


def ocal_active_record(records, uid: bytes) -> OcalRecord | None:
    """The active record by OCD 1.3 steps 2 and 3: among own records in force the higher
    sequence (slot 0 on a tie); None when its format is not 1 (the meter sets OF_NEWER)."""
    cand = [r for r in records if r.in_force(uid)]
    if not cand:
        return None
    best = cand[0]
    for r in cand[1:]:
        if r.sequence > best.sequence:
            best = r
    return best if best.format == OCAL_FORMAT else None


@dataclass
class OcalInfo:
    """OCAL_INFO, 64 bytes (OCD 4.2)."""
    flags: int
    active_slot: int
    format: int
    active_length: int
    active_sequence: int
    other_sequence: int
    active_crc: int
    sections_present: int
    gain_frequencies: int
    monitor_frequencies: int
    sections_rejected: int
    t_max: int
    meta_time: int
    meta_note: str
    raw: bytes = field(default=b"", repr=False)
    SIZE = 64

    def flag(self, bit: int) -> bool:
        return bool(self.flags >> bit & 1)

    @property
    def active(self) -> bool:
        return self.flag(OF_ACTIVE) and self.active_slot != OCAL_NO_SLOT

    def has_section(self, sid: int) -> bool:
        return bool(self.sections_present >> sid & 1)

    def gain_hz(self) -> list:
        return [OCAL_FREQ[i] for i in range(6) if self.gain_frequencies >> i & 1]

    def monitor_hz(self) -> list:
        return [OCAL_FREQ[i] for i in range(6) if self.monitor_frequencies >> i & 1]

    def rejected_names(self) -> list:
        return [SECTION_NAME.get(i, "section %d" % i) for i in range(8)
                if self.sections_rejected >> i & 1]

    def describe(self) -> str:
        parts = ["flags 0x%04X: %s" % (self.flags, ", ".join(flag_names(self.flags, OCAL_INFO_FLAGS))
                                       or "none")]
        if self.active:
            parts.append("active slot %d, format %d, %d bytes, sequence %d, CRC-32 0x%08X"
                         % (self.active_slot, self.format, self.active_length,
                            self.active_sequence, self.active_crc))
        else:
            parts.append("no active record")
        parts.append("other slot sequence %d" % self.other_sequence)
        return "; ".join(parts)


def parse_ocal_info(d: bytes) -> OcalInfo:
    _short("OCAL_INFO", d, OcalInfo.SIZE)
    (flags, slot, fmt, length, seq, other, crc, present, gain, mon, rej, tmax, _pad, mtime,
     note) = struct.unpack_from("<IBBHIIIBBBBHHI28s", d)
    return OcalInfo(flags, slot, fmt, length, seq, other, crc, present, gain, mon, rej, tmax,
                    mtime, note.rstrip(b"\0").decode("ascii", "replace"),
                    bytes(d[:OcalInfo.SIZE]))


@dataclass
class OcalRaw:
    """OCAL_RAW record of the last closed window, 64 bytes (OCD 2.2, 4.1 0x35)."""
    window: int
    c5: int
    c6: int
    c8: int
    lock: int
    frequency: int
    level: float
    bias: float
    n: int
    cls: int
    flags: int
    z_re: float
    z_im: float
    a_v: float
    a_i: float
    sigma2_v: float
    sigma2_i: float
    max1: int
    min1: int
    mid1: int
    max2: int
    min2: int
    mid2: int
    raw: bytes = field(default=b"", repr=False)
    SIZE = 64

    @property
    def cell(self) -> tuple:
        return (self.c5, self.c6, self.c8)

    @property
    def locked(self) -> bool:
        return bool(self.lock)

    @property
    def saturated(self) -> bool:
        return bool(self.flags & RAW_SATURATED)

    @property
    def overload(self) -> bool:
        return bool(self.flags & RAW_OVERLOAD)

    @property
    def znom(self) -> complex:
        return complex(self.z_re, self.z_im)

    def variance(self) -> float:
        """Variance of log Znom (real and imaginary part alike) from the window's statistics: a
        DFT over 128 samples of noise sigma^2 has component variance sigma^2 / 64 per
        acquisition, so relative to the peak amplitudes A_V, A_I in counts (OCD 2.2) the variance
        of one part of log Z is (sigma2_v / A_v^2 + sigma2_i / A_i^2) / (64 N)."""
        if self.a_v <= 0 or self.a_i <= 0 or self.n <= 0:
            return math.inf
        return (self.sigma2_v / self.a_v ** 2 + self.sigma2_i / self.a_i ** 2) / (64.0 * self.n)


def parse_ocal_raw(d: bytes) -> OcalRaw:
    _short("OCAL_RAW", d, OcalRaw.SIZE)
    v = struct.unpack_from("<IBBBBIffHBBffffffHHHHHH", d)
    return OcalRaw(*v, raw=bytes(d[:OcalRaw.SIZE]))


@dataclass
class TraceEntry:
    tick: int
    z_re: float
    z_im: float
    flags: int
    c5: int
    c6: int
    c8: int

    @property
    def z(self) -> complex:
        return complex(self.z_re, self.z_im)

    @property
    def discarded(self) -> bool:
        return bool(self.flags & TRACE_DISCARDED)

    @property
    def saturated(self) -> bool:
        return bool(self.flags & TRACE_SATURATED)

    @property
    def out_of_band(self) -> bool:
        return bool(self.flags & TRACE_OUT_OF_BAND)


@dataclass
class OcalTrace:
    """OCAL_TRACE read answer (OCD 4.1 0x36): `count` entries are recorded, `entries` the ones
    this answer (or both reads of ocal_trace_read_all) carries."""
    state: int
    kind: int
    change_tick: int
    count: int
    entries: list
    SIZE = 8

    @property
    def state_name(self) -> str:
        return TRACE_STATE_NAME.get(self.state, "?%d" % self.state)


def parse_ocal_trace(d: bytes) -> OcalTrace:
    _short("OCAL_TRACE", d, OcalTrace.SIZE)
    state, kind, tick, count, n = struct.unpack_from("<BBIBB", d)
    if len(d) < OcalTrace.SIZE + 16 * n:
        raise DeviceError("OCAL_TRACE answer of %d bytes for %d entries" % (len(d), n))
    entries = [TraceEntry(*struct.unpack_from("<IffBBBB", d, OcalTrace.SIZE + 16 * i))
               for i in range(n)]
    return OcalTrace(state, kind, tick, count, entries)


@dataclass
class Monitor:
    """MONITOR answer, 25 bytes (OCD 4.1 0x37, 5). v_rms / i_rms are NaN without a calibration,
    without a reading or on overload."""
    v_rms: float
    i_rms: float
    a_v_ref: float
    a_i_ref: float
    flags: int
    mid1: int
    mid2: int
    window: int
    SIZE = 25

    @property
    def v_calibrated(self) -> bool:
        return bool(self.flags & MON_V_CALIBRATED)

    @property
    def gain_applied(self) -> bool:
        return bool(self.flags & MON_GAIN_APPLIED)

    @property
    def no_reading(self) -> bool:
        return bool(self.flags & MON_NO_READING)

    @property
    def lock(self) -> bool:
        return bool(self.flags & MON_LOCK)


def parse_monitor(d: bytes) -> Monitor:
    _short("MONITOR", d, Monitor.SIZE)
    return Monitor(*struct.unpack_from("<ffffBHHI", d))


def monitor_kv(v_dmm: float, a_v: float, c5: int, nu_mag: float = 1.0) -> float:
    """kV = V_dmm * G[c5] / (A_V * |nu|) (OCD 5 Calibration): V rms per count of peak amplitude
    referred to V gain 1. nu_mag is |nu[fi][c5]| when the gain is applied at the cell, else 1."""
    if not 0 <= c5 < 8:
        raise ValueError("c5 0..7")
    if not (math.isfinite(v_dmm) and v_dmm > 0 and math.isfinite(a_v) and a_v > 0
            and math.isfinite(nu_mag) and nu_mag > 0):
        raise ValueError("V_dmm, A_V and |nu| must be finite and above 0")
    return v_dmm * OCAL_GAIN_CODES[c5] / (a_v * nu_mag)


@dataclass
class OcalWriteResult:
    """Last OCAL_WRITE answer (status 0) or the data of its status 5 answer."""
    result: int
    slot: int
    sequence: int

    @property
    def ok(self) -> bool:
        return self.result == 0

    def describe(self) -> str:
        return "commit %s, slot %d, sequence %d" % (FLASH_RESULT.get(self.result, self.result),
                                                    self.slot, self.sequence)


@dataclass
class OcalRollbackResult:
    slot: int
    sequence: int
    flags: int

    @property
    def active(self) -> bool:
        return self.slot != OCAL_NO_SLOT

    def describe(self) -> str:
        if not self.active:
            return "no record active (flags 0x%04X)" % self.flags
        return "record in slot %d, sequence %d, flags 0x%04X active" % (self.slot, self.sequence,
                                                                          self.flags)


# ----------------------------------------------------------------------------------------------
# MEASURE record -> Reading (SI units, validity, statistics key)
# ----------------------------------------------------------------------------------------------

def _block_reading(quantity: str, b: ValueBlock, t: float, wall: float, meter_ms: int,
                   key: tuple) -> Reading:
    """One value block as a Reading (value in SI base units; SL 6.2)."""
    scale, si = QUANTITY_SI.get(quantity, (1.0, ""))
    validity = VALIDITY_NAME.get(b.validity, "unknown")
    value: float | None
    if quantity not in QUANTITY_SI or validity in ("overload", "no reading", "unknown") \
            or not math.isfinite(b.value):
        value = None
    else:
        value = b.value * scale
    unit_text = UNIT_TEXT.get(b.unit, "")
    if validity in ("overload", "no reading") or not unit_text:
        text = b.text
    else:
        text = (b.text + " " + unit_text) if b.text else unit_text
    return Reading(t, wall, meter_ms, quantity, value, si, text, b.decimals, validity, None, key)


def measure_to_reading(m: Measure, t: float, wall: float) -> Reading:
    """The MEASURE record as a Reading with the secondary row attached. The key changes with the
    quantity of either row, the test conditions (frequency, level, bias, range, circuit mode,
    series / parallel values), so statistics and trend restart on those changes."""
    main_q = MAIN_NAME.get(m.main_row, "")
    sec_q = SECONDARY_NAME.get(m.secondary_kind, "")
    key = (main_q, sec_q, QUANTITY_SI.get(main_q, (0, ""))[1], QUANTITY_SI.get(sec_q, (0, ""))[1],
           m.frequency, round(m.level, 4), round(m.bias, 4), m.range, m.circuit, m.parallel)
    main = _block_reading(main_q, m.main, t, wall, m.time, key)
    main.secondary = _block_reading(sec_q, m.secondary, t, wall, m.time, key + ("secondary",))
    main.unspecified = m.main_unspecified
    main.secondary.unspecified = m.secondary_unspecified
    main.gain_flags = m.ocal_flags
    return main


# ----------------------------------------------------------------------------------------------
# Files: UF2, slot files, settings copies
# ----------------------------------------------------------------------------------------------

def check_uf2(data: bytes) -> int:
    """Checks an application .uf2 for the slot bootloader (SL 13.1 step 1): 512-byte blocks with
    the UF2 magic words, the family flag and family ID 0xC6B3602B, 256-byte payloads for
    0x08006000..0x08061FFF. Returns the number of blocks."""
    if not data or len(data) % 512:
        raise DeviceError("UF2 file length %d is not a multiple of 512" % len(data))
    n = len(data) // 512
    for i in range(n):
        m0, m1, flags, addr, size, _num, _total, family = struct.unpack_from("<8I", data, 512 * i)
        (mend,) = struct.unpack_from("<I", data, 512 * i + 508)
        if (m0, m1, mend) != (UF2_MAGIC0, UF2_MAGIC1, UF2_MAGIC_END):
            raise DeviceError("block %d: not a UF2 block" % i)
        if not flags & UF2_FLAG_FAMILY or family != UF2_FAMILY:
            raise DeviceError("block %d: family 0x%08X is not the LC1020E application (0x%08X)"
                              % (i, family, UF2_FAMILY))
        if size != 256 or addr % 256 or not APP_BASE <= addr < APP_END:
            raise DeviceError("block %d: target 0x%08X / %d bytes outside the application area"
                              % (i, addr, size))
    return n


def check_slot_file(data: bytes, base: int) -> int:
    """The checks of BOOT_BEGIN and BOOT_END on the host (SL 5.6, 13.2 step 2) for a slot file to
    be written at `base`. Returns the header version."""
    if len(data) < SLOT_FILE_MIN or len(data) > SLOT_SIZE or len(data) % 4:
        raise DeviceError("slot file of %d bytes: must be 0x%X..0x%X bytes and a multiple of 4"
                          % (len(data), SLOT_FILE_MIN, SLOT_SIZE))
    magic, length, icrc, version, link = struct.unpack_from("<5I", data, 0)
    (hcrc,) = struct.unpack_from("<I", data, 28)
    if magic != HDR_MAGIC:
        raise DeviceError('slot file: no "SLOT" header')
    if crc32(data[:28]) != hcrc:
        raise DeviceError("slot file: header CRC-32 does not match")
    if link != base + 0x200:
        raise DeviceError("slot file is linked for 0x%08X; the free slot needs 0x%08X"
                          % (link, base + 0x200))
    if length < 8 or length > SLOT_ROOM or length % 4 or 0x200 + length > len(data):
        raise DeviceError("slot file: image length 0x%X does not fit" % length)
    image = data[0x200:0x200 + length]
    if crc32(image) != icrc:
        raise DeviceError("slot file: image CRC-32 does not match")
    sp, reset = struct.unpack_from("<II", image, 0)
    if not SRAM_LO < sp <= SRAM_HI:
        raise DeviceError("slot file: initial SP 0x%08X outside RAM" % sp)
    if not reset & 1 or not link <= (reset & ~1) < link + length:
        raise DeviceError("slot file: reset vector 0x%08X outside the image" % reset)
    return version


def check_copy(copy: bytes) -> tuple[bool, str]:
    """A settings copy (0x1000 bytes as CAL_READ gives it) -> (valid, description). SL 9: both
    markers 0x12345678 at 0 and 0xC10; trailer sequence at 0xFF8 and CRC-32 at 0xFFC over the
    block bytes followed by the 4 sequence bytes; a vendor-format copy has both trailer words
    0xFFFFFFFF."""
    if len(copy) != COPY_SIZE:
        return False, "copy of %d bytes, expected %d" % (len(copy), COPY_SIZE)
    first, last = struct.unpack_from("<I", copy, 0)[0], struct.unpack_from("<I", copy, 0xC10)[0]
    seq, crc = struct.unpack_from("<II", copy, 0xFF8)
    if first != BLOCK_MARKER or last != BLOCK_MARKER:
        return False, "markers missing (0x%08X, 0x%08X)" % (first, last)
    if seq == 0xFFFFFFFF and crc == 0xFFFFFFFF:
        return True, "valid, vendor format (no trailer)"
    if zlib.crc32(copy[0xFF8:0xFFC], zlib.crc32(copy[:BLOCK_SIZE])) & 0xFFFFFFFF != crc:
        return False, "both markers, trailer CRC-32 wrong (sequence %d)" % seq
    return True, "valid, sequence %d" % seq


# ----------------------------------------------------------------------------------------------
# Exceptions of this module
# ----------------------------------------------------------------------------------------------

class UpdateFailed(DeviceError):
    """INFO after an update reports a last update result other than 1 (SL 13.1 step 5)."""

    def __init__(self, result: int):
        self.result = result
        super().__init__("last update result %d (%s)" % (result, UPDATE_RESULT.get(result, "?")))


class BackupMismatch(SafetyRefused):
    """A backup file that is not for this meter or is damaged (DESIGN 13.9)."""


class UpdateCopyFailed(DeviceError):
    """The .uf2 could not be written to the update drive, which is still there: the meter stays
    in update mode (SL 13.1 step 4) and the file can be copied onto `drive` again."""

    def __init__(self, drive: str, cause: OSError):
        self.drive, self.cause = drive, cause
        super().__init__("writing the update file to %s failed: %s. The meter stays in update "
                         "mode (SL 13.1 step 4); its drive %s is offered on the Update drive page "
                         "for the copy again" % (drive, cause, drive))


# ----------------------------------------------------------------------------------------------
# The device
# ----------------------------------------------------------------------------------------------

Progress = Callable[[float, str], None]


class _Job:
    """State of one exclusive job: resume = restart the live stream when the job ends."""

    def __init__(self):
        self.resume = True


class LC1020E(Device):
    """The LC1020E on a SerialLink.

    backup_folder: folder of the backups the save methods write first (DESIGN 13.8, 13.9).
    reconnect(uid_hex, timeout) -> link: waits for the meter's port after a restart (the same
        USB serial number) and returns the opened new link (DESIGN 13.13); needed by the update
        flows, optional for the save commands that restart the meter.
    sleep, clock, now: injected time sources.
    """

    model = MODEL
    COMMANDS = COMMANDS

    def __init__(self, link, info: Info, *, backup_folder: Path | None = None,
                 reconnect: Callable[[str, float], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        super().__init__(link, info)
        self.backup_folder = Path(backup_folder) if backup_folder is not None else None
        self._reconnect = reconnect
        self._sleep, self._clock, self._now = sleep, clock, now
        self._live = False
        self._stream_connected = False
        self._event_connected = False
        self.bad_frames = 0
        self._attach(link)

    # -- link plumbing ------------------------------------------------------------------------
    def _attach(self, link) -> None:
        link.event_frame.connect(self._on_event)
        self._event_connected = True

    def _detach(self, link) -> None:
        if self._event_connected:
            try:
                link.event_frame.disconnect(self._on_event)
            except (RuntimeError, TypeError):
                pass
            self._event_connected = False
        if self._stream_connected:
            try:
                link.stream.disconnect(self._on_stream)
            except (RuntimeError, TypeError):
                pass
            self._stream_connected = False

    def close(self) -> None:
        """Disconnects from the link's signals (the link itself is closed by its owner)."""
        self._live = False
        self._detach(self.link)

    def timeout_for(self, code: int, payload: bytes = b"") -> float:
        if code == KEY:                                   # SL 4.3: 23 s + the hold time
            hold = struct.unpack_from("<H", payload, 1)[0] if len(payload) >= 3 else 0
            return T_JOB + hold / 1000.0
        if code in (SCREEN, LCR_SET, HOLD, SETTINGS_SET, COMPARE_SET) \
                and self.info.has(CAP_DIRECT_SET):        # SL 4.3 / 14: direct set, no key job
            return T_DIRECT
        if code == CALIBRATE and self.info.has(CAP_CAL_SWEEP):   # SL 4.3: 360 000 ms in the meter
            return T_CALIBRATE_SWEEP
        return super().timeout_for(code, payload)

    # -- live ---------------------------------------------------------------------------------
    def start_live(self) -> None:
        """MEASURE_STREAM argument 0: a stream frame (cmd 0x22) for every new result (SL 5.3, 11).
        The link's stream signal then delivers Frame objects, turned into `reading` signals."""
        if self._live:
            return
        if not self._stream_connected:
            self.link.stream.connect(self._on_stream)
            self._stream_connected = True
        self.call(MEASURE_STREAM, struct.pack("<H", 0))
        self._live = True

    def stop_live(self) -> None:
        """MEASURE_STREAM 0xFFFF stops the stream (SL 5.3)."""
        if not self._live:
            return
        self._live = False
        try:
            self.call(MEASURE_STREAM, struct.pack("<H", 0xFFFF))
        finally:
            if self._stream_connected:
                try:
                    self.link.stream.disconnect(self._on_stream)
                except (RuntimeError, TypeError):
                    pass
                self._stream_connected = False

    @property
    def live(self) -> bool:
        return self._live

    def _parse_measure(self, d: bytes) -> Measure:
        """MEASURE record of this meter: byte 65 is parsed only with CAP_OCAL (OCD 2.8)."""
        return parse_measure(d, self.info.has(CAP_OCAL))

    def _on_stream(self, frame) -> None:
        try:
            if frame.cmd == MEASURE:
                self.reading.emit(measure_to_reading(self._parse_measure(frame.payload), frame.t,
                                                     time.time()))
            elif frame.cmd == STATE:
                self.state.emit(parse_state(frame.payload))
        except DeviceError:
            self.bad_frames += 1

    def _on_event(self, frame) -> None:
        self.meter_event.emit(parse_event(frame.cmd, frame.payload, frame.seq))

    @contextmanager
    def _job(self):
        """An exclusive port job (DESIGN 13.11): the stream is stopped first and started again
        afterwards unless the job set resume = False (the meter restarts)."""
        job = _Job()
        with self.link.exclusive():
            was_live = self._live
            if was_live:
                self.stop_live()
            try:
                yield job
            finally:
                if was_live and job.resume:
                    try:
                        self.start_live()
                    except (PortLost, AnswerTimeout):
                        pass

    # -- request helpers ----------------------------------------------------------------------
    def _rec(self, code: int, payload: bytes, parse, *, allow_dangerous: bool = False):
        """A request whose answer is a record; the record that comes with an error status (job
        commands answer it, SL 3) is attached to the StatusError as `.record`."""
        try:
            data = self.call(code, payload, allow_dangerous=allow_dangerous)
        except StatusError as e:
            rec = None
            if e.data:
                try:
                    rec = parse(e.data)
                except DeviceError:
                    rec = None
            e.record = rec
            raise
        return parse(data)

    @staticmethod
    def _need_confirm(confirm: str) -> None:
        if confirm != CONFIRM_WORD:
            raise SafetyRefused('typed confirmation "%s" required, got %r' % (CONFIRM_WORD, confirm))

    # -- 5.1 device ---------------------------------------------------------------------------
    def read_info(self) -> Info:
        """INFO again from the meter (the identified one stays in `info`)."""
        return Info.parse(self.call(INFO))

    def ping(self, data: bytes = b"") -> bytes:
        return self.call(PING, data)

    def reset(self) -> None:
        """RESET: soft restart; the port goes and comes back (SL 5.1, 7.6)."""
        self.call(RESET)
        self._live = False

    def battery(self) -> Battery:
        return parse_battery(self.call(BATTERY))

    def power_off(self, confirm: str) -> None:
        """POWER_OFF with the meter's confirmation 0x4F46 (SL 5.1). The meter saves its settings
        on the way, so a backup is made first (DESIGN 13.8)."""
        self._need_confirm(confirm)
        self._backup_before()
        with self._job() as job:
            job.resume = False
            self.call(POWER_OFF, struct.pack("<H", CONFIRM_POWER_OFF), allow_dangerous=True)

    def factory_mode(self) -> int:
        """0, 1 or 2 (2 = factory mode; SL 5.1)."""
        return self.call(FACTORY_MODE)[0]

    # -- 5.2 state and keys -------------------------------------------------------------------
    def read_state(self) -> State:
        return parse_state(self.call(STATE))

    def stream(self, period_ms: int) -> None:
        """STATE stream every period_ms, 0 stops (SL 5.2)."""
        self.call(STREAM, struct.pack("<H", period_ms))

    def screen(self, screen: int) -> State:
        return self._rec(SCREEN, bytes([screen]), parse_state)

    def key(self, bit: int, hold_ms: int = 0) -> State:
        if not 0 <= bit <= 13:
            raise ValueError("key bit 0..13")
        return self._rec(KEY, struct.pack("<BH", bit, hold_ms), parse_state)

    # -- 5.3 measurement ----------------------------------------------------------------------
    def lcr_table_count(self) -> int:
        return self.call(LCR_TABLES)[0]

    def lcr_tables(self, list_id: int) -> LcrList:
        return parse_lcr_list(self.call(LCR_TABLES, bytes([list_id])))

    def lcr_set(self, setting: int, index: int) -> Measure:
        return self._rec(LCR_SET, bytes([setting, index]), self._parse_measure)

    def measure(self) -> Measure:
        return self._parse_measure(self.call(MEASURE))

    def measure_reading(self) -> Reading:
        """One single MEASURE as a Reading."""
        return measure_to_reading(self.measure(), self._clock(), time.time())

    def measure_stream(self, period: int) -> None:
        """Raw MEASURE_STREAM: 0 every new result, 0xFFFF stop, else milliseconds (SL 5.3).
        start_live / stop_live are the ones that connect the readings."""
        self.call(MEASURE_STREAM, struct.pack("<H", period))

    def compare(self) -> Compare:
        return parse_compare(self.call(COMPARE))

    def compare_set(self, nominal: float, tolerance: float, warn_type: int, warn_sound: int,
                    warn_led: int) -> Compare:
        """SL 5.3.1 limits are checked by the meter (status 2)."""
        return self._rec(COMPARE_SET, struct.pack("<ffBBB", nominal, tolerance, warn_type,
                                                  warn_sound, warn_led), parse_compare)

    def tolerance_set(self, mode: int, lower: int = 0, upper: int = 0, letter: str = "",
                      secondary_kind: int = 0, secondary_value: float = 0.0) -> Compare:
        """TOLERANCE_SET (CAP_TOLERANCE), answered at once. mode 0 vendor +-% of COMPARE_SET's
        tolerance; 1 lower / upper in 0.01 % (-9999 .. 99990); 2 lower / upper in 0.01 pF
        (-99999 .. 99999, compare quantity C only, else status 3); 3 IEC 60062 `letter` (one of
        B C D F G J K M Z). Secondary limit kind 0 none, 1 D max, 2 Q min, 3 ESR max with a finite
        value >= 0. Arguments the meter refuses answer status 2 with the record (.record). RAM
        only; a later change of the meter's tolerance returns the mode to 0."""
        if mode not in TOLERANCE_MODE_NAME:
            raise ValueError("tolerance mode %r is not 0..3" % (mode,))
        if mode in (TOL_MODE_PERCENT, TOL_MODE_PF):
            lo_max = TOL_PERCENT_RANGE if mode == TOL_MODE_PERCENT else TOL_PF_RANGE
            if not lo_max[0] <= lower <= upper <= lo_max[1]:
                raise ValueError("limits %r .. %r outside %d .. %d or lower > upper"
                                 % (lower, upper, lo_max[0], lo_max[1]))
        else:
            lower = upper = 0
        if mode == TOL_MODE_LETTER:
            if len(letter) != 1 or letter not in TOL_LETTERS:
                raise ValueError("tolerance letter %r is not one of %s" % (letter, TOL_LETTERS))
        else:
            letter = ""
        if secondary_kind not in SECONDARY_LIMIT_NAME:
            raise ValueError("secondary limit kind %r is not 0..3" % (secondary_kind,))
        if not math.isfinite(secondary_value) or secondary_value < 0:
            raise ValueError("secondary limit value %r is not finite and >= 0" % (secondary_value,))
        payload = struct.pack("<BiiBBf", mode, lower, upper, ord(letter) if letter else 0,
                              secondary_kind, secondary_value)
        return self._rec(TOLERANCE_SET, payload, parse_compare)

    def hold(self, on: bool) -> State:
        return self._rec(HOLD, bytes([1 if on else 0]), parse_state)

    def calibrate(self, cal_type: int, confirm: str) -> CalResult:
        """CALIBRATE (SL 5.3.3): types 0 short, 11 open, 1..10 factory mode only. The result is
        saved by the meter itself, so a backup is made first (DESIGN 13.8)."""
        self._need_confirm(confirm)
        if not 0 <= cal_type <= 11:
            raise ValueError("calibration type 0..11")
        self._backup_before()
        payload = struct.pack("<BH", cal_type, CONFIRM_OK)
        with self._job():
            try:
                data = self.call(CALIBRATE, payload, allow_dangerous=True)
            except StatusError as e:
                e.record = parse_cal_answer(e.status, e.data) if len(e.data) >= 6 else None
                raise
        return parse_cal_answer(0, data)

    # -- 5.4 settings and storage -------------------------------------------------------------
    def settings_count(self) -> int:
        return self.call(SETTINGS_TABLES)[0]

    def settings_tables(self, setting: int) -> Setting:
        return parse_setting(self.call(SETTINGS_TABLES, bytes([setting])))

    def settings_set(self, setting: int, value: int) -> State:
        """RAM only, nothing saved (SL 5.4)."""
        return self._rec(SETTINGS_SET, struct.pack("<BH", setting, value), parse_state)

    def _save(self, code: int, payload: bytes, restarts_on_ok: bool = False) -> SaveResult:
        """One save command (answer: save status, save flags). A restart that follows is
        followed by the reconnect function when one was given."""
        result = None
        error = None
        with self._job() as job:
            try:
                data = self.call(code, payload, allow_dangerous=True)
                status = 0
            except StatusError as e:
                data, status, error = e.data, e.status, e
            if len(data) >= 8:
                result = parse_save_answer(status, data, restarts_on_ok)
                if result.restart:
                    job.resume = False
        if result is not None and result.restart:
            self._after_restart()
        if error is not None:
            error.record = result
            raise error
        if result is None:
            raise DeviceError("%s: save answer of %d bytes" % (COMMANDS[code].name, len(data)))
        return result

    def settings_save(self, confirm: str) -> SaveResult:
        """SETTINGS_SAVE (SL 5.4); backup first."""
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(SETTINGS_SAVE, struct.pack("<H", CONFIRM_OK))

    def restore_defaults(self, confirm: str) -> SaveResult:
        """RESTORE_DEFAULTS: the user calibration becomes the factory one (SL 5.4); backup first."""
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(RESTORE_DEFAULTS, struct.pack("<H", CONFIRM_OK))

    def settings_restore_b(self, confirm: str) -> SaveResult:
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(SETTINGS_RESTORE_B, struct.pack("<H", CONFIRM_OK), True)

    def settings_accept_a(self, confirm: str) -> SaveResult:
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(SETTINGS_ACCEPT_A, struct.pack("<H", CONFIRM_OK))

    def settings_accept_b(self, confirm: str) -> SaveResult:
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(SETTINGS_ACCEPT_B, struct.pack("<H", CONFIRM_OK))

    def settings_accept_defaults(self, confirm: str) -> SaveResult:
        self._need_confirm(confirm)
        self._backup_before()
        return self._save(SETTINGS_ACCEPT_DEFAULTS, struct.pack("<H", CONFIRM_OK))

    def write_block(self, block: bytes, confirm: str, chunk: int = 500) -> SaveResult:
        """SETTINGS_WRITE_BLOCK of a whole 0xC14-byte block (SL 9.4); backup first. Prefer
        restore(), which takes the block from a backup of this meter."""
        self._need_confirm(confirm)
        self._backup_before()
        return self._write_block(block, chunk)

    def _write_block(self, block: bytes, chunk: int = 500) -> SaveResult:
        block = bytes(block)
        if len(block) != BLOCK_SIZE:
            raise ValueError("block of %d bytes, expected 0x%X" % (len(block), BLOCK_SIZE))
        if not 1 <= chunk <= 502:
            raise ValueError("chunk 1..502 bytes")
        if struct.unpack_from("<I", block, 0)[0] != BLOCK_MARKER or \
                struct.unpack_from("<I", block, 0xC10)[0] != BLOCK_MARKER:
            raise DeviceError("block markers missing: the meter would refuse it (status 7)")
        crc = crc32(block)
        for attempt in range(WRITE_BLOCK_RETRIES):
            try:
                return self._write_block_once(block, chunk, crc)
            except StatusError as e:
                # SL 9.4: after status 7 repeat from offset 0 (a meter save during the transfer
                # makes the final check fail); the block itself was checked above.
                if e.status != 7 or attempt == WRITE_BLOCK_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")        # the loop returns or raises

    def _write_block_once(self, block: bytes, chunk: int, crc: int) -> SaveResult:
        off = 0
        with self._job() as job:
            while True:
                part = block[off:off + chunk]
                head = struct.pack("<HHHI", CONFIRM_OK, off, BLOCK_SIZE, crc)
                if off + len(part) < BLOCK_SIZE:
                    nxt = self.call(SETTINGS_WRITE_BLOCK, head + part, allow_dangerous=True)
                    _short("WRITE_BLOCK", nxt, 2)
                    (got,) = struct.unpack_from("<H", nxt)
                    if got != off + len(part):
                        raise DeviceError("WRITE_BLOCK: meter expects offset %d, sent %d"
                                          % (got, off + len(part)))
                    off = got
                    continue
                error = None
                try:
                    data = self.call(SETTINGS_WRITE_BLOCK, head + part, allow_dangerous=True)
                    status = 0
                except StatusError as e:
                    data, status, error = e.data, e.status, e
                result = parse_save_answer(status, data, True) if len(data) >= 8 else None
                if result is not None and result.restart:
                    job.resume = False
                break
        if result is not None and result.restart:
            self._after_restart()
        if error is not None:
            error.record = result
            raise error
        if result is None:
            raise DeviceError("WRITE_BLOCK: answer of %d bytes" % len(data))
        return result

    def cal_read(self, copy: int, offset: int, length: int) -> bytes:
        """SL 5.4: copy 0 = A at 0x0807F000, 1 = B at 0x08066000; offset + length <= 0x1000."""
        if copy not in COPY_BASE or not 1 <= length <= 256 or offset < 0 or \
                offset + length > COPY_SIZE:
            raise ValueError("CAL_READ: copy 0/1, length 1..256, offset + length <= 0x1000")
        data = self.call(CAL_READ, struct.pack("<BHH", copy, offset, length))
        if len(data) != length:
            raise DeviceError("CAL_READ: %d bytes, expected %d" % (len(data), length))
        return data

    def read_copy(self, copy: int) -> bytes:
        """The whole 0x1000 bytes of settings copy 0 (A) or 1 (B): 16 requests of 256 bytes
        (a cancel point before each)."""
        out = bytearray()
        for off in range(0, COPY_SIZE, 256):
            self._cancel_point()
            out += self.cal_read(copy, off, 256)
        return bytes(out)

    # -- backup and restore (DESIGN 13.9) -----------------------------------------------------
    def backup_dir(self, folder: Path) -> Path:
        return Path(folder) / ("%s-%s" % (MODEL, self.info.uid_hex))

    def _check_same_meter(self) -> None:
        """INFO read again: the chip UID must be the identified meter's (DESIGN 13.13)."""
        uid = self.read_info().uid
        if uid != self.info.uid:
            raise BackupMismatch("the meter on the link is not the identified one")

    def backup(self, folder: Path) -> Path:
        """Reads both settings copies (CAL_READ, 2 x 16 requests of 256 bytes, SL 9.4) into
        folder/LC1020E-<UID>/<date>.json: a JSON file with model, uid, date and for each copy its
        address, CRC-32, validity and the bytes. When the newest backup of this UID holds the same
        CRC-32s, no file is written and that file is returned. Cancellable between the requests
        (nothing is written then)."""
        out_dir = self.backup_dir(folder)
        with self._job(), self._cancellable():
            self._check_same_meter()
            copies = [self.read_copy(0), self.read_copy(1)]
        crcs = [crc32(c) for c in copies]
        newest = self._newest_backup(out_dir)
        if newest is not None and newest[1] == crcs:
            return newest[0]
        stamp = self._now()
        doc = {
            "format": 1,
            "model": MODEL,
            "uid": self.info.uid_hex,
            "firmware": self.info.firmware,
            "date": stamp.astimezone().isoformat(timespec="seconds") if stamp.tzinfo is None
            else stamp.isoformat(timespec="seconds"),
            "copies": [{"name": COPY_NAME[i], "address": "0x%08X" % COPY_BASE[i],
                        "length": len(c), "crc32": "0x%08X" % crcs[i],
                        "valid": check_copy(c)[0], "description": check_copy(c)[1],
                        "data": c.hex()} for i, c in enumerate(copies)],
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        base = stamp.strftime("%Y%m%d-%H%M%S")
        path = out_dir / (base + ".json")
        n = 1
        while path.exists():
            n += 1
            path = out_dir / ("%s-%d.json" % (base, n))
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    @staticmethod
    def load_backup(path: Path) -> dict:
        """A backup file as a dict with the copies decoded to bytes (`data`) and every stored
        CRC-32 checked against its data. Raises BackupMismatch for a damaged file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            copies = []
            for c in doc["copies"]:
                data = bytes.fromhex(c["data"])
                if crc32(data) != int(c["crc32"], 16):
                    raise BackupMismatch("%s: copy %s does not match its stored CRC-32"
                                         % (path, c["name"]))
                copies.append(dict(c, data=data))
            if doc.get("format") != 1 or doc.get("model") != MODEL or len(copies) != 2:
                raise BackupMismatch("%s: not an LC1020E backup (format 1, two copies)" % path)
            return dict(doc, copies=copies)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BackupMismatch("%s: unreadable backup (%s)" % (path, e)) from e

    def _newest_backup(self, out_dir: Path):
        """(path, [crc A, crc B]) of the newest readable backup of this UID, or None."""
        if not out_dir.is_dir():
            return None
        for path in sorted(out_dir.glob("*.json"), key=lambda p: p.name, reverse=True):
            try:
                doc = self.load_backup(path)
            except BackupMismatch:
                continue
            if doc.get("uid") != self.info.uid_hex:
                continue
            return path, [crc32(c["data"]) for c in doc["copies"]]
        return None

    def _backup_before(self) -> Path:
        """The backup every save / destroy method makes first (DESIGN 13.8)."""
        if self.backup_folder is None:
            raise SafetyRefused("no backup folder is set: nothing is sent without a backup")
        return self.backup(self.backup_folder)

    def restore(self, backup_path: Path, copy: str, confirm: str) -> SaveResult:
        """Restores one copy of a backup file of THIS meter (same UID only) with
        SETTINGS_WRITE_BLOCK (SL 9.4, DESIGN 13.9): the chosen copy is checked with check_copy,
        the current state is backed up first, status 7 restarts the transfer from offset 0, the
        meter restarts after the last chunk."""
        self._need_confirm(confirm)
        if copy not in ("A", "B"):
            raise ValueError('copy "A" or "B"')
        doc = self.load_backup(backup_path)
        if doc["uid"] != self.info.uid_hex:
            raise BackupMismatch("the backup is of meter %s, this meter is %s: refused"
                                 % (doc["uid"], self.info.uid_hex))
        chosen = next((c for c in doc["copies"] if c["name"] == copy), None)
        if chosen is None:
            raise BackupMismatch("the backup has no copy %s" % copy)
        ok, why = check_copy(chosen["data"])
        if not ok:
            raise BackupMismatch("copy %s of the backup is not valid: %s" % (copy, why))
        self._backup_before()
        return self._write_block(chosen["data"][:BLOCK_SIZE])

    # -- own calibration area (OCD 1, 4, 6.1) -------------------------------------------------
    @contextmanager
    def ocal_session(self):
        """The port held for a run of OCAL requests (the stream stopped and started again after),
        for the host procedures that send many (OCD 3.3)."""
        with self._job() as job:
            yield job

    def ocal_info(self) -> OcalInfo:
        return parse_ocal_info(self.call(OCAL_INFO))

    def ocal_read(self, slot: int, offset: int, length: int) -> bytes:
        """OCD 4.1: slot 0 / 1, length 1..256, offset + length <= 0x2000; the bytes as they are in
        flash."""
        if slot not in (0, 1) or not 1 <= length <= OCAL_READ_MAX or offset < 0 or \
                offset + length > OCAL_SLOT_SIZE:
            raise ValueError("OCAL_READ: slot 0/1, length 1..256, offset + length <= 0x2000")
        data = self.call(OCAL_READ, struct.pack("<BHH", slot, offset, length))
        if len(data) != length:
            raise DeviceError("OCAL_READ: %d bytes, expected %d" % (len(data), length))
        return data

    def ocal_read_slot(self, slot: int, length: int = OCAL_SLOT_SIZE) -> bytes:
        """The first `length` bytes of one slot in requests of 256 bytes (a cancel point before
        each)."""
        out = bytearray()
        for off in range(0, length, OCAL_READ_MAX):
            self._cancel_point()
            out += self.ocal_read(slot, off, min(OCAL_READ_MAX, length - off))
        return bytes(out)

    def ocal_read_slots(self) -> bytes:
        """Both slots, 2 x 32 requests of 256 bytes: 0x4000 bytes, slot 0 then slot 1 (OCD 6.1)."""
        return self.ocal_read_slot(0) + self.ocal_read_slot(1)

    def ocal_records(self) -> list:
        """Both slots read and parsed as records."""
        with self._job(), self._cancellable():
            return parse_ocal_file(self.ocal_read_slots())

    def ocal_backup(self, folder: Path) -> Path:
        """Both area slots (OCD 6.1) into folder/LC1020E-<UID>/LC1020E-<UID>-<date>.ocal, next to
        the settings backup: the 0x4000 bytes of slot 0 and slot 1. The UID is in the file name
        and in the header of every record the file holds. When the newest .ocal file of this
        UID holds the same bytes no file is written and that file is returned. Cancellable between
        the requests (nothing is written then)."""
        out_dir = self.backup_dir(folder)
        with self._job(), self._cancellable():
            self._check_same_meter()
            data = self.ocal_read_slots()
        newest = self._newest_ocal(out_dir)
        if newest is not None and newest[1] == data:
            return newest[0]
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "%s-%s-%s" % (MODEL, self.info.uid_hex, stamp)
        path = out_dir / (stem + ".ocal")
        n = 1
        while path.exists():
            n += 1
            path = out_dir / ("%s-%d.ocal" % (stem, n))
        tmp = path.with_suffix(".ocal.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    @staticmethod
    def load_ocal_file(path: Path, uid_hex: str | None = None) -> bytes:
        """The 0x4000 bytes of a .ocal backup. With `uid_hex` the UID in the file name must be that
        one (BackupMismatch otherwise); a file of the wrong size is refused."""
        path = Path(path)
        m = OCAL_SOURCE_NAME.match(path.name)
        if m is None:
            raise BackupMismatch("%s: not a LC1020E-<UID>-<date>.ocal file" % path.name)
        if uid_hex is not None and m.group(1) != uid_hex:
            raise BackupMismatch("the file is of meter %s, this meter is %s: refused"
                                 % (m.group(1), uid_hex))
        try:
            data = path.read_bytes()
        except OSError as e:
            raise BackupMismatch("%s: unreadable (%s)" % (path, e)) from e
        if len(data) != OCAL_FILE_SIZE:
            raise BackupMismatch("%s: %d bytes, expected %d" % (path.name, len(data), OCAL_FILE_SIZE))
        return data

    def _newest_ocal(self, out_dir: Path):
        """(path, bytes) of the newest readable .ocal file of this UID, or None."""
        if not out_dir.is_dir():
            return None
        for path in sorted(out_dir.glob("*.ocal"), key=lambda p: p.name, reverse=True):
            try:
                return path, self.load_ocal_file(path, self.info.uid_hex)
            except BackupMismatch:
                continue
        return None

    def list_ocal_backups(self, folder: Path) -> list:
        """The .ocal files of this meter, newest first."""
        out_dir = self.backup_dir(folder)
        if not out_dir.is_dir():
            return []
        return sorted(out_dir.glob("*.ocal"), key=lambda p: p.name, reverse=True)

    def _ocal_backup_before(self) -> Path:
        """The backup every OCAL write path makes first (OCD 6.1)."""
        if self.backup_folder is None:
            raise SafetyRefused("no backup folder is set: nothing is sent without a backup")
        return self.ocal_backup(self.backup_folder)

    @staticmethod
    def _describe_ocal_error(e: StatusError) -> None:
        """OCAL_WRITE status 2 with 3 data bytes is a validation failure (section id u8, entry
        index u16), status 5 carries the commit result (OCD 4.1)."""
        if e.status == 2 and len(e.data) == 3:
            sid, idx = struct.unpack("<BH", e.data)
            e.detail = "the meter refused the values: %s" % ocal_where(sid, idx)
        elif e.status == 5 and len(e.data) >= 6:
            e.record = OcalWriteResult(*struct.unpack_from("<BBI", e.data))

    def ocal_write(self, body: bytes, confirm: str, chunk: int = OCAL_CHUNK_MAX) -> OcalWriteResult:
        """OCAL_WRITE of a whole record body (OCD 1.2, 4.1): checked on the host with the rules
        of OCD 2.4 (OcalValidationError before anything is sent), the .ocal backup of both slots
        first, then chunks of `chunk` bytes with the CRC-32 of the whole body. Status 7 repeats
        the transfer from offset 0 (as SL 9.4). Returns the commit result, active slot and
        sequence; a commit result other than 0 raises DeviceError."""
        self._need_confirm(confirm)
        if not 1 <= chunk <= OCAL_CHUNK_MAX:
            raise ValueError("chunk 1..502 bytes")
        body = bytes(body)
        validate_body(body)
        self._ocal_backup_before()
        crc = crc32(body)
        for attempt in range(WRITE_BLOCK_RETRIES):
            try:
                return self._ocal_write_once(body, chunk, crc)
            except StatusError as e:
                if e.status != 7 or attempt == WRITE_BLOCK_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")        # the loop returns or raises

    def _ocal_write_once(self, body: bytes, chunk: int, crc: int) -> OcalWriteResult:
        total, off = len(body), 0
        with self._job():
            while True:
                part = body[off:off + chunk]
                head = struct.pack("<HHHI", CONFIRM_OK, off, total, crc)
                try:
                    data = self.call(OCAL_WRITE, head + part, allow_dangerous=True)
                except StatusError as e:
                    self._describe_ocal_error(e)
                    raise
                if off + len(part) < total:
                    _short("OCAL_WRITE", data, 2)
                    (got,) = struct.unpack_from("<H", data)
                    if got != off + len(part):
                        raise DeviceError("OCAL_WRITE: meter expects offset %d, sent %d"
                                          % (got, off + len(part)))
                    off = got
                    continue
                _short("OCAL_WRITE", data, 6)
                res = OcalWriteResult(*struct.unpack_from("<BBI", data))
                if not res.ok:
                    raise DeviceError("OCAL_WRITE: %s" % res.describe())
                return res

    def ocal_rollback(self, mode: int, confirm: str) -> OcalRollbackResult:
        """OCAL_ROLLBACK (OCD 1.5): mode 0 revokes the active record, mode 1 every own format-1
        record in force. Status 3 when no record qualifies. The .ocal backup first."""
        self._need_confirm(confirm)
        if mode not in (0, 1):
            raise ValueError("rollback mode 0 or 1")
        self._ocal_backup_before()
        with self._job():
            try:
                data = self.call(OCAL_ROLLBACK, struct.pack("<HB", CONFIRM_OK, mode),
                                 allow_dangerous=True)
            except StatusError as e:
                if e.status == 5 and len(e.data) >= 6:
                    e.record = OcalWriteResult(*struct.unpack_from("<BBI", e.data))
                raise
        _short("OCAL_ROLLBACK", data, 9)
        return OcalRollbackResult(*struct.unpack_from("<BII", data))

    def ocal_restore(self, path: Path, slot: int, confirm: str) -> OcalWriteResult:
        """Restores the record of one slot of a .ocal file of THIS meter (OCD 6.1): its body goes
        through OCAL_WRITE, so the meter gives it a new sequence and checks it (OCD 2.4). The
        UID in the file name and in the record header must be this meter's, the record must be
        CRC-valid and of format 1; the current slots are backed up first."""
        self._need_confirm(confirm)
        if slot not in (0, 1):
            raise ValueError("slot 0 or 1")
        data = self.load_ocal_file(path, self.info.uid_hex)
        rec = parse_ocal_file(data)[slot]
        if not rec.valid:
            raise BackupMismatch("slot %d of %s holds no valid record (%s)" % (slot, Path(path).name,
                                                                                rec.why))
        if rec.uid != self.info.uid:
            raise BackupMismatch("the record in slot %d carries UID %s, this meter is %s: refused"
                                 % (slot, rec.uid.hex().upper(), self.info.uid_hex))
        if rec.format != OCAL_FORMAT:
            raise BackupMismatch("the record in slot %d has format %d; only format %d is restored"
                                 % (slot, rec.format, OCAL_FORMAT))
        return self.ocal_write(rec.body, confirm)

    def ocal_active_sections(self) -> tuple:
        """(OcalInfo, {section id: bytes}) of the active record, read from the meter and checked
        (CRC-32, UID, format). Without an active record the dict is empty."""
        with self._job():
            info = self.ocal_info()
            if not info.active:
                return info, {}
            if info.active_slot not in (0, 1) or not OCAL_HEADER <= info.active_length <= OCAL_SLOT_SIZE:
                raise DeviceError("OCAL_INFO names slot %d, length %d" % (info.active_slot,
                                                                         info.active_length))
            raw = self.ocal_read_slot(info.active_slot, info.active_length)
        rec = parse_ocal_record(raw, info.active_slot)
        if not rec.valid or rec.uid != self.info.uid or rec.sequence != info.active_sequence:
            raise DeviceError("the active record read back does not match OCAL_INFO (%s)" % rec.why)
        return info, parse_body(rec.body)

    def ocal_update_sections(self, changes: dict, confirm: str,
                             note: str | None = None) -> OcalWriteResult:
        """Writes the active record with `changes` {section id: bytes, or None to drop it}
        applied; every other section is kept. With `note` META is renewed (host time, note)."""
        self._need_confirm(confirm)
        _info, sections = self.ocal_active_sections()
        for sid, data in changes.items():
            if data is None:
                sections.pop(sid, None)
            else:
                sections[sid] = bytes(data)
        if note is not None:
            sections[SEC_META] = pack_meta(int(self._now().timestamp()), note)
        return self.ocal_write(build_body(sections), confirm)

    def ocal_lock(self, c5: int, c6: int, c8: int) -> OcalRaw:
        """OCAL_LOCK (OCD 2.2): fixes the gain codes c5 (V, 0..7), c6 (I, 0..7) and the range
        code c8 (0..3); the answer is the raw record of the last window. RAM only."""
        if not (0 <= c5 <= 7 and 0 <= c6 <= 7 and 0 <= c8 <= 3):
            raise ValueError("lock codes: c5 0..7, c6 0..7, c8 0..3")
        return parse_ocal_raw(self.call(OCAL_LOCK, bytes([c5, c6, c8])))

    def ocal_release(self) -> OcalRaw:
        """OCAL_LOCK 0xFF 0xFF 0xFF: autoranging again."""
        return parse_ocal_raw(self.call(OCAL_LOCK, b"\xFF\xFF\xFF"))

    def ocal_raw(self) -> OcalRaw:
        return parse_ocal_raw(self.call(OCAL_RAW))

    def ocal_wait_window(self, after: int, count: int = 2, timeout: float = 30.0) -> OcalRaw:
        """Polls OCAL_RAW until the window number is at least after + count (the window closed
        after a change of codes, frequency or level is the second one: OCD 3.3 step 2) and returns
        that record. OCAL_RAW also keeps a lock alive (OCD 2.2: 30 s). DeviceError after
        `timeout` seconds."""
        deadline = self._clock() + timeout
        while True:
            raw = self.ocal_raw()
            if raw.window >= after + count:
                return raw
            if self._clock() > deadline:
                raise DeviceError("no window closed within %.0f s (window %d, wanted %d)"
                                  % (timeout, raw.window, after + count))
            self._sleep(0.1)

    def ocal_trace_arm(self) -> OcalTrace:
        return parse_ocal_trace(self.call(OCAL_TRACE, bytes([TRACE_ARM, 0])))

    def ocal_trace_disarm(self) -> OcalTrace:
        return parse_ocal_trace(self.call(OCAL_TRACE, bytes([TRACE_DISARM, 0])))

    def ocal_trace_read(self, start: int = 0) -> OcalTrace:
        """One read (OCD 4.1 0x36): entries from `start` (0..31), at most 31 per answer."""
        if not 0 <= start <= 31:
            raise ValueError("trace start 0..31")
        return parse_ocal_trace(self.call(OCAL_TRACE, bytes([TRACE_READ, start])))

    def ocal_trace_read_all(self) -> OcalTrace:
        """The whole trace: the read at start 0, and a second at start 31 when the trace holds
        more than 31 entries."""
        first = self.ocal_trace_read(0)
        if first.count > len(first.entries) and len(first.entries) == 31:
            second = self.ocal_trace_read(31)
            first.entries = first.entries + second.entries
        return first

    def ocal_wait_trace(self, timeout: float = 30.0) -> OcalTrace:
        """Polls OCAL_TRACE until the trace is full (state 3) and returns all its entries;
        DeviceError after `timeout` seconds (nothing changed while it was armed, or the change
        left the meter on a screen other than 1)."""
        deadline = self._clock() + timeout
        while True:
            if self.ocal_trace_read(0).state == 3:
                return self.ocal_trace_read_all()
            if self._clock() > deadline:
                raise DeviceError("the trace did not fill within %.0f s" % timeout)
            self._sleep(0.1)

    def monitor(self) -> Monitor:
        return parse_monitor(self.call(MONITOR))

    def _after_restart(self) -> None:
        """After a restart the old link is dead: with a reconnect function the new link is
        taken over and the meter identified again (UID checked, DESIGN 13.13)."""
        self._live = False
        if self._reconnect is None:
            return
        self._swap_link(self._reconnect_link(60.0))

    def _reconnect_link(self, timeout: float):
        if self._reconnect is None:
            raise DeviceError("no reconnect function was given")
        try:
            self.link.close()
        except Exception:                       # the port is already gone
            pass
        return self._reconnect(self.info.uid_hex, timeout)

    def _swap_link(self, link) -> Info:
        """Takes the new link over after INFO and the UID check; when either fails, the new link
        is closed before the error goes on (its port is not left open)."""
        try:
            info = identify(link, MODEL)
            if info.uid != self.info.uid:
                raise BackupMismatch("after the restart the meter on the port has another UID")
        except BaseException:
            try:
                link.close()
            except Exception:                   # the port may be gone already
                pass
            raise
        self._detach(self.link)
        self.link, self.info = link, info
        self._attach(link)
        return info

    # -- 5.5 screenshot and diagnostics -------------------------------------------------------
    def _capture(self, band: int, pixels: array, have: bytearray, frames: "queue.Queue",
                 inactivity: float = 3.5) -> dict[int, int]:
        """One SCREENSHOT command (band 0..23 or 0xFF; SL 12): pixel frames into pixels / have;
        returns {band: end result} of the end frames received."""
        head = parse_screenshot_header(self.call(SCREENSHOT, bytes([band])))
        if (head.width, head.height, head.format) != (SCR_W, SCR_H, 0):
            raise DeviceError("screenshot format %dx%d / %d not known"
                              % (head.width, head.height, head.format))
        expected = set(range(SCR_BANDS)) if band == 0xFF else {band}
        if len(expected) != head.bands:
            raise DeviceError("screenshot: %d bands announced, %d expected"
                              % (head.bands, len(expected)))
        results: dict[int, int] = {}
        last = self._clock()
        while set(results) != expected:
            try:
                f = frames.get(timeout=0.05)
            except queue.Empty:
                if self._clock() - last > inactivity:
                    break               # the capture stopped without its end frame
                continue
            last = self._clock()
            p = f.payload
            if len(p) < 11:
                continue
            b, off, x1, y1, x2, y2 = struct.unpack_from("<BHhhhh", p)
            if off == 0xFFFF:
                results[b] = p[11] if len(p) > 11 else 0xFF
                continue
            pix = array("H")
            pix.frombytes(bytes(p[11:11 + (len(p) - 11) // 2 * 2]))
            if sys.byteorder != "little":
                pix.byteswap()
            wd = x2 - x1 + 1
            if wd <= 0:
                continue
            for k, v in enumerate(pix):
                i = off + k
                x, y = x1 + i % wd, y1 + i // wd
                if 0 <= x < SCR_W and 0 <= y < SCR_H and y <= y2:
                    pixels[y * SCR_W + x] = v
                    have[y * SCR_W + x] = 1
        return results

    def screenshot_pixels(self, progress: Progress | None = None, retries: int = 3) -> array:
        """The screen as 320 x 240 RGB565 values (array 'H', row-major). Procedure of SL 12:
        SCREENSHOT 0xFF, then every band whose result was not 0, whose end frame is missing or
        whose rows are incomplete is requested again (`retries` rounds). Holds the port;
        cancellable between the SCREENSHOT requests."""
        pixels = array("H", [0]) * (SCR_W * SCR_H)
        have = bytearray(SCR_W * SCR_H)
        frames: queue.Queue = queue.Queue()

        def collect(f):
            if f.cmd == SCREENSHOT:
                frames.put(f)

        def say(frac, text):
            if progress is not None:
                progress(frac, text)

        self.link.stream.connect(collect, Qt.DirectConnection)
        try:
            with self._job(), self._cancellable():
                say(0.0, "screenshot")
                results = self._capture(0xFF, pixels, have, frames)

                def band_ok(b):
                    rows = have[b * SCR_ROWS * SCR_W:(b + 1) * SCR_ROWS * SCR_W]
                    return results.get(b) == 0 and all(rows)

                for rnd in range(retries):
                    todo = [b for b in range(SCR_BANDS) if not band_ok(b)]
                    if not todo:
                        break
                    for i, b in enumerate(todo):
                        self._cancel_point()
                        say(0.5 + 0.5 * (rnd + i / len(todo)) / retries, "band %d again" % b)
                        results.update(self._capture(b, pixels, have, frames))
                bad = [b for b in range(SCR_BANDS) if not band_ok(b)]
                if bad:
                    raise DeviceError("screenshot: bands %s incomplete (results %s)" % (
                        bad, {b: SCR_RESULT.get(results.get(b), results.get(b)) for b in bad}))
                say(1.0, "screenshot done")
        finally:
            try:
                self.link.stream.disconnect(collect)
            except (RuntimeError, TypeError):
                pass
        return pixels

    def screenshot(self, progress: Progress | None = None):
        """The screen as a QImage (RGB565 decoded as SL 12: bits 15..11 red, 10..5 green,
        4..0 blue)."""
        from PySide6.QtGui import QImage
        pixels = self.screenshot_pixels(progress)
        rgb = rgb565_to_rgb(pixels)
        img = QImage(bytes(rgb), SCR_W, SCR_H, SCR_W * 3, QImage.Format_RGB888)
        return img.copy()                      # own the bytes

    def mem_read(self, address: int, length: int) -> bytes:
        """SL 5.5: length 1..256 inside the ranges the meter allows (else status 2)."""
        return self.call(MEM_READ, struct.pack("<IH", address, length))

    # -- 5.6 updates --------------------------------------------------------------------------
    def update_mode(self, confirm: str) -> UpdateMode:
        """UPDATE_MODE: the meter restarts into the slot bootloader's update mode (SL 5.6).
        Status 6 (nothing done) raises StatusError. update_app() is the normal way."""
        self._need_confirm(confirm)
        try:
            data = self.call(UPDATE_MODE, allow_dangerous=True)
        except StatusError as e:
            e.record = parse_update_mode(e.status, e.data) if len(e.data) >= 6 else None
            raise
        self._live = False
        return parse_update_mode(0, data)

    def boot_status(self) -> BootStatus:
        return parse_boot_status(self.call(BOOT_STATUS))

    def boot_begin(self, slot: int, length: int, crc: int, confirm: str) -> None:
        self._need_confirm(confirm)
        self.call(BOOT_BEGIN, struct.pack("<BII", slot, length, crc), allow_dangerous=True)

    def boot_data(self, offset: int, chunk: bytes, confirm: str) -> None:
        """BOOT_DATA; status 5 carries the flash result and the file offset of the failing word
        (SL 5.6, 9.1): attached as `.detail` of the StatusError."""
        self._need_confirm(confirm)
        try:
            self.call(BOOT_DATA, struct.pack("<I", offset) + bytes(chunk), allow_dangerous=True)
        except StatusError as e:
            if e.status == 5 and len(e.data) >= 5:
                res, at = struct.unpack_from("<BI", e.data)
                e.detail = "flash %s at file offset 0x%X" % (FLASH_RESULT.get(res, res), at)
            raise

    def boot_end(self, confirm: str) -> int:
        """The slot header's version (SL 5.6)."""
        self._need_confirm(confirm)
        data = self.call(BOOT_END, allow_dangerous=True)
        _short("BOOT_END", data, 4)
        return struct.unpack_from("<I", data)[0]

    def boot_install(self, confirm: str) -> None:
        self._need_confirm(confirm)
        self.call(BOOT_INSTALL, allow_dangerous=True)
        self._live = False

    # -- the update flows (SL 13) -------------------------------------------------------------
    def update_app(self, path: Path, drive_finder: Callable[[], list], progress: Progress | None = None,
                   *, confirm: str, drive_timeout: float = 60.0, port_timeout: float = 120.0) -> Info:
        """Application update (SL 13.1, DESIGN 9, 13.3): the .uf2 is checked first; both settings
        copies are backed up (the backup of the saves, DESIGN 13.8; refused without a backup
        folder); UPDATE_MODE; the update drive is waited for (drive_finder() -> list of objects
        with .model, .path and .status, i.e. link.discover.list_update_drives; only an LC1020E
        drive that was not there before counts and that holds Ready.TXT; a drive listed as
        "unmounted" is reported with its device node and the mount command and waited for until
        it is mounted, DESIGN 13.29); the file is written as a plain file and flushed; the port is
        waited for (reconnect) and INFO must report last update result 1. Returns the new INFO.

        A write error while the drive still lists Ready.TXT raises UpdateCopyFailed at once (the
        meter stays in update mode). A write error after which the drive no longer answers is the
        bootloader's reset after the last block (SL 13.1 step 4): INFO then decides.
        Cancellable during the backup and while the drive is waited for (the meter then stays in
        update mode); not during the copy and not after it."""
        self._need_confirm(confirm)
        say = progress or (lambda frac, text: None)
        with open(path, "rb") as f:
            data = f.read()
        blocks = check_uf2(data)
        if self._reconnect is None:
            raise DeviceError("no reconnect function was given: the update cannot be followed")
        before_info = self.read_info()
        if before_info.uid != self.info.uid:
            raise BackupMismatch("the meter on the link is not the identified one")
        saved = self._backup_before()
        say(0.03, "settings copies A and B backed up: %s" % saved)
        known = {d.path for d in drive_finder() if d.model == MODEL}
        say(0.05, "running %s, slot %s (version %d)" % (
            before_info.firmware, SLOT_NAME.get(before_info.running_slot, before_info.running_slot),
            before_info.slot_version))
        with self._job() as job:
            um = self.update_mode(confirm)
            job.resume = False
        say(0.15, "update mode: slot %s (version %d) starts next"
            % (SLOT_NAME.get(um.next_slot, um.next_slot), um.next_version))
        try:
            self.link.close()
        except Exception:
            pass
        drive = self._wait_drive(drive_finder, known, drive_timeout, say)
        target = os.path.join(drive, DRIVE_FILE)
        say(0.3, "copying %d UF2 blocks to %s" % (blocks, target))
        try:
            self._write_plain(target, data)
        except OSError as e:
            if self._is_update_drive(drive):
                raise UpdateCopyFailed(drive, e) from e
            # the bootloader resets about 1 s after the last block (SL 13.1 step 4): INFO decides
            say(0.6, "note: %s (the drive no longer answers: the bootloader restarted after the "
                "last block)" % e)
        say(0.7, "waiting for the meter")
        new_link = self._reconnect(self.info.uid_hex, port_timeout)
        after = self._swap_link(new_link)
        if after.update_result != 1:
            raise UpdateFailed(after.update_result)
        say(1.0, "updated: %s, application %d bytes" % (after.firmware, after.app_size))
        return after

    def _wait_drive(self, drive_finder, known: set, timeout: float, say=None) -> str:
        """The path of the new update drive. While one is listed but not mounted (Linux without
        automount) its node and the mount command are reported and the time limit does not run
        out. Cancellable: the meter then stays in update mode."""
        say = say or (lambda frac, text: None)
        deadline = self._clock() + timeout
        shown = None
        try:
            with self._cancellable():
                while True:
                    self._cancel_point()
                    unmounted = None
                    for d in drive_finder():
                        if d.model != MODEL or d.path in known:
                            continue
                        if getattr(d, "status", None) == "unmounted":
                            unmounted = d.path
                        elif self._is_update_drive(d.path):
                            return d.path
                    if unmounted is not None:
                        if unmounted != shown:
                            say(0.2, "the update drive %s is not mounted: mount it with  %s  "
                                "(the copy starts when it is mounted)"
                                % (unmounted, MOUNT_COMMAND % unmounted))
                            shown = unmounted
                    elif self._clock() > deadline:
                        raise DeviceError("update drive did not appear within %.0f s: the meter "
                                          "stays in update mode (SL 13.1 step 4)" % timeout)
                    self._sleep(0.5)
        except Cancelled as e:
            raise Cancelled("cancelled while waiting for the update drive: the meter stays in "
                            "update mode (SL 13.1 step 4); copy the file onto its drive on the "
                            "Update drive page, or start the update again") from e

    @staticmethod
    def _is_update_drive(path: str) -> bool:
        """The volume's root holds Ready.TXT (SL 13.1 step 3)."""
        try:
            return os.path.isdir(path) and any(n.lower() == DRIVE_READY_FILE
                                               for n in os.listdir(path))
        except OSError:
            return False

    @staticmethod
    def _write_plain(target: str, data: bytes) -> None:
        """A plain open / write / flush: no extended attributes, so macOS writes no "._" companion
        (SL 13.1 step 3); on macOS the flush goes through the drive cache (F_FULLFSYNC)."""
        with open(target, "wb") as g:
            g.write(data)
            g.flush()
            try:
                import fcntl
                fcntl.fcntl(g.fileno(), fcntl.F_FULLFSYNC)
            except (ImportError, AttributeError, OSError):
                os.fsync(g.fileno())

    def boot_update(self, slot_path: Path, progress: Progress | None = None, *, confirm: str,
                    known_sha256: set[str] | None = None, confirm_foreign: str | None = None,
                    port_timeout: float = 60.0) -> tuple[BootStatus, Info]:
        """Bootloader slot update through BOOT_* (SL 13.2): the free slot that BOOT_STATUS names,
        only with a slot file linked for it (checked before anything is written). When
        known_sha256 is given and the file's SHA-256 is not in it, confirm_foreign "OK" is needed
        too (DESIGN 13.26). Both settings copies are backed up before BOOT_BEGIN (the backup of the
        saves; refused without a backup folder). Returns (BootStatus, Info) after the meter
        restarted. Cancellable during the backup only; BOOT_BEGIN .. BOOT_INSTALL run to their
        end."""
        self._need_confirm(confirm)
        say = progress or (lambda frac, text: None)
        with open(slot_path, "rb") as f:
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        if known_sha256 is not None and sha not in {h.lower() for h in known_sha256}:
            if confirm_foreign != CONFIRM_WORD:
                raise SafetyRefused('slot file %s is not in any SHA256SUMS of the releases: '
                                    'typed confirmation "%s" required' % (sha, CONFIRM_WORD))
        if self._reconnect is None:
            raise DeviceError("no reconnect function was given: the update cannot be followed")
        st = self.boot_status()
        free = st.free_slot
        if free not in (1, 2):
            raise DeviceError("BOOT_STATUS names no free slot (%d)" % free)
        version = check_slot_file(data, SLOT_BASE[free])
        crc = crc32(data)
        saved = self._backup_before()
        say(0.0, "settings copies A and B backed up: %s; free slot %s at 0x%08X: %d bytes, "
            "version %d, CRC-32 0x%08X" % (saved, SLOT_NAME[free], SLOT_BASE[free], len(data),
                                            version, crc))
        with self._job() as job:
            self.boot_begin(free, len(data), crc, confirm)
            for off in range(0, len(data), 256):
                self.boot_data(off, data[off:off + 256], confirm)
                say(0.1 + 0.8 * min(off + 256, len(data)) / len(data), "writing slot")
            got = self.boot_end(confirm)
            if got != version:
                raise DeviceError("BOOT_END reports version %d, file has %d" % (got, version))
            say(0.9, "slot %s written and checked; installing" % SLOT_NAME[free])
            job.resume = False
            self.boot_install(confirm)
        new_link = self._reconnect_link(port_timeout)
        self._swap_link(new_link)
        after = self.boot_status()
        info = self.read_info()
        started = info.running_slot == free or (
            info.running_slot == 0xFF and after.newest_slot == free and after.newest_state == 3)
        if not started:
            raise DeviceError("after the install slot %s runs (newest record: slot %s %s)" % (
                SLOT_NAME.get(info.running_slot, info.running_slot),
                SLOT_NAME.get(after.newest_slot, after.newest_slot),
                RECORD_STATE.get(after.newest_state, after.newest_state)))
        say(1.0, "slot %s runs; newest record %s" % (SLOT_NAME[free],
                                                     RECORD_STATE.get(after.newest_state, "?")))
        return after, info


def rgb565_to_rgb(pixels) -> bytearray:
    """RGB565 values -> RGB888 bytes (SL 12: bits 15..11 red, 10..5 green, 4..0 blue)."""
    out = bytearray(len(pixels) * 3)
    for i, v in enumerate(pixels):
        r, g, b = v >> 11 & 0x1F, v >> 5 & 0x3F, v & 0x1F
        out[3 * i] = r << 3 | r >> 2
        out[3 * i + 1] = g << 2 | g >> 4
        out[3 * i + 2] = b << 3 | b >> 2
    return out
