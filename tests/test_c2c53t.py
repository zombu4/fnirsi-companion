"""Tests of devices/c2c53t.py against the fake 2C53T (tests/fake_2c53t.py). No serial port is
opened; the UIDs, pages, files and images are invented. The command table is checked against the
command tables parsed from PROTOCOL.md of fnirsi-2c53t-firmware-rewrite (sibling checkout, or the
file named by the environment variable FNIRSI_2C53T_PROTOCOL)."""

import math
import os
import re
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from fnirsi_companion.devices import base, c2c53t as c2          # noqa: E402
import fake_2c53t as fk                                            # noqa: E402

# a QApplication (a QCoreApplication), so that test_ui.py can build widgets in the same run
_app = QCoreApplication.instance() or QApplication([])


def spec_path():
    env = os.environ.get("FNIRSI_2C53T_PROTOCOL")
    p = Path(env) if env else (HERE.parents[2] / "2C53T Firmware V1.2.0" /
                               "fnirsi-2c53t-firmware-rewrite" / "PROTOCOL.md")
    return p if p.is_file() else None


class Bench:
    """A FakeMeter, a FakeLink and a C2C53T on it, with a temporary backup folder and a
    reconnect function that hands out a new link to the same meter."""

    def __init__(self, tc, with_backup=True, **meter_kw):
        self.tmp = Path(tempfile.mkdtemp())
        tc.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.meter = fk.FakeMeter(**meter_kw)
        self.link = fk.FakeLink(self.meter)
        self.t0 = datetime(2026, 11, 21, 10, 0, 0)
        self.ticks = 0
        self.links = [self.link]
        self.reconnects = 0
        self.dev = c2.C2C53T(self.link, base.Info.parse(self.meter.info()),
                             backup_folder=self.tmp / "bk" if with_backup else None,
                             reconnect=self.reconnect, sleep=lambda s: None,
                             clock=lambda: 0.0, now=self.now)

    def now(self):
        self.ticks += 1
        return self.t0 + timedelta(seconds=self.ticks)

    def reconnect(self, uid_hex, timeout):
        assert uid_hex == self.meter.uid.hex().upper()
        self.reconnects += 1
        link = fk.FakeLink(self.meter)
        self.links.append(link)
        return link

    def sent(self, code=None):
        return [r for r in self.meter.log if code is None or r[0] == code]

    def backups(self):
        d = self.dev.backup_dir(self.tmp / "bk")
        return sorted(d.glob("*.bin")) if d.is_dir() else []


class Base(unittest.TestCase):
    def bench(self, **kw):
        return Bench(self, **kw)


# ----------------------------------------------------------------------------------------------
class StateParsing(unittest.TestCase):
    def reading(self, **kw):
        s = c2.parse_state(fk.build_state(**kw))
        return c2.state_to_reading(s, 1.5, 1000.0)

    def test_49_byte_fields(self):
        s = c2.parse_state(fk.build_state(page=2, flags=0b10100101, battery=77, function=6,
                                          value=-1.25, unit=4, decimals=3, timebase=12,
                                          ch1=(1, 4, -30, 1, 1, 1), ch2=(0, 6, 40, 0, 0, 0),
                                          trigger=(1, 1, -20, 2), gen=(1, 3, 2500, 40, 12),
                                          time=99999, status=0, shown_unit=4, exponent=-3,
                                          unit_text=5, prefix=2))
        self.assertEqual((s.length, s.page, s.page_name, s.battery), (49, 2, "scope", 77))
        self.assertEqual(c2.flag_names(s.flags), ["hold", "scope running", "external power",
                                                  "DTR set by the host"])
        d = s.dmm
        self.assertEqual((d.function, d.unit, d.decimals, d.status, d.shown_unit, d.exponent,
                          d.unit_text, d.prefix), (6, 4, 3, 0, 4, -3, 5, 2))
        self.assertAlmostEqual(d.value, -1.25)
        self.assertEqual(s.timebase, 12)
        self.assertEqual((s.ch1.enabled, s.ch1.vdiv, s.ch1.offset, s.ch1.coupling, s.ch1.probe,
                          s.ch1.fft), (True, 4, -30, 1, 1, True))
        self.assertEqual((s.ch2.enabled, s.ch2.vdiv, s.ch2.offset, s.ch2.fft), (False, 6, 40, False))
        self.assertEqual((s.trigger.source, s.trigger.edge, s.trigger.level, s.trigger.mode_name),
                         (1, 1, -20, "single"))
        self.assertEqual((s.gen.on, s.gen.waveform, s.gen.frequency, s.gen.duty, s.gen.amplitude),
                         (True, 3, 2500, 40, 12))
        self.assertEqual(s.time_ms, 99999)

    def test_44_byte_record_has_no_si_view(self):
        s = c2.parse_state(fk.build_state(length=44))
        self.assertEqual(s.length, 44)
        self.assertIsNone(s.dmm.status)
        self.assertFalse(s.dmm.has_si_view)

    def test_longer_record_ignores_the_tail(self):
        s = c2.parse_state(fk.build_state() + b"\xAA\xBB\xCC")
        self.assertEqual(s.length, 52)
        self.assertEqual(s.dmm.prefix, 1)

    def test_too_short_record_raises(self):
        with self.assertRaises(base.DeviceError):
            c2.parse_state(fk.build_state(length=43))

    def test_47_byte_record_reads_the_fields_inside(self):
        s = c2.parse_state(fk.build_state(length=47, value=4.7, exponent=1, unit_text=3))
        self.assertEqual((s.dmm.status, s.dmm.shown_unit, s.dmm.exponent), (0, 3, 1))
        self.assertIsNone(s.dmm.unit_text)
        r = c2.state_to_reading(s, 0.0, 0.0)
        self.assertEqual(r.validity, "valid")
        self.assertAlmostEqual(r.value, 4700.0, places=6)
        self.assertEqual(r.text, "4.70 kΩ")                # the prefix from the exponent

    # -- the reading, SI view -----------------------------------------------------------------
    def test_kilo_ohm(self):
        r = self.reading(function=5, value=4.7, unit=3, decimals=2, shown_unit=3, exponent=1,
                         unit_text=3)
        self.assertEqual((r.validity, r.unit, r.quantity, r.text, r.decimals),
                         ("valid", "Ω", "R", "4.70 kΩ", 2))
        self.assertAlmostEqual(r.value, 4700.0, places=9)
        self.assertEqual((r.t, r.wall, r.meter_ms), (1.5, 1000.0, 123456))
        self.assertIsNone(r.secondary)

    def test_mega_ohm(self):
        r = self.reading(function=5, value=1.5, shown_unit=3, exponent=2, unit_text=4)
        self.assertAlmostEqual(r.value, 1.5e6)
        self.assertEqual(r.text, "1.50 MΩ")

    def test_milli_volt(self):
        r = self.reading(function=1, value=123.4, unit=1, decimals=1, shown_unit=1, exponent=-1,
                         unit_text=0)
        self.assertEqual((r.unit, r.text, r.quantity), ("V", "123.4 mV", "V DC"))
        self.assertAlmostEqual(r.value, 0.1234, places=12)

    def test_micro_ampere(self):
        r = self.reading(function=9, value=56.0, unit=2, decimals=1, shown_unit=2, exponent=-2,
                         unit_text=10)
        self.assertEqual((r.unit, r.text), ("A", "56.0 uA"))
        self.assertAlmostEqual(r.value, 56e-6, places=15)

    def test_nano_farad_and_float32_artifacts(self):
        r = self.reading(function=6, value=4.7, unit=4, decimals=2, shown_unit=4, exponent=-3,
                         unit_text=5, prefix=0)
        self.assertEqual((r.unit, r.text), ("F", "4.70 nF"))
        self.assertAlmostEqual(r.value, 4.7e-9, places=20)          # not 4.699999809e-9

    def test_temperature_units(self):
        r = self.reading(function=7, value=23.5, unit=5, decimals=1, shown_unit=5, exponent=0,
                         unit_text=9)
        self.assertEqual((r.unit, r.text, r.value), ("°C", "23.5 °C", 23.5))
        r = self.reading(function=8, value=74.3, unit=6, decimals=1, shown_unit=6, exponent=0,
                         unit_text=13)
        self.assertEqual((r.unit, r.text), ("°F", "74.3 °F"))

    def test_auto_mode_uses_the_shown_unit(self):
        r = self.reading(function=0, value=3.3, unit=0, decimals=2, shown_unit=1, exponent=0,
                         unit_text=1)
        self.assertEqual((r.quantity, r.unit, r.text, r.value), ("auto", "V", "3.30 V", 3.3))

    def test_every_status(self):
        expect = {0: ("valid", ""), 1: ("no reading", "AUTO"), 2: ("overload", ".OL"),
                  3: ("no reading", ""), 4: ("no reading", "CAL"), 5: ("no reading", "PAS"),
                  6: ("no reading", "Er1"), 7: ("no reading", "Er2"), 8: ("no reading", "Er3"),
                  9: ("no reading", "ErC")}
        for status, (validity, text) in expect.items():
            r = self.reading(function=5, value=4.7, shown_unit=3, exponent=1, unit_text=3,
                             status=status)
            if status == 0:
                self.assertEqual(r.validity, "valid")
                self.assertIsNotNone(r.value)
                continue
            self.assertEqual((r.validity, r.text), (validity, text), status)
            self.assertIsNone(r.value, status)

    def test_unknown_status_is_unknown(self):
        r = self.reading(status=10)
        self.assertEqual((r.validity, r.value, r.text), ("unknown", None, ""))

    def test_nan_reading_is_no_reading(self):
        r = self.reading(value=float("nan"))
        self.assertEqual((r.validity, r.value), ("no reading", None))

    def test_no_unit_text_means_digits_only(self):
        r = self.reading(function=3, value=0.0, unit=0, decimals=0, shown_unit=0, exponent=0,
                         unit_text=0xFF)
        self.assertEqual((r.unit, r.text, r.validity), ("", "0", "valid"))

    # -- the raw view of a 44-byte record -----------------------------------------------------
    def test_raw_view(self):
        r = self.reading(length=44, function=5, value=4.7, unit=3, decimals=2)
        self.assertEqual((r.validity, r.unit, r.text, r.decimals, r.quantity),
                         ("unknown", "Ω", "4.70", 2, "R"))
        self.assertEqual(r.value, 4.7)                         # raw: no prefix applied

    def test_raw_view_nan(self):
        r = self.reading(length=44, value=float("inf"))
        self.assertEqual((r.value, r.validity), (None, "unknown"))

    # -- key 13.24 -----------------------------------------------------------------------------
    def test_key_is_function_and_shown_unit(self):
        a = self.reading(function=5, value=4.7, shown_unit=3, exponent=1, unit_text=3)
        b = self.reading(function=5, value=0.47, shown_unit=3, exponent=2, unit_text=4)
        c = self.reading(function=5, value=0.0, shown_unit=3, exponent=0, unit_text=2, status=2)
        d = self.reading(function=6, value=4.7, unit=4, shown_unit=4, exponent=-3, unit_text=5)
        self.assertEqual(a.key, ("R", "Ω"))
        self.assertEqual(a.key, b.key)                         # a range change keeps the key
        self.assertEqual(a.key, c.key)                         # overload keeps it too
        self.assertNotEqual(a.key, d.key)
        raw = self.reading(length=44, function=5, unit=3)
        self.assertEqual(raw.key, ("R", "Ω"))


