"""Tests of devices/base.py and devices/lc1020e.py against the fake LC1020E (tests/fake_lc1020e.py).
No serial port is opened; the UIDs are invented. The command table is checked against the
command tables parsed from PROTOCOL.md of fnirsi-lc1020e-firmware-rewrite (sibling checkout, or
the file named by the environment variable FNIRSI_LC1020E_PROTOCOL)."""

import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from fnirsi_companion.devices import base, lc1020e as lc          # noqa: E402
import fake_lc1020e as fk                                           # noqa: E402


def spec_path():
    env = os.environ.get("FNIRSI_LC1020E_PROTOCOL")
    p = Path(env) if env else HERE.parents[2] / "LC1020E" / "fnirsi-lc1020e-firmware-rewrite" / "PROTOCOL.md"
    return p if p.is_file() else None


@dataclass
class Drive:
    model: str
    path: str
    status: str = "ready"


class FakeClock:
    """A clock that advances by `step` seconds per reading (drive time-outs without waiting)."""

    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class Bench:
    """A FakeMeter, a FakeLink and an LC1020E on it, with a temporary backup folder, a fake
    update drive and a reconnect function that hands out a new link to the same meter."""

    def __init__(self, tc, **meter_kw):
        self.tmp = Path(tempfile.mkdtemp())
        tc.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.meter = fk.FakeMeter(**meter_kw)
        self.link = fk.FakeLink(self.meter)
        self.t0 = datetime(2026, 11, 21, 10, 0, 0)
        self.ticks = 0
        self.drive_dir = self.tmp / "vol" / "LC1020E-IAP"
        self.drive_up = False
        self.apply_result = 1
        self.links = [self.link]
        self.meter.on_update_mode = self._drive_appears
        self.dev = lc.LC1020E(self.link, base.Info.parse(self.meter.info()),
                              backup_folder=self.tmp / "bk", reconnect=self.reconnect,
                              sleep=lambda s: None, clock=FakeClock(), now=self.now)

    def now(self):
        self.ticks += 1
        return self.t0 + timedelta(seconds=self.ticks)

    def _drive_appears(self):
        self.drive_dir.mkdir(parents=True)
        (self.drive_dir / "READY.TXT").write_text("ready")
        self.drive_up = True

    def drives(self):
        out = [Drive("2C53T", str(self.tmp / "other"))]
        if self.drive_up:
            out.append(Drive("LC1020E", str(self.drive_dir)))
        return out

    def reconnect(self, uid_hex, timeout):
        assert uid_hex == self.meter.uid.hex().upper()
        f = self.drive_dir / lc.DRIVE_FILE
        if self.meter.in_update_mode and f.exists():
            self.meter.apply_uf2(f.read_bytes(), self.apply_result)
        link = fk.FakeLink(self.meter)
        self.links.append(link)
        return link

    def sent(self, code=None):
        return [r for r in self.meter.log if code is None or r[0] == code]


class SpecTable(unittest.TestCase):
    """The command table against PROTOCOL.md."""

    @classmethod
    def setUpClass(cls):
        p = spec_path()
        if p is None:
            raise unittest.SkipTest("PROTOCOL.md of the LC1020E repository not found")
        text = p.read_text(encoding="utf-8")
        sec = text[text.index("## 5. Commands"):text.index("## 6. Records")]
        cls.spec = {}
        for m in re.finditer(r"^\| 0x([0-9A-Fa-f]{2}) \| \(?([A-Z0-9_]+)\)? \|", sec, re.M):
            cls.spec[int(m.group(1), 16)] = m.group(2)
        cls.text = text

    def test_every_spec_command_is_in_the_table(self):
        self.assertGreaterEqual(len(self.spec), 36)
        for code, name in self.spec.items():
            self.assertIn(code, lc.COMMANDS, "0x%02X %s missing" % (code, name))
            self.assertEqual(lc.COMMANDS[code].name, name)

    def test_table_has_no_command_the_spec_lacks(self):
        # PROTOCOL.md does not carry the own calibration area (OCD section 4) yet: 0x30 .. 0x37
        self.assertEqual(sorted(lc.COMMANDS), sorted(set(self.spec) | set(lc.OCAL_COMMANDS)))

    def test_capability_bits_section_14(self):
        sec = self.text[self.text.index("## 14."):self.text.index("## 15.")]
        for bit in (0, 7, 10, 14, 19):
            self.assertRegex(sec, r"\b%d [A-Z]" % bit)
        self.assertIn("bit 20 (SETUP_LANGUAGE)", sec)
        expect = {
            0: ["STATE", "STREAM"], 1: ["SCREEN"], 2: ["KEY"], 3: ["LCR_TABLES", "LCR_SET"],
            4: ["MEASURE", "MEASURE_STREAM"], 5: ["COMPARE", "COMPARE_SET"], 6: ["HOLD"],
            7: ["CALIBRATE"], 8: ["SETTINGS_TABLES", "SETTINGS_SET", "SETTINGS_SAVE"],
            9: ["CAL_READ"], 10: ["SCREENSHOT"], 11: ["MEM_READ"], 13: ["UPDATE_MODE"],
            14: ["BOOT_BEGIN", "BOOT_DATA", "BOOT_END", "BOOT_INSTALL", "BOOT_STATUS"],
            15: ["BATTERY"], 16: ["POWER_OFF"], 17: ["RESTORE_DEFAULTS"],
            18: ["SETTINGS_RESTORE_B", "SETTINGS_ACCEPT_A", "SETTINGS_ACCEPT_B",
                 "SETTINGS_ACCEPT_DEFAULTS", "SETTINGS_WRITE_BLOCK"],
            19: ["FACTORY_MODE"], 20: ["SETUP_LANGUAGE"], 23: ["TOLERANCE_SET"],
            26: ["OCAL_INFO", "OCAL_READ", "OCAL_WRITE", "OCAL_ROLLBACK", "OCAL_LOCK", "OCAL_RAW",
                 "OCAL_TRACE", "MONITOR"],
        }
        by_name = {c.name: c for c in lc.COMMANDS.values()}
        seen = set()
        for bit, names in expect.items():
            for n in names:
                self.assertEqual(by_name[n].cap_bit, bit, n)
                seen.add(n)
        for n in ("INFO", "PING", "RESET"):
            self.assertIsNone(by_name[n].cap_bit)
            seen.add(n)
        self.assertEqual(seen, set(by_name))

    def test_timeouts_section_4_3(self):
        t = {c.name: c.timeout for c in lc.COMMANDS.values()}
        for n in ("SETTINGS_SAVE", "RESTORE_DEFAULTS", "SETTINGS_RESTORE_B", "SETTINGS_ACCEPT_A",
                  "SETTINGS_ACCEPT_B", "SETTINGS_ACCEPT_DEFAULTS", "SETTINGS_WRITE_BLOCK",
                  "BOOT_INSTALL"):
            self.assertEqual(t[n], 4.0, n)
        for n in ("SCREEN", "LCR_SET", "HOLD", "SETTINGS_SET", "KEY"):
            self.assertEqual(t[n], 23.0, n)
        self.assertEqual(t["COMPARE_SET"], 63.0)
        self.assertEqual(t["CALIBRATE"], 126.0)
        self.assertEqual(t["BOOT_BEGIN"], 13.0)
        for n in ("INFO", "PING", "STATE", "MEASURE", "CAL_READ", "SCREENSHOT", "BOOT_DATA"):
            self.assertEqual(t[n], 1.5, n)
        for row in ("| every command not listed here | answered in the pass of the request | 1.5 s |",
                    "| SCREEN, LCR_SET, HOLD, SETTINGS_SET, COMPARE_SET | direct (bit 21)",
                    "4 s with bit 21 set; without it (older firmware: jobs of key presses) 23 s, "
                    "COMPARE_SET 63 s |",
                    "| CALIBRATE | job, 360 000 ms with capability bit 25",
                    "120 000 ms without it | 366 s with bit 25, 126 s without it |"):
            self.assertIn(row, self.text)
        sec = self.text[self.text.index("## 14."):self.text.index("## 15.")]
        self.assertIn("21 direct set", sec)              # lc.CAP_DIRECT_SET
        self.assertEqual(lc.CAP_DIRECT_SET, 21)


class Parsers(unittest.TestCase):
    def test_info(self):
        i = base.Info.parse(fk.build_info(update_result=1))
        self.assertEqual((i.protocol, i.capabilities, i.firmware, i.uid, i.running_slot,
                          i.slot_version, i.app_size, i.update_result),
                         (1, 0xFFFFF, "LC1020E V1.1 OWN", fk.INVENTED_UID, 1, 2, 0x1F000, 1))
        self.assertEqual(i.uid_hex, "A1B2C3D4E5F60718293A4B5C")
        self.assertTrue(i.has(19) and not i.has(20))
        with self.assertRaises(base.DeviceError):
            base.Info.parse(b"\0" * 45)

    def test_state(self):
        s = lc.parse_state(fk.build_state(screen=2, flags=0x0006, frequency=10000, level=0.3,
                                          dropped=9, time=777))
        self.assertEqual((s.screen, s.frequency, s.dropped, s.time), (2, 10000, 9, 777))
        self.assertAlmostEqual(s.level, 0.3, 5)
        self.assertTrue(s.flag("compare screen") and s.flag("DTR") and not s.flag("hold"))
        self.assertEqual(len(s.raw), 32)
        with self.assertRaises(base.DeviceError):
            lc.parse_state(b"\0" * 31)

    def test_measure(self):
        m = lc.parse_measure(fk.build_measure(result=42, parallel=1, frequency=100000, rng=3))
        self.assertEqual((m.result, m.main_row, m.secondary_kind, m.parallel, m.frequency,
                          m.range), (42, 2, 2, 1, 100000, 3))
        self.assertEqual((m.main.unit, m.main.decimals, m.main.validity, m.main.text),
                         (5, 2, 0, "100.00"))
        self.assertAlmostEqual(m.main.value, 100.0)
        self.assertAlmostEqual(m.z, 1591.5, 2)
        with self.assertRaises(base.DeviceError):
            lc.parse_measure(b"\0" * 63)

    def test_compare_battery(self):
        c = lc.parse_compare(fk.build_compare(nominal=1000.0, passed=9, failed=2))
        self.assertEqual((c.quantity, c.passed, c.failed, c.warn_led), (2, 9, 2, 1))
        self.assertAlmostEqual(c.nominal, 1000.0)
        b = lc.parse_battery(struct.pack("<fBB", 3.9, 3, 0))
        self.assertEqual((round(b.volts, 2), b.level, b.low_popup), (3.9, 3, 0))

    def test_lcr_list_types(self):
        idx = lc.parse_lcr_list(fk.build_lcr_list(1, 0, "Secondary", "", [(0, "R"), (4, "theta")]))
        self.assertEqual((idx.id, idx.value_type, idx.name, idx.entries), (1, 0, "Secondary",
                                                                          [(0, "R"), (4, "theta")]))
        fq = lc.parse_lcr_list(fk.build_lcr_list(2, 1, "FREQ", "Hz", [(100, "100Hz")]))
        self.assertEqual((fq.unit, fq.entries), ("Hz", [(100, "100Hz")]))
        lv = lc.parse_lcr_list(fk.build_lcr_list(3, 2, "AMP", "V", [(0.6, "0.6V")]))
        self.assertAlmostEqual(lv.entries[0][0], 0.6, 5)

    def test_lcr_list_cut_text(self):
        d = fk.build_lcr_list(0, 0, "Func", "", [(0, "AUTO")])[:-1]       # last zero cut (8)
        self.assertEqual(lc.parse_lcr_list(d).entries, [(0, "AUTO")])

    def test_setting(self):
        s = lc.parse_setting(fk.build_setting(2, 0, 1, 0, 3, 1, "Off Time", ["NO", "5min"]))
        self.assertEqual((s.id, s.kind, s.current, s.min, s.max, s.step, s.name, s.choices),
                         (2, 0, 1, 0, 3, 1, "Off Time", ["NO", "5min"]))

    def test_boot_status(self):
        d = fk.build_boot_status([(2, 0x2000, 7, 1), (3, 0x2100, 8, 1), (0xFFFFFFFF, 0, 0, 0)],
                                 1, 3, 17, 1, 2)
        b = lc.parse_boot_status(d)
        self.assertEqual(len(d), 47)
        self.assertEqual((b.slots[1].version, b.slots[2].ok, b.newest_slot, b.newest_state,
                          b.newest_sequence, b.next_slot, b.free_slot), (3, 0, 1, 3, 17, 1, 2))

    def test_save_cal_update_answers(self):
        r = lc.parse_save_answer(0, struct.pack("<II", 0x44 << 8 | 1 << 16, 0x80))
        self.assertTrue(r.restart)
        self.assertEqual(r.result, 0)
        self.assertIn("step verify, copy A", r.describe())
        self.assertFalse(lc.parse_save_answer(0, struct.pack("<II", 0, 0)).restart)
        self.assertTrue(lc.parse_save_answer(0, struct.pack("<II", 0, 0), True).restart)
        self.assertFalse(lc.parse_save_answer(7, struct.pack("<II", 7, 0), True).restart)
        c = lc.parse_cal_answer(0, struct.pack("<BBI", 2, 11, 0x10000))
        self.assertEqual((c.cal_status, c.cal_type, c.save_status), (2, 11, 0x10000))
        u = lc.parse_update_mode(0, struct.pack("<BIB", 1, 2, 0))
        self.assertEqual((u.next_slot, u.next_version, u.running_slot), (1, 2, 0))

    def test_events(self):
        e = lc.parse_event(0x90, struct.pack("<HBB", 0x0400, 1, 1), 5)
        self.assertEqual((e.name, e.fields["mask"], e.fields["source"], e.seq), ("KEY", 0x400,
                                                                                "injected", 5))
        self.assertEqual(lc.parse_event(0x91, bytes([2, 1])).fields, dict(screen=2, previous=1))
        self.assertEqual(lc.parse_event(0x93, struct.pack("<HI", 0xFFFE, 9)).fields,
                         dict(id=0xFFFE, value=9))
        self.assertEqual(lc.parse_event(0x94, b"").fields, {})
        self.assertEqual(lc.parse_event(0x95, struct.pack("<I", 8)).fields, dict(result=8))
        self.assertEqual(lc.parse_event(0x96, struct.pack("<II", 1, 2)).fields,
                         dict(save_status=1, flags=2))
        self.assertEqual(lc.parse_event(0x97, bytes([1, 2, 11])).fields,
                         dict(status=1, step=2, type=11))
        self.assertTrue(lc.parse_event(0x91, b"\x02").fields["malformed"])
        self.assertEqual(lc.parse_event(0x7E, b"ab").name, "0x7E")

    def test_screenshot_header(self):
        h = lc.parse_screenshot_header(struct.pack("<HHBB", 320, 240, 0, 24))
        self.assertEqual((h.width, h.height, h.format, h.bands), (320, 240, 0, 24))


class FileChecks(unittest.TestCase):
    def test_copy_valid_and_vendor(self):
        ok, why = lc.check_copy(fk.build_copy(5, 1))
        self.assertTrue(ok)
        self.assertIn("sequence 5", why)
        vendor = bytearray(fk.build_copy(5, 1))
        vendor[0xFF8:0x1000] = b"\xFF" * 8
        self.assertEqual(lc.check_copy(bytes(vendor)), (True, "valid, vendor format (no trailer)"))

    def test_copy_invalid(self):
        c = bytearray(fk.build_copy(5, 1))
        c[100] ^= 1
        ok, why = lc.check_copy(bytes(c))
        self.assertFalse(ok)
        self.assertIn("trailer CRC-32 wrong", why)
        c = bytearray(fk.build_copy(5, 1))
        c[0] = 0
        self.assertIn("markers missing", lc.check_copy(bytes(c))[1])
        self.assertFalse(lc.check_copy(b"\0" * 10)[0])

    def test_uf2(self):
        self.assertEqual(lc.check_uf2(fk.build_uf2(4)), 4)
        with self.assertRaisesRegex(base.DeviceError, "family"):
            lc.check_uf2(fk.build_uf2(2, family=0xE48BFF56))
        with self.assertRaisesRegex(base.DeviceError, "outside the application area"):
            lc.check_uf2(fk.build_uf2(1, base_addr=0x08062000))
        with self.assertRaisesRegex(base.DeviceError, "multiple of 512"):
            lc.check_uf2(fk.build_uf2(1)[:-1])
        with self.assertRaisesRegex(base.DeviceError, "multiple of 512"):
            lc.check_uf2(b"")
        bad = bytearray(fk.build_uf2(1))
        bad[0] ^= 1
        with self.assertRaisesRegex(base.DeviceError, "not a UF2 block"):
            lc.check_uf2(bytes(bad))

    def test_slot_file(self):
        base_a = lc.SLOT_BASE[1]
        f = fk.build_slot_file(base_a, version=3)
        self.assertEqual(lc.check_slot_file(f, base_a), 3)
        with self.assertRaisesRegex(base.DeviceError, "linked for"):
            lc.check_slot_file(f, lc.SLOT_BASE[2])
        bad = bytearray(f)
        bad[0x200 + 20] ^= 1
        with self.assertRaisesRegex(base.DeviceError, "image CRC-32"):
            lc.check_slot_file(bytes(bad), base_a)
        bad = bytearray(f)
        bad[8] ^= 1
        with self.assertRaisesRegex(base.DeviceError, "header CRC-32"):
            lc.check_slot_file(bytes(bad), base_a)
        with self.assertRaisesRegex(base.DeviceError, "must be"):
            lc.check_slot_file(f[:0x100], base_a)
        bad = bytearray(f)
        bad[0] ^= 1
        with self.assertRaisesRegex(base.DeviceError, "SLOT"):
            lc.check_slot_file(bytes(bad), base_a)


