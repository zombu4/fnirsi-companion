"""FNIRSI 2C53T device module: every command of the control protocol as a method.

Source of every number: PROTOCOL.md of fnirsi-2c53t-firmware-rewrite ("S2", section numbers cited
in the comments) and the owner's host tool tools/fnirsi_ctl.py ("CTL", whose records, chunk loops
and time limits are ported here). The file checks of the firmware update are those of the
firmware: FW_BEGIN in src/usb_ctl.s (cmd_fw_begin) and the slot bootloader's staged-update check
in bootloader/src/slot_11.s (slot_staged_update), constants from common/layout.inc. The parse_*
and check_* functions are pure (testable without a link); class C2C53T(Device) works on the
SerialLink of API.md (or a test double with the same calls).

Safety (DESIGN.md 9, 13.1, 13.2, 13.7, 13.25, 13.26): methods that save, power off, restart into
an update or send to the meter chip take `confirm` and refuse unless it is "OK", and read the
settings / calibration page (CAL_READ) into a backup file first; FILE_WRITE / FILE_DELETE never
touch the start-up files or the staged image, and on drive 3: only the user-interface pictures,
which are read back and kept first. The generic Device.call refuses dangerous commands.

Time limits per command are those of CTL (default 0.5 s, DMM 2 s / 3 s, timebase 10 s, scope
channel and trigger 30 s, screenshot 20 s, files 3 s); the firmware update commands use the files
value, because they write the staged file (DESIGN 4).
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
                   Info, PortLost, Reading, SafetyRefused, StatusError, identify, payload_bytes)

MODEL = "2C53T"
USB_VID, USB_PID = 0x2E3C, 0x2C53          # S2 1: the meter's USB device (also while the drive is shared)

# ----------------------------------------------------------------------------------------------
# Command codes (S2 4.1 .. 4.13) and event codes (S2 4.12)
# ----------------------------------------------------------------------------------------------

INFO, PING, RESET, BATTERY, KEEP_AWAKE = 0x00, 0x01, 0x02, 0x03, 0x04
STATE, STREAM, PAGE, KEY = 0x10, 0x11, 0x12, 0x13
DMM_FUNCTION, DMM_HOLD, DMM_RELATIVE = 0x20, 0x21, 0x22
SCOPE_TIMEBASE, SCOPE_CHANNEL, SCOPE_TRIGGER, SCOPE_TABLES = 0x30, 0x31, 0x32, 0x33
SCOPE_RUN, SCOPE_AUTOSET, SCOPE_WAVE, SCOPE_AUTOCORRECT = 0x34, 0x35, 0x36, 0x37
GEN_SET = 0x40
SETTINGS_GET, SETTINGS_SET, SETTINGS_SAVE, CAL_READ = 0x50, 0x51, 0x52, 0x53
SCREENSHOT = 0x60
FILE_LIST, FILE_READ, FILE_WRITE, FILE_DELETE, DRIVE = 0x70, 0x71, 0x72, 0x73, 0x74
FW_BEGIN, FW_DATA, FW_END, FW_INSTALL = 0x80, 0x81, 0x82, 0x83
MEM_READ, FLASH_READ, METER_LOG, FLASH_ID, METER_SEND, TEXT_COLOUR = 0xF0, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6

EV_KEY_PRESSED, EV_PAGE_CHANGED, EV_BATTERY_LOW = 0x90, 0x91, 0x92
EVENT_NAME = {EV_KEY_PRESSED: "KEY_PRESSED", EV_PAGE_CHANGED: "PAGE_CHANGED",
              EV_BATTERY_LOW: "BATTERY_LOW"}

# Capability bits of INFO (S2 4.1, "Capability bits")
CAP_STATE, CAP_STREAM, CAP_PAGE, CAP_KEY, CAP_GEN_SET, CAP_CAL_READ = 0, 1, 2, 3, 4, 5
CAP_DRIVE, CAP_FIRMWARE_UPDATE, CAP_BATTERY, CAP_KEEP_AWAKE, CAP_MEM_READ = 6, 7, 8, 9, 10
CAP_DMM, CAP_SCOPE_SETTINGS, CAP_SCOPE_WAVE, CAP_SETTINGS, CAP_SCREENSHOT = 11, 12, 13, 14, 15
CAP_FILES, CAP_EVENTS, CAP_FLASH_READ, CAP_METER_LOG, CAP_FLASH_ID = 16, 17, 18, 19, 20
CAP_METER_SEND, CAP_TEXT_COLOUR = 21, 22

# Time limits in seconds (CTL values, DESIGN 4)
# T_FAST is S2 2 "Most answers come within 500 ms". It holds for PAGE, KEY and GEN_SET too: they
# are not the key-stepping commands of S2 4.0 (those are the jobs of src/usb_remote.s: DMM_*,
# SCOPE_*), as the firmware source shows (src/usb_ctl.s of fnirsi-2c53t-firmware-rewrite):
# - KEY (cmd_key, usb_ctl.s:1307): one key into the key queue with wait 0 (post_key, :1296-1301;
#   a full queue answers status 4 at once), then a sleep of 20 ticks (:1318) before the STATE
#   record is built and answered; a tick is 1 ms (usb_ctl.s:438).
# - PAGE (cmd_page, :1338): the page's key (22, 17, 18 or 19) through the same cmd_key path.
# - GEN_SET (cmd_gen_set, :1369): the generator words are written directly, no key is pressed;
#   only with the generator page open, four redraw codes go to the display queue with an
#   unlimited wait (post_display, :1431). No measured answer time of that case exists, so the
#   limit stays the stated 500 ms; a late answer is a time-out followed by the PING resync of
#   the link (DESIGN 13.12); the meter may have applied the setting, which STATE shows.
T_FAST = 0.5
T_DMM = 2.0
T_DMM_FUNCTION = 3.0
T_TIMEBASE = 10.0
T_SCOPE_SET = 30.0
T_SCREENSHOT = 20.0
T_FILES = 3.0

# Status codes (S2 3)
STATUS_NAME = {0: "OK", 1: "unknown command", 2: "bad length or argument",
               3: "not allowed in the current state", 4: "busy", 5: "file system error",
               6: "refused for safety", 7: "CRC or length mismatch"}


def _cmd(code, name, cap, timeout=T_FAST, dangerous=False):
    return Command(code, name, cap, timeout, dangerous)


# One entry per command of the S2 tables 4.1 .. 4.11 and 4.13 (the events of 4.12 are not
# commands). The capability bit of each is the one S2 4.1 lists for its group. KEY is not
# dangerous as a command: key code 1 (power off, saves) is refused in C2C53T.call and key().
COMMANDS: dict[int, Command] = {c.code: c for c in [
    _cmd(INFO, "INFO", None),
    _cmd(PING, "PING", None),
    _cmd(RESET, "RESET", None),
    _cmd(BATTERY, "BATTERY", CAP_BATTERY),
    _cmd(KEEP_AWAKE, "KEEP_AWAKE", CAP_KEEP_AWAKE),
    _cmd(STATE, "STATE", CAP_STATE),
    _cmd(STREAM, "STREAM", CAP_STREAM),
    _cmd(PAGE, "PAGE", CAP_PAGE, T_FAST),                 # one key + 20 ms (T_FAST comment)
    _cmd(KEY, "KEY", CAP_KEY, T_FAST),                    # one key + 20 ms (T_FAST comment)
    _cmd(DMM_FUNCTION, "DMM_FUNCTION", CAP_DMM, T_DMM_FUNCTION),
    _cmd(DMM_HOLD, "DMM_HOLD", CAP_DMM, T_DMM),
    _cmd(DMM_RELATIVE, "DMM_RELATIVE", CAP_DMM, T_DMM),
    _cmd(SCOPE_TIMEBASE, "SCOPE_TIMEBASE", CAP_SCOPE_SETTINGS, T_TIMEBASE),
    _cmd(SCOPE_CHANNEL, "SCOPE_CHANNEL", CAP_SCOPE_SETTINGS, T_SCOPE_SET),
    _cmd(SCOPE_TRIGGER, "SCOPE_TRIGGER", CAP_SCOPE_SETTINGS, T_SCOPE_SET),
    _cmd(SCOPE_TABLES, "SCOPE_TABLES", CAP_SCOPE_SETTINGS),
    _cmd(SCOPE_RUN, "SCOPE_RUN", CAP_SCOPE_SETTINGS, T_DMM),
    _cmd(SCOPE_AUTOSET, "SCOPE_AUTOSET", CAP_SCOPE_SETTINGS, T_DMM),
    _cmd(SCOPE_WAVE, "SCOPE_WAVE", CAP_SCOPE_WAVE),
    _cmd(SCOPE_AUTOCORRECT, "SCOPE_AUTOCORRECT", CAP_SCOPE_SETTINGS, T_DMM),
    _cmd(GEN_SET, "GEN_SET", CAP_GEN_SET, T_FAST),        # no key stepping (T_FAST comment)
    _cmd(SETTINGS_GET, "SETTINGS_GET", CAP_SETTINGS),
    _cmd(SETTINGS_SET, "SETTINGS_SET", CAP_SETTINGS),
    _cmd(SETTINGS_SAVE, "SETTINGS_SAVE", CAP_SETTINGS, dangerous=True),
    _cmd(CAL_READ, "CAL_READ", CAP_CAL_READ),
    _cmd(SCREENSHOT, "SCREENSHOT", CAP_SCREENSHOT, T_SCREENSHOT),
    _cmd(FILE_LIST, "FILE_LIST", CAP_FILES, T_FILES),
    _cmd(FILE_READ, "FILE_READ", CAP_FILES, T_FILES),
    _cmd(FILE_WRITE, "FILE_WRITE", CAP_FILES, T_FILES, dangerous=True),
    _cmd(FILE_DELETE, "FILE_DELETE", CAP_FILES, T_FILES, dangerous=True),
    _cmd(DRIVE, "DRIVE", CAP_DRIVE),
    _cmd(FW_BEGIN, "FW_BEGIN", CAP_FIRMWARE_UPDATE, T_FILES, dangerous=True),
    _cmd(FW_DATA, "FW_DATA", CAP_FIRMWARE_UPDATE, T_FILES, dangerous=True),
    _cmd(FW_END, "FW_END", CAP_FIRMWARE_UPDATE, T_FILES, dangerous=True),
    _cmd(FW_INSTALL, "FW_INSTALL", CAP_FIRMWARE_UPDATE, T_FILES, dangerous=True),
    _cmd(MEM_READ, "MEM_READ", CAP_MEM_READ),
    _cmd(FLASH_READ, "FLASH_READ", CAP_FLASH_READ),
    _cmd(METER_LOG, "METER_LOG", CAP_METER_LOG),
    _cmd(FLASH_ID, "FLASH_ID", CAP_FLASH_ID),
    _cmd(METER_SEND, "METER_SEND", CAP_METER_SEND, dangerous=True),
    _cmd(TEXT_COLOUR, "TEXT_COLOUR", CAP_TEXT_COLOUR),
]}

# ----------------------------------------------------------------------------------------------
# Tables of the record fields
# ----------------------------------------------------------------------------------------------

STATE_MIN = 44                     # S2 4.6: firmware 1.3.3 .. 1.4.17
STATE_FULL = 49                    # S2 4.6: since then; fields only ever added at the end
PAGE_NAME = {0: "menu", 1: "meter", 2: "scope", 3: "generator", 4: "settings",
             7: "drive shared", 9: "scope menu"}                                  # S2 4.6 offset 0
FLAG_NAMES = ["hold", "relative", "scope running", "generator output on", "drive shared",
              "external power", "battery low", "DTR set by the host"]             # S2 4.6 offset 1
DMM_FUNCTION_NAME = ["auto", "V DC", "V AC", "continuity", "diode", "R", "C", "T °C", "T °F",
                     "I DC small", "I AC small", "I DC large", "I AC large"]      # S2 4.3
UNIT_SYMBOL = {0: "", 1: "V", 2: "A", 3: "Ω", 4: "F", 5: "°C", 6: "°F"}           # S2 4.6 offset 9
DMM_STATUS_TEXT = {0: "", 1: "AUTO", 2: ".OL", 3: "", 4: "CAL", 5: "PAS", 6: "Er1", 7: "Er2",
                   8: "Er3", 9: "ErC"}                                            # S2 4.6 offset 44
DMM_UNIT_TEXT = ["mV", "V", "Ω", "kΩ", "MΩ", "nF", "uF", "mF", "F", "°C", "uA", "mA", "A",
                 "°F"]                                                            # S2 4.6 offset 47
PREFIX_SYMBOL = {-3: "n", -2: "u", -1: "m", 0: "", 1: "k", 2: "M"}                # S2 4.6 offset 46
TRIGGER_MODE = {0: "normal", 1: "auto", 2: "single"}                              # S2 4.4
SLOT_NAME = {0: "F", 1: "A", 2: "B", 0xFF: "unknown"}                             # S2 4.1 INFO
UPDATE_RESULT = {0: "none", 1: "OK", 2: "no file", 3: "image does not match",
                 4: "programmed image does not compare"}                          # S2 4.1 INFO

# Generator, scope and settings argument ranges (S2 4.4, 4.5, 4.8)
GEN_WAVEFORMS = 13
SCOPE_TIMEBASES = 29
KEY_MIN, KEY_MAX, KEY_POWER_OFF = 1, 23, 1                                        # S2 4.7

# Settings / calibration page (S2 4.8 CAL_READ; DESIGN 13.1)
CAL_BASE = 0x08006000
CAL_SIZE = 0x800
CAL_CHUNK = 256

# Screen (S2 4.9)
SCR_W, SCR_H = 320, 240
SCR_BYTES = SCR_W * SCR_H * 2
SCR_FRAME_DATA = 320

# MEM_READ ranges (S2 4.13)
MEM_RANGES = [(0x08000000, 0x08100000), (0x20037FB0, 0x20038000)]
FAULT_ADDRESS = 0x20037FB0
FAULT_SIZE = 48
FAULT_MAGIC = 0xFA017EC0
FLASH_READ_LIMIT = 16 * 1024 * 1024

# Application image (common/layout.inc; slot_11.s slot_staged_update; usb_ctl.s cmd_fw_begin)
RAM_LO, RAM_HI = 0x20000000, 0x20038000
APP_BASE, APP_END = 0x08007000, 0x080E7000
APP_MAX = APP_END - APP_BASE                 # 0xE0000
APP_MIN = 8
BOOT_VERSION_STAGED = 2                      # INFO bootloader version needed for FW_INSTALL
FW_DATA_CHUNK = 508                          # 512-byte payload limit minus the u32 offset
UF2_MAGIC_START = 0x0A324655                 # "UF2\n"

# Start-up files the program never writes or deletes (DESIGN 13.2; boot_install.s:7-21,
# usb_ctl.s:1850): compared on drive 2:, case-insensitively, after path normalisation.
PROTECTED_FILES = {"bootinst.txt", "boottest.txt", "bootdump.txt", "update/app.bin"}
PICTURE_PATH = re.compile(r"^3:/System file/([1-9][0-9]{0,2})\.jpg$")   # S2 4.10, FILE_WRITE
PICTURE_MIN, PICTURE_MAX = 1, 199
PATH_MAX = 95                                # S2 4.10, FILE_READ / FILE_DELETE


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    """CRC-32 of the image and of backups (zlib's: reflected, polynomial 0xEDB88320; the value
    the firmware compares in FW_END, usb_ctl.s cmd_fw_end)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def flag_names(value: int) -> list[str]:
    return [n for i, n in enumerate(FLAG_NAMES) if value >> i & 1]