# ----------------------------------------------------------------------------------------------
class OtherParsers(unittest.TestCase):
    def test_battery_and_settings(self):
        self.assertEqual(c2.parse_battery(bytes([55, 1])), c2.Battery(55, True))
        self.assertEqual(c2.parse_settings(bytes([80, 5, 2, 1])), c2.Settings(80, 5, 2, 1))
        with self.assertRaises(base.DeviceError):
            c2.parse_battery(b"\x01")

    def test_scope_wave(self):
        w = c2.parse_scope_wave(fk.build_wave(channel=1, timebase=7, vdiv=3, probe=1, coupling=0,
                                              offset=-25, enabled=1, running=0, start=5, count=290))
        self.assertEqual((w.channel, w.timebase, w.vdiv, w.probe, w.coupling, w.offset),
                         (1, 7, 3, 1, 0, -25))
        self.assertEqual((w.enabled, w.running, w.start, w.count, len(w.samples)),
                         (True, False, 5, 290, 301))
        self.assertEqual(w.rows()[0], 248 - w.samples[0])
        with self.assertRaises(base.DeviceError):
            c2.parse_scope_wave(fk.build_wave()[:-1])
        with self.assertRaises(base.DeviceError):
            c2.parse_scope_wave(fk.build_wave() + b"\0")

    def test_scope_tables(self):
        t = c2.parse_scope_tables(fk.build_tables())
        self.assertEqual((len(t.timebase), len(t.vdiv)), (29, 13))
        self.assertEqual((t.timebase[0], t.vdiv[12]), ("TB0", "V12"))
        ch = c2.ScopeChannel(True, 2, 0, 1, 1, False)
        self.assertEqual(t.vdiv_text(ch), "V5")                # probe * 3 + step (S2 4.4)
        with self.assertRaises(base.DeviceError):
            c2.parse_scope_tables(fk.build_tables()[:-3])

    def test_file_list(self):
        d = struct.pack("<H", 0xFFFF) + struct.pack("<IBB", 5, 0x20, 3) + b"A.B" + \
            struct.pack("<IBB", 0, 0x10, 1) + b"D"
        nxt, ents = c2.parse_file_list(d)
        self.assertEqual(nxt, 0xFFFF)
        self.assertEqual([(e.name, e.size, e.attribute) for e in ents], [("A.B", 5, 0x20), ("D", 0, 0x10)])
        with self.assertRaises(base.DeviceError):
            c2.parse_file_list(d[:-1])

    def test_fault_record(self):
        meter = fk.FakeMeter()
        f = c2.parse_fault_record(meter.ram_fault)
        self.assertTrue(f.valid)
        self.assertEqual((f.exception, f.pc, f.count), (3, 0x08010001, 2))

    def test_event(self):
        e = c2.parse_event(0x90, b"\x0F", 4)
        self.assertEqual((e.name, e.value, e.seq), ("KEY_PRESSED", 15, 4))
        self.assertEqual(c2.parse_event(0x92, b"\x05").name, "BATTERY_LOW")
        self.assertEqual(c2.parse_event(0x99, b"").value, None)

    def test_screenshot_header(self):
        h = c2.parse_screenshot_header(struct.pack("<IHHB", 153600, 320, 240, 0))
        self.assertEqual((h.total, h.width, h.height, h.format), (153600, 320, 240, 0))


# ----------------------------------------------------------------------------------------------
class SpecTable(unittest.TestCase):
    """The command table against PROTOCOL.md."""

    @classmethod
    def setUpClass(cls):
        p = spec_path()
        if p is None:
            raise unittest.SkipTest("PROTOCOL.md of the 2C53T repository not found")
        text = p.read_text(encoding="utf-8")
        cls.text = text
        sec = text[text.index("### 4.1 Device"):text.index("### 4.12 Events")] + \
            text[text.index("### 4.13 Diagnostics"):text.index("Fault record (MEM_READ")]
        cls.spec = {}
        for m in re.finditer(r"^\| 0x([0-9A-Fa-f]{2}) \| ([A-Z0-9_]+) \|", sec, re.M):
            cls.spec[int(m.group(1), 16)] = m.group(2)

    def test_every_spec_command_is_in_the_table(self):
        self.assertGreaterEqual(len(self.spec), 40)
        for code, name in self.spec.items():
            self.assertIn(code, c2.COMMANDS, "0x%02X %s missing" % (code, name))
            self.assertEqual(c2.COMMANDS[code].name, name)

    def test_table_has_no_command_the_spec_lacks(self):
        self.assertEqual(sorted(c2.COMMANDS), sorted(self.spec))

    def test_capability_bits(self):
        m = re.search(r"Capability bits: (.*?)\nFirmware 1\.3\.0", self.text, re.S)
        bits = {}
        for part in re.split(r",\s*", " ".join(m.group(1).split()).rstrip(".")):
            n, name = part.split(" ", 1)
            bits[int(n)] = name
        self.assertEqual(sorted(bits), list(range(23)))
        expect = {                                   # bit -> the commands that carry it
            0: ["STATE"], 1: ["STREAM"], 2: ["PAGE"], 3: ["KEY"], 4: ["GEN_SET"],
            5: ["CAL_READ"], 6: ["DRIVE"], 7: ["FW_BEGIN", "FW_DATA", "FW_END", "FW_INSTALL"],
            8: ["BATTERY"], 9: ["KEEP_AWAKE"], 10: ["MEM_READ"],
            11: ["DMM_FUNCTION", "DMM_HOLD", "DMM_RELATIVE"],
            12: ["SCOPE_TIMEBASE", "SCOPE_CHANNEL", "SCOPE_TRIGGER", "SCOPE_TABLES", "SCOPE_RUN",
                 "SCOPE_AUTOSET", "SCOPE_AUTOCORRECT"],
            13: ["SCOPE_WAVE"], 14: ["SETTINGS_GET", "SETTINGS_SET", "SETTINGS_SAVE"],
            15: ["SCREENSHOT"], 16: ["FILE_LIST", "FILE_READ", "FILE_WRITE", "FILE_DELETE"],
            18: ["FLASH_READ"], 19: ["METER_LOG"], 20: ["FLASH_ID"], 21: ["METER_SEND"],
            22: ["TEXT_COLOUR"],
        }
        by_name = {c.name: c for c in c2.COMMANDS.values()}
        seen = set()
        for bit, names in expect.items():
            for n in names:
                self.assertEqual(by_name[n].cap_bit, bit, n)
                seen.add(n)
        # bit 17 is the events (no command); INFO, PING and RESET have no bit
        self.assertEqual(sorted(set(by_name) - seen), ["INFO", "PING", "RESET"])
        for n in ("INFO", "PING", "RESET"):
            self.assertIsNone(by_name[n].cap_bit)

    def test_events_match_spec(self):
        sec = self.text[self.text.index("### 4.12 Events"):self.text.index("### 4.13 Diagnostics")]
        spec = {int(m.group(1), 16): m.group(2)
                for m in re.finditer(r"^\| 0x([0-9A-Fa-f]{2}) \| ([A-Z_]+) \|", sec, re.M)}
        self.assertEqual(c2.EVENT_NAME, spec)

    def test_state_record_size_and_offsets_in_spec(self):
        self.assertIn("### 4.6 STATE record (49 bytes)", self.text)
        for off, field in ((44, "dmm.status"), (45, "dmm.shown_unit"), (46, "dmm.exponent"),
                           (47, "dmm.unit_text"), (48, "dmm.prefix")):
            self.assertRegex(self.text, r"\| %d \| %s \|" % (off, re.escape(field)))

    def test_timeouts(self):
        t = {c.name: c.timeout for c in c2.COMMANDS.values()}
        self.assertEqual((t["PING"], t["STATE"], t["DMM_FUNCTION"], t["DMM_HOLD"], t["SCOPE_TIMEBASE"],
                          t["SCOPE_CHANNEL"], t["SCOPE_TRIGGER"], t["SCREENSHOT"], t["FILE_LIST"]),
                         (0.5, 0.5, 3.0, 2.0, 10.0, 30.0, 30.0, 20.0, 3.0))

    def test_dangerous_commands(self):
        d = sorted(c.name for c in c2.COMMANDS.values() if c.dangerous)
        self.assertEqual(d, ["FILE_DELETE", "FILE_WRITE", "FW_BEGIN", "FW_DATA", "FW_END",
                             "FW_INSTALL", "METER_SEND", "SETTINGS_SAVE"])


