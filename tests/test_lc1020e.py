"""Tests of devices/base.py and devices/lc1020e.py against the fake LC1020E (tests/fake_lc1020e.py).
No serial port is opened; the UIDs are invented. The command table is checked against the
command tables parsed from PROTOCOL.md of fnirsi-lc1020e-firmware-rewrite (sibling checkout, or
the file named by the environment variable FNIRSI_LC1020E_PROTOCOL)."""

import json
import os
import re
import struct
import sys
import tempfile
import unittest
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
        self.assertEqual(sorted(lc.COMMANDS), sorted(self.spec))

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
            19: ["FACTORY_MODE"], 20: ["SETUP_LANGUAGE"],
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
                    "| COMPARE_SET | job, 60 000 ms", "| CALIBRATE | job, 120 000 ms"):
            self.assertIn(row, self.text)


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
        b = Bench(self)
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


if __name__ == "__main__":
    unittest.main()