def _short(name: str, data: bytes, size: int) -> None:
    if len(data) < size:
        raise DeviceError("%s answer of %d bytes, expected at least %d" % (name, len(data), size))


def _clean_f32(v: float) -> float:
    """The decimal the meter's single-precision value stands for (7 significant digits), so 4.7
    stays 4.7 and not 4.699999809."""
    return float("%.7g" % v) if math.isfinite(v) else v


def _scale(v: float, exponent: int) -> float:
    """v * 1000**exponent (S2 4.6 offset 46); negative powers divide, so no 1/1000 rounding."""
    return v * 1000 ** exponent if exponent >= 0 else v / 1000 ** -exponent


# ----------------------------------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------------------------------

@dataclass
class Dmm:
    """The multimeter part of STATE (S2 4.6 offsets 4, 5, 9, 10 and 44..48). The last five are
    None when the record is too short to hold them (firmware of 44 bytes)."""
    function: int
    value: float                  # f32 as the meter has it: the displayed digits, without prefix
    unit: int                     # offset 9: from the function alone, 0 in auto mode
    decimals: int
    status: int | None = None     # offset 44
    shown_unit: int | None = None    # offset 45
    exponent: int | None = None      # offset 46 (s8)
    unit_text: int | None = None     # offset 47 (0xFF: no unit shown)
    prefix: int | None = None        # offset 48

    @property
    def function_name(self) -> str:
        return DMM_FUNCTION_NAME[self.function] if self.function < len(DMM_FUNCTION_NAME) \
            else "function %d" % self.function

    @property
    def status_text(self) -> str | None:
        return None if self.status is None else DMM_STATUS_TEXT.get(self.status)

    @property
    def has_si_view(self) -> bool:
        """Status, shown unit and exponent are present (record of 47 bytes or more)."""
        return self.status is not None and self.shown_unit is not None \
            and self.exponent is not None