# ----------------------------------------------------------------------------------------------
class Guards(Base):
    PROTECTED = [
        "2:/BOOTINST.TXT", "2:/bootinst.txt", "2:\\BOOTTEST.TXT", "2:/BootDump.Txt",
        "2:/UPDATE/APP.BIN", "2:\\UPDATE\\app.bin", "2:/update/App.Bin", "2://UPDATE//APP.BIN",
        "2:/./UPDATE/APP.BIN", "2:/DATA/../UPDATE/APP.BIN", "2:/UPDATE/APP.BIN.",
        "2:/UPDATE/APP.BIN ", "2:/UPDATE./APP.BIN", "2:/UPDATE/.//APP.BIN", "2:UPDATE/APP.BIN",
        "2:\\BOOTINST.TXT",
    ]

    def test_protected_files_never_reach_the_meter(self):
        b = self.bench()
        for p in self.PROTECTED:
            with self.assertRaises(base.SafetyRefused, msg=p):
                b.dev.file_write(p, b"x")
            with self.assertRaises(base.SafetyRefused, msg=p):
                b.dev.file_delete(p)
        self.assertEqual(b.sent(c2.FILE_WRITE) + b.sent(c2.FILE_DELETE), [])
        self.assertEqual(b.meter.files["2:/UPDATE/APP.BIN"], b"old")

    def test_guard_function(self):
        for p in self.PROTECTED:
            with self.assertRaises(base.SafetyRefused, msg=p):
                c2.guard_file_path(p)
        self.assertEqual(c2.guard_file_path("2:\\DATA\\x.csv"), "2:/DATA/x.csv")
        self.assertEqual(c2.guard_file_path("2:/UPDATE/APP.BAK"), "2:/UPDATE/APP.BAK")
        self.assertEqual(c2.guard_file_path("2:/SUB/APP.BIN"), "2:/SUB/APP.BIN")

    def test_paths_without_a_usable_drive_are_refused(self):
        for p in ("UPDATE/APP.BIN", "/UPDATE/APP.BIN", "0:/UPDATE/APP.BIN", "4:/x", "2:/",
                  "2:/../x", "2:/a\0b", "2:/...", "5:", ""):
            with self.assertRaises((base.SafetyRefused, ValueError), msg=p):
                c2.guard_file_path(p)

    def test_drive_3_takes_only_numbered_pictures(self):
        for p in ("3:/System file/1.jpg", "3:/System file/199.jpg", "3:\\System file\\42.jpg",
                  "3:/System file//7.jpg"):
            self.assertRegex(c2.guard_file_path(p), r"^3:/System file/\d+\.jpg$")
        for p in ("3:/System file/9999.bin", "3:/System file/0.jpg", "3:/System file/200.jpg",
                  "3:/System file/001.jpg", "3:/System file/5.JPG", "3:/System file/5.jpg.",
                  "3:/system file/5.jpg", "3:/System file/a.jpg", "3:/x.jpg",
                  "3:/System file/sub/5.jpg", "3:/System file/../9999.bin", "3:/"):
            with self.assertRaises(base.SafetyRefused, msg=p):
                c2.guard_file_path(p)

    def test_picture_check(self):
        c2.check_picture(b"\xff\xd8\xff\xe0\x00\x04ab\xff\xdb\x00\x04cd\xff\xc0\x00\x0b" + bytes(9))
        for bad in (b"", b"GIF89a....", b"\xff\xd8\xff\xc2\x00\x0b" + bytes(9),
                    b"\xff\xd8\xff\xda\x00\x04ab", b"\xff\xd8\x00\x00"):
            with self.assertRaises(base.DeviceError, msg=bad):
                c2.check_picture(bad)

    def test_other_drives_and_confirm_for_pictures(self):
        b = self.bench()
        jpg = b"\xff\xd8\xff\xc0new-picture"
        with self.assertRaises(base.SafetyRefused):              # no confirm
            b.dev.file_write("3:/System file/5.jpg", jpg)
        with self.assertRaises(base.SafetyRefused):
            b.dev.file_write("3:/System file/5.jpg", jpg, confirm="ok")
        with self.assertRaises(base.SafetyRefused):
            b.dev.file_delete("3:/System file/5.jpg")
        with self.assertRaises(base.SafetyRefused):
            b.dev.file_write("3:/System file/9999.bin", b"x", confirm="OK")
        self.assertEqual(b.sent(c2.FILE_WRITE) + b.sent(c2.FILE_DELETE) + b.sent(c2.FILE_READ), [])
        self.assertEqual(b.meter.files["3:/System file/5.jpg"], b"\xff\xd8\xff\xc0old-picture")

    def test_picture_is_read_back_and_kept_before_write(self):
        b = self.bench()
        b.dev.file_write("3:/System file/5.jpg", b"\xff\xd8\xff\xc0new-picture", confirm="OK")
        kept = sorted((b.dev.backup_dir(b.tmp / "bk") / "pictures").glob("5-*.jpg"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"\xff\xd8\xff\xc0old-picture")
        self.assertEqual(b.meter.files["3:/System file/5.jpg"], b"\xff\xd8\xff\xc0new-picture")
        order = [c for c, _ in b.meter.log if c in (c2.FILE_READ, c2.FILE_WRITE)]
        self.assertLess(order.index(c2.FILE_READ), order.index(c2.FILE_WRITE))

    def test_picture_delete_keeps_first_and_same_picture_kept_once(self):
        b = self.bench()
        b.dev.file_delete("3:/System file/6.jpg", confirm="OK")
        self.assertNotIn("3:/System file/6.jpg", b.meter.files)
        kept = list((b.dev.backup_dir(b.tmp / "bk") / "pictures").glob("6-*.jpg"))
        self.assertEqual([k.read_bytes() for k in kept], [b"\xff\xd8\xff\xc0second"])
        b.meter.files["3:/System file/6.jpg"] = b"\xff\xd8\xff\xc0second"
        b.dev.file_delete("3:/System file/6.jpg", confirm="OK")
        self.assertEqual(len(list((b.dev.backup_dir(b.tmp / "bk") / "pictures").glob("6-*.jpg"))), 1)

    def test_new_picture_without_old_one_keeps_nothing(self):
        b = self.bench()
        b.dev.file_write("3:/System file/7.jpg", b"\xff\xd8\xff\xc0fresh", confirm="OK")
        self.assertEqual(b.meter.files["3:/System file/7.jpg"], b"\xff\xd8\xff\xc0fresh")
        self.assertFalse((b.dev.backup_dir(b.tmp / "bk") / "pictures").exists())

    def test_picture_listed_but_unreadable_is_refused(self):
        b = self.bench()
        orig = b.meter._c_71
        b.meter._c_71 = lambda p: (5, b"")
        with self.assertRaises(base.SafetyRefused):
            b.dev.file_write("3:/System file/5.jpg", b"\xff\xd8\xff\xc0new", confirm="OK")
        self.assertEqual(b.sent(c2.FILE_WRITE), [])
        b.meter._c_71 = orig

    def test_bad_picture_refused_before_anything(self):
        b = self.bench()
        with self.assertRaises(base.DeviceError):
            b.dev.file_write("3:/System file/5.jpg", b"not a jpeg", confirm="OK")
        self.assertEqual(b.sent(c2.FILE_WRITE), [])

    def test_picture_needs_a_backup_folder(self):
        b = self.bench(with_backup=False)
        with self.assertRaises(base.SafetyRefused):
            b.dev.file_write("3:/System file/5.jpg", b"\xff\xd8\xff\xc0new", confirm="OK")
        self.assertEqual(b.sent(c2.FILE_WRITE), [])

    def test_ordinary_files(self):
        b = self.bench()
        data = bytes(range(256)) * 5                                 # 1280 bytes: three chunks
        b.dev.file_write("2:\\DATA\\X.BIN", data)
        writes = b.sent(c2.FILE_WRITE)
        self.assertEqual(len(writes), 3)
        self.assertTrue(all(w[1][8:].startswith(b"2:/DATA/X.BIN\0") for w in writes))
        self.assertTrue(all(len(w[1]) <= 512 for w in writes))
        self.assertEqual(b.meter.files["2:/DATA/X.BIN"], data)
        self.assertEqual(b.dev.file_read("2:/DATA/X.BIN"), data)
        b.dev.file_delete("2:/DATA/X.BIN")
        self.assertNotIn("2:/DATA/X.BIN", b.meter.files)
        b.dev.file_write("2:/EMPTY.TXT", b"")
        self.assertEqual(b.meter.files["2:/EMPTY.TXT"], b"")
        self.assertEqual(b.dev.file_read("2:/EMPTY.TXT"), b"")

    def test_list_pages(self):
        b = self.bench()
        root = b.dev.file_list("2:/")
        self.assertEqual(sorted(e.name for e in root), ["BOOTINST.TXT", "README.TXT"])
        self.assertGreaterEqual(len(b.sent(c2.FILE_LIST)), 1)
        sysdir = b.dev.file_list("3:/System file")
        self.assertEqual(sorted(e.name for e in sysdir), ["5.jpg", "6.jpg", "9999.bin"])
        self.assertEqual(len(b.sent(c2.FILE_LIST)), 3)               # 1 + 2 pages (two per answer)
        self.assertEqual(b.dev.file_read("2:/README.TXT"), b"hello")


# ----------------------------------------------------------------------------------------------
class Backup(Base):
    def read_header(self, path):
        import json
        raw = path.read_bytes()
        head, page = raw.split(b"\n", 1)
        return json.loads(head), page

    def test_backup_file(self):
        b = self.bench()
        path = b.dev.backup(b.tmp / "bk")
        self.assertEqual(path.parent.name, "2C53T-" + fk.INVENTED_UID.hex().upper())
        self.assertEqual(path.suffix, ".bin")
        doc, page = self.read_header(path)
        self.assertEqual(page, b.meter.page)
        self.assertEqual(len(page), 0x800)
        self.assertEqual((doc["model"], doc["uid"], doc["firmware"], doc["address"], doc["length"]),
                         ("2C53T", fk.INVENTED_UID.hex().upper(), "1.4.17", "0x08006000", 2048))
        self.assertEqual(doc["crc32"], "0x%08X" % fk.crc32(b.meter.page))
        self.assertTrue(doc["date"].startswith("2026-11-21T10:00:"))
        self.assertEqual(len(b.sent(c2.CAL_READ)), 8)                 # 8 x 256 bytes
        self.assertEqual([struct.unpack("<I", p)[0] for c, p in b.sent(c2.CAL_READ)],
                         [0, 256, 512, 768, 1024, 1280, 1536, 1792])
        self.assertEqual(c2.C2C53T.load_backup(path)[1], b.meter.page)

    def test_same_crc_is_skipped_changed_content_is_new(self):
        b = self.bench()
        first = b.dev.backup(b.tmp / "bk")
        again = b.dev.backup(b.tmp / "bk")
        self.assertEqual(first, again)
        self.assertEqual(len(b.backups()), 1)
        b.meter.page = fk.invented_page(2)
        third = b.dev.backup(b.tmp / "bk")
        self.assertNotEqual(first, third)
        self.assertEqual(len(b.backups()), 2)
        b.meter.page = fk.invented_page(1)                           # back to the first content:
        fourth = b.dev.backup(b.tmp / "bk")                          # newest is the second, so new
        self.assertEqual(len(b.backups()), 3)
        self.assertEqual(self.read_header(fourth)[1], fk.invented_page(1))

    def test_other_meters_files_do_not_count(self):
        b = self.bench()
        d = b.dev.backup_dir(b.tmp / "bk")
        d.mkdir(parents=True)
        import json
        page = b.meter.page
        head = {"format": 1, "model": "2C53T", "uid": fk.OTHER_UID.hex().upper(), "firmware": "1.4.17",
                "date": "2099-01-01T00:00:00", "address": "0x08006000", "length": 2048,
                "crc32": "0x%08X" % fk.crc32(page)}
        (d / "20990101-000000.bin").write_bytes(json.dumps(head).encode() + b"\n" + page)
        (d / "20990102-000000.bin").write_bytes(b"damaged, no newline")
        path = b.dev.backup(b.tmp / "bk")
        self.assertNotEqual(path.name, "20990101-000000.bin")        # not skipped on a foreign file
        self.assertEqual(self.read_header(path)[0]["uid"], fk.INVENTED_UID.hex().upper())

    def test_load_backup_detects_damage(self):
        b = self.bench()
        path = b.dev.backup(b.tmp / "bk")
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))
        with self.assertRaises(c2.BackupMismatch):
            c2.C2C53T.load_backup(path)
        path.write_bytes(b"{}\n" + bytes(10))
        with self.assertRaises(c2.BackupMismatch):
            c2.C2C53T.load_backup(path)
        with self.assertRaises(c2.BackupMismatch):
            c2.C2C53T.load_backup(b.tmp / "missing.bin")

    def test_other_meter_on_the_link_is_refused(self):
        b = self.bench()
        b.meter.uid = fk.OTHER_UID
        with self.assertRaises(c2.BackupMismatch):
            b.dev.backup(b.tmp / "bk")
        self.assertEqual(b.sent(c2.CAL_READ), [])
        self.assertEqual(b.backups(), [])

    def test_wrong_total_is_refused(self):
        b = self.bench()
        b.meter.page = bytes(1024)
        with self.assertRaises(base.DeviceError):
            b.dev.backup(b.tmp / "bk")
        self.assertEqual(b.backups(), [])

    def test_short_chunk_is_refused(self):
        b = self.bench()
        orig = b.meter._c_53
        b.meter._c_53 = lambda p: (0, struct.pack("<I", 0x800))     # total, no data
        with self.assertRaises(base.DeviceError):
            b.dev.backup(b.tmp / "bk")
        b.meter._c_53 = orig

    def test_backup_runs_with_the_port_held_and_resumes_live(self):
        b = self.bench()
        b.dev.start_live(100)
        b.dev.backup(b.tmp / "bk")
        self.assertGreaterEqual(b.link.exclusive_count, 1)
        periods = [struct.unpack("<H", p)[0] for c, p in b.sent(c2.STREAM)]
        self.assertEqual(periods, [100, 0, 100])                     # stopped, then resumed
        b.dev.close()