class ReadingConversion(unittest.TestCase):
    def conv(self, **kw):
        return lc.measure_to_reading(lc.parse_measure(fk.build_measure(**kw)), 3.0, 99.0)

    def test_capacitance_nf_to_farad(self):
        r = self.conv()
        self.assertEqual((r.quantity, r.unit, r.decimals, r.validity, r.t, r.wall, r.meter_ms),
                         ("C", "F", 2, "valid", 3.0, 99.0, 5000))
        self.assertAlmostEqual(r.value, 100e-9, 15)
        self.assertEqual(r.text, "100.00 nF")
        s = r.secondary
        self.assertEqual((s.quantity, s.unit, s.text, s.secondary), ("D", "", "0.0012", None))
        self.assertAlmostEqual(s.value, 0.0012, 6)

    def test_inductance_uh_to_henry(self):
        r = self.conv(main_row=3, main=fk.build_value(47.0, 9, 1, 0, "0.0470"),
                      secondary_kind=3, secondary=fk.build_value(12.5, 0, 2, 0, "12.50"))
        self.assertEqual((r.quantity, r.unit, r.text), ("L", "H", "0.0470 mH"))
        self.assertAlmostEqual(r.value, 47e-6, 12)
        self.assertEqual(r.secondary.quantity, "Q")

    def test_resistance_impedance_theta(self):
        r = self.conv(main_row=1, main=fk.build_value(4700.0, 2, 3, 0, "4.700"),
                      secondary_kind=4, secondary=fk.build_value(-45.5, 11, 1, 0, "-45.5"))
        self.assertEqual((r.quantity, r.unit, r.value, r.text), ("R", "Ω", 4700.0, "4.700 kΩ"))
        self.assertEqual((r.secondary.quantity, r.secondary.unit, r.secondary.text),
                         ("theta", "°", "-45.5 °"))
        z = self.conv(main_row=4, main=fk.build_value(50.0, 1, 2, 0, "50.00"))
        self.assertEqual((z.quantity, z.unit), ("Z", "Ω"))

    def test_validity(self):
        over = self.conv(main=fk.build_value(0.0, 5, 0, 1, "0L"))
        self.assertEqual((over.validity, over.value, over.text), ("overload", None, "0L"))
        none = self.conv(main=fk.build_value(0.0, 5, 0, 2, ""))
        self.assertEqual((none.validity, none.value), ("no reading", None))
        held = self.conv(main=fk.build_value(10.0, 5, 1, 3, "10.0"))
        self.assertEqual(held.validity, "held")
        self.assertAlmostEqual(held.value, 10e-9, 15)
        unk = self.conv(main=fk.build_value(10.0, 5, 1, 9, "10.0"))
        self.assertEqual((unk.validity, unk.value), ("unknown", None))
        nan = self.conv(main=fk.build_value(float("nan"), 5, 1, 0, "x"))
        self.assertIsNone(nan.value)

    def test_unknown_main_row(self):
        r = self.conv(main_row=0, main=fk.build_value(0.0, 0, 0, 0, ""))
        self.assertEqual((r.quantity, r.value, r.unit), ("", None, ""))

    def test_key_restarts_on_quantity_or_conditions_not_on_value(self):
        a = self.conv()
        same = self.conv(result=8, time=6000, main=fk.build_value(101.0, 5, 2, 0, "101.00"))
        self.assertEqual(a.key, same.key)
        self.assertNotEqual(a.key, self.conv(main_row=3).key)               # quantity
        self.assertNotEqual(a.key, self.conv(secondary_kind=3).key)         # secondary row
        self.assertNotEqual(a.key, self.conv(rng=2).key)                    # range
        self.assertNotEqual(a.key, self.conv(frequency=10000).key)
        self.assertNotEqual(a.key, self.conv(level=0.3).key)
        self.assertNotEqual(a.key, self.conv(bias=0.5).key)
        self.assertNotEqual(a.key, self.conv(circuit=2).key)
        self.assertNotEqual(a.key, self.conv(parallel=1).key)
        self.assertNotEqual(a.key, a.secondary.key)


class Identify(unittest.TestCase):
    def link(self, **kw):
        return fk.FakeLink(fk.FakeMeter(**kw))

    def test_lc1020e(self):
        i = base.identify(self.link(), "LC1020E")
        self.assertEqual(i.firmware, "LC1020E V1.1 OWN")

    def test_protocol_version(self):
        m = fk.FakeMeter()
        m.info = lambda: fk.build_info(protocol=2)
        with self.assertRaisesRegex(base.IdentifyError, "protocol version 2"):
            base.identify(fk.FakeLink(m), "LC1020E")

    def test_text_rules(self):
        for text, model, ok in (("2.5.1", "2C53T", True), ("02.05.01", "2C53T", True),
                                ("2.5", "2C53T", False), ("2.5.1a", "2C53T", False),
                                ("V2.5.1", "2C53T", False), ("2.5.1", "LC1020E", False),
                                ("LC1020E V1.1", "2C53T", False), ("LC1020E", "LC1020E", True),
                                ("lc1020e", "LC1020E", False)):
            link = self.link(text=text)
            if ok:
                base.identify(link, model)
            else:
                with self.assertRaises(base.IdentifyError, msg=(text, model)):
                    base.identify(link, model)

    def test_answers_as(self):
        with self.assertRaises(base.IdentifyError) as c:
            base.identify(self.link(text="2.5.1"), "LC1020E")
        self.assertEqual(c.exception.answers_as, "2C53T")
        with self.assertRaises(base.IdentifyError) as c:
            base.identify(self.link(), "2C53T")
        self.assertEqual(c.exception.answers_as, "LC1020E")
        with self.assertRaises(ValueError):
            base.identify(self.link(), "X")


class CallAndCapability(unittest.TestCase):
    def test_has_and_call_timeouts(self):
        b = Bench(self)
        self.assertTrue(b.dev.has(lc.SCREENSHOT))
        self.assertFalse(b.dev.has(lc.SETUP_LANGUAGE))
        self.assertFalse(b.dev.has(0x7F))
        b.dev.call(lc.STATE)
        self.assertEqual(b.link.requests[-1], (lc.STATE, b"", 1.5))
        b.dev.call(lc.COMPARE_SET, struct.pack("<ffBBB", 1.0, 1.0, 0, 0, 0))
        self.assertEqual(b.link.requests[-1][2], 63.0)

    def test_key_timeout_adds_hold(self):
        b = Bench(self)
        b.dev.key(3, 1500)
        self.assertEqual(b.link.requests[-1], (lc.KEY, struct.pack("<BH", 3, 1500), 24.5))
        b.dev.key(3)
        self.assertEqual(b.link.requests[-1][2], 23.0)
        with self.assertRaises(ValueError):
            b.dev.key(14)

    def test_capability_refusal_sends_nothing(self):
        b = Bench(self, caps=fk.ALL_CAPS & ~(1 << 10) & ~(1 << 15))
        for fn in (b.dev.screenshot_pixels, b.dev.battery):
            with self.assertRaises(base.CapabilityError):
                fn()
        with self.assertRaises(base.CapabilityError):
            b.dev.call(lc.SETUP_LANGUAGE)
        self.assertEqual(b.link.requests, [])
        self.assertFalse(b.dev.has(lc.SCREENSHOT))

    def test_unknown_command(self):
        b = Bench(self)
        with self.assertRaises(base.UnknownCommand):
            b.dev.call(0x7F)

    def test_dangerous_refused_by_generic_call(self):
        b = Bench(self, caps=fk.OCAL_CAPS)
        for code, c in lc.COMMANDS.items():
            if c.dangerous:
                with self.assertRaises(base.SafetyRefused, msg=c.name):
                    b.dev.call(code, struct.pack("<H", lc.CONFIRM_OK))
        self.assertEqual(b.link.requests, [])
        self.assertTrue(lc.COMMANDS[lc.SETTINGS_SAVE].dangerous)
        self.assertFalse(lc.COMMANDS[lc.SETTINGS_SET].dangerous)


class RecordMethods(unittest.TestCase):
    def test_reads(self):
        b = Bench(self)
        d = b.dev
        self.assertEqual(d.read_info().firmware, "LC1020E V1.1 OWN")
        self.assertEqual(d.ping(b"abc"), b"abc")
        self.assertEqual(d.read_state().screen, 1)
        self.assertEqual(d.screen(3).screen, 3)
        self.assertEqual(d.hold(True).flags & 1, 1)
        self.assertEqual(d.lcr_table_count(), 8)
        self.assertEqual(d.lcr_tables(2).unit, "Hz")
        self.assertEqual(d.lcr_set(2, 1).main_row, 2)
        self.assertEqual(d.measure().result, 7)
        self.assertEqual(d.measure_reading().quantity, "C")
        self.assertEqual(d.compare().passed, 3)
        self.assertEqual(d.compare_set(220.0, 2.5, 1, 1, 0).warn_sound, 1)
        self.assertEqual(d.settings_count(), 6)
        self.assertEqual(d.settings_tables(2).choices[3], "30min")
        self.assertEqual(d.settings_set(0, 60).screen, 3)
        self.assertEqual(d.battery().level, 3)
        self.assertEqual(d.factory_mode(), 0)
        self.assertEqual(d.mem_read(0x08000000, 4), bytes([0, 1, 2, 3]))
        self.assertEqual(len(d.read_copy(1)), 0x1000)
        d.stream(100)
        d.measure_stream(0xFFFF)
        self.assertEqual(b.meter.measure_stream, 0xFFFF)
        self.assertEqual(b.link.requests[-2][:2], (lc.STREAM, struct.pack("<H", 100)))

    def test_cal_read_arguments_and_length(self):
        b = Bench(self)
        self.assertEqual(b.dev.cal_read(0, 0xF00, 256), b.meter.copies[0][0xF00:])
        for args in ((2, 0, 1), (0, 0, 0), (0, 0, 257), (0, 0xF01, 256), (0, -1, 4)):
            with self.assertRaises(ValueError):
                b.dev.cal_read(*args)
        b.meter.copies[0] = b.meter.copies[0]
        b.meter._c_53 = lambda p: (0, b"\0" * 10)
        with self.assertRaisesRegex(base.DeviceError, "10 bytes"):
            b.dev.cal_read(0, 0, 256)

    def test_job_error_status_carries_the_record(self):
        b = Bench(self)
        b.meter._c_13 = lambda p: (4, fk.build_state(screen=3))
        with self.assertRaises(base.StatusError) as c:
            b.dev.key(2)
        self.assertEqual(c.exception.status, 4)
        self.assertEqual(c.exception.record.screen, 3)

    def test_screen_state_reply_too_short_is_a_device_error(self):
        b = Bench(self)
        b.meter._c_12 = lambda p: (0, b"\0\0")
        with self.assertRaises(base.DeviceError):
            b.dev.screen(1)


class DirectSet(unittest.TestCase):
    """CAP_DIRECT_SET (bit 21): answer limit of the five set commands and the screen after
    SETTINGS_SET. Old firmware (bit clear) keeps 23 s / 63 s and screen 3."""
    FIVE = (lc.SCREEN, lc.LCR_SET, lc.HOLD, lc.SETTINGS_SET, lc.COMPARE_SET)

    def test_bit_and_limit_constants(self):
        self.assertEqual(lc.CAP_DIRECT_SET, 21)
        self.assertEqual(lc.T_DIRECT, 4.0)
        self.assertEqual(fk.DIRECT_CAPS, 0x002FFFFF)
        self.assertEqual(fk.ALL_CAPS, 0x000FFFFF)

    def test_old_firmware_timeouts(self):
        d = Bench(self).dev
        self.assertFalse(d.info.has(lc.CAP_DIRECT_SET))
        for code in self.FIVE:
            want = lc.T_COMPARE_SET if code == lc.COMPARE_SET else lc.T_JOB
            self.assertEqual(d.timeout_for(code, b"\0\0\0"), want, code)

    def test_new_firmware_timeouts(self):
        d = Bench(self, caps=fk.DIRECT_CAPS).dev
        self.assertTrue(d.info.has(lc.CAP_DIRECT_SET))
        for code in self.FIVE:
            self.assertEqual(d.timeout_for(code, b"\0\0\0"), lc.T_DIRECT, code)
        self.assertEqual(d.timeout_for(lc.KEY, struct.pack("<BH", 1, 500)), lc.T_JOB + 0.5)
        self.assertEqual(d.timeout_for(lc.CALIBRATE), lc.T_CALIBRATE)
        self.assertEqual(d.timeout_for(lc.SETTINGS_SAVE), lc.T_SAVE)
        self.assertEqual(d.timeout_for(lc.STATE), lc.T_FAST)

    def test_calibration_sweep_bit_25(self):
        # SL 4.3 / 5.3.3 / 14: bit 25 -> CALIBRATE waits 366 s, answers 8 bytes, step counts windows
        self.assertEqual(lc.CAP_CAL_SWEEP, 25)
        self.assertEqual(Bench(self, caps=fk.NEW_CAPS | 1 << 25).dev.timeout_for(lc.CALIBRATE),
                         lc.T_CALIBRATE_SWEEP)
        self.assertEqual(lc.T_CALIBRATE_SWEEP, 366.0)
        self.assertEqual(Bench(self, caps=fk.NEW_CAPS).dev.timeout_for(lc.CALIBRATE), lc.T_CALIBRATE)
        c = lc.parse_cal_answer(3, struct.pack("<BBIBB", 3, 11, 0, 4, 3))
        self.assertEqual((c.cal_status, c.cal_type, c.point, c.reason), (3, 11, 4, 3))
        self.assertIn("ended at point 5/15: an open point below 100 kohm", c.describe())
        ok = lc.parse_cal_answer(0, struct.pack("<BBIBB", 2, 0, 0, 0xFF, 0))
        self.assertNotIn("ended at", ok.describe())
        old = lc.parse_cal_answer(0, struct.pack("<BBI", 2, 0, 0))
        self.assertIsNone(old.point)
        self.assertEqual(lc.cal_progress({"status": 1, "step": 7, "type": 11}, True),
                         "status busy, point 3/15, 1 of 3 windows, type 11")
        self.assertEqual(lc.cal_progress({"status": 1, "step": 45, "type": 11}, True),
                         "status busy, storing, type 11")
        self.assertEqual(lc.cal_progress({"status": 1, "step": 7, "type": 11}, False),
                         "status busy, step 7 s, type 11")

    def test_old_firmware_settings_set_goes_to_screen_3(self):
        d = Bench(self).dev
        self.assertEqual(d.screen(2).screen, 2)
        self.assertEqual(d.settings_set(0, 60).screen, 3)

    def test_new_firmware_screen_stays(self):
        d = Bench(self, caps=fk.DIRECT_CAPS).dev
        self.assertEqual(d.screen(2).screen, 2)
        self.assertEqual(d.settings_set(0, 60).screen, 2)
        d.compare_set(220.0, 2.5, 1, 1, 0)
        self.assertEqual(d.read_state().screen, 2)