@dataclass
class ScopeChannel:
    enabled: bool
    vdiv: int                     # V/div step
    offset: int
    coupling: int                 # 0 AC / 1 DC (S2 4.4 SCOPE_CHANNEL)
    probe: int                    # 0 x1 / 1 x10
    fft: bool


@dataclass
class ScopeTrigger:
    source: int                   # 0 CH1 / 1 CH2
    edge: int
    level: int
    mode: int                     # 0 normal / 1 auto / 2 single

    @property
    def mode_name(self) -> str:
        return TRIGGER_MODE.get(self.mode, str(self.mode))


@dataclass
class Generator:
    on: bool
    waveform: int
    frequency: int                # Hz
    duty: int                     # %
    amplitude: int                # 0.1 V steps


@dataclass
class State:
    """STATE record (S2 4.6)."""
    length: int
    page: int
    flags: int
    battery: int
    dmm: Dmm
    timebase: int
    ch1: ScopeChannel
    ch2: ScopeChannel
    trigger: ScopeTrigger
    gen: Generator
    time_ms: int
    raw: bytes = field(default=b"", repr=False)

    @property
    def page_name(self) -> str:
        return PAGE_NAME.get(self.page, "page %d" % self.page)

    def flag(self, name: str) -> bool:
        return name in flag_names(self.flags)


def parse_state(d: bytes) -> State:
    """STATE record of 44 bytes or more; the fields that lie inside the length are read, the
    bytes after the last known field are ignored (S2 4.6)."""
    d = bytes(d)
    if len(d) < STATE_MIN:
        raise DeviceError("STATE record of %d bytes, expected at least %d" % (len(d), STATE_MIN))
    page, flags, battery, function, value, unit, decimals = struct.unpack_from("<BHBBfBB", d, 0)
    dmm = Dmm(function, value, unit, decimals)
    if len(d) > 44:
        dmm.status = d[44]
    if len(d) > 45:
        dmm.shown_unit = d[45]
    if len(d) > 46:
        dmm.exponent = struct.unpack_from("<b", d, 46)[0]
    if len(d) > 47:
        dmm.unit_text = d[47]
    if len(d) > 48:
        dmm.prefix = d[48]
    timebase = d[12]
    chans = []
    for n in range(2):
        en, vdiv, off = struct.unpack_from("<BBh", d, 13 + 4 * n)
        coupling, probe, fft = d[38 + 3 * n], d[39 + 3 * n], d[40 + 3 * n]
        chans.append(ScopeChannel(bool(en), vdiv, off, coupling, probe, bool(fft)))
    src, edge, level, mode = struct.unpack_from("<BBhB", d, 21)
    gon, wave, freq, duty, amp = struct.unpack_from("<BBIBB", d, 26)
    (time_ms,) = struct.unpack_from("<I", d, 34)
    return State(len(d), page, flags, battery, dmm, timebase, chans[0], chans[1],
                 ScopeTrigger(src, edge, level, mode), Generator(bool(gon), wave, freq, duty, amp),
                 time_ms, d)


def state_to_reading(s: State, t: float, wall: float) -> Reading:
    """The multimeter reading of a STATE record (DESIGN 6, 13.4, 13.24).

    With bytes 44..46 present: value in the SI unit of the shown unit (value * 1000**exponent),
    validity "valid" for status 0, "overload" for 2, "no reading" for 3 and, with the meter's own
    text ("AUTO", "CAL", "PAS", "Er1".."ErC") in Reading.text, for 1 and 4..9. Without them
    (44-byte record): the raw view, value / unit code / decimals as the meter has them, validity
    "unknown". The key is (function, shown unit) (13.24; no range field)."""
    dmm = s.dmm
    finite = math.isfinite(dmm.value)
    if not dmm.has_si_view:
        unit = UNIT_SYMBOL.get(dmm.unit, "")
        value = _clean_f32(dmm.value) if finite else None
        text = "%.*f" % (dmm.decimals, dmm.value) if finite else ""
        return Reading(t, wall, s.time_ms, dmm.function_name, value, unit, text, dmm.decimals,
                       "unknown", None, (dmm.function_name, unit))
    unit = UNIT_SYMBOL.get(dmm.shown_unit, "")
    key = (dmm.function_name, unit)
    status = dmm.status
    if status != 0:
        text = DMM_STATUS_TEXT.get(status, "")
        validity = {2: "overload", 3: "no reading"}.get(status)
        if validity is None:
            validity = "no reading" if status in DMM_STATUS_TEXT else "unknown"
        return Reading(t, wall, s.time_ms, dmm.function_name, None, unit, text, dmm.decimals,
                       validity, None, key)
    if not finite or dmm.shown_unit not in UNIT_SYMBOL:
        return Reading(t, wall, s.time_ms, dmm.function_name, None, unit, "", dmm.decimals,
                       "no reading" if not finite else "unknown", None, key)
    digits = "%.*f" % (dmm.decimals, dmm.value)
    if dmm.unit_text is not None and dmm.unit_text < len(DMM_UNIT_TEXT):
        unit_txt = DMM_UNIT_TEXT[dmm.unit_text]
    else:
        unit_txt = PREFIX_SYMBOL.get(dmm.exponent, "") + unit if unit else ""
    text = digits + (" " + unit_txt if unit_txt else "")
    value = _scale(_clean_f32(dmm.value), dmm.exponent)
    return Reading(t, wall, s.time_ms, dmm.function_name, value, unit, text, dmm.decimals,
                   "valid", None, key)


@dataclass
class Battery:
    level: int                    # 5..100
    charging: bool                # flag 0: not established


def parse_battery(d: bytes) -> Battery:
    _short("BATTERY", d, 2)
    return Battery(d[0], bool(d[1]))


@dataclass
class Settings:
    backlight: int                # 25..100
    sound: int                    # 0..10
    auto_shutdown: int            # 0 off, 1..3 the menu's three times
    startup: int                  # 0..2


def parse_settings(d: bytes) -> Settings:
    _short("SETTINGS", d, 4)
    return Settings(d[0], d[1], d[2], d[3])


@dataclass
class ScopeWave:
    """SCOPE_WAVE answer (S2 4.4)."""
    channel: int
    timebase: int
    vdiv: int
    probe: int
    coupling: int
    offset: int                   # s8
    enabled: bool
    running: bool
    start: int                    # u16, as the meter reports it
    count: int                    # u16, as the meter reports it
    samples: bytes                # 301 display samples, 128 = centre line

    CENTRE = 128
    ROW_BASE = 248

    def rows(self) -> list[int]:
        """Screen row of every sample (S2 4.4: row = 248 - sample)."""
        return [self.ROW_BASE - s for s in self.samples]


WAVE_SAMPLES = 301