# ----------------------------------------------------------------------------------------------
class Confirmed(Base):
    def before_save(self, b, code):
        """True when a CAL_READ preceded the first request `code`."""
        log = [c for c, _ in b.meter.log]
        return c2.CAL_READ in log and log.index(c2.CAL_READ) < log.index(code)

    def test_settings_save(self):
        b = self.bench()
        for conf in (None, "", "ok", "Ok", "YES"):
            with self.assertRaises(base.SafetyRefused):
                b.dev.settings_save(conf)
        self.assertEqual(b.meter.log, [])                            # nothing was sent at all
        b.dev.settings_save("OK")
        self.assertTrue(self.before_save(b, c2.SETTINGS_SAVE))
        self.assertEqual(len(b.backups()), 1)
        self.assertEqual(b.meter.restarts, 1)
        self.assertEqual(b.reconnects, 1)                            # the new link was taken over
        self.assertIs(b.dev.link, b.links[-1])

    def test_key_power_off(self):
        b = self.bench()
        with self.assertRaises(base.SafetyRefused):
            b.dev.key(1)
        with self.assertRaises(base.SafetyRefused):
            b.dev.power_off("nope")
        with self.assertRaises(base.SafetyRefused):
            b.dev.call(c2.KEY, bytes([1]))                           # the generic call refuses too
        self.assertEqual(b.meter.log, [])
        b.dev.power_off("OK")
        self.assertTrue(self.before_save(b, c2.KEY))
        self.assertEqual(b.meter.keys, [1])
        self.assertEqual(len(b.backups()), 1)

    def test_other_keys_are_free(self):
        b = self.bench()
        s = b.dev.key(15)
        self.assertEqual(b.meter.keys, [15])
        self.assertIsInstance(s, c2.State)
        self.assertEqual(b.sent(c2.CAL_READ), [])
        for bad in (0, 24, -1):
            with self.assertRaises(ValueError):
                b.dev.key(bad)

    def test_meter_send(self):
        b = self.bench()
        with self.assertRaises(base.SafetyRefused):
            b.dev.meter_send(0x10, 0x20, "")
        self.assertEqual(b.meter.log, [])
        b.dev.meter_send(0x10, 0x20, "OK")
        self.assertTrue(self.before_save(b, c2.METER_SEND))
        self.assertEqual(b.meter.sent_to_chip, [(0x10, 0x20)])

    def test_generic_call_refuses_dangerous(self):
        b = self.bench()
        for code in (c2.SETTINGS_SAVE, c2.METER_SEND, c2.FILE_WRITE, c2.FILE_DELETE, c2.FW_BEGIN,
                     c2.FW_DATA, c2.FW_END, c2.FW_INSTALL):
            with self.assertRaises(base.SafetyRefused, msg=hex(code)):
                b.dev.call(code, b"\0" * 8)
        self.assertEqual(b.meter.log, [])

    def test_no_backup_folder_no_send(self):
        b = self.bench(with_backup=False)
        for fn in (lambda: b.dev.settings_save("OK"), lambda: b.dev.power_off("OK"),
                   lambda: b.dev.meter_send(1, 2, "OK")):
            with self.assertRaises(base.SafetyRefused):
                fn()
        self.assertEqual([c for c, _ in b.meter.log if c in (0x52, 0x13, 0xF5)], [])

    def test_failed_backup_stops_the_save(self):
        b = self.bench()
        b.meter.page = bytes(16)
        with self.assertRaises(base.DeviceError):
            b.dev.settings_save("OK")
        self.assertEqual(b.sent(c2.SETTINGS_SAVE), [])

    def test_second_save_with_same_content_writes_no_second_file(self):
        b = self.bench()
        b.dev.settings_save("OK")
        b.dev.settings_save("OK")
        self.assertEqual(len(b.backups()), 1)

    def test_settings_get_set(self):
        b = self.bench()
        self.assertEqual(b.dev.settings_get(), c2.Settings(80, 5, 1, 0))
        self.assertEqual(b.dev.settings_set(60, 3, 2, 1), c2.Settings(60, 3, 2, 1))
        self.assertEqual(b.sent(c2.SETTINGS_SET)[0][1], bytes([60, 3, 2, 1]))
        for bad in ((24, 0, 0, 0), (101, 0, 0, 0), (50, 11, 0, 0), (50, 0, 4, 0), (50, 0, 0, 3)):
            with self.assertRaises(ValueError):
                b.dev.settings_set(*bad)
        self.assertEqual(len(b.sent(c2.SETTINGS_SET)), 1)