class NewFirmwareFeatures(unittest.TestCase):
    """Capability bits 22 (60 Hz), 23 (tolerance modes), 24 (MEASURE markers, save result 9);
    older firmware (records of 64 and 29 bytes) keeps working."""

    def test_bits_and_caps_word(self):
        self.assertEqual((lc.CAP_FREQ_60HZ, lc.CAP_TOLERANCE, lc.CAP_MEASURE_MARKER), (22, 23, 24))
        self.assertEqual(fk.NEW_CAPS, 0x01EFFFFF)
        self.assertEqual(lc.COMMANDS[lc.TOLERANCE_SET].cap_bit, lc.CAP_TOLERANCE)
        self.assertEqual(lc.TOLERANCE_SET, 0x28)
        self.assertEqual(lc.COMMANDS[lc.TOLERANCE_SET].timeout, lc.T_FAST)
        d = Bench(self, caps=fk.NEW_CAPS).dev
        self.assertEqual(d.timeout_for(lc.TOLERANCE_SET), lc.T_FAST)

    def test_measure_65_and_64_bytes(self):
        m = lc.parse_measure(fk.build_measure(markers=0x02))
        self.assertEqual((len(m.raw), m.markers, m.secondary_unspecified), (65, 2, True))
        self.assertFalse(lc.parse_measure(fk.build_measure(markers=0x01)).secondary_unspecified)
        old = lc.parse_measure(fk.build_measure())
        self.assertEqual((len(old.raw), old.markers, old.secondary_unspecified), (64, 0, False))
        with self.assertRaises(base.DeviceError):
            lc.parse_measure(fk.build_measure()[:63])

    def test_signed_and_overload_texts_and_marker_reading(self):
        neg = fk.build_value(-1.0036, 1, 4, 0, "-1.0036")
        m = lc.parse_measure(fk.build_measure(secondary_kind=0, secondary=neg, markers=2))
        r = lc.measure_to_reading(m, 1.0, 2.0)
        self.assertEqual(r.secondary.text, "-1.0036 Ω")
        self.assertAlmostEqual(r.secondary.value, -1.0036, 4)
        self.assertTrue(r.secondary.unspecified)
        self.assertFalse(r.unspecified)
        r1 = lc.measure_to_reading(lc.parse_measure(fk.build_measure(markers=1)), 1.0, 2.0)
        self.assertTrue(r1.unspecified)                  # bit 0: the main value
        self.assertFalse(r1.secondary.unspecified)
        ol = fk.build_value(0.0, 3, 0, 1, "0L  ")
        m = lc.parse_measure(fk.build_measure(main_row=1, main=ol))
        r = lc.measure_to_reading(m, 1.0, 2.0)
        self.assertEqual((r.validity, r.value, r.text), ("overload", None, "0L  "))
        self.assertFalse(r.secondary.unspecified)

    def test_measure_over_the_link(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        b.meter.measure_markers = 2
        self.assertTrue(b.dev.measure_reading().secondary.unspecified)
        self.assertEqual(len(b.dev.measure().raw), 65)
        b.meter.old_records = True
        self.assertEqual(len(b.dev.measure().raw), 64)
        self.assertFalse(b.dev.measure_reading().secondary.unspecified)

    def test_compare_50_and_29_bytes(self):
        tail = dict(mode_in_force=2, mode_set=3, letter="J", lower=-0.25, upper=0.5, result=3,
                    signed_deviation=-0.4, secondary_kind=3, secondary_value=1.5)
        c = lc.parse_compare(fk.build_compare(tail=tail))
        self.assertEqual(len(c.raw), 50)
        self.assertEqual((c.mode_in_force, c.mode_set, c.letter, c.result, c.secondary_kind),
                         (2, 3, "J", 3, 3))
        self.assertEqual(c.result_name, "LO")
        self.assertAlmostEqual(c.lower, -0.25)
        self.assertAlmostEqual(c.upper, 0.5)
        self.assertAlmostEqual(c.signed_deviation, -0.4, 5)
        self.assertAlmostEqual(c.secondary_value, 1.5)
        self.assertEqual(c.passed, 3)                                  # the old fields are unchanged
        old = lc.parse_compare(fk.build_compare())
        self.assertEqual(len(old.raw), 29)
        for f in ("mode_in_force", "mode_set", "letter", "lower", "upper", "result",
                  "signed_deviation", "secondary_kind", "secondary_value", "result_name"):
            self.assertIsNone(getattr(old, f), f)
        for res, name in ((0, "---"), (1, "IN"), (2, "HI"), (3, "LO"), (5, "OUT of range"),
                          (6, "---")):
            t = dict(tail, result=res, letter="")
            self.assertEqual(lc.parse_compare(fk.build_compare(tail=t)).result_name, name)
        with self.assertRaises(base.DeviceError):
            lc.parse_compare(fk.build_compare()[:28])

    def test_compare_over_the_link(self):
        d = Bench(self, caps=fk.NEW_CAPS).dev
        c = d.compare()
        self.assertEqual((len(c.raw), c.ok, c.result), (50, 1, 1))
        old = Bench(self).dev.compare()
        self.assertEqual(len(old.raw), 29)

    def test_tolerance_set_encoding(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        c = b.dev.tolerance_set(1, -250, 500, secondary_kind=1, secondary_value=0.05)
        self.assertEqual(b.sent(lc.TOLERANCE_SET)[0][1],
                         struct.pack("<BiiBBf", 1, -250, 500, 0, 1, 0.05))
        self.assertEqual(len(b.sent(lc.TOLERANCE_SET)[0][1]), 15)
        self.assertEqual((c.mode_set, c.mode_in_force, c.result_name), (1, 1, "IN"))
        self.assertAlmostEqual(c.lower, -2.5)
        self.assertAlmostEqual(c.upper, 5.0)
        self.assertEqual(c.secondary_kind, 1)
        c = b.dev.tolerance_set(3, letter="B", secondary_kind=2, secondary_value=100.0)
        self.assertEqual(b.sent(lc.TOLERANCE_SET)[1][1],
                         struct.pack("<BiiBBf", 3, 0, 0, ord("B"), 2, 100.0))
        self.assertEqual((c.mode_set, c.letter, c.result_name), (3, "B", "HI"))
        c = b.dev.tolerance_set(2, -100, 100)
        self.assertEqual((c.mode_in_force, c.mode_set), (2, 2))
        c = b.dev.tolerance_set(0)
        self.assertEqual((c.mode_in_force, c.mode_set), (0, 0))

    def test_compare_set_returns_the_mode_to_0(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        b.dev.tolerance_set(3, letter="K")
        self.assertEqual(b.dev.compare().mode_set, 3)
        c = b.dev.compare_set(220.0, 2.5, 1, 1, 0)
        self.assertEqual((c.mode_set, c.mode_in_force, c.letter), (0, 0, ""))

    def test_tolerance_set_validation_on_the_host(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        bad = [dict(mode=4), dict(mode=1, lower=-10000, upper=0), dict(mode=1, lower=0, upper=99991),
               dict(mode=1, lower=5, upper=4), dict(mode=2, lower=-100000, upper=0),
               dict(mode=2, lower=0, upper=100000), dict(mode=3, letter="A"),
               dict(mode=3, letter=""), dict(mode=3, letter="BC"), dict(mode=0, secondary_kind=4),
               dict(mode=0, secondary_value=-1.0), dict(mode=0, secondary_value=float("nan")),
               dict(mode=0, secondary_value=float("inf"))]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                b.dev.tolerance_set(**kw)
        self.assertEqual(b.sent(lc.TOLERANCE_SET), [])
        for ch in lc.TOL_LETTERS:
            b.dev.tolerance_set(3, letter=ch)
        b.dev.tolerance_set(1, -9999, 99990)
        b.dev.tolerance_set(2, -99999, 99999)
        self.assertEqual(len(b.sent(lc.TOLERANCE_SET)), 11)

    def test_tolerance_set_meter_statuses(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        b.meter.compare_quantity = 1                          # R: pF limits are not allowed
        with self.assertRaises(base.StatusError) as cm:
            b.dev.tolerance_set(2, -100, 100)
        self.assertEqual(cm.exception.status, 3)
        self.assertEqual(cm.exception.record.mode_set, 0)    # the record comes with the status
        with self.assertRaises(base.StatusError) as cm:       # the meter's own check (status 2)
            b.dev.call(lc.TOLERANCE_SET, b"\0" * 14)
        self.assertEqual(cm.exception.status, 2)

    def test_tolerance_set_needs_bit_23(self):
        b = Bench(self)
        with self.assertRaises(base.CapabilityError):
            b.dev.tolerance_set(0)
        self.assertEqual(b.sent(lc.TOLERANCE_SET), [])

    def test_save_result_9(self):
        self.assertEqual(lc.SAVE_RESULT[9],
                         "unchanged: both copies already held the block, nothing written")
        b = Bench(self, caps=fk.NEW_CAPS)
        b.meter.unchanged_saves = True
        r = b.dev.settings_save("OK")
        self.assertEqual((r.status, r.result, r.restart), (0, lc.SAVE_UNCHANGED, False))
        self.assertIn("unchanged", r.describe())
        ev = lc.parse_event(lc.EV_SAVE_RESULT, struct.pack("<II", 9, 0))
        self.assertEqual(ev.fields["save_status"] & 0xFF, lc.SAVE_UNCHANGED)

    def test_60hz_in_the_frequency_list(self):
        old = Bench(self).dev.lcr_tables(2)
        self.assertEqual(len(old.entries), 2)
        d = Bench(self, caps=fk.NEW_CAPS).dev
        fq = d.lcr_tables(2)
        self.assertEqual((fq.entries[5], len(fq.entries)), ((60, "60Hz"), 6))
        self.assertEqual(fq.entries[1], (1000, "1kHz"))
        self.assertEqual(d.lcr_set(2, 5).main_row, 2)


class Confirmation(unittest.TestCase):
    """Every save / destroy method refuses without the typed OK and sends nothing."""

    def calls(self, d, bad):
        blk = fk.build_copy(5, 1)[:lc.BLOCK_SIZE]
        return [lambda: d.settings_save(bad), lambda: d.restore_defaults(bad),
                lambda: d.settings_restore_b(bad), lambda: d.settings_accept_a(bad),
                lambda: d.settings_accept_b(bad), lambda: d.settings_accept_defaults(bad),
                lambda: d.write_block(blk, bad), lambda: d.calibrate(0, bad),
                lambda: d.power_off(bad), lambda: d.update_mode(bad),
                lambda: d.boot_begin(1, 0x300, 0, bad), lambda: d.boot_data(0, b"\0" * 4, bad),
                lambda: d.boot_end(bad), lambda: d.boot_install(bad),
                lambda: d.restore(Path("x.json"), "A", bad),
                lambda: d.update_app(Path("x.uf2"), lambda: [], None, confirm=bad),
                lambda: d.boot_update(Path("x.slot"), None, confirm=bad)]

    def test_refused_without_ok(self):
        for bad in ("", "ok", "Ok", "YES", " OK", None):
            b = Bench(self)
            for fn in self.calls(b.dev, bad):
                with self.assertRaises(base.SafetyRefused):
                    fn()
            self.assertEqual(b.meter.log, [], bad)
            self.assertFalse((b.tmp / "bk").exists())

    def test_no_backup_folder_refuses_before_sending_the_command(self):
        b = Bench(self)
        b.dev.backup_folder = None
        for fn in (lambda: b.dev.settings_save("OK"), lambda: b.dev.calibrate(0, "OK"),
                   lambda: b.dev.power_off("OK"), lambda: b.dev.restore_defaults("OK"),
                   lambda: b.dev.settings_accept_defaults("OK")):
            with self.assertRaisesRegex(base.SafetyRefused, "backup folder"):
                fn()
        self.assertEqual(b.meter.log, [])


class SaveMethods(unittest.TestCase):
    def test_backup_comes_first_then_the_command(self):
        for name, code in (("settings_save", lc.SETTINGS_SAVE),
                           ("restore_defaults", lc.RESTORE_DEFAULTS),
                           ("settings_accept_a", lc.SETTINGS_ACCEPT_A),
                           ("settings_accept_b", lc.SETTINGS_ACCEPT_B),
                           ("settings_accept_defaults", lc.SETTINGS_ACCEPT_DEFAULTS)):
            b = Bench(self)
            r = getattr(b.dev, name)("OK")
            self.assertEqual((r.status, r.result, r.restart), (0, 0, False), name)
            codes = b.meter.cmds()
            first = codes.index(code)
            self.assertEqual(codes[:first].count(lc.CAL_READ), 32, name)
            self.assertEqual(codes.count(code), 1)
            self.assertEqual(b.sent(code)[0][1], struct.pack("<H", 0x4F4B))
            self.assertEqual(len(list((b.tmp / "bk").rglob("*.json"))), 1)

    def test_restore_b_restart_is_followed(self):
        b = Bench(self)
        r = b.dev.settings_restore_b("OK")
        self.assertTrue(r.restart)
        self.assertEqual(len(b.links), 2)                  # reconnect handed out a new link
        self.assertIs(b.dev.link, b.links[1])
        self.assertTrue(b.links[0].closed)
        self.assertEqual(b.dev.info.uid, fk.INVENTED_UID)

    def test_held_saves_status_3_with_the_save_record(self):
        b = Bench(self)
        b.meter.saves_held = True
        with self.assertRaises(base.StatusError) as c:
            b.dev.settings_save("OK")
        self.assertEqual(c.exception.status, 3)
        self.assertEqual(c.exception.record.result, 1)
        self.assertIn("saves held", c.exception.record.describe())

    def test_power_off_payload(self):
        b = Bench(self)
        b.dev.power_off("OK")
        self.assertEqual(b.sent(lc.POWER_OFF), [(lc.POWER_OFF, struct.pack("<H", 0x4F46))])
        self.assertLess(b.meter.cmds().index(lc.CAL_READ), b.meter.cmds().index(lc.POWER_OFF))

    def test_calibrate(self):
        b = Bench(self)
        r = b.dev.calibrate(11, "OK")
        self.assertEqual((r.cal_status, r.cal_type), (2, 11))
        self.assertEqual(b.sent(lc.CALIBRATE)[0][1], bytes([11, 0x4B, 0x4F]))
        self.assertLess(b.meter.cmds().index(lc.CAL_READ), b.meter.cmds().index(lc.CALIBRATE))
        with self.assertRaises(ValueError):
            b.dev.calibrate(12, "OK")
        with self.assertRaises(base.StatusError) as c:          # 1..10 only in factory mode
            b.dev.calibrate(5, "OK")
        self.assertEqual(c.exception.status, 2)
        b.meter.saves_held = True
        with self.assertRaises(base.StatusError) as c:
            b.dev.calibrate(0, "OK")
        self.assertEqual((c.exception.status, c.exception.record.cal_status), (3, 3))

    def test_write_block_chunks_and_result(self):
        b = Bench(self)
        blk = fk.build_copy(9, 3)[:lc.BLOCK_SIZE]
        r = b.dev.write_block(blk, "OK", chunk=500)
        self.assertEqual((r.status, r.result, r.restart), (0, 0, True))
        wb = b.sent(lc.SETTINGS_WRITE_BLOCK)
        self.assertEqual(len(wb), 7)                       # 0xC14 = 3092 bytes in chunks of 500
        offs = [struct.unpack_from("<H", p, 2)[0] for _, p in wb]
        self.assertEqual(offs, [0, 500, 1000, 1500, 2000, 2500, 3000])
        for _, p in wb:
            self.assertEqual(struct.unpack_from("<H", p, 4)[0], lc.BLOCK_SIZE)
            self.assertEqual(struct.unpack_from("<I", p, 6)[0], fk.crc32(blk))
        self.assertEqual(b.meter.written_blocks, [blk])

    def test_write_block_rejects_a_bad_block_before_sending(self):
        b = Bench(self)
        with self.assertRaises(ValueError):
            b.dev.write_block(b"\0" * 10, "OK")
        bad = bytearray(fk.build_copy(9, 3)[:lc.BLOCK_SIZE])
        bad[0] = 0
        with self.assertRaisesRegex(base.DeviceError, "markers"):
            b.dev.write_block(bytes(bad), "OK")
        self.assertEqual(b.sent(lc.SETTINGS_WRITE_BLOCK), [])


class BackupRestore(unittest.TestCase):
    def test_backup_file_content(self):
        b = Bench(self)
        path = b.dev.backup(b.tmp / "bk")
        self.assertEqual(path.parent.name, "LC1020E-" + fk.INVENTED_UID.hex().upper())
        self.assertEqual(path.name, "20261121-100001.json")
        doc = json.loads(path.read_text())
        self.assertEqual((doc["format"], doc["model"], doc["uid"], doc["firmware"]),
                         (1, "LC1020E", fk.INVENTED_UID.hex().upper(), "LC1020E V1.1 OWN"))
        self.assertTrue(doc["date"].startswith("2026-11-21T10:00:01"))
        for i, c in enumerate(doc["copies"]):
            data = bytes.fromhex(c["data"])
            self.assertEqual(data, b.meter.copies[i])
            self.assertEqual(c["crc32"], "0x%08X" % fk.crc32(data))
            self.assertEqual((c["name"], c["length"], c["valid"]), ("AB"[i], 0x1000, True))
        self.assertEqual(doc["copies"][0]["address"], "0x0807F000")
        self.assertEqual(doc["copies"][1]["address"], "0x08066000")
        self.assertEqual(b.meter.cmds().count(lc.CAL_READ), 32)
        self.assertEqual(b.link.exclusive_count, 1)

    def test_skip_when_newest_has_same_crc(self):
        b = Bench(self)
        p1 = b.dev.backup(b.tmp / "bk")
        n = len(b.sent(lc.CAL_READ))
        p2 = b.dev.backup(b.tmp / "bk")
        self.assertEqual(p1, p2)
        self.assertEqual(len(list(p1.parent.glob("*.json"))), 1)
        self.assertEqual(len(b.sent(lc.CAL_READ)), 2 * n)          # it read again to compare
        b.meter.copies[0] = fk.build_copy(6, 2)                    # content changed
        p3 = b.dev.backup(b.tmp / "bk")
        self.assertNotEqual(p1, p3)
        self.assertEqual(len(list(p1.parent.glob("*.json"))), 2)
        self.assertEqual(b.dev.backup(b.tmp / "bk"), p3)           # same as the new newest
        b.meter.copies[0] = fk.build_copy(5, 1)                    # back to the old content:
        p5 = b.dev.backup(b.tmp / "bk")                            # newest is p3, so a new file
        self.assertNotIn(p5, (p1, p3))
        self.assertEqual(len(list(p1.parent.glob("*.json"))), 3)

    def test_damaged_newest_backup_is_not_trusted(self):
        b = Bench(self)
        p1 = b.dev.backup(b.tmp / "bk")
        doc = json.loads(p1.read_text())
        d = bytearray(bytes.fromhex(doc["copies"][0]["data"]))
        d[5] ^= 1
        doc["copies"][0]["data"] = d.hex()
        p1.write_text(json.dumps(doc))
        p2 = b.dev.backup(b.tmp / "bk")
        self.assertNotEqual(p1, p2)
        self.assertEqual(bytes.fromhex(json.loads(p2.read_text())["copies"][0]["data"]),
                         b.meter.copies[0])

    def test_backups_are_per_uid(self):
        b1 = Bench(self)
        b2 = Bench(self, uid=fk.OTHER_UID)
        b2.dev.backup_folder = b1.tmp / "bk"
        b1.dev.backup(b1.tmp / "bk")
        p = b2.dev.backup(b1.tmp / "bk")
        self.assertEqual(p.parent.name, "LC1020E-" + fk.OTHER_UID.hex().upper())
        self.assertEqual(len(list((b1.tmp / "bk").iterdir())), 2)

    def test_backup_refuses_when_the_meter_is_another(self):
        b = Bench(self)
        b.meter.uid = fk.OTHER_UID                     # a different meter answers on the link
        with self.assertRaises(lc.BackupMismatch):
            b.dev.backup(b.tmp / "bk")
        self.assertFalse((b.tmp / "bk").exists())

    def test_backup_while_live_stops_and_restarts_the_stream(self):
        b = Bench(self)
        b.dev.start_live()
        b.dev.backup(b.tmp / "bk")
        ms = [struct.unpack("<H", p)[0] for c, p, _ in b.link.requests if c == lc.MEASURE_STREAM]
        self.assertEqual(ms, [0, 0xFFFF, 0])
        self.assertTrue(b.dev.live)

    def test_restore_same_uid(self):
        b = Bench(self)
        path = b.dev.backup(b.tmp / "bk")
        b.meter.copies = [fk.build_copy(8, 7), fk.build_copy(7, 7)]    # the meter changed since
        r = b.dev.restore(path, "A", "OK")
        self.assertTrue(r.restart)
        want = bytes.fromhex(json.loads(path.read_text())["copies"][0]["data"])[:lc.BLOCK_SIZE]
        self.assertEqual(b.meter.written_blocks[-1], want)
        codes = b.meter.cmds()
        self.assertGreaterEqual(len(list((b.tmp / "bk" / path.parent.name).glob("*.json"))), 2)
        self.assertLess(codes.index(lc.CAL_READ), codes.index(lc.SETTINGS_WRITE_BLOCK))
        self.assertEqual(codes.count(lc.CAL_READ), 64)             # the pre-restore backup
        self.assertEqual(len(b.links), 2)

    def test_restore_copy_b(self):
        b = Bench(self)
        path = b.dev.backup(b.tmp / "bk")
        b.dev.restore(path, "B", "OK")
        want = bytes.fromhex(json.loads(path.read_text())["copies"][1]["data"])[:lc.BLOCK_SIZE]
        self.assertEqual(b.meter.written_blocks[-1], want)

    def test_restore_other_uid_refused(self):
        a = Bench(self)
        path = a.dev.backup(a.tmp / "bk")
        b = Bench(self, uid=fk.OTHER_UID)
        with self.assertRaisesRegex(lc.BackupMismatch, "this meter is"):
            b.dev.restore(path, "A", "OK")
        self.assertEqual(b.meter.log, [])

    def test_restore_refuses_invalid_copy_and_damaged_file(self):
        b = Bench(self)
        b.meter.copies[0] = bytes(0x1000)                          # an erased / invalid copy A
        path = b.dev.backup(b.tmp / "bk")
        with self.assertRaisesRegex(lc.BackupMismatch, "not valid"):
            b.dev.restore(path, "A", "OK")
        self.assertEqual(b.sent(lc.SETTINGS_WRITE_BLOCK), [])
        junk = b.tmp / "junk.json"
        junk.write_text("{not json")
        with self.assertRaises(lc.BackupMismatch):
            b.dev.restore(junk, "A", "OK")
        with self.assertRaises(ValueError):
            b.dev.restore(path, "C", "OK")
        doc = json.loads(path.read_text())
        doc["model"] = "2C53T"
        wrong = b.tmp / "wrong.json"
        wrong.write_text(json.dumps(doc))
        with self.assertRaises(lc.BackupMismatch):
            b.dev.restore(wrong, "B", "OK")

    def test_restore_status_7_restarts_from_offset_0(self):
        b = Bench(self)
        path = b.dev.backup(b.tmp / "bk")
        b.meter.block_fail_finals = 1
        r = b.dev.restore(path, "B", "OK")
        self.assertEqual(r.result, 0)
        wb = b.sent(lc.SETTINGS_WRITE_BLOCK)
        offs = [struct.unpack_from("<H", p, 2)[0] for _, p in wb]
        self.assertEqual(offs, [0, 500, 1000, 1500, 2000, 2500, 3000] * 2)

    def test_restore_status_7_gives_up_after_three(self):
        b = Bench(self)
        path = b.dev.backup(b.tmp / "bk")
        b.meter.block_fail_finals = 5
        with self.assertRaises(base.StatusError) as c:
            b.dev.restore(path, "A", "OK")
        self.assertEqual(c.exception.status, 7)
        self.assertEqual(len(b.sent(lc.SETTINGS_WRITE_BLOCK)), 21)
        self.assertEqual(b.meter.written_blocks, [])


class LiveAndEvents(unittest.TestCase):
    def test_live_readings_from_stream_frames(self):
        b = Bench(self)
        got, states = [], []
        b.dev.reading.connect(got.append)
        b.dev.state.connect(states.append)
        b.dev.start_live()
        self.assertEqual(b.link.requests[-1], (lc.MEASURE_STREAM, struct.pack("<H", 0), 1.5))
        b.link.emit_stream(lc.MEASURE, fk.build_measure())
        b.link.emit_stream(lc.STATE, fk.build_state(screen=2))
        b.link.emit_stream(lc.MEASURE, b"\0" * 10)                 # short frame: counted, no crash
        b.link.emit_stream(0x77, b"x")                             # unknown stream: ignored
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].quantity, got[0].t), ("C", 12.5))
        self.assertEqual(states[0].screen, 2)
        self.assertEqual(b.dev.bad_frames, 1)
        b.dev.stop_live()
        self.assertEqual(b.link.requests[-1][:2], (lc.MEASURE_STREAM, struct.pack("<H", 0xFFFF)))
        b.link.emit_stream(lc.MEASURE, fk.build_measure())
        self.assertEqual(len(got), 1)                              # disconnected after stop
        b.dev.start_live()
        b.dev.start_live()                                         # second start: no new request
        self.assertEqual(sum(1 for c, p, _ in b.link.requests
                             if c == lc.MEASURE_STREAM and p == b"\0\0"), 2)

    def test_events(self):
        b = Bench(self)
        ev = []
        b.dev.meter_event.connect(ev.append)
        b.link.emit_event(0x91, bytes([2, 1]))
        b.link.emit_event(0x95, struct.pack("<I", 12))
        self.assertEqual([e.name for e in ev], ["SCREEN_CHANGED", "MEASURE_DONE"])
        self.assertEqual(ev[0].fields, dict(screen=2, previous=1))

    def test_close_detaches(self):
        b = Bench(self)
        ev = []
        b.dev.meter_event.connect(ev.append)
        b.dev.close()
        b.link.emit_event(0x94, b"")
        self.assertEqual(ev, [])


class Screenshot(unittest.TestCase):
    def check_pixels(self, px):
        self.assertEqual(len(px), 320 * 240)
        for x, y in ((0, 0), (319, 0), (5, 77), (319, 239), (160, 120)):
            self.assertEqual(px[y * 320 + x], fk.pixel(x, y), (x, y))

    def test_full_capture(self):
        b = Bench(self)
        steps = []
        px = b.dev.screenshot_pixels(lambda f, t: steps.append((f, t)))
        self.check_pixels(px)
        self.assertEqual(b.meter.cmds(lc.SCREENSHOT), [lc.SCREENSHOT])
        self.assertEqual(b.sent(lc.SCREENSHOT)[0][1], b"\xFF")
        self.assertEqual(b.link.exclusive_count, 1)
        self.assertEqual(steps[0][0], 0.0)
        self.assertEqual(steps[-1][0], 1.0)

    def test_retry_of_a_band_without_room(self):
        b = Bench(self)
        b.meter.screenshot_bad = {5: 1, 17: 2}
        px = b.dev.screenshot_pixels()
        self.check_pixels(px)
        again = [p for _, p in b.sent(lc.SCREENSHOT)]
        self.assertEqual(again, [b"\xFF", bytes([5]), bytes([17])])

    def test_gives_up_after_retries(self):
        b = Bench(self)
        meter = b.meter
        orig = meter._emit_band
        meter._emit_band = lambda band, bad: orig(band, 1 if band == 3 else bad)
        with self.assertRaisesRegex(base.DeviceError, r"bands \[3\] incomplete"):
            b.dev.screenshot_pixels(retries=2)
        self.assertEqual(len(b.sent(lc.SCREENSHOT)), 3)

    def test_missing_end_frame_times_out(self):
        b = Bench(self)
        orig = b.meter._emit_band

        lost = []

        def no_end(band, bad):
            orig(band, bad)
            if band == 4 and not lost:
                lost.append(1)
                b.meter.pending.pop()           # the end frame never arrives (first time only)
        b.meter._emit_band = no_end
        b.dev.screenshot_pixels()               # band 4 is asked again and then complete
        self.assertEqual([p for _, p in b.sent(lc.SCREENSHOT)][:2], [b"\xFF", bytes([4])])

    def test_image(self):
        b = Bench(self)
        img = b.dev.screenshot()
        self.assertEqual((img.width(), img.height()), (320, 240))
        v = fk.pixel(5, 77)
        r, g, bl = v >> 11 & 31, v >> 5 & 63, v & 31
        c = img.pixelColor(5, 77)
        self.assertEqual((c.red(), c.green(), c.blue()),
                         (r << 3 | r >> 2, g << 2 | g >> 4, bl << 3 | bl >> 2))

    def test_stream_hook_is_removed(self):
        b = Bench(self)
        b.dev.screenshot_pixels()
        b.link.emit_stream(lc.SCREENSHOT, b"")          # no collector left: nothing breaks


class UpdateApp(unittest.TestCase):
    def setUp(self):
        self.b = Bench(self)
        self.uf2 = self.b.tmp / "app.uf2"
        self.uf2.write_bytes(fk.build_uf2(6))

    def run_update(self, **kw):
        steps = []
        info = self.b.dev.update_app(self.uf2, self.b.drives, lambda f, t: steps.append((f, t)),
                                     confirm="OK", **kw)
        return info, steps

    def test_success_result_1(self):
        b = self.b
        info, steps = self.run_update()
        self.assertEqual(info.update_result, 1)
        self.assertEqual(b.dev.info.update_result, 1)
        self.assertEqual((b.drive_dir / lc.DRIVE_FILE).read_bytes(), self.uf2.read_bytes())
        self.assertEqual(sorted(p.name for p in b.drive_dir.iterdir()), ["LC1020E.UF2", "READY.TXT"])
        self.assertIs(b.dev.link, b.links[-1])
        self.assertTrue(b.links[0].closed)
        self.assertEqual(steps[-1][0], 1.0)
        codes = b.meter.cmds()
        self.assertEqual(codes.count(lc.UPDATE_MODE), 1)
        self.assertLess(codes.index(lc.INFO), codes.index(lc.UPDATE_MODE))
        self.assertEqual(len(b.links), 2)

    def test_result_0_is_an_error(self):
        b = self.b
        b.apply_result = 0
        with self.assertRaises(lc.UpdateFailed) as c:
            self.run_update()
        self.assertEqual(c.exception.result, 0)
        self.assertIn("none", str(c.exception))

    def test_results_2_and_3_are_errors(self):
        for result, word in ((2, "image check failed"), (3, "programming failed")):
            b = Bench(self)
            b.apply_result = result
            with self.assertRaisesRegex(lc.UpdateFailed, word):
                b.dev.update_app(self.uf2, b.drives, confirm="OK")

    def test_wrong_family_refused_before_anything_is_sent(self):
        b = self.b
        self.uf2.write_bytes(fk.build_uf2(3, family=0xE48BFF56))
        with self.assertRaisesRegex(base.DeviceError, "family"):
            self.run_update()
        self.assertEqual(b.meter.log, [])
        self.assertFalse(b.drive_dir.exists())

    def test_out_of_range_uf2_refused(self):
        self.uf2.write_bytes(fk.build_uf2(1, base_addr=0x08000000))
        with self.assertRaises(base.DeviceError):
            self.run_update()
        self.assertEqual(self.b.meter.log, [])

    def test_update_mode_status_6_changes_nothing(self):
        b = self.b
        b.meter.update_mode_status6 = True
        with self.assertRaises(base.StatusError) as c:
            self.run_update()
        self.assertEqual(c.exception.status, 6)
        self.assertFalse(b.drive_up)
        self.assertFalse(b.links[0].closed)
        self.assertEqual(len(b.links), 1)

    def test_drive_never_appears(self):
        b = self.b
        b.meter.on_update_mode = lambda: None
        with self.assertRaisesRegex(base.DeviceError, "did not appear"):
            self.run_update(drive_timeout=5.0)
        self.assertFalse((b.drive_dir / lc.DRIVE_FILE).exists())

    def test_drive_already_present_before_does_not_count(self):
        b = self.b
        b.drive_dir.mkdir(parents=True)
        (b.drive_dir / "READY.TXT").write_text("x")
        b.drive_up = True                              # some LC1020E drive was there before
        b.meter.on_update_mode = lambda: None
        with self.assertRaisesRegex(base.DeviceError, "did not appear"):
            self.run_update(drive_timeout=3.0)
        self.assertFalse((b.drive_dir / lc.DRIVE_FILE).exists())

    def test_drive_without_ready_file_is_not_taken(self):
        b = self.b
        b.meter.on_update_mode = lambda: (b.drive_dir.mkdir(parents=True), setattr(b, "drive_up", True))
        with self.assertRaisesRegex(base.DeviceError, "did not appear"):
            self.run_update(drive_timeout=3.0)

    def test_other_models_drive_is_ignored(self):
        b = self.b
        other = b.tmp / "other"
        other.mkdir()
        (other / "READY.TXT").write_text("x")
        self.run_update()
        self.assertFalse((other / lc.DRIVE_FILE).exists())

    def test_reconnect_with_other_uid_refused(self):
        b = self.b
        orig = b.reconnect

        def swapped(uid, timeout):
            link = orig(uid, timeout)
            b.meter.uid = fk.OTHER_UID
            return link
        b.dev._reconnect = swapped
        with self.assertRaises(lc.BackupMismatch):
            self.run_update()

    def test_no_reconnect_function_refuses_before_update_mode(self):
        b = self.b
        b.dev._reconnect = None
        with self.assertRaisesRegex(base.DeviceError, "reconnect"):
            self.run_update()
        self.assertEqual(b.meter.log, [])

    def test_write_error_after_last_block_is_not_fatal(self):
        # the bootloader restarted after the last block: the drive no longer answers (its folder
        # is gone here), so INFO decides
        b = self.b
        orig = lc.LC1020E._write_plain

        def boom(target, data):
            orig(target, data)
            gone = b.drive_dir.with_name("gone")
            b.drive_dir.rename(gone)
            b.drive_dir = gone                   # where the fake's bootloader reads the file
            raise OSError("drive gone")
        lc.LC1020E._write_plain = staticmethod(boom)
        self.addCleanup(setattr, lc.LC1020E, "_write_plain", staticmethod(orig))
        info, steps = self.run_update()
        self.assertEqual(info.update_result, 1)
        self.assertTrue(any("drive gone" in t for _, t in steps))


class BootUpdate(unittest.TestCase):
    def test_success_installs_the_free_slot(self):
        b = Bench(self)
        slot = b.tmp / "slot.bin"
        slot.write_bytes(fk.build_slot_file(lc.SLOT_BASE[1], version=3, length=0x400))
        steps = []
        st, info = b.dev.boot_update(slot, lambda f, t: steps.append(f), confirm="OK")
        self.assertEqual(st.newest_slot, 1)
        self.assertEqual((st.newest_state, info.running_slot), (3, 1))
        codes = b.meter.cmds()
        self.assertEqual(codes.count(lc.BOOT_BEGIN), 1)
        self.assertEqual(codes.count(lc.BOOT_DATA), 6)       # 0x600 bytes in chunks of 256
        self.assertEqual(codes.count(lc.BOOT_END), 1)
        self.assertEqual(codes.count(lc.BOOT_INSTALL), 1)
        self.assertEqual(b.meter.slot_files[1], slot.read_bytes())
        begin = b.sent(lc.BOOT_BEGIN)[0][1]
        self.assertEqual(struct.unpack("<BII", begin),
                         (1, slot.stat().st_size, fk.crc32(slot.read_bytes())))
        self.assertEqual(steps[-1], 1.0)
        self.assertEqual(len(b.links), 2)

    def test_file_linked_for_the_other_slot_refused_before_begin(self):
        b = Bench(self)
        slot = b.tmp / "slot.bin"
        slot.write_bytes(fk.build_slot_file(lc.SLOT_BASE[2], length=0x400))
        with self.assertRaisesRegex(base.DeviceError, "linked for"):
            b.dev.boot_update(slot, None, confirm="OK")
        self.assertEqual(b.meter.cmds(lc.BOOT_BEGIN), [])
        self.assertEqual(b.meter.cmds(lc.BOOT_INSTALL), [])

    def test_no_free_slot(self):
        b = Bench(self)
        b.meter.free_slot = 0
        slot = b.tmp / "slot.bin"
        slot.write_bytes(fk.build_slot_file(lc.SLOT_BASE[1], length=0x400))
        with self.assertRaisesRegex(base.DeviceError, "no free slot"):
            b.dev.boot_update(slot, None, confirm="OK")

    def test_boot_data_flash_error_detail(self):
        b = Bench(self)
        b.meter._c_86 = lambda p: (5, struct.pack("<BI", 5, 0x100))
        with self.assertRaises(base.StatusError) as c:
            b.dev.boot_data(0, b"\0" * 4, "OK")
        self.assertEqual(c.exception.detail, "flash read-back differs at file offset 0x100")

    def test_begin_status_3_stops_before_data(self):
        b = Bench(self)
        slot = b.tmp / "slot.bin"
        slot.write_bytes(fk.build_slot_file(lc.SLOT_BASE[1], length=0x400))
        b.meter._c_85 = lambda p: (3, b"")
        with self.assertRaises(base.StatusError):
            b.dev.boot_update(slot, None, confirm="OK")
        self.assertEqual(b.meter.cmds(lc.BOOT_DATA), [])
        self.assertEqual(len(b.links), 1)

    def test_status_and_low_level_methods(self):
        b = Bench(self)
        st = b.dev.boot_status()
        self.assertEqual((st.free_slot, st.next_slot), (1, 1))
        with self.assertRaises(base.StatusError):
            b.dev.boot_begin(2, 0x300, 0, "OK")                # not the free slot: status 3
        um = b.dev.update_mode("OK")
        self.assertEqual((um.next_slot, um.next_version), (1, 2))


# ----------------------------------------------------------------------------------------------
class ReviewRound1(unittest.TestCase):
    """The fixes of review_r1.md that concern the LC1020E module."""

    def setUp(self):
        self.b = Bench(self)
        self.uf2 = self.b.tmp / "app.uf2"
        self.uf2.write_bytes(fk.build_uf2(6))
        self.slot = self.b.tmp / "slot.bin"
        self.slot.write_bytes(fk.build_slot_file(lc.SLOT_BASE[1], version=3, length=0x400))

    def backups(self):
        d = self.b.dev.backup_dir(self.b.tmp / "bk")
        return sorted(d.glob("*.json")) if d.is_dir() else []

    # finding 2
    def test_update_app_backs_up_both_copies_before_update_mode(self):
        b = self.b
        steps = []
        b.dev.update_app(self.uf2, b.drives, lambda f, t: steps.append(t), confirm="OK")
        codes = b.meter.cmds()
        self.assertEqual(codes.count(lc.CAL_READ), 32)                  # 2 copies x 16 requests
        self.assertLess(codes.index(lc.CAL_READ), codes.index(lc.UPDATE_MODE))
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(any("backed up" in t for t in steps))

    def test_boot_update_backs_up_before_boot_begin(self):
        b = self.b
        b.dev.boot_update(self.slot, None, confirm="OK")
        codes = b.meter.cmds()
        self.assertLess(codes.index(lc.CAL_READ), codes.index(lc.BOOT_BEGIN))
        self.assertEqual(len(self.backups()), 1)

    def test_updates_refused_without_a_backup_folder(self):
        b = self.b
        b.dev.backup_folder = None
        with self.assertRaisesRegex(base.SafetyRefused, "backup folder"):
            b.dev.update_app(self.uf2, b.drives, confirm="OK")
        with self.assertRaisesRegex(base.SafetyRefused, "backup folder"):
            b.dev.boot_update(self.slot, None, confirm="OK")
        for code in (lc.UPDATE_MODE, lc.BOOT_BEGIN, lc.BOOT_DATA, lc.BOOT_INSTALL):
            self.assertNotIn(code, b.meter.cmds())
        self.assertFalse(b.drive_up)

    # finding 3
    def test_foreign_slot_file_needs_the_second_confirmation(self):
        b = self.b
        sha = __import__("hashlib").sha256(self.slot.read_bytes()).hexdigest()
        with self.assertRaisesRegex(base.SafetyRefused, "SHA256SUMS"):
            b.dev.boot_update(self.slot, None, confirm="OK", known_sha256={"0" * 64})
        self.assertEqual(b.meter.log, [])
        st, info = b.dev.boot_update(self.slot, None, confirm="OK", known_sha256=set(),
                                     confirm_foreign="OK")
        self.assertEqual(info.running_slot, 1)
        b2 = Bench(self)
        b2.dev.boot_update(self.slot, None, confirm="OK", known_sha256={sha.upper()})
        self.assertIn(lc.BOOT_INSTALL, b2.meter.cmds())

    # finding 4
    def test_key_payload_of_any_type_gets_the_hold_time(self):
        b = self.b
        b.dev.call(lc.KEY, [3, 0xDC, 0x05])
        self.assertEqual(b.link.requests[-1], (lc.KEY, bytes([3, 0xDC, 0x05]), 24.5))
        with self.assertRaises(base.SafetyRefused):
            b.dev.call(lc.POWER_OFF, [0x46, 0x4F])
        with self.assertRaises(TypeError):
            b.dev.call(lc.KEY, 3)
        self.assertNotIn(lc.POWER_OFF, b.meter.cmds())

    # finding 9
    def test_failed_info_after_restart_closes_the_new_link(self):
        b = self.b
        orig = b.reconnect

        def swapped(uid, timeout):
            link = orig(uid, timeout)
            b.meter.uid = fk.OTHER_UID
            return link
        b.dev._reconnect = swapped
        with self.assertRaises(lc.BackupMismatch):
            b.dev.update_app(self.uf2, b.drives, confirm="OK")
        self.assertTrue(b.links[-1].closed)

    # finding 10
    def test_failed_copy_is_reported_at_once(self):
        b = self.b

        def boom(target, data):
            raise PermissionError(13, "Permission denied", target)
        orig = lc.LC1020E._write_plain
        lc.LC1020E._write_plain = staticmethod(boom)
        self.addCleanup(setattr, lc.LC1020E, "_write_plain", staticmethod(orig))
        with self.assertRaises(lc.UpdateCopyFailed) as c:
            b.dev.update_app(self.uf2, b.drives, confirm="OK")
        self.assertIn("stays in update mode", str(c.exception))
        self.assertEqual(c.exception.drive, str(b.drive_dir))
        self.assertEqual(len(b.links), 1)                 # no wait for the port
        self.assertTrue(b.meter.in_update_mode)

    # finding 11
    def test_unmounted_drive_is_reported_and_waited_for(self):
        b = self.b
        calls = []

        def drives():
            calls.append(1)
            if not b.drive_up:
                return []
            if len(calls) < 400:                          # far beyond drive_timeout's clock
                return [Drive("LC1020E", "/dev/sdz1", "unmounted")]
            return [Drive("LC1020E", str(b.drive_dir))]
        steps = []
        info = b.dev.update_app(self.uf2, drives, lambda f, t: steps.append(t), confirm="OK",
                                drive_timeout=5.0)
        self.assertEqual(info.update_result, 1)
        said = [t for t in steps if "/dev/sdz1" in t]
        self.assertEqual(len(said), 1)
        self.assertIn("udisksctl mount -b /dev/sdz1", said[0])

    # finding 12
    def test_cancel_while_waiting_for_the_drive(self):
        b = self.b
        b.meter.on_update_mode = lambda: None
        token = base.CancelToken()
        calls = []

        def drives():
            calls.append(1)
            if len(calls) == 3:
                self.assertTrue(token.request())
            return []
        with base.cancel_scope(token), self.assertRaisesRegex(base.Cancelled, "update mode"):
            b.dev.update_app(self.uf2, drives, confirm="OK", drive_timeout=1000.0)
        self.assertFalse(token.allowed)

    def test_backup_cancelled_between_requests(self):
        b = self.b
        token = base.CancelToken()
        orig = b.meter.handle
        n = []

        def handle(cmd, payload):
            if cmd == lc.CAL_READ:
                n.append(1)
                if len(n) == 5:
                    token.request()
            return orig(cmd, payload)
        b.meter.handle = handle
        with base.cancel_scope(token), self.assertRaises(base.Cancelled):
            b.dev.settings_save("OK")
        self.assertEqual(len(n), 5)
        self.assertNotIn(lc.SETTINGS_SAVE, b.meter.cmds())
        self.assertEqual(self.backups(), [])

    def test_no_cancel_during_the_slot_writes(self):
        b = self.b
        token = base.CancelToken()
        orig = b.meter.handle
        answers = []

        def handle(cmd, payload):
            if cmd in (lc.BOOT_BEGIN, lc.BOOT_DATA, lc.BOOT_INSTALL):
                answers.append(token.request())
            return orig(cmd, payload)
        b.meter.handle = handle
        with base.cancel_scope(token):
            b.dev.boot_update(self.slot, None, confirm="OK")
        self.assertEqual(set(answers), {False})


class SerialLevelFake(unittest.TestCase):
    """The serial-level fake answers real frames (needs link/frames.py of part L)."""

    def setUp(self):
        try:
            from fnirsi_companion.link import frames
        except ImportError:
            self.skipTest("fnirsi_companion.link.frames is not there yet")
        self.fr = frames

    def test_info_and_stream_over_frames(self):
        meter = fk.FakeMeter()
        ser = fk.FakeSerial(meter)
        fr = self.fr
        ser.write(fr.encode(1, fr.REQUEST, 0x00))
        frames = fr.FrameParser().feed(ser.read(ser.in_waiting), 0.0)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual((f.seq, f.ftype, f.cmd, f.payload[0]), (1, fr.ANSWER, 0x00, 0))
        self.assertEqual(f.payload[1:], meter.info())
        ser.write(fr.encode(2, fr.REQUEST, lc.SCREENSHOT, b"\x03"))
        got = fr.FrameParser().feed(ser.read(ser.in_waiting), 0.0)
        self.assertEqual(got[0].ftype, fr.ANSWER)
        self.assertTrue(all(g.ftype == fr.STREAM and g.cmd == lc.SCREENSHOT for g in got[1:]))
        self.assertEqual(len(got), 1 + 14 + 1)
        ser.write(fr.encode(3, fr.REQUEST, 0x7F))
        bad = fr.FrameParser().feed(ser.read(ser.in_waiting), 0.0)[0]
        self.assertEqual(bad.payload[0], 1)


# ----------------------------------------------------------------------------------------------
# The own calibration area (OWN_CAL_DESIGN.md "OCD"): bodies, records, the OCAL_* commands
# ----------------------------------------------------------------------------------------------

UID_HEX = fk.INVENTED_UID.hex().upper()


def meta_body(note="first") -> bytes:
    """The smallest body: META only, 4 + 32 = 36 bytes."""
    return lc.build_body({lc.SEC_META: lc.pack_meta(1700000000, note)})


def default_gain() -> bytes:
    return lc.pack_gain([lc.GainFreq() for _ in range(6)])


class OcalBodies(unittest.TestCase):
    def test_section_sizes_and_the_record_size(self):
        # OCD 1.2: META 32, GAIN 6 x 332 = 1992, CLIP 64, SETTLE 84, MONITOR 28, BIAS 4;
        # (4+32) + (4+1992) + (4+64) + (4+84) + (4+28) + (4+4) = 2228, plus the 0x40 header = 0x8F4
        parts = {lc.SEC_META: lc.pack_meta(1, "x"), lc.SEC_GAIN: default_gain(),
                 lc.SEC_CLIP: lc.pack_clip([(0, 4095)] * 16),
                 lc.SEC_SETTLE: lc.pack_settle([list(lc.DEFAULT_SETTLE_MS[k:k + 1]) * 6
                                                for k in range(7)]),
                 lc.SEC_MONITOR: lc.pack_monitor(0, [0.0] * 6), lc.SEC_BIAS: lc.pack_bias(100)}
        self.assertEqual([len(parts[i]) for i in range(6)], [32, 1992, 64, 84, 28, 4])
        body = lc.build_body(parts)
        self.assertEqual(len(body), 2228)
        self.assertEqual(0x40 + len(body), 0x8F4)
        self.assertEqual(lc.parse_body(body), parts)

    def test_gain_entry_layout(self):
        g = lc.GainFreq(zeta_mag_mask=0x11, zeta_phase_mask=0x22, nu_mag_mask=3, nu_phase_mask=5)
        g.zeta[1] = complex(1.5, -0.5)
        g.nu[7] = complex(0.75, 0.25)
        d = lc.pack_gain_freq(g)
        self.assertEqual(len(d), 332)
        self.assertEqual(struct.unpack_from("<IIBBH", d), (0x11, 0x22, 3, 5, 0))
        self.assertEqual(struct.unpack_from("<ff", d, 12 + 8), (1.5, -0.5))         # zeta[1]
        self.assertEqual(struct.unpack_from("<ff", d, 12 + 256 + 7 * 8), (0.75, 0.25))   # nu[7]
        self.assertEqual(lc.unpack_gain_freq(d), g)
        self.assertTrue(lc.GainFreq(1 << 9, 1 << 9, 1 << 3, 1 << 3).cell_calibrated(3, 1, 1))  # k = 8*1+1
        self.assertFalse(lc.GainFreq(1 << 9, 1 << 9, 1 << 3, 0).cell_calibrated(3, 1, 1))

    def test_gain_factor_bounds(self):
        # 0.25 <= re^2 + im^2 <= 4, re > 0 and 4 re^2 >= re^2 + im^2 (arg <= 60 degrees)
        ok = [1 + 0j, 1.9 + 0j, 0.6 + 1.0j, 0.5 + 0j]       # |z|^2 = 1, 3.61, 1.36 (4*0.36 = 1.44), 0.25
        bad = [0.4 + 0j, 2.1 + 0j, 0.5 + 0.9j, -1 + 0j, complex(math.nan, 0), complex(math.inf, 0)]
        for z in ok:
            self.assertIsNone(lc.check_gain_factor(z), z)
        for z in bad:                                       # 0.16, 4.41, 4*0.25 = 1.0 < 1.06, re < 0, nan, inf
            self.assertIsNotNone(lc.check_gain_factor(z), z)

    def test_gain_entry_index_and_unused_values(self):
        entries = [lc.GainFreq() for _ in range(6)]
        entries[2].zeta[5] = 0.4 + 0j
        lc.validate_section(lc.SEC_GAIN, lc.pack_gain(entries))      # no mask bit: not checked
        entries[2].zeta_mag_mask = 1 << 5
        with self.assertRaises(lc.OcalValidationError) as c:
            lc.validate_section(lc.SEC_GAIN, lc.pack_gain(entries))
        self.assertEqual((c.exception.section, c.exception.index), (lc.SEC_GAIN, 2 * 41 + 5))
        entries[2].zeta[5] = 1 + 0j
        entries[3].nu[6] = 5 + 0j
        entries[3].nu_phase_mask = 1 << 6
        with self.assertRaises(lc.OcalValidationError) as c:
            lc.validate_section(lc.SEC_GAIN, lc.pack_gain(entries))
        self.assertEqual(c.exception.index, 3 * 41 + 32 + 6)
        entries[3].nu_phase_mask = 0
        entries[4].pad = 1
        with self.assertRaises(lc.OcalValidationError) as c:
            lc.validate_section(lc.SEC_GAIN, lc.pack_gain(entries))
        self.assertEqual(c.exception.index, 4 * 41 + 40)

    def test_clip_settle_monitor_bias_rules(self):
        def bad(sid, data, index):
            with self.assertRaises(lc.OcalValidationError) as c:
                lc.validate_section(sid, data)
            self.assertEqual((c.exception.section, c.exception.index), (sid, index))
        clip = [(0, 4095)] * 16
        lc.validate_section(lc.SEC_CLIP, lc.pack_clip(clip))
        clip[9] = (0, 3601)                       # hi - lo = 3601 > 3600
        lc.validate_section(lc.SEC_CLIP, lc.pack_clip(clip))
        clip[9] = (0, 3600)                       # 3600 is not above 3600
        bad(lc.SEC_CLIP, lc.pack_clip(clip), 9)
        clip[9] = (100, 4096)                     # hi above 4095
        bad(lc.SEC_CLIP, lc.pack_clip(clip), 9)
        clip[9] = (4000, 4095)                    # a window of 95 counts
        bad(lc.SEC_CLIP, lc.pack_clip(clip), 9)
        table = [[26] * 6 for _ in range(7)]
        for v in (1, 500):
            table[3][4] = v
            lc.validate_section(lc.SEC_SETTLE, lc.pack_settle(table))
        for v in (0, 501):
            table[3][4] = v
            bad(lc.SEC_SETTLE, lc.pack_settle(table), 3 * 6 + 4)
        kv = [0.0] * 6
        lc.validate_section(lc.SEC_MONITOR, lc.pack_monitor(0, kv))      # unset bits are not checked
        for v in (1e-6, 1.0):
            kv[2] = v
            lc.validate_section(lc.SEC_MONITOR, lc.pack_monitor(1 << 2, kv))
        for v in (2e-7, 1.0001, math.nan):
            kv[2] = v
            bad(lc.SEC_MONITOR, lc.pack_monitor(1 << 2, kv), 2)
        bad(lc.SEC_MONITOR, lc.pack_monitor(0x40, [0.01] * 6), 0)
        for limit in (1, 4095):
            lc.validate_section(lc.SEC_BIAS, lc.pack_bias(limit))
        for limit, pad in ((0, 0), (4096, 0), (10, 1)):
            bad(lc.SEC_BIAS, lc.pack_bias(limit, pad), 0)

    def test_structure(self):
        w = struct.pack
        for body, section in ((w("<HH", 2, 4) + b"\0" * 4 + w("<HH", 1, 4) + b"\0" * 4, 1),   # ids descend
                              (w("<HH", 5, 8) + b"\0" * 4, 5),                                # runs past the end
                              (w("<HH", 5, 3) + b"\0" * 4, 5),                                # not a multiple of 4
                              (meta_body() + b"\0\0", 0)):                                    # trailing bytes
            with self.assertRaises(lc.OcalValidationError) as c:
                lc.parse_body(body)
            self.assertEqual((c.exception.section, c.exception.index), (section, lc.STRUCTURE_INDEX))
        with self.assertRaises(lc.OcalValidationError):
            lc.build_body({})                                               # 0 bytes, below 8
        with self.assertRaises(lc.OcalValidationError):
            lc.build_body({lc.SEC_CLIP: b"\0" * 8})                         # a known id with a wrong length
        with self.assertRaises(lc.OcalValidationError):                     # 4 + 1992 + 4 + 600 > 0x9C0
            lc.build_body({lc.SEC_GAIN: default_gain(), 9: b"\0" * 600})
        body = lc.build_body({lc.SEC_META: lc.pack_meta(1, "x"), 9: b"abcd"})    # unknown ids pass
        self.assertEqual(lc.parse_body(body)[9], b"abcd")
        with self.assertRaises(lc.OcalValidationError):
            lc.validate_body(b"\0" * 6)

    def test_meta_and_monitor_helpers(self):
        t, note = lc.unpack_meta(lc.pack_meta(1700000000, "a" * 40))
        self.assertEqual((t, note), (1700000000, "a" * 28))                # the note is cut at 28
        self.assertEqual(lc.unpack_monitor(lc.pack_monitor(5, [0.5] * 6)), (5, [0.5] * 6))
        self.assertEqual(lc.unpack_settle(lc.pack_settle([[k] * 6 for k in range(1, 8)]))[2], [3] * 6)
        self.assertEqual(lc.unpack_clip(lc.pack_clip([(1, 4000)] * 16))[15], (1, 4000))

    def test_monitor_kv(self):
        self.assertEqual(lc.monitor_kv(1.0, 250.0, 3), 1.0 * 8 / 250.0)       # G[3] = 8
        self.assertAlmostEqual(lc.monitor_kv(1.0, 250.0, 3, 1.01), 8 / (250.0 * 1.01), places=12)
        for args in ((1.0, 250.0, 8), (0.0, 250.0, 1), (1.0, 0.0, 1), (math.nan, 250.0, 1),
                     (1.0, 250.0, 1, 0.0)):
            with self.assertRaises(ValueError):
                lc.monitor_kv(*args)
        self.assertEqual(lc.OCAL_GAIN_CODES, (1, 2, 4, 8, 16, 32, 64, 128))
        self.assertEqual(lc.OCAL_RANGE_OHM, (100, 1000, 10000, 100000))


class OcalRecords(unittest.TestCase):
    def test_parse_a_record(self):
        body = meta_body("hello")
        rec = lc.parse_ocal_record(fk.ocal_slot(fk.ocal_record(body, 5, shopen=0xCAFE)), 1)
        self.assertTrue(rec.valid)
        self.assertEqual((rec.slot, rec.format, rec.length, rec.sequence, rec.uid, rec.shopen_crc,
                          rec.revoked), (1, 1, 0x40 + 36, 5, fk.INVENTED_UID, 0xCAFE, False))
        self.assertEqual(rec.body, body)
        self.assertTrue(rec.in_force(fk.INVENTED_UID))
        self.assertFalse(rec.own(fk.OTHER_UID))
        self.assertIn("sequence 5", rec.describe())

    def test_revoke_word_is_outside_the_crc(self):
        rec = lc.parse_ocal_record(fk.ocal_slot(fk.ocal_record(meta_body(), 2, revoked=True)))
        self.assertTrue(rec.valid and rec.revoked)
        self.assertFalse(rec.in_force(fk.INVENTED_UID))

    def test_invalid_records(self):
        body = meta_body()
        good = bytearray(fk.ocal_slot(fk.ocal_record(body, 1)))
        bad_body = bytearray(good)
        bad_body[0x44] ^= 1
        cases = [(bytes(bad_body), "CRC-32"), (b"\xFF" * 0x2000, "no magic"),
                 (bytes(fk.ocal_slot(fk.ocal_record(body, 0))), "sequence"),
                 (bytes(fk.ocal_slot(fk.ocal_record(body, 0xFFFFFFFF))), "sequence"),
                 (bytes(fk.ocal_slot(fk.ocal_record(body, 1, length=101))), "length 101"),
                 (bytes(fk.ocal_slot(fk.ocal_record(body, 1, magic=0x4C41434E))), "no magic"),
                 (b"\0" * 10, "fewer")]
        for data, why in cases:
            rec = lc.parse_ocal_record(data)
            self.assertFalse(rec.valid, why)
            self.assertIn(why, rec.why)
        bad_hsize = bytearray(good)
        bad_hsize[6] = 0x44                          # header size field
        self.assertIn("header size", lc.parse_ocal_record(bytes(bad_hsize)).why)

    def test_active_record_rules(self):
        uid = fk.INVENTED_UID

        def rec(slot, seq, **kw):
            return lc.parse_ocal_record(fk.ocal_slot(fk.ocal_record(meta_body(), seq, **kw)), slot)
        a = lc.ocal_active_record([rec(0, 3), rec(1, 3)], uid)
        self.assertEqual(a.slot, 0)                                       # slot 0 wins a tie
        self.assertEqual(lc.ocal_active_record([rec(0, 3), rec(1, 4)], uid).slot, 1)
        self.assertIsNone(lc.ocal_active_record([rec(0, 9, fmt=2), rec(1, 3)], uid))   # OF_NEWER
        self.assertEqual(lc.ocal_active_record([rec(0, 9, uid=fk.OTHER_UID), rec(1, 3)], uid).slot, 1)
        self.assertEqual(lc.ocal_active_record([rec(0, 9, revoked=True), rec(1, 3)], uid).slot, 1)
        self.assertIsNone(lc.ocal_active_record([rec(0, 9, revoked=True), rec(1, 3, fmt=3)], uid))

    def test_parsers_of_info_raw_trace_monitor_event(self):
        d = bytearray(64)                                         # offsets of OCD 4.2, derived from the spec
        struct.pack_into("<I", d, 0, 0x01C7)
        d[4], d[5] = 1, 1
        struct.pack_into("<H", d, 6, 0x08A8)
        struct.pack_into("<III", d, 8, 7, 6, 0xAABBCCDD)
        d[20], d[21], d[22], d[23] = 0x1B, 0x04, 0x14, 0x04
        struct.pack_into("<H", d, 24, 120)
        struct.pack_into("<I", d, 28, 1700000000)
        d[32:37] = b"hello"
        i = lc.parse_ocal_info(bytes(d))
        self.assertEqual((i.flags, i.active_slot, i.format, i.active_length, i.active_sequence,
                          i.other_sequence, i.active_crc, i.sections_present, i.gain_frequencies,
                          i.monitor_frequencies, i.sections_rejected, i.t_max, i.meta_time,
                          i.meta_note),
                         (0x1C7, 1, 1, 0x8A8, 7, 6, 0xAABBCCDD, 0x1B, 4, 0x14, 4, 120, 1700000000,
                          "hello"))
        self.assertTrue(i.active and i.flag(lc.OF_RETAKE) and i.flag(lc.OF_LOCK))
        self.assertTrue(i.has_section(lc.SEC_GAIN) and not i.has_section(lc.SEC_CLIP))
        self.assertEqual((i.gain_hz(), i.monitor_hz(), i.rejected_names()),
                         ([1000], [1000, 100000], ["CLIP"]))
        with self.assertRaises(base.DeviceError):
            lc.parse_ocal_info(bytes(63))
        r = bytearray(64)                                         # OCD 4.1 0x35 offsets
        struct.pack_into("<I", r, 0, 41)
        r[4:8] = bytes([3, 4, 2, 1])
        struct.pack_into("<I", r, 8, 10000)
        struct.pack_into("<ff", r, 12, 0.5, 1.25)
        struct.pack_into("<H", r, 20, 30)
        r[22], r[23] = 1, 3
        struct.pack_into("<ffffff", r, 24, 100.0, -2.0, 2000.0, 1500.0, 1.0, 2.0)
        struct.pack_into("<HHHHHH", r, 48, 3000, 1000, 2000, 2900, 1100, 2001)
        raw = lc.parse_ocal_raw(bytes(r))
        self.assertEqual((raw.window, raw.cell, raw.locked, raw.frequency, raw.level, raw.bias,
                          raw.n, raw.cls, raw.flags), (41, (3, 4, 2), True, 10000, 0.5, 1.25, 30, 1, 3))
        self.assertEqual((raw.z_re, raw.z_im, raw.a_v, raw.a_i, raw.sigma2_v, raw.sigma2_i),
                         (100.0, -2.0, 2000.0, 1500.0, 1.0, 2.0))
        self.assertEqual((raw.max1, raw.min1, raw.mid1, raw.max2, raw.min2, raw.mid2),
                         (3000, 1000, 2000, 2900, 1100, 2001))
        self.assertTrue(raw.saturated and raw.overload)
        self.assertEqual(raw.znom, complex(100.0, -2.0))
        # variance of log Z: (1 / 2000^2 + 2 / 1500^2) / (64 * 30)
        self.assertAlmostEqual(raw.variance(), (1 / 4e6 + 2 / 2.25e6) / 1920, delta=1e-15)
        self.assertEqual(lc.parse_ocal_raw(fk.build_ocal_raw(a_v=0.0)).variance(), math.inf)
        t = fk.build_ocal_trace(3, 2, 1000, 32, [(1005, 1500.0, 0.0, 1, 2, 2, 1),
                                                  (1010, 1400.0, -1.0, 0, 2, 2, 1)])
        tr = lc.parse_ocal_trace(t)
        self.assertEqual((tr.state, tr.state_name, tr.kind, tr.change_tick, tr.count, len(tr.entries)),
                         (3, "full", 2, 1000, 32, 2))
        self.assertEqual((tr.entries[0].tick, tr.entries[0].discarded, tr.entries[1].z),
                         (1005, True, complex(1400.0, -1.0)))
        with self.assertRaises(base.DeviceError):
            lc.parse_ocal_trace(t[:-1])
        m = lc.parse_monitor(fk.build_monitor(1.5, 0.25, 100.0, 4.0, 0x0B, 2048, 2047, 9))
        self.assertEqual((m.v_rms, m.i_rms, m.a_v_ref, m.a_i_ref, m.mid1, m.mid2, m.window),
                         (1.5, 0.25, 100.0, 4.0, 2048, 2047, 9))
        self.assertTrue(m.v_calibrated and m.gain_applied and m.lock and not m.no_reading)
        self.assertEqual(len(fk.build_monitor()), 25)
        ev = lc.parse_event(0x98, struct.pack("<BII", 1, 7, 0x13))
        self.assertEqual((ev.name, ev.fields), ("OCAL_CHANGED", dict(slot=1, sequence=7, flags=0x13)))
        self.assertTrue(lc.parse_event(0x98, b"\x01").fields["malformed"])


class OcalCommands(unittest.TestCase):
    def bench(self, **kw):
        kw.setdefault("caps", fk.OCAL_CAPS)
        return Bench(self, **kw)

    def test_table_and_constants(self):
        self.assertEqual((lc.CAP_OCAL, fk.OCAL_CAPS, lc.EV_OCAL_CHANGED), (26, 0x05EFFFFF, 0x98))
        names = ["OCAL_INFO", "OCAL_READ", "OCAL_WRITE", "OCAL_ROLLBACK", "OCAL_LOCK", "OCAL_RAW",
                 "OCAL_TRACE", "MONITOR"]
        for code, name in zip(range(0x30, 0x38), names):
            c = lc.COMMANDS[code]
            self.assertEqual((c.name, c.cap_bit), (name, 26))
            self.assertEqual(c.dangerous, name in ("OCAL_WRITE", "OCAL_ROLLBACK"))
            self.assertEqual(c.timeout, 4.0 if c.dangerous else 1.5)

    def test_without_bit_26_nothing_is_sent_to_the_area(self):
        b = Bench(self, caps=fk.NEW_CAPS)
        with self.assertRaises(base.CapabilityError):
            b.dev.ocal_info()
        with self.assertRaises(base.CapabilityError):
            b.dev.ocal_write(meta_body(), "OK")
        with self.assertRaises(base.CapabilityError):
            b.dev.monitor()
        self.assertEqual([c for c, _p in b.meter.log if 0x30 <= c <= 0x37], [])
        self.assertEqual(b.meter.handle(0x30, b"")[0], 1)                  # the fake answers status 1

    def test_info_of_an_empty_area(self):
        b = self.bench()
        i = b.dev.ocal_info()
        self.assertEqual((i.flags, i.active_slot, i.format, i.active_length, i.active_sequence,
                          i.other_sequence, i.t_max, i.meta_time, i.meta_note, len(i.raw)),
                         (0, 0xFF, 0, 0, 0, 0, 26, 0, "", 64))
        self.assertFalse(i.active)
        self.assertIn("no active record", i.describe())

    def test_info_of_a_record_with_every_section(self):
        b = self.bench()
        gain = [lc.GainFreq() for _ in range(6)]
        gain[2] = lc.GainFreq(1 << 1, 1 << 1, 1 << 1, 1 << 1)          # cell c5 1, c6 1, c8 0: all four bits
        gain[0] = lc.GainFreq(1 << 1, 0, 1 << 1, 0)                     # magnitude only: not counted
        table = [[26] * 6 for _ in range(7)]
        table[1][3] = 120
        body = fk.ocal_body(meta=fk.ocal_meta(1700000000, "hello"), gain=lc.pack_gain(gain),
                            settle=lc.pack_settle(table),
                            monitor=lc.pack_monitor(0x14, [0, 0, 0.01, 0, 0.02, 0]))
        self.assertEqual(len(body), 36 + 1996 + 88 + 32)
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(body, 7, shopen=b.meter.shopen_crc))
        i = b.dev.ocal_info()
        # flags: active 1 + slot 0 valid 2 + retake 64 (GAIN with the current short / open CRC)
        self.assertEqual((i.flags, i.active_slot, i.format, i.active_length, i.active_sequence,
                          i.other_sequence), (67, 0, 1, 0x40 + len(body), 7, 0))
        self.assertEqual(i.active_crc, struct.unpack_from("<I", b.meter.slots[0], 0x3C)[0])
        self.assertEqual((i.sections_present, i.gain_frequencies, i.monitor_frequencies,
                          i.sections_rejected, i.t_max, i.meta_time, i.meta_note),
                         (0b11011, 4, 0x14, 0, 120, 1700000000, "hello"))
        self.assertEqual((i.gain_hz(), i.monitor_hz()), ([1000], [1000, 100000]))
        self.assertTrue(i.flag(lc.OF_RETAKE))
        b.meter.shopen_crc = 1                                          # a new short / open: retake done
        self.assertFalse(b.dev.ocal_info().flag(lc.OF_RETAKE))

    def test_info_rejects_a_bad_section_and_skips_unknown_ones(self):
        b = self.bench()
        clip = lc.pack_clip([(0, 4095)] * 3 + [(0, 3000)] + [(0, 4095)] * 12)
        body = fk.ocal_section(2, clip) + fk.ocal_section(3, b"\0" * 8) + fk.ocal_section(9, b"abcd")
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(body, 1))
        i = b.dev.ocal_info()
        # active 1 + slot 0 valid 2 + OF_SECT 32; CLIP (bit 2) and SETTLE (bit 3) rejected
        self.assertEqual((i.flags, i.sections_rejected, i.sections_present, i.t_max),
                         (35, 0b1100, 0, 26))
        self.assertEqual(i.rejected_names(), ["CLIP", "SETTLE"])

    def test_read_arguments_and_the_two_slots(self):
        b = self.bench()
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(meta_body(), 1))
        self.assertEqual(b.dev.ocal_read(0, 0, 4), b"OCAL")                 # 0x4C41434F little-endian
        self.assertEqual(b.sent(lc.OCAL_READ)[-1][1], struct.pack("<BHH", 0, 0, 4))
        for args in ((2, 0, 4), (0, 0, 0), (0, 0, 257), (0, 0x1F01, 256), (0, -1, 4)):
            with self.assertRaises(ValueError):
                b.dev.ocal_read(*args)
        both = b.dev.ocal_read_slots()
        self.assertEqual(len(both), 0x4000)
        self.assertEqual(both[:0x2000], bytes(b.meter.slots[0]))
        self.assertEqual(both[0x2000:], b"\xFF" * 0x2000)
        self.assertEqual(len(b.sent(lc.OCAL_READ)), 1 + 64)                 # 2 x 32 requests of 256 bytes
        recs = b.dev.ocal_records()
        self.assertEqual([r.valid for r in recs], [True, False])

    def test_backup_file_is_named_bound_and_not_repeated(self):
        b = self.bench()
        folder = b.tmp / "bk"
        p = b.dev.ocal_backup(folder)
        self.assertEqual(p, folder / ("LC1020E-" + UID_HEX) / ("LC1020E-%s-20261121-100001.ocal" % UID_HEX))
        self.assertEqual(p.read_bytes(), b"\xFF" * 0x4000)
        self.assertEqual(b.dev.ocal_backup(folder), p)                     # same bytes: no new file
        self.assertEqual(len(list(p.parent.glob("*.ocal"))), 1)
        b.meter.slots[1] = fk.ocal_slot(fk.ocal_record(meta_body(), 2))
        q = b.dev.ocal_backup(folder)
        self.assertNotEqual(q, p)
        self.assertEqual(q.read_bytes()[0x2000:0x2004], b"OCAL")
        self.assertEqual(b.dev.list_ocal_backups(folder), [q, p])         # newest first
        self.assertEqual(lc.LC1020E.load_ocal_file(q, UID_HEX), q.read_bytes())

    def test_backup_refuses_another_meter(self):
        b = self.bench()
        b.meter.uid = fk.OTHER_UID
        with self.assertRaises(lc.BackupMismatch):
            b.dev.ocal_backup(b.tmp / "bk")
        self.assertFalse((b.tmp / "bk").exists())

    def test_write_backs_up_first_and_fills_slot_0(self):
        b = self.bench()
        ev = []
        b.dev.meter_event.connect(ev.append)
        body = meta_body("first")
        res = b.dev.ocal_write(body, "OK")
        self.assertEqual((res.result, res.slot, res.sequence, res.ok), (0, 0, 1, True))
        codes = [c for c, _p, _t in b.link.requests]
        self.assertEqual(codes.count(lc.OCAL_READ), 64)
        self.assertLess(max(i for i, c in enumerate(codes) if c == lc.OCAL_READ),
                        codes.index(lc.OCAL_WRITE))                          # backup first
        crc = zlib.crc32(body) & 0xFFFFFFFF
        self.assertEqual(b.sent(lc.OCAL_WRITE)[0][1], struct.pack("<HHHI", 0x4F4B, 0, 36, crc) + body)
        self.assertEqual(b.link.requests[codes.index(lc.OCAL_WRITE)][2], 4.0)
        file = next((b.tmp / "bk").rglob("*.ocal"))
        self.assertEqual(file.read_bytes(), b"\xFF" * 0x4000)                # as it was before the write
        rec = lc.parse_ocal_record(bytes(b.meter.slots[0]), 0)
        self.assertEqual((rec.valid, rec.sequence, rec.uid, rec.length, rec.body),
                         (True, 1, fk.INVENTED_UID, 100, body))
        self.assertEqual(rec.shopen_crc, b.meter.shopen_crc)
        i = b.dev.ocal_info()
        self.assertEqual((i.flags, i.active_slot, i.active_sequence, i.meta_note), (3, 0, 1, "first"))
        self.assertEqual([(e.name, e.fields) for e in ev],
                         [("OCAL_CHANGED", dict(slot=0, sequence=1, flags=3))])

    def test_chunks(self):
        b = self.bench()
        body = meta_body()
        b.dev.ocal_write(body, "OK", chunk=10)
        sent = [p for _c, p in b.sent(lc.OCAL_WRITE)]
        self.assertEqual([struct.unpack_from("<H", p, 2)[0] for p in sent], [0, 10, 20, 30])
        self.assertEqual([len(p) - 10 for p in sent], [10, 10, 10, 6])
        self.assertEqual(b"".join(p[10:] for p in sent), body)
        self.assertEqual({p[4:10] for p in sent}, {struct.pack("<HI", 36, zlib.crc32(body) & 0xFFFFFFFF)})
        for chunk in (0, 503):
            with self.assertRaises(ValueError):
                b.dev.ocal_write(body, "OK", chunk=chunk)
        self.assertEqual(lc.parse_ocal_record(bytes(b.meter.slots[0])).body, body)

    def test_slots_alternate_and_sequences_count(self):
        b = self.bench()
        got = [b.dev.ocal_write(meta_body(n), "OK") for n in "abc"]
        self.assertEqual([(r.slot, r.sequence) for r in got], [(0, 1), (1, 2), (0, 3)])
        i = b.dev.ocal_info()
        self.assertEqual((i.active_slot, i.active_sequence, i.other_sequence), (0, 3, 2))
        self.assertEqual(len(list((b.tmp / "bk").rglob("*.ocal"))), 3)       # one backup before each

    def test_refusals_without_confirmation_validation_or_folder(self):
        b = self.bench()
        with self.assertRaises(base.SafetyRefused):
            b.dev.ocal_write(meta_body(), "ok")
        bad = struct.pack("<HH", 2, 64) + lc.pack_clip([(0, 3000)] * 16)
        with self.assertRaises(lc.OcalValidationError):
            b.dev.ocal_write(bad, "OK")
        with self.assertRaises(base.SafetyRefused):
            b.dev.ocal_rollback(0, "no")
        self.assertEqual(b.link.requests, [])                              # nothing was sent at all
        nofolder = lc.LC1020E(b.link, base.Info.parse(b.meter.info()), backup_folder=None)
        with self.assertRaises(base.SafetyRefused):
            nofolder.ocal_write(meta_body(), "OK")
        with self.assertRaises(base.SafetyRefused):
            nofolder.ocal_rollback(0, "OK")
        self.assertEqual(b.link.requests, [])

    def test_meter_side_validation_and_crc(self):
        b = self.bench()
        clip = lc.pack_clip([(0, 4095)] * 3 + [(0, 3000)] + [(0, 4095)] * 12)
        body = struct.pack("<HH", 2, 64) + clip                             # the host would refuse it
        crc = zlib.crc32(body) & 0xFFFFFFFF
        with self.assertRaises(base.StatusError) as c:
            b.dev._ocal_write_once(body, 502, crc)
        self.assertEqual((c.exception.status, c.exception.data), (2, bytes([2, 3, 0])))
        self.assertIn("CLIP entry 3", c.exception.detail)
        with self.assertRaises(base.StatusError) as c:
            b.dev._ocal_write_once(meta_body(), 502, 12345)
        self.assertEqual((c.exception.status, c.exception.data), (7, b""))
        self.assertEqual(b.meter.ocal_ops, [])
        self.assertEqual(b.meter.slots[0], bytearray(b"\xFF" * 0x2000))
        self.assertIsNone(b.meter.ocal_xfer)

    def test_transfer_rules(self):
        b = self.bench()
        body = meta_body()
        crc = zlib.crc32(body) & 0xFFFFFFFF
        head = lambda off, total=36, c=crc: struct.pack("<HHHI", 0x4F4B, off, total, c)    # noqa: E731
        h = b.meter.handle
        self.assertEqual(h(lc.OCAL_WRITE, head(0) + body[:10]), (0, struct.pack("<H", 10)))
        self.assertEqual(h(lc.OCAL_WRITE, head(20) + body[20:30])[0], 2)               # not the offset asked
        self.assertEqual(h(lc.OCAL_WRITE, head(10, 40) + body[10:20])[0], 2)           # another total
        self.assertEqual(h(lc.OCAL_WRITE, head(10, 36, crc ^ 1) + body[10:20])[0], 2)  # another CRC
        self.assertEqual(h(lc.OCAL_WRITE, head(10) + body[10:] + b"xx")[0], 2)         # past the total
        self.assertEqual(h(lc.OCAL_WRITE, head(10) + body[10:20]), (0, struct.pack("<H", 20)))
        self.assertEqual(h(lc.OCAL_WRITE, head(0) + body[:10]), (0, struct.pack("<H", 10)))   # offset 0 restarts
        self.assertEqual(h(lc.OCAL_WRITE, b"\x4b\x4f\0\0")[0], 2)                      # too short
        self.assertEqual(h(lc.OCAL_WRITE, struct.pack("<HHHI", 0x4F4C, 0, 36, crc) + body[:10])[0], 2)
        self.assertEqual(h(lc.OCAL_WRITE, struct.pack("<HHHI", 0x4F4B, 0, 6, crc) + body[:6])[0], 2)
        self.assertEqual(h(lc.OCAL_WRITE, head(0, 36) + b"")[0], 2)                   # no chunk
        # the transfer sits at offset 10 again: its last chunk completes the body and commits
        self.assertEqual(h(lc.OCAL_WRITE, head(10) + body[10:]), (0, struct.pack("<BBI", 0, 0, 1)))
        self.assertEqual(h(lc.OCAL_WRITE, head(10) + body[10:])[0], 2)              # closed: offset 10 is not a start

    def test_refused_last_chunk_leaves_the_transfer_open(self):
        b = self.bench()
        body = meta_body()
        for flag, status in (("off_mode", 3), ("cal_running", 6), ("job_active", 4), ("rewrite_a", 4)):
            setattr(b.meter, flag, True)
            with self.assertRaises(base.StatusError) as c:
                b.dev.ocal_write(body, "OK", chunk=20)
            self.assertEqual(c.exception.status, status, flag)
            self.assertIsNotNone(b.meter.ocal_xfer)
            self.assertEqual(b.meter.ocal_ops, [])
            setattr(b.meter, flag, False)
        b.meter.off_mode = b.meter.cal_running = True                       # the order of OCD 4.3: 3 first
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_write(body, "OK", chunk=20)
        self.assertEqual(c.exception.status, 3)
        b.meter.off_mode = b.meter.cal_running = False
        crc = zlib.crc32(body) & 0xFFFFFFFF
        # the same last chunk again, as the host may after a refusal (the transfer sits at offset 0 of
        # the 20-byte chunk's predecessor: restarted here with the whole first chunk)
        self.assertEqual(b.meter.handle(lc.OCAL_WRITE, struct.pack("<HHHI", 0x4F4B, 0, 36, crc) + body[:20])[0], 0)
        self.assertEqual(b.meter.handle(lc.OCAL_WRITE, struct.pack("<HHHI", 0x4F4B, 20, 36, crc) + body[20:]),
                         (0, struct.pack("<BBI", 0, 0, 1)))

    def test_flash_failure_and_the_retry(self):
        b = self.bench()
        b.meter.ocal_fail_attempts = 2
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_write(meta_body(), "OK")
        self.assertEqual(c.exception.status, 5)
        self.assertEqual((c.exception.record.result, c.exception.record.slot, c.exception.record.sequence),
                         (5, 0xFF, 0))
        self.assertIn("read-back differs", c.exception.record.describe())
        self.assertEqual(b.meter.ocal_ops, [("erase", 0, 0), ("erase", 0, 0)])
        self.assertFalse(b.dev.ocal_info().active)
        b.meter.ocal_ops.clear()
        b.meter.ocal_fail_attempts = 1                                       # the retry succeeds
        res = b.dev.ocal_write(meta_body(), "OK")
        self.assertEqual((res.slot, res.sequence), (0, 1))
        # one failed attempt, then 1 page erase, 100 / 4 - 1 = 24 words (the CRC word is last), the CRC word
        self.assertEqual(b.meter.ocal_ops, [("erase", 0, 0), ("erase", 0, 0), ("program", 0, 24),
                                            ("crc", 0, 1)])

    def test_the_previous_record_survives_a_failed_write(self):
        b = self.bench()
        b.dev.ocal_write(meta_body("a"), "OK")
        before = bytes(b.meter.slots[0])
        b.meter.ocal_fail_attempts = 2
        with self.assertRaises(base.StatusError):
            b.dev.ocal_write(meta_body("b"), "OK")
        self.assertEqual(bytes(b.meter.slots[0]), before)
        i = b.dev.ocal_info()
        self.assertEqual((i.active_slot, i.active_sequence), (0, 1))

    def test_rollback_modes_and_the_target_after_it(self):
        b = self.bench()
        b.dev.ocal_write(meta_body("a"), "OK")               # slot 0, sequence 1
        b.dev.ocal_write(meta_body("b"), "OK")               # slot 1, sequence 2: active
        r = b.dev.ocal_rollback(0, "OK")
        self.assertEqual((r.slot, r.sequence, r.flags, r.active), (0, 1, 7, True))      # active 1 + both valid 2 + 4
        self.assertEqual(b.sent(lc.OCAL_ROLLBACK)[-1][1], b"\x4b\x4f\x00")
        self.assertEqual(b.meter.ocal_ops[-1], ("revoke", 1, 1))
        self.assertIn("slot 0", r.describe())
        res = b.dev.ocal_write(meta_body("c"), "OK")         # the revoked slot is the target (OCD 1.4 step 1 a)
        self.assertEqual((res.slot, res.sequence), (1, 3))
        b.meter.ocal_ops.clear()
        r = b.dev.ocal_rollback(1, "OK")                      # every own format-1 record, the older first
        self.assertEqual((r.slot, r.sequence, r.flags, r.active), (0xFF, 0, 6, False))
        self.assertEqual(b.meter.ocal_ops, [("revoke", 0, 1), ("revoke", 1, 1)])
        self.assertEqual(b.sent(lc.OCAL_ROLLBACK)[-1][1], b"\x4b\x4f\x01")
        self.assertIn("no record active", r.describe())
        for mode in (0, 1):                                   # nothing qualifies any more: status 3
            with self.assertRaises(base.StatusError) as c:
                b.dev.ocal_rollback(mode, "OK")
            self.assertEqual(c.exception.status, 3)
        with self.assertRaises(ValueError):
            b.dev.ocal_rollback(2, "OK")
        codes = [c for c, _p, _t in b.link.requests]
        self.assertLess(codes.index(lc.OCAL_READ), codes.index(lc.OCAL_ROLLBACK))

    def test_rollback_refusals(self):
        b = self.bench()
        b.dev.ocal_write(meta_body(), "OK")
        for flag, status in (("off_mode", 3), ("cal_running", 6), ("job_active", 4)):
            setattr(b.meter, flag, True)
            with self.assertRaises(base.StatusError) as c:
                b.dev.ocal_rollback(0, "OK")
            self.assertEqual(c.exception.status, status)
            setattr(b.meter, flag, False)
        self.assertEqual(b.dev.ocal_info().active_slot, 0)

    def test_foreign_record_rules(self):
        b = self.bench()
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(meta_body("x"), 5, uid=fk.OTHER_UID))
        i = b.dev.ocal_info()
        self.assertEqual((i.flags, i.active_slot), (0x12, 0xFF))              # slot 0 valid + OF_UID
        foreign = bytes(b.meter.slots[0])
        res = b.dev.ocal_write(meta_body("mine"), "OK")                      # the empty slot, sequence 5 + 1
        self.assertEqual((res.slot, res.sequence), (1, 6))
        self.assertEqual(bytes(b.meter.slots[0]), foreign)
        i = b.dev.ocal_info()
        self.assertEqual((i.flags, i.active_slot, i.other_sequence), (23, 1, 5))
        # a newer foreign record next to this meter's own: the own one is active, the foreign is overwritten
        b = self.bench()
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(meta_body("x"), 9, uid=fk.OTHER_UID))
        b.meter.slots[1] = fk.ocal_slot(fk.ocal_record(meta_body("y"), 3))
        self.assertEqual(b.dev.ocal_info().active_slot, 1)
        res = b.dev.ocal_write(meta_body("z"), "OK")
        self.assertEqual((res.slot, res.sequence), (0, 10))

    def test_newer_format_is_not_applied_and_the_older_slot_is_overwritten(self):
        b = self.bench()
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(meta_body("n"), 9, fmt=2))
        b.meter.slots[1] = fk.ocal_slot(fk.ocal_record(meta_body("o"), 3))
        i = b.dev.ocal_info()
        self.assertEqual((i.flags, i.active_slot), (14, 0xFF))                # both valid 2 + 4, OF_NEWER 8
        res = b.dev.ocal_write(meta_body("w"), "OK")                         # the format-1 slot, sequence 10
        self.assertEqual((res.slot, res.sequence), (1, 10))
        self.assertEqual(b.dev.ocal_info().active_slot, 1)

    def test_sequence_overflow_is_refused(self):
        b = self.bench()
        b.meter.slots[0] = fk.ocal_slot(fk.ocal_record(meta_body(), 0xFFFFFFFE))
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_write(meta_body(), "OK")
        self.assertEqual(c.exception.status, 3)
        self.assertEqual(b.meter.ocal_ops, [])

    def test_restore_a_record_from_a_backup_file(self):
        b = self.bench()
        body_a, body_b = meta_body("A"), meta_body("B")
        b.dev.ocal_write(body_a, "OK")
        b.dev.ocal_write(body_b, "OK")
        path = b.dev.ocal_backup(b.tmp / "bk")
        res = b.dev.ocal_restore(path, 0, "OK")                              # slot 0's record, written again
        self.assertEqual((res.slot, res.sequence), (0, 3))                   # the active one is slot 1
        rec = lc.parse_ocal_record(bytes(b.meter.slots[0]))
        self.assertEqual((rec.sequence, rec.body), (3, body_a))
        with self.assertRaises(base.SafetyRefused):
            b.dev.ocal_restore(path, 0, "no")

    def test_restore_refusals_send_nothing(self):
        b = self.bench()
        body = meta_body("A")
        b.dev.ocal_write(body, "OK")
        path = b.dev.ocal_backup(b.tmp / "bk")
        n = len(b.link.requests)
        other = path.with_name(path.name.replace(UID_HEX, fk.OTHER_UID.hex().upper()))
        shutil.copy(path, other)
        with self.assertRaises(lc.BackupMismatch) as c:
            b.dev.ocal_restore(other, 0, "OK")                               # another UID in the name
        self.assertIn("refused", str(c.exception))
        named = path.with_name("LC1020E-%s-20261121-120000.ocal" % UID_HEX)
        slots = [bytes(fk.ocal_slot(fk.ocal_record(body, 4, uid=fk.OTHER_UID))),
                 bytes(fk.ocal_slot(fk.ocal_record(body, 5, fmt=2)))]
        named.write_bytes(slots[0] + slots[1])
        for slot, why in ((0, "carries UID"), (1, "format 2")):
            with self.assertRaises(lc.BackupMismatch) as c:
                b.dev.ocal_restore(named, slot, "OK")
            self.assertIn(why, str(c.exception))
        named.write_bytes(b"\xFF" * 0x4000)
        with self.assertRaises(lc.BackupMismatch) as c:
            b.dev.ocal_restore(named, 1, "OK")
        self.assertIn("no valid record", str(c.exception))
        short = path.with_name("LC1020E-%s-20261121-130000.ocal" % UID_HEX)
        short.write_bytes(b"\0" * 100)
        for p in (short, path.with_name("backup.ocal")):
            with self.assertRaises(lc.BackupMismatch):
                b.dev.ocal_restore(p, 0, "OK")
        with self.assertRaises(ValueError):
            b.dev.ocal_restore(path, 2, "OK")
        self.assertEqual(len(b.link.requests), n)

    def test_update_sections_keeps_the_others(self):
        b = self.bench()
        info, secs = b.dev.ocal_active_sections()
        self.assertEqual((info.active, secs), (False, {}))
        clip = lc.pack_clip([(0, 4095)] * 16)
        b.dev.ocal_write(lc.build_body({lc.SEC_META: lc.pack_meta(5, "old"), lc.SEC_CLIP: clip}), "OK")
        settle = lc.pack_settle([[40] * 6 for _ in range(7)])
        res = b.dev.ocal_update_sections({lc.SEC_SETTLE: settle}, "OK", note="gain")
        self.assertEqual((res.slot, res.sequence), (1, 2))
        info, secs = b.dev.ocal_active_sections()
        self.assertEqual((info.active_slot, sorted(secs)), (1, [0, 2, 3]))
        self.assertEqual((secs[lc.SEC_CLIP], secs[lc.SEC_SETTLE]), (clip, settle))
        t, note = lc.unpack_meta(secs[lc.SEC_META])
        self.assertEqual(note, "gain")
        self.assertTrue(1.7e9 < t < 1.9e9)
        b.dev.ocal_update_sections({lc.SEC_CLIP: None}, "OK")                  # a section dropped
        info, secs = b.dev.ocal_active_sections()
        self.assertEqual(sorted(secs), [0, 3])
        self.assertEqual(lc.unpack_meta(secs[lc.SEC_META])[1], "gain")         # META kept without a note
        with self.assertRaises(base.SafetyRefused):
            b.dev.ocal_update_sections({}, "x")

    def test_lock_release_raw_and_waiting(self):
        b = self.bench()
        raw = b.dev.ocal_lock(1, 2, 3)
        self.assertEqual(b.sent(lc.OCAL_LOCK)[-1][1], bytes([1, 2, 3]))
        self.assertEqual((raw.cell, raw.locked, raw.frequency, raw.window), ((1, 2, 3), True, 1000, 101))
        raw = b.dev.ocal_release()
        self.assertEqual(b.sent(lc.OCAL_LOCK)[-1][1], b"\xFF\xFF\xFF")
        self.assertFalse(raw.locked)
        self.assertEqual(raw.cell, (2, 2, 1))                                  # |Z| 1000: range code 1
        for args in ((8, 0, 0), (0, 8, 0), (0, 0, 4), (-1, 0, 0)):
            with self.assertRaises(ValueError):
                b.dev.ocal_lock(*args)
        before = b.dev.ocal_raw().window
        self.assertEqual(b.dev.ocal_wait_window(before, 2).window, before + 2)     # the fake counts +1 per read
        with self.assertRaises(base.DeviceError):
            b.dev.ocal_wait_window(10 ** 6, 1, timeout=3.0)

    def test_lock_refusals(self):
        b = self.bench()
        b.meter.screen = 2
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_lock(1, 1, 1)
        self.assertEqual(c.exception.status, 3)
        b.meter.screen = 1
        b.meter.cal_running = True
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_lock(1, 1, 1)
        self.assertEqual(c.exception.status, 6)
        b.meter.cal_running, b.meter.off_mode = False, True
        with self.assertRaises(base.StatusError) as c:
            b.dev.ocal_release()
        self.assertEqual(c.exception.status, 3)
        b.meter.off_mode = False
        b.dev.ocal_lock(1, 1, 1)
        with self.assertRaises(base.StatusError) as c:
            b.dev.lcr_set(5, 0)                                                # the range, while locked: OCD 2.2
        self.assertEqual(c.exception.status, 3)

    def test_trace(self):
        b = self.bench()
        self.assertEqual(b.dev.ocal_trace_arm().state, 1)
        self.assertEqual(b.sent(lc.OCAL_TRACE)[-1][1], bytes([1, 0]))
        b.dev.lcr_set(2, 1)                                                    # the traced change: a frequency
        t = b.dev.ocal_trace_read_all()
        self.assertEqual([p for _c, p in b.sent(lc.OCAL_TRACE)][-2:], [bytes([2, 0]), bytes([2, 31])])
        self.assertEqual((t.state_name, t.kind, t.change_tick, t.count, len(t.entries)), ("full", 1, 1000, 32, 32))
        self.assertEqual([e.tick for e in t.entries[:3]], [1005, 1010, 1015])
        self.assertEqual([e.discarded for e in t.entries[:3]], [True, True, False])
        self.assertFalse(any(e.out_of_band for e in t.entries))
        # PROTOCOL 5.7 0x36 flags bit 2: discarded by the ranging band rule
        e = lc.TraceEntry(0, 0.0, 0.0, lc.TRACE_OUT_OF_BAND | lc.TRACE_SATURATED, 0, 2, 3)
        self.assertEqual((e.out_of_band, e.saturated, e.discarded), (True, True, False))
        self.assertAlmostEqual(t.entries[0].z.real, 1500.0, delta=1e-3)        # 1000 + 500 exp(0)
        self.assertAlmostEqual(t.entries[31].z.real, 1000.0 + 500 * math.exp(-31 / 4), delta=1e-2)
        self.assertEqual(b.dev.ocal_trace_disarm().state, 0)
        one = b.dev.ocal_trace_read(5)
        self.assertEqual((one.state, one.entries), (0, []))
        for start in (-1, 32):
            with self.assertRaises(ValueError):
                b.dev.ocal_trace_read(start)

    def test_trace_wait_has_a_deadline(self):
        b = self.bench()
        b.dev.ocal_trace_arm()
        with self.assertRaises(base.DeviceError):
            b.dev.ocal_wait_trace(timeout=3.0)

    def test_monitor(self):
        b = self.bench()
        m = b.dev.monitor()
        self.assertFalse(m.v_calibrated)
        self.assertTrue(math.isnan(m.v_rms) and math.isnan(m.i_rms))
        b.meter.monitor_values = dict(v=1.0, i=0.5, a_v=250.0, a_i=4.0, flags=0x03)
        m = b.dev.monitor()
        self.assertEqual((m.v_rms, m.i_rms, m.a_v_ref, m.a_i_ref, m.v_calibrated, m.gain_applied,
                          m.no_reading, m.lock), (1.0, 0.5, 250.0, 4.0, True, True, False, False))

    def test_measure_byte_65(self):
        b = self.bench()
        b.meter.ocal_flags = 0b0101
        m = b.dev.measure()
        self.assertEqual((len(m.raw), m.markers, m.ocal_flags, m.gain_applied, m.gain_missing),
                         (66, 0, 5, True, False))
        b.meter.ocal_flags = 0b0010
        m = b.dev.lcr_set(2, 1)                                                # LCR_SET answers the same record
        self.assertEqual((m.ocal_flags, m.gain_applied, m.gain_missing), (2, False, True))
        self.assertEqual(b.dev.measure_reading().gain_flags, 2)
        # without bit 26 the record has 65 bytes and byte 65 is never read
        old = Bench(self, caps=fk.NEW_CAPS)
        m = old.dev.measure()
        self.assertEqual((len(m.raw), m.ocal_flags, m.gain_applied), (65, None, False))
        self.assertIsNone(old.dev.measure_reading().gain_flags)

    def test_parse_measure_with_and_without_the_capability(self):
        rec = fk.build_measure(markers=1, ocal=0b1001)
        self.assertEqual(len(rec), 66)
        m = lc.parse_measure(rec, True)
        self.assertEqual((len(m.raw), m.markers, m.ocal_flags), (66, 1, 9))
        self.assertTrue(m.gain_applied and not m.gain_missing)
        m = lc.parse_measure(rec)                                              # the capability bit is clear
        self.assertEqual((len(m.raw), m.markers, m.ocal_flags), (65, 1, None))
        m = lc.parse_measure(fk.build_measure(markers=0), True)                # 65 bytes: no byte 65
        self.assertEqual((len(m.raw), m.ocal_flags), (65, None))
        self.assertEqual(lc.parse_measure(fk.build_measure(ocal=0)).raw, fk.build_measure(ocal=0)[:65])
        r = lc.measure_to_reading(lc.parse_measure(rec, True), 1.0, 2.0)
        self.assertEqual(r.gain_flags, 9)

    def test_stream_reading_carries_the_gain_flags(self):
        b = self.bench()
        got = []
        b.dev.reading.connect(got.append)
        b.dev.start_live()
        b.link.emit_stream(lc.MEASURE, fk.build_measure(markers=0, ocal=2))
        self.assertEqual(got[0].gain_flags, 2)
        b.dev.stop_live()


