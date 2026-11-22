"""FNIRSI LC1020E device module: every command of the control protocol as a method.

Source of every number: PROTOCOL.md of fnirsi-lc1020e-firmware-rewrite ("SL"), cited by section in
the comments; the records, parsers, file checks and the update flows are the owner's host library
tools/lc1020e.py ("LL") ported into this structure. The parse_* functions and the check_* functions
are pure (testable without a link). The class LC1020E(Device) works on the SerialLink of API.md.

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
SETTINGS_TABLES, SETTINGS_SET, SETTINGS_SAVE, CAL_READ = 0x50, 0x51, 0x52, 0x53
RESTORE_DEFAULTS, SETTINGS_RESTORE_B, SETTINGS_ACCEPT_A = 0x54, 0x55, 0x56
SETTINGS_WRITE_BLOCK, SETUP_LANGUAGE, SETTINGS_ACCEPT_B, SETTINGS_ACCEPT_DEFAULTS = \
    0x57, 0x58, 0x59, 0x5A
SCREENSHOT, MEM_READ = 0x60, 0xF0
UPDATE_MODE, BOOT_BEGIN, BOOT_DATA, BOOT_END, BOOT_INSTALL, BOOT_STATUS = \
    0x84, 0x85, 0x86, 0x87, 0x88, 0x89

# Events (SL 10)
EV_KEY, EV_SCREEN_CHANGED, EV_SETTING_CHANGED, EV_POWERED_OFF = 0x90, 0x91, 0x93, 0x94
EV_MEASURE_DONE, EV_SAVE_RESULT, EV_CALIBRATION = 0x95, 0x96, 0x97
EVENT_NAME = {EV_KEY: "KEY", EV_SCREEN_CHANGED: "SCREEN_CHANGED",
              EV_SETTING_CHANGED: "SETTING_CHANGED", EV_POWERED_OFF: "POWERED_OFF",
              EV_MEASURE_DONE: "MEASURE_DONE", EV_SAVE_RESULT: "SAVE_RESULT",
              EV_CALIBRATION: "CALIBRATION"}

# Answer time limits in seconds (SL 4.3: the column "tools/lc1020e.py waits")
T_FAST = 1.5
T_SAVE = 4.0
T_JOB = 23.0
T_COMPARE_SET = 63.0
T_DIRECT = 4.0                   # SL 4.3 / 14: the five set commands when CAP_DIRECT_SET is set
T_CALIBRATE = 126.0
T_BOOT_BEGIN = 13.0
T_BOOT_INSTALL = 4.0

# INFO capability bits (SL 14)
CAP_STATE, CAP_SCREEN, CAP_KEY, CAP_LCR, CAP_MEASURE, CAP_COMPARE, CAP_HOLD, CAP_CALIBRATE = range(8)
CAP_SETTINGS, CAP_CAL_READ, CAP_SCREENSHOT, CAP_MEM_READ, CAP_EVENTS, CAP_UPDATE_MODE = 8, 9, 10, 11, 12, 13
CAP_BOOT, CAP_BATTERY, CAP_POWER_OFF, CAP_RESTORE_DEFAULTS, CAP_RECOVERY, CAP_FACTORY_MODE = \
    14, 15, 16, 17, 18, 19
CAP_SETUP_LANGUAGE = 20          # clear in this firmware: English only (SL 5.6)
CAP_DIRECT_SET = 21              # SL 4.3 / 14: the five set commands set the value directly, no key job

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
               5: "program failed", 6: "verify failed", 7: "bad block", 8: "source copy invalid"}
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
    """MEASURE record, 64 bytes (SL 6.2)."""
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
    SIZE = 64


def parse_measure(d: bytes) -> Measure:
    _short("MEASURE", d, Measure.SIZE)
    res, t, mrow = struct.unpack_from("<IIB", d, 0)
    main = parse_value_block(d, 9)
    skind = d[24]
    sec = parse_value_block(d, 25)
    par, z, th, fq, lv, bs, rg, sp, ci = struct.unpack_from("<BffIffBBB", d, 40)
    return Measure(res, t, mrow, main, skind, sec, par, z, th, fq, lv, bs, rg, sp, ci,
                   bytes(d[:Measure.SIZE]))


@dataclass
class Compare:
    """COMPARE record, 29 bytes (SL 6.3)."""
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
    SIZE = 29


def parse_compare(d: bytes) -> Compare:
    _short("COMPARE", d, Compare.SIZE)
    return Compare(*struct.unpack_from("<BfffBIIfBBB", d), raw=bytes(d[:Compare.SIZE]))


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


@dataclass
class CalResult:
    """CALIBRATE answer, 6 bytes (SL 5.3)."""
    status: int          # answer status
    cal_status: int      # 0 start, 1 busy, 2 finish, 3 fail
    cal_type: int
    save_status: int


def parse_cal_answer(status: int, d: bytes) -> CalResult:
    _short("CALIBRATE", d, 6)
    st, ty, sv = struct.unpack_from("<BBI", d)
    return CalResult(status, st, ty, sv)


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

    def _on_stream(self, frame) -> None:
        try:
            if frame.cmd == MEASURE:
                self.reading.emit(measure_to_reading(parse_measure(frame.payload), frame.t,
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
        return self._rec(LCR_SET, bytes([setting, index]), parse_measure)

    def measure(self) -> Measure:
        return parse_measure(self.call(MEASURE))

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