# ----------------------------------------------------------------------------------------------
class Capabilities(Base):
    def test_every_capability_bit_refuses_its_commands(self):
        for code, cmd in c2.COMMANDS.items():
            if cmd.cap_bit is None:
                continue
            b = self.bench(caps=fk.ALL_CAPS & ~(1 << cmd.cap_bit))
            self.assertFalse(b.dev.has(code), cmd.name)
            with self.assertRaises(base.CapabilityError, msg=cmd.name):
                b.dev.call(code, b"\0\0\0\0\0\0\0\0", allow_dangerous=True)
            self.assertEqual(b.meter.log, [], cmd.name)

    def test_methods_refuse_without_their_bit(self):
        b = self.bench(caps=fk.ALL_CAPS & ~(1 << c2.CAP_SCREENSHOT) & ~(1 << c2.CAP_DMM)
                       & ~(1 << c2.CAP_FIRMWARE_UPDATE))
        with self.assertRaises(base.CapabilityError):
            b.dev.screenshot_pixels()
        with self.assertRaises(base.CapabilityError):
            b.dev.dmm_function(1)
        self.assertTrue(b.dev.has(c2.STATE))
        self.assertTrue(b.dev.has(c2.PING))
        self.assertEqual(b.meter.log, [])

    def test_old_firmware_bits(self):
        b = self.bench(caps=0x7FFFF)                                 # firmware 1.3.0 (S2 4.1)
        self.assertTrue(b.dev.has(c2.SCREENSHOT))
        self.assertTrue(b.dev.has(c2.FLASH_READ))                    # bit 18 is inside 0x7FFFF
        self.assertFalse(b.dev.has(c2.FLASH_ID))
        self.assertFalse(b.dev.has(c2.METER_SEND))
        with self.assertRaises(base.CapabilityError):
            b.dev.flash_id()

    def test_unknown_command(self):
        b = self.bench()
        with self.assertRaises(base.UnknownCommand):
            b.dev.call(0x7E)
        self.assertFalse(b.dev.has(0x7E))


# ----------------------------------------------------------------------------------------------
class ImageChecks(unittest.TestCase):
    def test_good_image(self):
        img = fk.build_app_image(0x400)
        c = c2.check_app_image(img)
        self.assertEqual((c.length, c.stack_pointer, c.reset_vector),
                         (0x400, 0x20030000, 0x08007101))
        self.assertEqual(c.crc32, fk.crc32(img))
        self.assertEqual(len(c.sha256), 64)

    def test_limits_are_accepted(self):
        c2.check_app_image(fk.build_app_image(8, vec_offset=1, sp=0x20000004))   # shortest, SP > LO
        c2.check_app_image(fk.build_app_image(0xE0000, sp=0x20038000, vec_offset=0xDFFFF))
        c2.check_app_image(fk.build_app_image(0x400, vec_offset=1))               # vec - 1 == base
        c2.check_app_image(fk.build_app_image(0x400, vec_offset=0x3FF))           # last byte

    def test_uf2_refused(self):
        with self.assertRaisesRegex(c2.ImageRefused, "UF2"):
            c2.check_app_image(fk.build_uf2())

    def test_length_refusals(self):
        good = fk.build_app_image(0x400)
        for bad in (b"", good[:4], good[:7], good[:0x3FE], good + b"\0", fk.build_app_image(0xE0004)):
            with self.assertRaises(c2.ImageRefused, msg=len(bad)):
                c2.check_app_image(bad)

    def test_stack_pointer_refusals(self):
        for sp in (0, 0x1FFFFFFC, 0x20000000, 0x20038004, 0x08010000, 0xFFFFFFFF):
            with self.assertRaises(c2.ImageRefused, msg=hex(sp)):
                c2.check_app_image(fk.build_app_image(0x400, sp=sp))

    def test_reset_vector_refusals(self):
        for off in (0x100, 0x400 + 1, 0x4001, -0x1001 + 1):
            img = fk.build_app_image(0x400, vec_offset=off) if off > 0 else \
                fk.build_app_image(0x400, base_addr=0x08007000, vec_offset=off)
            with self.assertRaises(c2.ImageRefused, msg=hex(off)):
                c2.check_app_image(img)
        img = bytearray(fk.build_app_image(0x400))
        struct.pack_into("<I", img, 4, 0x20001001)                    # odd, but in RAM
        with self.assertRaises(c2.ImageRefused):
            c2.check_app_image(bytes(img))