class OcalProcedures(unittest.TestCase):
    """The collection procedures of core/ocal_run.py against the fake meter."""

    def setUp(self):
        from fnirsi_companion.core import ocal_run, ocal_solve
        self.run, self.solve = ocal_run, ocal_solve
        self.b = Bench(self, caps=fk.OCAL_CAPS)

    def test_meter_points(self):
        # the fake's frequency list: 100, 1000, 10000, 100000, 120, 60 (indices 0..5)
        by_fi, levels = self.run.meter_points(self.b.dev)
        self.assertEqual(by_fi, {0: 0, 1: 4, 2: 1, 3: 2, 4: 3, 5: 5})
        self.assertEqual([i for _v, i in levels], [0, 1])
        self.assertAlmostEqual(levels[0][0], 0.1, places=6)
        self.assertAlmostEqual(levels[1][0], 0.6, places=6)

    def test_cells(self):
        near = self.run.neighbour_cells((2, 2, 1))
        self.assertEqual(len(near), 10)                       # c8 +-1, c5 +-1 +-2, c6 +-1 +-2: all exist
        self.assertEqual(len(self.run.neighbour_cells((0, 0, 0))), 5)       # only the positive steps
        self.assertEqual(len(self.run.neighbour_cells((7, 7, 3))), 5)
        self.assertEqual(self.run.nearest_cell((2, 2, 0), {(5, 5, 3), (3, 2, 0)}), (3, 2, 0))
        self.assertEqual(self.run.nearest_cell((2, 2, 0), {(2, 2, 0), (3, 2, 0)}), (2, 2, 0))
        self.assertEqual(self.run.nearest_cell((2, 2, 0), set()), (2, 2, 0))

    def test_collect_a_resistor(self):
        progress = []
        windows, notes = self.run.collect_resistor(self.b.dev, 3, lambda f, t: progress.append((f, t)))
        # 6 frequencies x 2 levels = 12 points; each: the autoranged cell (2, 2, 1) + its 10 neighbours
        self.assertEqual((len(windows), len(notes)), (132, 0))
        self.assertEqual(sum(w.autoranged for w in windows), 12)
        self.assertTrue(all(w.znom == 1000 and w.resistor == 3 for w in windows))
        self.assertEqual({w.cell for w in windows if w.autoranged}, {(2, 2, 1)})
        self.assertEqual({w.fi for w in windows}, {0, 1, 2, 3, 4, 5})
        self.assertAlmostEqual(windows[0].var, 2 / 4e6 / 1920, delta=1e-15)
        self.assertIsNone(self.b.meter.lock)                                  # released at the end
        self.assertEqual(progress[-1][0], 1.0)

    def test_weak_and_saturated_cells_are_dropped(self):
        self.b.meter.weak_cells = {(3, 2, 1)}                                # 100 counts: below 300
        windows, notes = self.run.collect_resistor(self.b.dev, 0)
        self.assertEqual((len(windows), len(notes)), (120, 0))               # 12 points x (1 + 9)
        self.assertNotIn((3, 2, 1), {w.cell for w in windows})
        self.b.meter.weak_cells = set()
        self.b.meter.saturated_cells = {(2, 2, 1)}                           # the autoranged cell
        windows, notes = self.run.collect_resistor(self.b.dev, 0)
        self.assertEqual((len(windows), len(notes)), (120, 12))              # no autoranged window, a note each
        self.assertFalse(any(w.autoranged for w in windows))

    def test_short_and_open(self):
        self.b.meter.dut_fn = lambda hz: 0.05 + 0.01j                        # the short bar; |Z| < 300: c8 0
        windows, notes = self.run.collect_shortopen(self.b.dev, "short", 2)
        self.assertEqual((len(windows), len(notes)), (96, 0))                # 12 points x 8 windows
        self.assertTrue(all(w.kind == "short" and w.take == 2 and w.cell == (2, 2, 0) for w in windows))
        self.assertAlmostEqual(windows[0].znom.real, 0.05, places=6)
        self.assertAlmostEqual(windows[0].znom.imag, 0.01, places=6)
        windows, notes = self.run.collect_shortopen(self.b.dev, "short", 1, {(5, 5, 3)})
        self.assertEqual((len(windows), len(notes)), (96, 12))
        self.assertEqual({w.cell for w in windows}, {(5, 5, 3)})
        self.b.meter.saturated_cells = {(2, 2, 0)}
        windows, notes = self.run.collect_shortopen(self.b.dev, "open", 1)
        self.assertEqual((len(windows), len(notes)), (0, 12))
        with self.assertRaises(ValueError):
            self.run.collect_shortopen(self.b.dev, "other")
        self.assertIsNone(self.b.meter.lock)

    def test_clip_sweep(self):
        obs = self.run.sweep_clip(self.b.dev)
        self.assertEqual(len(obs), 16)
        self.assertEqual({obs[(0, c)] for c in range(8)}, {(1000, 3000)})     # (min1, max1) of the fake's raw
        self.assertEqual({obs[(1, c)] for c in range(8)}, {(1100, 2900)})     # (min2, max2)
        total = self.run.merge_clip({(0, 0): (10, 100)}, {(0, 0): (5, 90), (1, 1): (1, 2)})
        self.assertEqual(total, {(0, 0): (5, 100), (1, 1): (1, 2)})
        prop = self.solve.propose_clip(obs, 16)
        self.assertEqual((prop[0], prop[8]), ((1016, 2984), (1116, 2884)))
        self.assertIsNone(self.b.meter.lock)

    def test_trace_kinds(self):
        for kind in (1, 2, 6):                    # the fake offers LCR_TABLES lists 1..3 only (no bias, speed)
            trace, ms = self.run.measure_trace(self.b.dev, kind, 2)
            self.assertEqual((trace.kind, len(trace.entries)), (kind, 32))
            self.assertEqual(ms, 55)          # derived in test_ocal_solve (tau 4 acquisitions of 5 ms)
            self.assertEqual(self.b.meter.trace["state"], 0)                  # disarmed
            self.assertIsNone(self.b.meter.lock)
        for kind in (0, 5, 7):
            with self.assertRaises(ValueError):
                self.run.measure_trace(self.b.dev, kind, 2)
        old = Bench(self, caps=fk.OCAL_CAPS & ~(1 << lc.CAP_FREQ_60HZ))
        with self.assertRaises(base.DeviceError):
            self.run.measure_trace(old.dev, 2, 5)                             # no 60 Hz in the meter's list


if __name__ == "__main__":
    unittest.main()