def parse_scope_wave(d: bytes) -> ScopeWave:
    _short("SCOPE_WAVE", d, 12 + WAVE_SAMPLES)
    if len(d) != 12 + WAVE_SAMPLES:
        raise DeviceError("SCOPE_WAVE answer of %d bytes, expected %d" % (len(d), 12 + WAVE_SAMPLES))
    ch, tb, vdiv, probe, coupling, off, en, run, start, count = struct.unpack_from("<BBBBBbBBHH", d)
    return ScopeWave(ch, tb, vdiv, probe, coupling, off, bool(en), bool(run), start, count,
                     bytes(d[12:]))


@dataclass
class ScopeTables:
    """SCOPE_TABLES answer (S2 4.4): the meter's own texts."""
    timebase: list[str]
    vdiv: list[str]

    def vdiv_text(self, channel: ScopeChannel) -> str:
        """The V/div text of a channel: entry probe * 3 + step (S2 4.4)."""
        return self.vdiv[channel.probe * 3 + channel.vdiv]


def parse_scope_tables(d: bytes) -> ScopeTables:
    _short("SCOPE_TABLES", d, 2)
    nt, nv = d[0], d[1]
    texts = bytes(d[2:]).split(b"\0")
    # n zero-terminated texts split into n + 1 pieces, the last one empty
    if len(texts) < nt + nv + 1 or any(texts[nt + nv:]):
        raise DeviceError("SCOPE_TABLES: %d + %d texts announced, %d zero-terminated found"
                          % (nt, nv, max(len(texts) - 1, 0)))
    dec = [t.decode("utf-8", "replace") for t in texts]
    return ScopeTables(dec[:nt], dec[nt:nt + nv])


@dataclass
class FileEntry:
    name: str
    size: int
    attribute: int


def parse_file_list(d: bytes) -> tuple[int, list[FileEntry]]:
    """FILE_LIST answer (S2 4.10): (next entry index, entries); next 0xFFFF = no more."""
    _short("FILE_LIST", d, 2)
    nxt = struct.unpack_from("<H", d)[0]
    out, o = [], 2
    while o < len(d):
        if o + 6 > len(d):
            raise DeviceError("FILE_LIST: entry header cut off at byte %d" % o)
        size, attr, n = struct.unpack_from("<IBB", d, o)
        o += 6
        if o + n > len(d):
            raise DeviceError("FILE_LIST: entry name cut off at byte %d" % o)
        out.append(FileEntry(bytes(d[o:o + n]).decode("utf-8", "replace"), size, attr))
        o += n
    return nxt, out


@dataclass
class ScreenshotHeader:
    total: int
    width: int
    height: int
    format: int                   # 0 = RGB565


def parse_screenshot_header(d: bytes) -> ScreenshotHeader:
    _short("SCREENSHOT", d, 9)
    return ScreenshotHeader(*struct.unpack_from("<IHHB", d))


@dataclass
class FaultRecord:
    """The fault record at 0x20037FB0 (S2 4.13): 12 little-endian words."""
    magic: int
    exception: int
    pc: int
    lr: int
    xpsr: int
    cfsr: int
    hfsr: int
    mmfar: int
    bfar: int
    sp: int
    task: int
    count: int

    @property
    def valid(self) -> bool:
        return self.magic == FAULT_MAGIC


def parse_fault_record(d: bytes) -> FaultRecord:
    _short("fault record", d, FAULT_SIZE)
    return FaultRecord(*struct.unpack_from("<12I", d))


@dataclass
class Event:
    """An event frame (S2 4.12): KEY_PRESSED (key code), PAGE_CHANGED (page), BATTERY_LOW
    (level): `value` is the first data byte."""
    code: int
    value: int | None
    seq: int = 0

    @property
    def name(self) -> str:
        return EVENT_NAME.get(self.code, "event 0x%02X" % self.code)


def parse_event(cmd: int, payload: bytes, seq: int = 0) -> Event:
    return Event(cmd, payload[0] if payload else None, seq)


# ----------------------------------------------------------------------------------------------
# Path guards (DESIGN 13.2, 13.25)
# ----------------------------------------------------------------------------------------------

def canonical_path(path: str) -> str:
    """The path in one form: drive digit, ':', '/', segments joined by '/'. Backslashes become
    '/', empty and '.' segments go, '..' is resolved (above the root is refused). A path needs a
    drive prefix ("2:/..."). Raises SafetyRefused / ValueError."""
    if not isinstance(path, str):
        raise ValueError("path must be text")
    if any(ord(c) < 32 for c in path):
        raise SafetyRefused("path with a control character: %r" % path)
    m = re.match(r"^([0-9]):(.*)$", path.replace("\\", "/"), re.S)
    if m is None:
        raise SafetyRefused("path %r has no drive prefix (2:/... or 3:/...)" % path)
    parts: list[str] = []
    for seg in m.group(2).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise SafetyRefused("path %r goes above the root" % path)
            parts.pop()
            continue
        if not seg.rstrip(" ."):
            raise SafetyRefused("path %r has a segment of only dots and blanks" % path)
        parts.append(seg)
    return "%s:/%s" % (m.group(1), "/".join(parts))


def _compare_form(canonical: str) -> str:
    """Lower case, trailing dots and blanks of every segment removed (a FAT file system treats
    "APP.BIN." and "app.bin " as the same file on some hosts): used only for the guard."""
    return "/".join(s.rstrip(" .").lower() for s in canonical[3:].split("/"))


def guard_file_path(path: str, *, write: bool = True) -> str:
    """Checks a FILE_WRITE / FILE_DELETE path and returns the canonical path to send.

    - drive 2: refuses 2:/BOOTINST.TXT, 2:/BOOTTEST.TXT, 2:/BOOTDUMP.TXT and 2:/UPDATE/APP.BIN
      in any case and any path spelling (DESIGN 13.2; FW_* is the only writer of APP.BIN);
    - drive 3: only the pictures 3:/System file/N.jpg, N = 1..199 without leading zero (S2 4.10;
      3:/System file/9999.bin must stay);
    - any other drive: refused (the firmware answers status 6)."""
    what = "FILE_WRITE" if write else "FILE_DELETE"
    cp = canonical_path(path)
    drive = cp[0]
    if drive == "2":
        if _compare_form(cp) in PROTECTED_FILES:
            raise SafetyRefused("%s of %s refused: start-up file of the 2C53T (DESIGN 13.2)"
                                % (what, cp))
        if cp == "2:/":
            raise SafetyRefused("%s needs a file name" % what)
        return cp
    if drive == "3":
        m = PICTURE_PATH.match(cp)
        if m is None or not PICTURE_MIN <= int(m.group(1)) <= PICTURE_MAX:
            raise SafetyRefused("%s of %s refused: drive 3: only takes the pictures "
                                "3:/System file/N.jpg, N 1..199 (S2 4.10)" % (what, cp))
        return cp
    raise SafetyRefused("%s of %s refused: only drives 2: and 3:" % (what, cp))