class Update(Base):
    def image_file(self, b, **kw):
        p = b.tmp / "APP_2C53T_V9_9_9.bin"
        p.write_bytes(fk.build_app_image(1200, **kw))
        return p

    def test_good_update(self):
        b = self.bench()
        p = self.image_file(b)
        steps = []
        rep = b.dev.update_app(p, lambda f, t: steps.append(f), confirm="OK")
        img = p.read_bytes()
        self.assertEqual(b.meter.installed, img)
        self.assertEqual((rep.before.firmware, rep.after.firmware, rep.restarts), ("1.4.17", "1.4.18", 0))
        self.assertEqual(rep.after.update_result, 1)
        self.assertEqual(rep.image.length, 1200)
        self.assertTrue(rep.backup is not None and rep.backup.exists())
        cmds = [c for c, _ in b.meter.log if c >= 0x80]
        self.assertEqual(cmds, [0x80, 0x81, 0x81, 0x81, 0x82, 0x83])  # 508 + 508 + 184
        self.assertEqual(struct.unpack("<II", b.sent(c2.FW_BEGIN)[0][1]), (1200, fk.crc32(img)))
        self.assertEqual([struct.unpack_from("<I", p)[0] for c, p in b.sent(c2.FW_DATA)], [0, 508, 1016])
        self.assertEqual(steps[-1], 1.0)
        self.assertEqual(sorted(steps), steps)
        self.assertGreaterEqual(b.link.exclusive_count, 1)
        self.assertEqual(b.reconnects, 1)
        self.assertIs(b.dev.link, b.links[-1])
        self.assertEqual(b.dev.info.firmware, "1.4.18")
        # the backup came before the first FW command
        log = [c for c, _ in b.meter.log]
        self.assertLess(log.index(c2.CAL_READ), log.index(c2.FW_BEGIN))

    def test_uf2_refused_before_anything_is_sent(self):
        b = self.bench()
        p = b.tmp / "x.uf2"
        p.write_bytes(fk.build_uf2())
        with self.assertRaises(c2.ImageRefused):
            b.dev.update_app(p, confirm="OK")
        self.assertEqual(b.meter.log, [])

    def test_bad_images_refused_before_anything_is_sent(self):
        for kw in ({"sp": 0x20038004}, {"vec_offset": 0x4000}, {"vec_offset": 0x100}):
            b = self.bench()
            with self.assertRaises(c2.ImageRefused):
                b.dev.update_app(self.image_file(b, **kw), confirm="OK")
            self.assertEqual(b.meter.log, [], kw)
        b = self.bench()
        p = b.tmp / "short.bin"
        p.write_bytes(fk.build_app_image(1200)[:1198])
        with self.assertRaises(c2.ImageRefused):
            b.dev.update_app(p, confirm="OK")
        self.assertEqual(b.meter.log, [])

    def test_bootloader_version_1_refused(self):
        b = self.bench(boot_version=1)
        with self.assertRaisesRegex(base.DeviceError, "bootloader version 1"):
            b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual([c for c, _ in b.meter.log if c >= 0x80], [])
        self.assertEqual(b.sent(c2.CAL_READ), [])

    def test_bootloader_version_read_fresh_not_from_cached_info(self):
        b = self.bench()
        b.meter.boot_version = 1                       # changed after identification
        with self.assertRaises(base.DeviceError):
            b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual([c for c, _ in b.meter.log if c >= 0x80], [])

    def test_confirm_and_backup_folder_and_reconnect_required(self):
        b = self.bench()
        with self.assertRaises(base.SafetyRefused):
            b.dev.update_app(self.image_file(b), confirm="yes")
        self.assertEqual(b.meter.log, [])
        b = self.bench(with_backup=False)
        with self.assertRaises(base.SafetyRefused):
            b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual([c for c, _ in b.meter.log if c >= 0x80], [])
        b = self.bench()
        b.dev._reconnect = None
        with self.assertRaises(base.DeviceError):
            b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual(b.meter.log, [])

    def test_foreign_image_needs_second_confirmation(self):
        b = self.bench()
        p = self.image_file(b)
        known = {"0" * 64}
        with self.assertRaises(base.SafetyRefused):
            b.dev.update_app(p, confirm="OK", known_sha256=known)
        self.assertEqual(b.meter.log, [])
        sha = c2.check_app_image(p.read_bytes()).sha256
        rep = b.dev.update_app(p, confirm="OK", known_sha256={sha.upper()})   # known: no second word
        self.assertEqual(rep.after.update_result, 1)
        b = self.bench()
        p = self.image_file(b)
        rep = b.dev.update_app(p, confirm="OK", known_sha256=known, confirm_foreign="OK")
        self.assertEqual(b.meter.installed, p.read_bytes())

    def test_lost_answer_during_fw_data_restarts_with_fw_begin(self):
        b = self.bench()
        p = self.image_file(b)
        done = []

        def lose(cmd, payload):
            if cmd == c2.FW_DATA and struct.unpack_from("<I", payload)[0] == 508 and not done:
                done.append(1)
                return True
            return False
        b.meter.lose = lose
        rep = b.dev.update_app(p, confirm="OK")
        self.assertEqual(rep.restarts, 1)
        self.assertEqual(b.meter.installed, p.read_bytes())
        fw = [(c, struct.unpack_from("<I", pl)[0] if c == c2.FW_DATA else None)
              for c, pl in b.meter.log if 0x80 <= c <= 0x83]
        lost_at = fw.index((c2.FW_DATA, 508))
        self.assertEqual(fw[lost_at + 1][0], c2.FW_BEGIN)           # the very next request
        self.assertEqual(fw.count((c2.FW_BEGIN, None)), 2)
        # within one transfer no FW_DATA offset is sent twice (13.12)
        run, seen = None, []
        for c, off in fw:
            if c == c2.FW_BEGIN:
                run = []
                seen.append(run)
            elif c == c2.FW_DATA:
                self.assertNotIn(off, run)
                run.append(off)
        self.assertEqual(seen, [[0, 508], [0, 508, 1016]])

    def test_lost_answer_at_fw_end_restarts_too(self):
        b = self.bench()
        done = []

        def lose(cmd, payload):
            if cmd == c2.FW_END and not done:
                done.append(1)
                return True
            return False
        b.meter.lose = lose
        rep = b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual(rep.restarts, 1)
        self.assertEqual(len(b.sent(c2.FW_BEGIN)), 2)

    def test_status_error_during_data_restarts(self):
        b = self.bench()
        orig, state = b.meter._c_81, []

        def flaky(p):
            if not state and struct.unpack_from("<I", p)[0] == 508:
                state.append(1)
                return 5, b""
            return orig(p)
        b.meter._c_81 = flaky
        rep = b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual(rep.restarts, 1)

    def test_giving_up_never_installs(self):
        b = self.bench()
        b.meter.lose = lambda cmd, payload: cmd == c2.FW_DATA
        with self.assertRaisesRegex(base.DeviceError, "staging failed 3 times"):
            b.dev.update_app(self.image_file(b), confirm="OK", max_restarts=2)
        self.assertEqual(len(b.sent(c2.FW_BEGIN)), 3)
        self.assertEqual(b.sent(c2.FW_INSTALL), [])
        self.assertIsNone(b.meter.installed)
        self.assertEqual(b.reconnects, 0)

    def test_refused_fw_begin_is_final(self):
        b = self.bench()
        b.meter._c_80 = lambda p: (3, b"")
        with self.assertRaises(base.StatusError) as cm:
            b.dev.update_app(self.image_file(b), confirm="OK")
        self.assertEqual(cm.exception.status, 3)
        self.assertEqual(len(b.sent(c2.FW_BEGIN)), 1)
        self.assertEqual(b.sent(c2.FW_DATA), [])

    def test_failed_install_result_is_reported(self):
        for result in (2, 3, 4):
            b = self.bench()
            b.meter.install_result = result
            with self.assertRaises(c2.UpdateFailed) as cm:
                b.dev.update_app(self.image_file(b), confirm="OK")
            self.assertEqual(cm.exception.result, result)
            self.assertIn(c2.UPDATE_RESULT[result], str(cm.exception))

    def test_other_meter_after_reconnect_is_refused(self):
        b = self.bench()

        def rc(uid_hex, timeout):
            b.meter.uid = fk.OTHER_UID
            return fk.FakeLink(b.meter)
        b.dev._reconnect = rc
        with self.assertRaises(c2.BackupMismatch):
            b.dev.update_app(self.image_file(b), confirm="OK")

    def test_stream_stopped_during_update(self):
        b = self.bench()
        b.dev.start_live(100)
        b.dev.update_app(self.image_file(b), confirm="OK")
        periods = [struct.unpack("<H", p)[0] for c, p in b.sent(c2.STREAM)]
        self.assertEqual(periods, [100, 0, 100, 0])    # backup job resumes it, the update job stops it
        self.assertFalse(b.dev.live)


# ----------------------------------------------------------------------------------------------
class Live(Base):
    def test_stream_signals(self):
        b = self.bench()
        readings, states, events = [], [], []
        b.dev.reading.connect(readings.append)
        b.dev.state.connect(states.append)
        b.dev.meter_event.connect(events.append)
        b.dev.start_live(250)
        self.assertEqual(b.sent(c2.STREAM), [(c2.STREAM, struct.pack("<H", 250))])
        self.assertEqual(b.meter.stream_period, 250)
        self.assertTrue(b.dev.live)
        b.link.emit_stream(c2.STATE, b.meter.state(value=4.7, shown_unit=3, exponent=1, unit_text=3))
        b.link.emit_stream(c2.STATE, b.meter.state(length=44))
        b.link.emit_stream(0x60, b"\0" * 8)                           # a screenshot frame: ignored
        b.link.emit_stream(c2.STATE, b"short")                        # bad record: counted
        self.assertEqual(len(states), 2)
        self.assertEqual([r.validity for r in readings], ["valid", "unknown"])
        self.assertAlmostEqual(readings[0].value, 4700.0, places=6)
        self.assertEqual(readings[0].t, 12.5)
        self.assertEqual(b.dev.bad_frames, 1)
        b.link.emit_event(0x90, b"\x0f")
        b.link.emit_event(0x91, b"\x02")
        self.assertEqual([(e.name, e.value) for e in events], [("KEY_PRESSED", 15), ("PAGE_CHANGED", 2)])
        b.dev.stop_live()
        self.assertEqual(b.meter.stream_period, 0)
        self.assertFalse(b.dev.live)
        b.link.emit_stream(c2.STATE, b.meter.state())
        self.assertEqual(len(states), 2)                              # disconnected
        b.dev.start_live()                                            # default period
        self.assertEqual(b.meter.stream_period, 200)
        b.dev.close()
        b.link.emit_event(0x90, b"\x01")
        self.assertEqual(len(events), 2)

    def test_period_validation(self):
        b = self.bench()
        for bad in (0, 65536, -1):
            with self.assertRaises(ValueError):
                b.dev.start_live(bad)
        self.assertEqual(b.meter.log, [])

    def test_job_pauses_and_resumes_the_stream(self):
        b = self.bench()
        b.dev.start_live(300)
        b.dev.mem_read(0x08000000, 4)
        b.dev.flash_read(0, 300)
        periods = [struct.unpack("<H", p)[0] for c, p in b.sent(c2.STREAM)]
        self.assertEqual(periods, [300])                              # only reads: no job
        b.dev.screenshot_pixels()
        periods = [struct.unpack("<H", p)[0] for c, p in b.sent(c2.STREAM)]
        self.assertEqual(periods, [300, 0, 300])
        b.dev.close()


class Commands(Base):
    def test_info_ping_battery(self):
        b = self.bench()
        info = b.dev.read_info()
        self.assertEqual((info.firmware, info.slot_version, info.uid), ("1.4.17", 2, fk.INVENTED_UID))
        self.assertEqual(b.dev.ping(b"abc"), b"abc")
        self.assertEqual(b.dev.battery(), c2.Battery(80, True))
        b.dev.keep_awake(True)
        self.assertEqual(b.sent(c2.KEEP_AWAKE)[0][1], b"\x01")
        b.dev.reset()
        self.assertEqual(b.meter.restarts, 1)

    def test_identify_accepts_the_fake(self):
        b = self.bench()
        info = base.identify(b.link, "2C53T")
        self.assertEqual(info.uid_hex, fk.INVENTED_UID.hex().upper())
        with self.assertRaises(base.IdentifyError):
            base.identify(fk.FakeLink(fk.FakeMeter(text="LC1020E V1.1")), "2C53T")

    def test_state_page_dmm(self):
        b = self.bench()
        s = b.dev.read_state()
        self.assertEqual((s.page_name, s.dmm.function, s.length), ("meter", 5, 49))
        self.assertEqual(b.dev.page(2).page_name, "scope")
        with self.assertRaises(ValueError):
            b.dev.page(4)
        self.assertEqual(b.dev.dmm_function(1).dmm.function, 1)
        self.assertEqual(b.sent(c2.DMM_FUNCTION)[0][1], b"\x01")
        self.assertTrue(b.dev.dmm_hold(True).flag("hold"))
        self.assertTrue(b.dev.dmm_relative(True).flag("relative"))
        self.assertFalse(b.dev.dmm_hold(False).flag("hold"))
        with self.assertRaises(ValueError):
            b.dev.dmm_function(13)

    def test_command_timeouts_come_from_the_table(self):
        b = self.bench()
        b.dev.dmm_function(1)
        b.dev.scope_timebase(3)
        b.dev.scope_channel(0, True, 4, -10, 1, 0, False)
        b.dev.scope_trigger(0, 1, 5, 1)
        b.dev.read_state()
        got = {c: t for c, _, t in b.link.requests}
        self.assertEqual((got[c2.DMM_FUNCTION], got[c2.SCOPE_TIMEBASE], got[c2.SCOPE_CHANNEL],
                          got[c2.SCOPE_TRIGGER], got[c2.STATE]), (3.0, 10.0, 30.0, 30.0, 0.5))

    def test_scope(self):
        b = self.bench()
        s = b.dev.scope_channel(1, True, 4, -10, 0, 1, True)
        self.assertEqual(b.sent(c2.SCOPE_CHANNEL)[0][1], struct.pack("<BBBbBBB", 1, 1, 4, -10, 0, 1, 1))
        self.assertEqual((s.ch2.enabled, s.ch2.vdiv, s.ch2.offset, s.ch2.coupling, s.ch2.probe, s.ch2.fft),
                         (True, 4, -10, 0, 1, True))
        s = b.dev.scope_trigger(1, 0, -50, 2)
        self.assertEqual(b.sent(c2.SCOPE_TRIGGER)[0][1], struct.pack("<BBhB", 1, 0, -50, 2))
        self.assertEqual((s.trigger.source, s.trigger.level, s.trigger.mode_name), (1, -50, "single"))
        self.assertEqual(b.dev.scope_timebase(28).timebase, 28)
        for fn in (lambda: b.dev.scope_timebase(29), lambda: b.dev.scope_channel(2, 1, 0, 0, 0, 0, 0),
                   lambda: b.dev.scope_channel(0, 1, 10, 0, 0, 0, 0),
                   lambda: b.dev.scope_channel(0, 1, 0, 101, 0, 0, 0),
                   lambda: b.dev.scope_trigger(0, 0, 101, 0), lambda: b.dev.scope_trigger(0, 0, 0, 3),
                   lambda: b.dev.scope_wave(2)):
            with self.assertRaises(ValueError):
                fn()
        for fn in (b.dev.scope_autoset, b.dev.scope_autocorrect):
            self.assertIsInstance(fn(), c2.State)
        self.assertIsInstance(b.dev.scope_run(True), c2.State)
        self.assertEqual(b.sent(c2.SCOPE_RUN)[0][1], b"\x01")
        self.assertEqual(b.dev.scope_tables().timebase[28], "TB28")
        w = b.dev.scope_wave(1)
        self.assertEqual((w.channel, len(w.samples)), (1, 301))

    def test_generator(self):
        b = self.bench()
        s = b.dev.gen_set(True, 3, 2500, 40, 12)
        self.assertEqual(b.sent(c2.GEN_SET)[0][1], struct.pack("<BBIBB", 1, 3, 2500, 40, 12))
        self.assertEqual((s.gen.on, s.gen.frequency, s.gen.amplitude), (True, 2500, 12))
        for bad in ((True, 13, 100, 50, 10), (True, 0, 0, 50, 10), (True, 0, 50001, 50, 10),
                    (True, 0, 100, 101, 10), (True, 0, 100, 50, 0), (True, 0, 100, 50, 31)):
            with self.assertRaises(ValueError):
                b.dev.gen_set(*bad)
        self.assertEqual(len(b.sent(c2.GEN_SET)), 1)

    def test_diagnostics(self):
        b = self.bench()
        self.assertEqual(b.dev.mem_read(0x08000000, 600), bytes((0x08000000 + i) * 7 & 0xFF for i in range(600)))
        self.assertEqual(len(b.sent(c2.MEM_READ)), 3)                 # 256 + 256 + 88
        f = b.dev.fault_record()
        self.assertTrue(f.valid)
        self.assertEqual((f.exception, f.pc, f.sp), (3, 0x08010001, 0x20030000))
        for addr, n in ((0x20000000, 4), (0x08100000, 1), (0x080FFFFF, 2), (0x20037FAF, 2),
                        (0x20037FB0, 0x51), (0x08000000, 0)):
            with self.assertRaises(ValueError, msg=hex(addr)):
                b.dev.mem_read(addr, n)
        b.dev.mem_read(0x080FFFFF, 1)
        b.dev.mem_read(0x20037FB0, 0x50)
        self.assertEqual(b.dev.flash_read(0x200000, 300), bytes((0x200000 + i) & 0xFF for i in range(300)))
        for addr, n in ((16 << 20, 1), ((16 << 20) - 1, 2), (0, 0)):
            with self.assertRaises(ValueError):
                b.dev.flash_read(addr, n)
        self.assertEqual(b.dev.flash_id(), (0xEF, 0x40, 0x17))
        b.dev.text_colour(0xFF9A)
        self.assertEqual(b.meter.text_colour, 0xFF9A)
        with self.assertRaises(ValueError):
            b.dev.text_colour(0x10000)
        b.dev.meter_log_start()
        self.assertEqual(b.dev.meter_log_read(), bytes(range(40)))
        b.dev.meter_log_stop()
        b.dev.drive(True)
        self.assertEqual(b.sent(c2.DRIVE)[0][1], b"\x01")

    def test_status_error_passes_through(self):
        b = self.bench()
        b.meter._c_20 = lambda p: (4, b"")
        with self.assertRaises(base.StatusError) as cm:
            b.dev.dmm_function(1)
        self.assertEqual(cm.exception.status, 4)