def check_picture(data: bytes) -> None:
    """A picture for drive 3: is a baseline JPEG (S2 4.10): starts with SOI, and the first
    start-of-frame marker is SOF0. Raises DeviceError. (The exact limits of the meter's decoder
    are in src/image_20.s; a failing decode shows on the meter, nothing else is touched.)"""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise DeviceError("picture is not a JPEG (no SOI)")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise DeviceError("picture: marker expected at byte %d" % pos)
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xC0:
            return
        if marker in (0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise DeviceError("picture is not baseline (SOF marker 0xFF%02X)" % marker)
        if marker == 0xDA or marker == 0xD9:
            break
        (length,) = struct.unpack_from(">H", data, pos + 2)
        pos += 2 + length
    raise DeviceError("picture has no baseline start-of-frame marker")


# ----------------------------------------------------------------------------------------------
# Application image check (DESIGN 13.17)
# ----------------------------------------------------------------------------------------------

class ImageRefused(DeviceError):
    """The image is refused before anything is sent."""


@dataclass
class ImageCheck:
    length: int
    crc32: int
    sha256: str
    stack_pointer: int
    reset_vector: int             # the odd word 1


def check_app_image(data: bytes) -> ImageCheck:
    """The checks of the meter, made on the host before anything is sent (DESIGN 13.17):
    FW_BEGIN (usb_ctl.s cmd_fw_begin): length 8..0xE0000 and a multiple of 4; the slot
    bootloader (slot_11.s slot_staged_update): word 0 with 0x20000000 < SP <= 0x20038000, word 1
    odd with 0x08007000 <= vector - 1 < 0x08007000 + length, CRC-32 equal to the one of FW_BEGIN.
    A UF2 file (the LC1020E's format) is refused with its own message."""
    data = bytes(data)
    if len(data) >= 4 and struct.unpack_from("<I", data)[0] == UF2_MAGIC_START:
        raise ImageRefused("this is a UF2 file (LC1020E update format), not a 2C53T .bin image")
    n = len(data)
    if n < APP_MIN or n > APP_MAX:
        raise ImageRefused("image length %d: the meter takes 8..%d bytes" % (n, APP_MAX))
    if n & 3:
        raise ImageRefused("image length %d is not a multiple of 4" % n)
    sp, vec = struct.unpack_from("<II", data)
    if not RAM_LO < sp <= RAM_HI:
        raise ImageRefused("initial stack pointer 0x%08X is not in RAM (0x%08X..0x%08X)"
                           % (sp, RAM_LO + 1, RAM_HI))
    if not vec & 1:
        raise ImageRefused("reset vector 0x%08X is not odd (Thumb)" % vec)
    if not APP_BASE <= vec - 1 < APP_BASE + n:
        raise ImageRefused("reset vector 0x%08X is outside the image (0x%08X..0x%08X)"
                           % (vec, APP_BASE, APP_BASE + n - 1))
    return ImageCheck(n, crc32(data), hashlib.sha256(data).hexdigest(), sp, vec)


class UpdateFailed(DeviceError):
    """INFO after the update does not report the last update as OK."""

    def __init__(self, result: int):
        self.result = result
        super().__init__("last update result %d (%s)" % (result, UPDATE_RESULT.get(result, "?")))


class BackupMismatch(SafetyRefused):
    """The meter on the link is not the identified one, or a backup file is damaged."""


@dataclass
class UpdateReport:
    before: Info
    after: Info
    image: ImageCheck
    restarts: int                 # transfers restarted with FW_BEGIN (DESIGN 13.12)
    backup: Path | None


Progress = Callable[[float, str], None]


# ----------------------------------------------------------------------------------------------
# The device
# ----------------------------------------------------------------------------------------------

class _Job:
    """State of one exclusive job: resume = restart the live stream when the job ends."""

    def __init__(self):
        self.resume = True


class C2C53T(Device):
    """The 2C53T on a SerialLink.

    backup_folder: folder of the backups that the save / power-off / update methods write first
        (DESIGN 13.1); nothing dangerous is sent without it.
    reconnect(uid_hex, timeout) -> link: waits for the meter's port after a restart and returns
        the opened new link (DESIGN 13.13); needed by update_app, optional elsewhere.
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
        self._period = 200
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

    def call(self, code: int, payload: bytes = b"", *, allow_dangerous: bool = False) -> bytes:
        """Device.call, plus: KEY with key code 1 (power off, saves settings; S2 4.7) is refused
        here; key() with the typed confirmation is its way. The payload is turned into bytes
        first, so a list, tuple or bytearray holding key code 1 is refused the same way."""
        payload = payload_bytes(payload)
        if code == KEY and payload[:1] == bytes([KEY_POWER_OFF]) and not allow_dangerous:
            raise SafetyRefused("KEY 1 powers the meter off and saves: use key() with confirm")
        return super().call(code, payload, allow_dangerous=allow_dangerous)

    @staticmethod
    def _need_confirm(confirm: str | None) -> None:
        if confirm != CONFIRM_WORD:
            raise SafetyRefused('typed confirmation "%s" required, got %r' % (CONFIRM_WORD, confirm))

    # -- live ---------------------------------------------------------------------------------
    def start_live(self, period_ms: int = 200) -> None:
        """STREAM (S2 4.2): STATE records as stream frames (cmd 0x10) every period_ms (1..65535);
        each becomes a `state` and a `reading` signal."""
        if not 1 <= period_ms <= 0xFFFF:
            raise ValueError("period 1..65535 ms")
        if self._live:
            return
        if not self._stream_connected:
            self.link.stream.connect(self._on_stream)
            self._stream_connected = True
        self._period = period_ms
        self.call(STREAM, struct.pack("<H", period_ms))
        self._live = True

    def stop_live(self) -> None:
        """STREAM with period 0 (S2 4.2)."""
        if not self._live:
            return
        self._live = False
        try:
            self.call(STREAM, struct.pack("<H", 0))
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
        if frame.cmd != STATE:
            return
        try:
            st = parse_state(frame.payload)
        except DeviceError:
            self.bad_frames += 1
            return
        self.state.emit(st)
        self.reading.emit(state_to_reading(st, frame.t, time.time()))

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
                        self.start_live(self._period)
                    except (PortLost, AnswerTimeout, StatusError):
                        pass

    def _state(self, code: int, payload: bytes = b"", *, allow_dangerous: bool = False) -> State:
        return parse_state(self.call(code, payload, allow_dangerous=allow_dangerous))

    # -- 4.1 device ---------------------------------------------------------------------------
    def read_info(self) -> Info:
        """INFO again from the meter (the identified one stays in `info`)."""
        return Info.parse(self.call(INFO))

    def ping(self, data: bytes = b"") -> bytes:
        return self.call(PING, data)

    def reset(self) -> None:
        """RESET: the meter restarts (no settings are written by it; S2 4.1)."""
        self.call(RESET)
        self._live = False

    def battery(self) -> Battery:
        return parse_battery(self.call(BATTERY))

    def keep_awake(self, on: bool) -> None:
        self.call(KEEP_AWAKE, bytes([1 if on else 0]))

    # -- 4.2 state and keys -------------------------------------------------------------------
    def read_state(self) -> State:
        return self._state(STATE)

    def stream(self, period_ms: int) -> None:
        """The raw STREAM command (0 stops); start_live / stop_live keep the bookkeeping."""
        if not 0 <= period_ms <= 0xFFFF:
            raise ValueError("period 0..65535 ms")
        self.call(STREAM, struct.pack("<H", period_ms))

    def page(self, page: int) -> State:
        """PAGE (S2 4.2): 0 menu, 1 meter, 2 scope, 3 generator."""
        if page not in (0, 1, 2, 3):
            raise ValueError("page 0..3")
        return self._state(PAGE, bytes([page]))

    def key(self, code: int, confirm: str | None = None) -> State:
        """KEY (S2 4.2, 4.7), key codes 1..23. Key 1 powers the meter off and saves its
        settings: it needs confirm "OK" and a backup of the settings page first (DESIGN 13.1)."""
        if not KEY_MIN <= code <= KEY_MAX:
            raise ValueError("key code 1..23")
        if code == KEY_POWER_OFF:
            self._need_confirm(confirm)
            self._backup_before()
            try:
                return self._state(KEY, bytes([code]), allow_dangerous=True)
            finally:
                self._live = False
        return self._state(KEY, bytes([code]))

    def power_off(self, confirm: str) -> State:
        """Key 1 with the safeguards of key()."""
        return self.key(KEY_POWER_OFF, confirm)

    # -- 4.3 multimeter -----------------------------------------------------------------------
    def dmm_function(self, function: int) -> State:
        if not 0 <= function <= 12:
            raise ValueError("DMM function 0..12")
        return self._state(DMM_FUNCTION, bytes([function]))

    def dmm_hold(self, on: bool) -> State:
        return self._state(DMM_HOLD, bytes([1 if on else 0]))

    def dmm_relative(self, on: bool) -> State:
        return self._state(DMM_RELATIVE, bytes([1 if on else 0]))

    # -- 4.4 oscilloscope ---------------------------------------------------------------------
    def scope_timebase(self, index: int) -> State:
        if not 0 <= index < SCOPE_TIMEBASES:
            raise ValueError("timebase index 0..28")
        return self._state(SCOPE_TIMEBASE, bytes([index]))

    def scope_channel(self, channel: int, enabled: bool, vdiv: int, offset: int, coupling: int,
                      probe: int, fft: bool) -> State:
        if channel not in (0, 1) or not 0 <= vdiv <= 9 or not -100 <= offset <= 100 \
                or coupling not in (0, 1) or probe not in (0, 1):
            raise ValueError("channel 0/1, V/div step 0..9, offset -100..100, coupling 0/1, "
                             "probe 0/1")
        return self._state(SCOPE_CHANNEL, struct.pack("<BBBbBBB", channel, 1 if enabled else 0, vdiv,
                                                       offset, coupling, probe, 1 if fft else 0))

    def scope_trigger(self, source: int, edge: int, level: int, mode: int) -> State:
        if source not in (0, 1) or edge not in (0, 1) or not -100 <= level <= 100 \
                or mode not in (0, 1, 2):
            raise ValueError("source 0/1, edge 0/1, level -100..100, mode 0..2")
        return self._state(SCOPE_TRIGGER, struct.pack("<BBhB", source, edge, level, mode))

    def scope_tables(self) -> ScopeTables:
        return parse_scope_tables(self.call(SCOPE_TABLES))

    def scope_run(self, run: bool) -> State:
        return self._state(SCOPE_RUN, bytes([1 if run else 0]))

    def scope_autoset(self) -> State:
        return self._state(SCOPE_AUTOSET)

    def scope_autocorrect(self) -> State:
        return self._state(SCOPE_AUTOCORRECT)

    def scope_wave(self, channel: int) -> ScopeWave:
        """SCOPE_WAVE is a request (DESIGN 13.22): the page polls it, one outstanding."""
        if channel not in (0, 1):
            raise ValueError("channel 0 or 1")
        return parse_scope_wave(self.call(SCOPE_WAVE, bytes([channel])))

    # -- 4.5 generator ------------------------------------------------------------------------
    def gen_set(self, on: bool, waveform: int, frequency: int, duty: int, amplitude: int) -> State:
        if not 0 <= waveform < GEN_WAVEFORMS or not 1 <= frequency <= 50000 \
                or not 0 <= duty <= 100 or not 1 <= amplitude <= 30:
            raise ValueError("waveform 0..12, frequency 1..50000 Hz, duty 0..100, amplitude 1..30")
        return self._state(GEN_SET, struct.pack("<BBIBB", 1 if on else 0, waveform, frequency,
                                                duty, amplitude))

    # -- 4.8 settings and the calibration page ------------------------------------------------
    def settings_get(self) -> Settings:
        return parse_settings(self.call(SETTINGS_GET))

    def settings_set(self, backlight: int, sound: int, auto_shutdown: int, startup: int) -> Settings:
        """In RAM only, not saved (S2 4.8)."""
        if not 25 <= backlight <= 100 or not 0 <= sound <= 10 or not 0 <= auto_shutdown <= 3 \
                or not 0 <= startup <= 2:
            raise ValueError("backlight 25..100, sound 0..10, auto shutdown 0..3, startup 0..2")
        return parse_settings(self.call(SETTINGS_SET,
                                        bytes([backlight, sound, auto_shutdown, startup])))

    def settings_save(self, confirm: str) -> None:
        """SETTINGS_SAVE: the meter writes its settings page and restarts, the port goes away
        and comes back (S2 4.8). Needs confirm "OK" and a backup of the page first (13.1)."""
        self._need_confirm(confirm)
        self._backup_before()
        self.call(SETTINGS_SAVE, allow_dangerous=True)
        self._after_restart()

    def cal_read(self, offset: int) -> tuple[int, bytes]:
        """One CAL_READ request (S2 4.8): (total, up to 256 bytes from offset)."""
        if not 0 <= offset <= 0xFFFFFFFF:
            raise ValueError("offset is a u32")
        a = self.call(CAL_READ, struct.pack("<I", offset))
        _short("CAL_READ", a, 4)
        return struct.unpack_from("<I", a)[0], bytes(a[4:])

    def read_calibration_page(self) -> bytes:
        """The whole settings / calibration page 0x08006000..0x080067FF (2048 bytes) with
        CAL_READ (CTL cal_read loop). The meter's total must be 0x800 (S2 4.8)."""
        out = bytearray()
        total = None
        while total is None or len(out) < total:
            self._cancel_point()
            t, chunk = self.cal_read(len(out))
            if total is None:
                if t != CAL_SIZE:
                    raise DeviceError("CAL_READ total is %d, the page is %d bytes" % (t, CAL_SIZE))
                total = t
            elif t != total:
                raise DeviceError("CAL_READ total changed from %d to %d" % (total, t))
            if not chunk:
                raise DeviceError("CAL_READ: empty chunk at %d of %d" % (len(out), total))
            out += chunk[:total - len(out)]
        return bytes(out)

    # -- backup (DESIGN 13.1) -----------------------------------------------------------------
    def backup_dir(self, folder: Path) -> Path:
        return Path(folder) / ("%s-%s" % (MODEL, self.info.uid_hex))

    def _check_same_meter(self) -> None:
        """INFO read again: the chip UID must be the identified meter's (DESIGN 13.13)."""
        if self.read_info().uid != self.info.uid:
            raise BackupMismatch("the meter on the link is not the identified one")

    def backup(self, folder: Path) -> Path:
        """Reads the settings / calibration page (CAL_READ, 8 requests of 256 bytes) into
        folder/2C53T-<UID>/<date>.bin: one JSON line (format, model, uid, firmware, date,
        address, length, crc32) and a newline, then the 2048 raw bytes. When the newest backup
        of this UID holds the same CRC-32, no file is written and that file is returned.
        Cancellable between the requests (nothing is written then)."""
        out_dir = self.backup_dir(folder)
        with self._job(), self._cancellable():
            self._check_same_meter()
            page = self.read_calibration_page()
        crc = crc32(page)
        newest = self._newest_backup(out_dir)
        if newest is not None and newest[1] == crc:
            return newest[0]
        stamp = self._now()
        date = stamp.astimezone().isoformat(timespec="seconds") if stamp.tzinfo is None \
            else stamp.isoformat(timespec="seconds")
        header = {"format": 1, "model": MODEL, "uid": self.info.uid_hex,
                  "firmware": self.info.firmware, "date": date,
                  "address": "0x%08X" % CAL_BASE, "length": len(page), "crc32": "0x%08X" % crc}
        out_dir.mkdir(parents=True, exist_ok=True)
        base = stamp.strftime("%Y%m%d-%H%M%S")
        path = out_dir / (base + ".bin")
        n = 1
        while path.exists():
            n += 1
            path = out_dir / ("%s-%d.bin" % (base, n))
        tmp = path.with_suffix(".bin.tmp")
        with open(tmp, "wb") as f:
            f.write(json.dumps(header, sort_keys=True).encode("utf-8") + b"\n" + page)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    @staticmethod
    def load_backup(path: Path) -> tuple[dict, bytes]:
        """(header, page) of a backup file, the stored CRC-32 and length checked against the
        data. Raises BackupMismatch for a damaged file."""
        try:
            raw = Path(path).read_bytes()
            head, page = raw.split(b"\n", 1)
            doc = json.loads(head.decode("utf-8"))
            if doc.get("format") != 1 or doc.get("model") != MODEL:
                raise BackupMismatch("%s: not a 2C53T backup (format 1)" % path)
            if len(page) != doc["length"] or crc32(page) != int(doc["crc32"], 16):
                raise BackupMismatch("%s: data does not match its stored length / CRC-32" % path)
            return doc, page
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BackupMismatch("%s: unreadable backup (%s)" % (path, e)) from e

    def _newest_backup(self, out_dir: Path):
        """(path, crc) of the newest readable backup of this UID, or None."""
        if not out_dir.is_dir():
            return None
        for path in sorted(out_dir.glob("*.bin"), key=lambda p: p.name, reverse=True):
            try:
                doc, page = self.load_backup(path)
            except BackupMismatch:
                continue
            if doc.get("uid") != self.info.uid_hex:
                continue
            return path, crc32(page)
        return None

    def _backup_before(self) -> Path:
        """The backup every save / power-off / restart method makes first (DESIGN 13.1)."""
        if self.backup_folder is None:
            raise SafetyRefused("no backup folder is set: nothing is sent without a backup")
        return self.backup(self.backup_folder)

    # -- restart handling ---------------------------------------------------------------------
    def _after_restart(self) -> None:
        """After a restart the old link is dead: with a reconnect function the new link is taken
        over and the meter identified again (UID checked, DESIGN 13.13)."""
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

    # -- 4.9 screenshot -----------------------------------------------------------------------
    def screenshot_pixels(self, progress: Progress | None = None, attempts: int = 3) -> array:
        """The screen as 320 x 240 RGB565 values (array 'H', row-major). SCREENSHOT sends 480
        stream frames (cmd 0x60: offset u32 + 320 bytes), then answers (S2 4.9). Holds the port
        (DESIGN 13.11); a capture with missing frames is read again (read-only command).
        Cancellable between the attempts."""
        say = progress or (lambda frac, text: None)
        frames: queue.Queue = queue.Queue()

        def collect(f):
            if f.cmd == SCREENSHOT:
                frames.put(f)

        self.link.stream.connect(collect, Qt.DirectConnection)
        try:
            with self._job(), self._cancellable():
                for attempt in range(attempts):
                    self._cancel_point()
                    say(attempt / attempts, "screenshot")
                    while not frames.empty():
                        frames.get_nowait()
                    head = parse_screenshot_header(self.call(SCREENSHOT))
                    if (head.total, head.width, head.height, head.format) != \
                            (SCR_BYTES, SCR_W, SCR_H, 0):
                        raise DeviceError("screenshot format %dx%d / %d, %d bytes not known"
                                          % (head.width, head.height, head.format, head.total))
                    img = bytearray(SCR_BYTES)
                    have = bytearray(SCR_BYTES)
                    while not frames.empty():
                        p = frames.get_nowait().payload
                        if len(p) < 4:
                            continue
                        off = struct.unpack_from("<I", p)[0]
                        data = p[4:]
                        if off + len(data) > SCR_BYTES:
                            continue
                        img[off:off + len(data)] = data
                        have[off:off + len(data)] = b"\x01" * len(data)
                    if all(have):
                        say(1.0, "screenshot done")
                        pix = array("H")
                        pix.frombytes(bytes(img))
                        if sys.byteorder != "little":
                            pix.byteswap()
                        return pix
                raise DeviceError("screenshot: %d of %d bytes after %d attempts"
                                  % (sum(have), SCR_BYTES, attempts))
        finally:
            try:
                self.link.stream.disconnect(collect)
            except (RuntimeError, TypeError):
                pass

    def screenshot(self, progress: Progress | None = None):
        """The screen as a QImage (RGB565: bits 15..11 red, 10..5 green, 4..0 blue)."""
        from PySide6.QtGui import QImage
        rgb = rgb565_to_rgb(self.screenshot_pixels(progress))
        return QImage(bytes(rgb), SCR_W, SCR_H, SCR_W * 3, QImage.Format_RGB888).copy()

    # -- 4.10 files and drive -----------------------------------------------------------------
    def file_list(self, path: str) -> list[FileEntry]:
        """FILE_LIST of a directory, every page (S2 4.10, CTL file_list)."""
        cp = canonical_path(path)
        out: list[FileEntry] = []
        idx = 0
        with self.link.exclusive():
            while idx != 0xFFFF:
                nxt, entries = parse_file_list(
                    self.call(FILE_LIST, struct.pack("<H", idx) + cp.encode("utf-8")))
                out += entries
                if nxt != 0xFFFF and nxt <= idx:
                    raise DeviceError("FILE_LIST: next entry %d does not advance from %d"
                                      % (nxt, idx))
                idx = nxt
        return out

    def file_read(self, path: str) -> bytes:
        """FILE_READ in order from offset 0: the file stays open on the meter between the
        requests, so the port is held and no other command comes in between (S2 4.10)."""
        cp = canonical_path(path)
        if len(cp) > PATH_MAX:
            raise ValueError("path above %d characters" % PATH_MAX)
        data = bytearray()
        total = None
        with self.link.exclusive():
            while total is None or len(data) < total:
                a = self.call(FILE_READ, struct.pack("<I", len(data)) + cp.encode("utf-8"))
                _short("FILE_READ", a, 4)
                total = struct.unpack_from("<I", a)[0]
                chunk = a[4:]
                if not chunk and len(data) < total:
                    raise DeviceError("FILE_READ: empty chunk at %d of %d" % (len(data), total))
                data += chunk[:total - len(data)]
        return bytes(data)

    def _keep_picture(self, cp: str) -> Path | None:
        """Reads the existing picture `cp` (3:/System file/N.jpg) back and keeps it in
        <backup>/2C53T-<UID>/pictures/ (DESIGN 13.25); None when the meter has no such file.
        "No such file" is only accepted when the meter's FILE_READ fails with status 5 and the
        directory listing has no entry of that name."""
        if self.backup_folder is None:
            raise SafetyRefused("no backup folder is set: nothing is sent without a backup")
        name = cp.rsplit("/", 1)[1]
        try:
            data = self.file_read(cp)
        except StatusError as e:
            if e.status != 5:
                raise
            listed = self.file_list(cp.rsplit("/", 1)[0])
            if any(en.name.lower() == name.lower() for en in listed):
                raise SafetyRefused("%s is listed but cannot be read back: refused" % cp) from e
            return None
        out_dir = self.backup_dir(self.backup_folder) / "pictures"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = name[:-4]
        for old in sorted(out_dir.glob("%s-*.jpg" % stem), reverse=True):
            if old.read_bytes() == data:
                return old                       # the same picture is already kept
        path = out_dir / ("%s-%s.jpg" % (stem, self._now().strftime("%Y%m%d-%H%M%S")))
        n = 1
        while path.exists():
            n += 1
            path = out_dir / ("%s-%s-%d.jpg" % (stem, self._now().strftime("%Y%m%d-%H%M%S"), n))
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return path

    def file_write(self, path: str, data: bytes, *, confirm: str | None = None) -> None:
        """FILE_WRITE in chunks (S2 4.10, CTL file_write). The path is guarded (guard_file_path:
        no start-up file, drive 3: only pictures). A picture on drive 3: needs confirm "OK", is
        checked as a baseline JPEG and the old picture is read back and kept first (13.25)."""
        cp = guard_file_path(path, write=True)
        data = bytes(data)
        p = cp.encode("utf-8") + b"\0"
        step = 512 - len(p) - 8
        if step < 1:
            raise ValueError("path too long")
        with self.link.exclusive():
            if cp[0] == "3":
                self._need_confirm(confirm)
                check_picture(data)
                self._keep_picture(cp)
            off = 0
            while True:
                self.call(FILE_WRITE, struct.pack("<II", off, len(data)) + p + data[off:off + step],
                          allow_dangerous=True)
                off += step
                if off >= len(data):
                    break

    def file_delete(self, path: str, *, confirm: str | None = None) -> None:
        """FILE_DELETE with the same guard; a picture on drive 3: is kept first and needs
        confirm "OK" (13.25)."""
        cp = guard_file_path(path, write=False)
        if len(cp) > PATH_MAX:
            raise ValueError("path above %d characters" % PATH_MAX)
        with self.link.exclusive():
            if cp[0] == "3":
                self._need_confirm(confirm)
                self._keep_picture(cp)
            self.call(FILE_DELETE, cp.encode("utf-8"), allow_dangerous=True)

    def drive(self, on: bool) -> None:
        """DRIVE (S2 4.10): shares the drive with the host; the port re-enumerates. The UI
        warns first that the start-up files must not be touched (DESIGN 13.2)."""
        self.call(DRIVE, bytes([1 if on else 0]))

    # -- 4.11 firmware update -----------------------------------------------------------------
    def update_app(self, path: Path, progress: Progress | None = None, *, confirm: str,
                   known_sha256: set[str] | None = None, confirm_foreign: str | None = None,
                   max_restarts: int = 2, port_timeout: float = 60.0) -> UpdateReport:
        """Application update (DESIGN 9, 13.12, 13.17, 13.26).

        Before anything is sent: confirm "OK"; the image is checked (check_app_image: UF2 and
        wrong lengths / vectors refused); when known_sha256 is given and the image's SHA-256 is
        not in it, confirm_foreign "OK" is needed too; INFO must show bootloader version >= 2;
        the settings page is backed up. Then, holding the port: FW_BEGIN, FW_DATA (never
        repeated), FW_END; a failed transfer restarts with FW_BEGIN (at most max_restarts times;
        a refused FW_BEGIN is final). FW_INSTALL, reconnect, INFO: the UID must be the same and
        the last update result 1.

        Cancellable during the backup and between the requests of the staging, up to FW_END: the
        staged file is then left incomplete on drive 2:, and the meter drops it at the next
        FW_BEGIN (usb_ctl.s:1538 "a previous transfer is dropped"; also when the drive is shared
        or the meter powers off: storage.s:360, 268). Not after FW_END: FW_INSTALL always
        follows."""
        self._need_confirm(confirm)
        say = progress or (lambda frac, text: None)
        data = Path(path).read_bytes()
        img = check_app_image(data)
        if known_sha256 is not None and img.sha256 not in {h.lower() for h in known_sha256}:
            if confirm_foreign != CONFIRM_WORD:
                raise SafetyRefused('image %s is not in any SHA256SUMS of the releases: '
                                    'typed confirmation "%s" required' % (img.sha256, CONFIRM_WORD))
        if self._reconnect is None:
            raise DeviceError("no reconnect function was given: the update cannot be followed")
        before = self.read_info()
        if before.uid != self.info.uid:
            raise BackupMismatch("the meter on the link is not the identified one")
        if before.slot_version < BOOT_VERSION_STAGED:
            raise DeviceError("bootloader version %d has no staged updates (needs %d)"
                              % (before.slot_version, BOOT_VERSION_STAGED))
        saved = self._backup_before()
        say(0.02, "running %s, bootloader slot %s version %d; image %d bytes, CRC-32 0x%08X"
            % (before.firmware, SLOT_NAME.get(before.running_slot, before.running_slot),
               before.slot_version, img.length, img.crc32))
        restarts = 0
        with self._job() as job:
            while True:
                step = FW_BEGIN
                try:
                    with self._cancellable():
                        self._cancel_point()
                        self.call(FW_BEGIN, struct.pack("<II", img.length, img.crc32),
                                  allow_dangerous=True)
                        step = FW_DATA
                        for off in range(0, img.length, FW_DATA_CHUNK):
                            self._cancel_point()
                            chunk = data[off:off + FW_DATA_CHUNK]
                            self.call(FW_DATA, struct.pack("<I", off) + chunk,
                                      allow_dangerous=True)
                            say(0.05 + 0.8 * (off + len(chunk)) / img.length, "staging")
                        self._cancel_point()
                    step = FW_END
                    self.call(FW_END, allow_dangerous=True)
                    break
                except StatusError as e:
                    if step == FW_BEGIN:
                        raise                  # the meter refuses the transfer itself
                    cause = e
                except AnswerTimeout as e:
                    cause = e
                restarts += 1
                if restarts > max_restarts:
                    raise DeviceError("staging failed %d times, last at %s: %s"
                                      % (restarts, COMMANDS[step].name, cause)) from cause
                say(0.05, "transfer failed at %s (%s): restarting with FW_BEGIN"
                    % (COMMANDS[step].name, cause))
            say(0.9, "staged and verified; installing")
            job.resume = False
            self.call(FW_INSTALL, allow_dangerous=True)
        self._live = False
        say(0.92, "waiting for the meter")
        after = self._swap_link(self._reconnect_link(port_timeout))
        if after.update_result != 1:
            raise UpdateFailed(after.update_result)
        say(1.0, "updated: %s" % after.firmware)
        return UpdateReport(before, after, img, restarts, saved)

    # -- 4.13 diagnostics ---------------------------------------------------------------------
    @staticmethod
    def _in_ranges(address: int, length: int, ranges) -> bool:
        return any(lo <= address and address + length <= hi for lo, hi in ranges)

    def mem_read(self, address: int, length: int) -> bytes:
        """MEM_READ in requests of at most 256 bytes (CTL mem_read): internal flash
        0x08000000..0x080FFFFF and the RAM words 0x20037FB0..0x20037FFF only (S2 4.13)."""
        if length < 1 or not self._in_ranges(address, length, MEM_RANGES):
            raise ValueError("MEM_READ: only 0x08000000..0x080FFFFF and 0x20037FB0..0x20037FFF")
        out = bytearray()
        with self.link.exclusive():
            while len(out) < length:
                k = min(length - len(out), 256)
                a = self.call(MEM_READ, struct.pack("<IH", address + len(out), k))
                if len(a) != k:
                    raise DeviceError("MEM_READ: %d bytes, expected %d" % (len(a), k))
                out += a
        return bytes(out)

    def fault_record(self) -> FaultRecord:
        return parse_fault_record(self.mem_read(FAULT_ADDRESS, FAULT_SIZE))

    def flash_read(self, address: int, length: int) -> bytes:
        """FLASH_READ of the external SPI flash in requests of 256 bytes (S2 4.13): drive 3:
        from 0, drive 2: from 0x200000; address + length at most 16 MB."""
        if length < 1 or address < 0 or address + length > FLASH_READ_LIMIT:
            raise ValueError("FLASH_READ: address + length at most 16 MB")
        out = bytearray()
        with self.link.exclusive():
            while len(out) < length:
                k = min(length - len(out), 256)
                a = self.call(FLASH_READ, struct.pack("<IH", address + len(out), k))
                if len(a) != k:
                    raise DeviceError("FLASH_READ: %d bytes, expected %d" % (len(a), k))
                out += a
        return bytes(out)

    def flash_id(self) -> tuple[int, int, int]:
        """JEDEC identification: manufacturer, memory type, capacity (S2 4.13)."""
        a = self.call(FLASH_ID)
        _short("FLASH_ID", a, 3)
        return a[0], a[1], a[2]

    def meter_log_start(self) -> None:
        self.call(METER_LOG, bytes([1]))

    def meter_log_stop(self) -> None:
        self.call(METER_LOG, bytes([0]))

    def meter_log_read(self) -> bytes:
        """The bytes recorded since start (METER_LOG op 2, CTL meter_log_read)."""
        out, off = b"", 0
        with self.link.exclusive():
            while True:
                a = self.call(METER_LOG, struct.pack("<BH", 2, off))
                _short("METER_LOG", a, 2)
                n = struct.unpack_from("<H", a)[0]
                out += a[2:]
                off += len(a) - 2
                if len(a) == 2 or off >= n:
                    return out

    def meter_send(self, m1: int, m0: int, confirm: str) -> None:
        """METER_SEND: the multimeter chip gets AA 55 m1 m0 ... (S2 4.13); meter page only.
        Needs confirm "OK" and a backup of the settings page first (DESIGN 13.7)."""
        if not 0 <= m1 <= 255 or not 0 <= m0 <= 255:
            raise ValueError("m1 and m0 are bytes")
        self._need_confirm(confirm)
        self._backup_before()
        self.call(METER_SEND, bytes([m1, m0]), allow_dangerous=True)

    def text_colour(self, rgb565: int) -> None:
        """TEXT_COLOUR: 0 = the default, 0xFFFF = white; not stored (S2 4.13)."""
        if not 0 <= rgb565 <= 0xFFFF:
            raise ValueError("RGB565 colour is a u16")
        self.call(TEXT_COLOUR, struct.pack("<H", rgb565))


def rgb565_to_rgb(pixels) -> bytearray:
    """RGB565 values -> RGB888 bytes (S2 4.9: little-endian pixels; 5-6-5 bits red, green, blue)."""
    out = bytearray(len(pixels) * 3)
    for i, v in enumerate(pixels):
        r, g, b = v >> 11 & 0x1F, v >> 5 & 0x3F, v & 0x1F
        out[3 * i] = r << 3 | r >> 2
        out[3 * i + 1] = g << 2 | g >> 4
        out[3 * i + 2] = b << 3 | b >> 2
    return out