# ----------------------------------------------------------------------------------------------
class Screenshot(Base):
    def test_pixels_and_image(self):
        b = self.bench()
        steps = []
        pix = b.dev.screenshot_pixels(lambda f, t: steps.append(f))
        self.assertEqual(len(pix), 320 * 240)
        for x, y in ((0, 0), (159, 0), (160, 0), (319, 239), (17, 100)):
            self.assertEqual(pix[y * 320 + x], fk.pixel(x, y), (x, y))
        self.assertEqual(steps[-1], 1.0)
        self.assertEqual(b.sent(c2.SCREENSHOT)[0][0], c2.SCREENSHOT)
        self.assertEqual(b.link.requests[0][2], 20.0)
        img = b.dev.screenshot()
        self.assertEqual((img.width(), img.height()), (320, 240))
        v = fk.pixel(17, 100)
        r, g, bl = v >> 11 & 31, v >> 5 & 63, v & 31
        c = img.pixelColor(17, 100)
        self.assertEqual((c.red(), c.green(), c.blue()), (r << 3 | r >> 2, g << 2 | g >> 4, bl << 3 | bl >> 2))
        self.assertEqual(b.link.exclusive_depth, 0)

    def test_missing_frames_are_captured_again(self):
        b = self.bench()
        b.meter.shot_drop_first = {0, 17, 479}
        pix = b.dev.screenshot_pixels()
        self.assertEqual(len(b.sent(c2.SCREENSHOT)), 2)
        self.assertEqual(pix[239 * 320 + 319], fk.pixel(319, 239))

    def test_gives_up(self):
        b = self.bench()
        orig = b.meter._c_60

        def always_short(p):
            st, d = orig(p)
            b.meter.pending.pop()
            return st, d
        b.meter._c_60 = always_short
        with self.assertRaises(base.DeviceError):
            b.dev.screenshot_pixels(attempts=2)
        self.assertEqual(len(b.sent(c2.SCREENSHOT)), 2)

    def test_unknown_format_is_refused(self):
        b = self.bench()
        b.meter._c_60 = lambda p: (0, struct.pack("<IHHB", 153600, 320, 240, 1))
        with self.assertRaises(base.DeviceError):
            b.dev.screenshot_pixels()


# ----------------------------------------------------------------------------------------------
class SerialLevel(Base):
    """The same fake through link.port.SerialLink and the real frame codec."""

    def open_link(self, meter):
        from fnirsi_companion.link.port import SerialLink
        link = SerialLink("fake", serial_factory=lambda port: fk.FakeSerial(meter))
        link.open()
        self.addCleanup(link.close)
        return link

    def test_identify_state_and_screenshot_over_frames(self):
        meter = fk.FakeMeter()
        link = self.open_link(meter)
        info = base.identify(link, "2C53T")
        dev = c2.C2C53T(link, info)
        self.addCleanup(dev.close)
        s = dev.read_state()
        self.assertEqual((s.length, s.dmm.status, s.dmm.exponent), (49, 0, 1))
        self.assertEqual(dev.read_info().uid, fk.INVENTED_UID)
        pix = dev.screenshot_pixels()
        self.assertEqual(pix[5 * 320 + 200], fk.pixel(200, 5))
        self.assertEqual(dev.file_read("2:/README.TXT"), b"hello")
        self.assertEqual(sorted(e.name for e in dev.file_list("2:/")), ["BOOTINST.TXT", "README.TXT"])

    def test_old_44_byte_firmware_over_frames(self):
        meter = fk.FakeMeter()
        meter.state_length = 44
        link = self.open_link(meter)
        dev = c2.C2C53T(link, base.identify(link, "2C53T"))
        self.addCleanup(dev.close)
        s = dev.read_state()
        self.assertEqual(s.length, 44)
        self.assertEqual(c2.state_to_reading(s, 0.0, 0.0).validity, "unknown")


# ----------------------------------------------------------------------------------------------
class ReviewRound1(Base):
    """The fixes of review_r1.md that concern the 2C53T module."""

    def hook(self, b, fn):
        """fn(cmd, payload) runs before the fake meter handles each request."""
        orig = b.meter.handle

        def handle(cmd, payload):
            fn(cmd, payload)
            return orig(cmd, payload)
        b.meter.handle = handle

    # finding 4
    def test_key_1_refused_for_every_payload_type(self):
        b = self.bench()
        for p in ([1], (1,), bytearray(b"\x01"), memoryview(b"\x01"), b"\x01", [1, 0]):
            with self.subTest(p=p), self.assertRaises(base.SafetyRefused):
                b.dev.call(c2.KEY, p)
        self.assertEqual(b.meter.log, [])
        with self.assertRaises(TypeError):
            b.dev.call(c2.KEY, 1)                       # bytes(1) would be b"\x00"
        b.dev.call(c2.KEY, [15])
        self.assertEqual(b.meter.keys, [15])
        self.assertEqual(b.link.requests[-1][1], b"\x0f")

    # finding 6
    def test_page_key_gen_set_wait_the_stated_500_ms(self):
        b = self.bench()
        b.dev.key(15)
        b.dev.page(1)
        b.dev.gen_set(True, 0, 50000, 50, 30)
        self.assertEqual([(c, t) for c, _p, t in b.link.requests],
                         [(c2.KEY, 0.5), (c2.PAGE, 0.5), (c2.GEN_SET, 0.5)])

    # finding 9
    def test_failed_info_after_restart_closes_the_new_link(self):
        b = self.bench()

        class Mute(fk.FakeLink):
            def request(self, cmd, payload=b"", timeout=1.5):
                if cmd == c2.INFO:
                    raise base.AnswerTimeout("no answer to INFO")
                return super().request(cmd, payload, timeout)
        made = []

        def rc(uid_hex, timeout):
            made.append(Mute(b.meter))
            return made[-1]
        b.dev._reconnect = rc
        with self.assertRaises(base.AnswerTimeout):
            b.dev.settings_save("OK")
        self.assertTrue(made[0].closed)
        self.assertIs(b.dev.link, b.link)

    def test_other_uid_after_restart_closes_the_new_link(self):
        b = self.bench()
        made = []

        def rc(uid_hex, timeout):
            b.meter.uid = fk.OTHER_UID
            made.append(fk.FakeLink(b.meter))
            return made[-1]
        b.dev._reconnect = rc
        with self.assertRaises(c2.BackupMismatch):
            b.dev.update_app(Update.image_file(None, b), confirm="OK")
        self.assertTrue(made[0].closed)

    # finding 12
    def test_backup_cancelled_between_requests_writes_nothing(self):
        b = self.bench()
        token = base.CancelToken()
        n = []

        def on(cmd, payload):
            if cmd == c2.CAL_READ:
                n.append(1)
                if len(n) == 2:
                    self.assertTrue(token.request())
        self.hook(b, on)
        with base.cancel_scope(token), self.assertRaises(base.Cancelled):
            b.dev.backup(b.tmp / "bk")
        self.assertEqual(len(n), 2)
        self.assertEqual(b.backups(), [])
        self.assertFalse(token.allowed)

    def test_cancel_during_the_backup_of_a_save_sends_no_save(self):
        b = self.bench()
        token = base.CancelToken()
        self.hook(b, lambda cmd, p: cmd == c2.CAL_READ and token.request())
        with base.cancel_scope(token), self.assertRaises(base.Cancelled):
            b.dev.settings_save("OK")
        self.assertNotIn(c2.SETTINGS_SAVE, b.meter.cmds())
        self.assertEqual(b.meter.restarts, 0)

    def test_staging_cancelled_then_the_next_fw_begin_starts_again(self):
        b = self.bench()
        img = Update.image_file(None, b)
        token = base.CancelToken()
        self.hook(b, lambda cmd, p: cmd == c2.FW_DATA and token.request())
        with base.cancel_scope(token), self.assertRaises(base.Cancelled):
            b.dev.update_app(img, confirm="OK")
        cmds = [c for c, _ in b.meter.log if c >= 0x80]
        self.assertEqual(cmds, [c2.FW_BEGIN, c2.FW_DATA])          # stopped before the 2nd chunk
        self.assertIsNone(b.meter.installed)
        del b.meter.handle                                          # the plain fake again
        b.dev.update_app(img, confirm="OK")                         # FW_BEGIN replaces the file
        self.assertEqual(b.meter.installed, img.read_bytes())

    def test_no_cancel_once_fw_end_is_sent(self):
        b = self.bench()
        token = base.CancelToken()
        answers = {}

        def on(cmd, payload):
            if cmd in (c2.FW_END, c2.FW_INSTALL):
                answers[cmd] = token.request()
        self.hook(b, on)
        with base.cancel_scope(token):
            b.dev.update_app(Update.image_file(None, b), confirm="OK")
        self.assertEqual(answers, {c2.FW_END: False, c2.FW_INSTALL: False})
        self.assertIsNotNone(b.meter.installed)

    def test_screenshot_cancelled_between_attempts(self):
        b = self.bench()
        token = base.CancelToken()
        orig = b.meter.handle

        def handle(cmd, payload):
            out = orig(cmd, payload)
            if cmd == c2.SCREENSHOT:
                b.meter.pending = b.meter.pending[1:]     # one frame missing: a second attempt
                token.request()
            return out
        b.meter.handle = handle
        with base.cancel_scope(token), self.assertRaises(base.Cancelled):
            b.dev.screenshot_pixels()
        self.assertEqual(b.meter.cmds().count(c2.SCREENSHOT), 1)


if __name__ == "__main__":
    unittest.main()
