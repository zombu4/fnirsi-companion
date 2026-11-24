"""UI tests (offscreen): the main window with a fake watcher and the fake meters of both models on
real SerialLinks (FakeSerial as the serial_factory result). Auto-detect and switch, unplug /
replug, identification refusals, the typed confirmation, an exclusive job blocking the switch,
every page built and its main actions driven against the fakes, the update-drive page.

Run: QT_QPA_PLATFORM=offscreen ../.venv/bin/python -I -m unittest discover -s tests -p test_ui.py
"""

import os
import struct
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Qt, Signal                       # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest                     # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton          # noqa: E402

_app = QApplication.instance() or QApplication([])

import fake_2c53t as f2                                          # noqa: E402
import fake_lc1020e as fl                                        # noqa: E402
import test_ocal_solve as ts                                     # noqa: E402
import test_releases                                             # noqa: E402
from fnirsi_companion import __main__ as main_mod                # noqa: E402
from fnirsi_companion.core import settings as settings_mod       # noqa: E402
from fnirsi_companion.devices import c2c53t as c2                # noqa: E402
from fnirsi_companion.devices import lc1020e as lc               # noqa: E402
from fnirsi_companion.devices import base as base_mod            # noqa: E402
from fnirsi_companion.link.discover import DriveInfo, PortInfo, Volume   # noqa: E402
from fnirsi_companion.ui import confirm as confirm_mod           # noqa: E402
from fnirsi_companion.ui import style, widgets                   # noqa: E402
from fnirsi_companion.ui.main_window import MainWindow           # noqa: E402

LC_SERIAL = fl.INVENTED_UID.hex().upper()
C2_SERIAL = "5A4B3C2D1E0F"                 # invented 12-digit hex ID string (DESIGN 13.18)
LC_PORT = PortInfo("LC1020E", "/dev/cu.fake-lc", LC_SERIAL, 0x19F5, 0x1020)
C2_PORT = PortInfo("2C53T", "/dev/cu.fake-2c", C2_SERIAL, 0x2E3C, 0x2C53)


def _quiet(cls):
    """A FakeSerial whose empty read waits a little, as a port's read timeout does (no busy loop
    of the link worker)."""
    class Quiet(cls):
        def read(self, n=1):
            if not self._rx:
                time.sleep(0.002)
                return b""
            return super().read(n)
    return Quiet


LcSerial, C2Serial = _quiet(fl.FakeSerial), _quiet(f2.FakeSerial)


class FakeWatcher(QObject):
    meters_changed = Signal(list)
    drives_changed = Signal(list)
    scan_failed = Signal(str)

    def start(self):
        pass

    def stop(self):
        pass


def no_releases(url):
    """GitHub stand-in: every release list is empty."""
    return b"[]"


def wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.003)
    QApplication.processEvents()
    return cond()


class Bench(unittest.TestCase):
    """A window with fake meters behind the device names of LC_PORT and C2_PORT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.settings = settings_mod.Settings(os.path.join(d, "cfg", "settings.json"))
        self.settings.set("backups_folder", os.path.join(d, "backups"))
        self.settings.set("recordings_folder", os.path.join(d, "rec"))
        self.settings.set("update_check", "never")
        self.lc_meter = fl.FakeMeter()
        self.c2_meter = f2.FakeMeter()
        self.meters = {LC_PORT.device: self.lc_meter, C2_PORT.device: self.c2_meter}
        self.denied = set()
        self.answers = []
        self.asked = []
        self.watcher = FakeWatcher()
        self.volumes = []                  # what the volume probe finds (no ioreg / sysfs here)
        self.yes_no = []
        self.win = MainWindow(self.settings, watcher=self.watcher, serial_factory=self.factory,
                              release_get=no_releases, cache_dir=os.path.join(d, "cache"),
                              confirmer=self.confirmer, asker=self.asker,
                              volume_probe=lambda wanted: list(self.volumes))
        self.win.reconnect_gone_wait = 0.05
        self.addCleanup(self.close)

    def close(self):
        self.win.shutdown()
        self.win.deleteLater()
        QApplication.processEvents()

    def factory(self, port):
        if port in self.denied:
            raise PermissionError(13, "Permission denied: %r" % port)
        m = self.meters[port]
        return LcSerial(m) if isinstance(m, fl.FakeMeter) else C2Serial(m)

    def asker(self, title, text):
        self.asked.append((title, text))
        return self.yes_no.pop(0) if self.yes_no else False

    def confirmer(self, title, text):
        self.asked.append((title, text))
        return self.answers.pop(0) if self.answers else "OK"

    # -- helpers --------------------------------------------------------------------------------
    def settle(self, timeout=10.0):
        self.assertTrue(wait(lambda: not self.win.jobs.running and self.win.connecting is None,
                             timeout), "jobs still running: %s"
                        % [j.what for j in self.win.jobs.running])

    def plug(self, *ports):
        self.watcher.meters_changed.emit(list(ports))
        self.settle()

    def connected(self, model):
        a = self.win.active
        return a is not None and a.device.model == model

    def page(self, title):
        for p in self.win.active.pages:
            if p.title == title:
                return p
        self.fail("no page %r" % title)

    def done(self, job, timeout=10.0):
        self.assertIsNotNone(job, "the job was refused")
        self.assertTrue(wait(job.done.is_set, timeout), "job %s did not end" % job.what)
        QApplication.processEvents()
        return job


# ----------------------------------------------------------------------------------------------
class DetectAndSwitch(Bench):
    def test_auto_detect_and_switch_to_a_new_meter(self):
        self.assertEqual(self.win.nav_titles(), ["Live", "Trend", "Log", "Diagnostics",
                                                 "Settings"])            # no meter yet
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        self.assertEqual(self.win.nav_titles(), ["Live", "Trend", "Log", "Compare",
                                                 "Meter settings", "Meter", "Screen",
                                                 "Diagnostics", "Settings"])
        self.assertEqual([p.title for p in self.win.active.pages],
                         ["LCR", "Compare", "Settings", "Calibration", "Gain calibration", "Backup",
                          "Keys", "Meter", "Screenshot", "Diagnostics"])
        self.assertIn(lc.MEASURE_STREAM, self.lc_meter.cmds())
        self.assertEqual(self.lc_meter.measure_stream, 0)
        self.plug(LC_PORT, C2_PORT)
        self.assertTrue(self.connected("2C53T"))
        self.assertEqual(self.win.nav_titles(), ["Live", "Trend", "Log", "Oscilloscope",
                                                 "Generator", "Files", "Meter settings", "Meter",
                                                 "Screen", "Diagnostics", "Settings"])
        self.assertEqual([p.title for p in self.win.active.pages],
                         ["Multimeter", "Oscilloscope", "Generator", "Files", "Settings", "Keys",
                          "Meter", "Screenshot", "Diagnostics"])
        self.assertNotIn("compare", self.win.views)                # the LC1020E's views went
        self.assertEqual(self.lc_meter.measure_stream, 0xFFFF)       # stopped when switched away
        # 13.1: the 2C53T's calibration page backed up at its first identification
        folder = os.path.join(self.tmp.name, "backups", "2C53T-" + f2.INVENTED_UID.hex().upper())
        self.assertEqual(len(os.listdir(folder)), 1)
        self.assertEqual(self.c2_meter.stream_period, 200)
        items = [self.win.device_list.itemText(i) for i in range(self.win.device_list.count())]
        self.assertTrue(any("LC1020E" in t for t in items) and any("— active" in t for t in items))

    def test_no_switch_when_the_preference_is_off(self):
        self.settings.set("switch_to_new_meter", False)
        self.plug(LC_PORT)
        self.plug(LC_PORT, C2_PORT)
        self.assertTrue(self.connected("LC1020E"))
        self.win.device_list.setCurrentIndex(1)
        self.win.connect_selected()                                # chosen by hand
        self.settle()
        self.assertTrue(self.connected("2C53T"))

    def test_unplug_and_replug_reconnects_by_serial(self):
        self.plug(LC_PORT)
        first = self.win.active.link
        self.plug()
        self.assertTrue(wait(lambda: self.win.active is None))      # after the link's `lost`
        self.settle()
        self.assertEqual(self.win.wanted, ("LC1020E", LC_SERIAL))
        self.assertIn("waiting for LC1020E", self.win.bar_status.text())
        self.assertFalse(first.is_open)
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        self.assertIsNot(self.win.active.link, first)
        self.assertIsNone(self.win.wanted)
        self.assertTrue(any("is back with the same UID" in t for t in self.win.events.lines()))

    def test_a_meter_that_left_is_waited_for(self):
        self.plug(LC_PORT)
        self.plug(C2_PORT)                                          # LC gone, 2C53T present
        self.assertTrue(wait(lambda: self.win.active is None))
        self.settle()
        self.assertIsNone(self.win.active)                          # 2C53T not opened by itself
        self.assertEqual(self.win.wanted, ("LC1020E", LC_SERIAL))
        self.plug(C2_PORT, LC_PORT)
        self.assertTrue(self.connected("LC1020E"))

    def test_meter_answering_as_another_model_is_refused(self):
        self.meters[C2_PORT.device] = fl.FakeMeter()               # an LC1020E behind a 2C53T ID
        self.plug(C2_PORT)
        self.assertIsNone(self.win.active)
        self.assertIn("meter answers as LC1020E", self.win.device_list.itemText(0))

    def test_permission_denied_is_shown_as_such(self):
        self.denied.add(LC_PORT.device)
        self.plug(LC_PORT)
        self.assertIsNone(self.win.active)
        self.assertIn("permission denied", self.win.device_list.itemText(0))

    def test_exclusive_job_blocks_the_switch(self):
        self.plug(LC_PORT)
        spy = QSignalSpy(self.win.jobs.busy_changed)
        release = threading.Event()
        job = self.win.jobs.submit(lambda p: release.wait(10), what="held", exclusive=True)
        self.assertEqual((spy.count(), spy.at(0)), (1, [True]))
        self.assertTrue(wait(lambda: self.win.jobs.busy))
        self.assertFalse(self.win.connect_button.isEnabled())
        self.assertFalse(self.page("Backup").backup_button.isEnabled())
        self.watcher.meters_changed.emit([LC_PORT, C2_PORT])
        wait(lambda: False, 0.2)
        self.assertTrue(self.connected("LC1020E"))
        self.assertFalse(self.win.close())                         # not closed under the job
        self.assertTrue(self.win.active.link.is_open)
        release.set()
        self.done(job)
        self.settle()
        self.assertTrue(self.connected("2C53T"))
        self.assertIn([False], [spy.at(i) for i in range(spy.count())])

    def test_typed_confirmation_dialog(self):
        dlg = confirm_mod.ConfirmDialog(None, "Title", "It saves.")
        self.assertFalse(dlg.ok_button.isEnabled())
        QTest.keyClicks(dlg.field, "ok")
        self.assertFalse(dlg.ok_button.isEnabled())
        dlg.accept()
        self.assertIsNone(dlg.word())
        dlg.field.setText("OK")
        self.assertTrue(dlg.ok_button.isEnabled())
        dlg.accept()
        self.assertEqual(dlg.word(), "OK")
        dlg2 = confirm_mod.ConfirmDialog(None, "Title", "It saves.")
        dlg2.field.setText("OK")
        dlg2.reject()
        self.assertIsNone(dlg2.word())

    def test_refused_confirmation_sends_nothing(self):
        self.plug(LC_PORT)
        page = self.page("Settings")
        self.answers.append(None)
        self.assertIsNone(page.save())
        self.settle()
        self.assertNotIn(lc.SETTINGS_SAVE, self.lc_meter.cmds())
        self.assertNotIn(lc.CAL_READ, self.lc_meter.cmds())
        self.assertIn("not confirmed", page.status.text())

    def test_no_autoconnect_opens_only_the_chosen_meter(self):
        self.win.autoconnect = False
        self.plug(LC_PORT)
        self.assertIsNone(self.win.active)
        self.win.device_list.setCurrentIndex(0)
        QTest.mouseClick(self.win.connect_button, Qt.LeftButton)
        self.settle()
        self.assertTrue(self.connected("LC1020E"))
        self.win._show_stats()
        self.assertIn("frames in", self.win.stats_label.text())
        self.assertIn("dropped by the meter 0", self.win.stats_label.text())
        self.win.disconnect_active()
        self.settle()
        self.assertIsNone(self.win.active)
        self.assertIn("disconnected by you", self.win.device_list.itemText(0))

    def test_main_options(self):
        a = main_mod.parse_args(["--no-autoconnect", "--config-dir", "/x"])
        self.assertTrue(a.no_autoconnect)
        self.assertEqual(a.config_dir, "/x")

    # review_r1 finding 12
    def cancellable_job(self, started):
        def fn(p):
            with base_mod.Device._cancellable():
                p(0.5, "reading the backup")
                started.set()
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    base_mod.Device._cancel_point()
                    time.sleep(0.005)
            return "ran to its end"
        return fn

    def test_cancel_button_stops_a_job_between_requests(self):
        self.plug(LC_PORT)
        started = threading.Event()
        errors = []
        job = self.win.jobs.submit(self.cancellable_job(started), what="backup", exclusive=True,
                                   on_error=errors.append)
        self.assertTrue(started.wait(5))
        self.assertTrue(wait(self.win.cancel_button.isEnabled))
        self.win.cancel_button.click()
        self.done(job)
        self.assertIsInstance(errors[0], base_mod.Cancelled)
        self.assertFalse(self.win.cancel_button.isEnabled())

    def test_close_offers_the_cancel_and_closes_after_the_job(self):
        self.plug(LC_PORT)
        started = threading.Event()
        job = self.win.jobs.submit(self.cancellable_job(started), what="backup", exclusive=True,
                                   on_error=lambda e: None)
        self.assertTrue(started.wait(5))
        self.assertTrue(wait(lambda: job.last_text == "reading the backup"))
        self.yes_no.append(True)
        self.assertFalse(self.win.close())                         # not at once
        self.assertIn("reading the backup", self.asked[-1][1])
        self.done(job)
        self.assertTrue(wait(lambda: not self.win.stats_timer.isActive()))   # shut down, closed

    def test_close_during_a_step_that_must_end_says_what_it_waits_for(self):
        self.plug(LC_PORT)
        release = threading.Event()

        def fn(p):
            p(0.9, "FW_INSTALL sent: waiting for the meter")
            release.wait(10)
        job = self.win.jobs.submit(fn, what="application update", exclusive=True)
        self.assertTrue(wait(lambda: job.last_text != ""))
        n = len(self.asked)
        self.assertFalse(self.win.close())
        self.assertEqual(len(self.asked), n)                       # no cancel offered
        last = self.win.events.list.item(self.win.events.list.count() - 1).text()
        self.assertIn("cannot be cancelled now", last)
        self.assertIn("FW_INSTALL sent", last)
        self.assertFalse(self.win.cancel_job())
        release.set()
        self.done(job)


# ----------------------------------------------------------------------------------------------
class Lc1020ePages(Bench):
    def setUp(self):
        super().setUp()
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        self.dev = self.win.active.device

    def test_live_trend_log_from_the_measure_stream(self):
        log = self.win.log
        self.assertTrue(log.start_recording())
        for _ in range(3):
            self.lc_meter.pending.append((lc.MEASURE, fl.build_measure()))
            self.done(self.page("Diagnostics").ping())
        self.assertTrue(wait(lambda: self.win.live.stats.n >= 3))
        self.assertEqual(self.win.live.big.text(), "100.00 nF")
        self.assertEqual(self.win.live.stat_labels["n"].text(), "3")
        self.win.live.lo_edit.setText("1e-7")
        self.win.live.hi_edit.setText("2e-7")
        self.win.live._set_limit()
        self.assertEqual(self.win.live.verdict_label.text(), "PASS")
        path = log.stop_recording()
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(self.win.trend.trend), 3)
        self.win.trend.export()
        self.assertIn("exported 3 points", self.win.trend.status.text())
        self.win.trend.cursors.setChecked(True)
        self.assertIn("A t", self.win.trend.cursor_info.text())
        self.assertIn(" s · 100 nF", self.win.trend.cursor_info.text())

    def test_lcr_selectors_from_the_meter_tables(self):
        p = self.page("LCR")
        self.assertTrue(wait(lambda: 2 in p.combos))
        self.assertEqual(sorted(p.combos), [1, 2, 3])            # the fake answers lists 1..3
        self.assertEqual([p.combos[2].itemText(i) for i in range(2)], ["100Hz", "1kHz"])
        self.assertEqual(p.combos[2].currentText(), "1kHz")       # STATE frequency 1000
        # the secondary list with the manual's names: the protocol's "R" is ESR, "theta" is θ
        sec = [p.combos[1].itemText(i) for i in range(p.combos[1].count())]
        self.assertNotIn("R", sec)
        self.assertNotIn("theta", sec)
        self.assertTrue({"ESR", "θ"} & set(sec) or sec == [], sec)
        self.done(p.set_value(2, 0))
        self.assertIn((lc.LCR_SET, bytes([2, 0])), self.lc_meter.log)
        self.assertEqual(p.combos[2].currentText(), "1kHz")       # the meter still answers 1000
        self.done(p._hold(True))
        self.assertTrue(p.hold.isChecked())
        self.done(p._screen(1))
        self.assertIn(lc.SCREEN, self.lc_meter.cmds())

    def test_compare_settings_keys(self):
        cp = self.page("Compare")
        self.done(cp.refresh())
        self.assertEqual(cp.labels["passed"].text(), "3")
        cp.nominal.setValue(47.0)
        self.done(cp.apply())
        self.assertEqual(cp.labels["nominal"].text(), "47")
        sp = self.page("Settings")
        self.assertTrue(wait(lambda: 2 in sp.editors))
        self.assertEqual(sorted(sp.editors), [0, 2])
        sp.editors[2].setCurrentIndex(3)
        self.done(sp.set_value(2))
        self.assertIn((lc.SETTINGS_SET, struct.pack("<BH", 2, 3)), self.lc_meter.log)
        self.done(sp.save())
        self.assertIn(lc.SETTINGS_SAVE, self.lc_meter.cmds())
        self.assertIn(lc.CAL_READ, self.lc_meter.cmds())          # backup first
        kp = self.page("Keys")
        kp.hold_ms.setValue(0)
        self.done(kp.press(9))
        self.assertIn((lc.KEY, struct.pack("<BH", 9, 0)), self.lc_meter.log)

    def test_calibration_backup_restore(self):
        cal = self.page("Calibration")
        self.done(cal.calibrate(0))
        self.assertIn("finish", cal.result.text())
        self.assertIn((lc.CALIBRATE, struct.pack("<BH", 0, lc.CONFIRM_OK)), self.lc_meter.log)
        bp = self.page("Backup")
        self.done(bp.backup())
        self.assertGreaterEqual(bp.files.count(), 1)
        bp.files.setCurrentRow(0)
        self.done(bp.compare())
        self.assertIn("copy A: same as the meter", bp.result.text())
        old_link = self.dev.link
        self.done(bp.restore(), 20)
        self.settle()
        self.assertEqual(len(self.lc_meter.written_blocks), 1)
        self.assertIsNot(self.win.active.link, old_link)          # followed the restart
        self.assertTrue(self.win.active.link.is_open)
        self.done(bp.recovery("settings_accept_a", "SETTINGS_ACCEPT_A", "x"))
        self.assertIn(lc.SETTINGS_ACCEPT_A, self.lc_meter.cmds())

    def test_slot_file_gets_the_foreign_warning(self):
        # review_r1 findings 2 and 3: the releases publish no slot file, so the warning names what
        # they list; refused: nothing is sent. Accepted: backup first, then the slot is written.
        self.win.release_get = test_releases.FakeGitHub()
        mp = self.page("Meter")
        slot = os.path.join(self.tmp.name, "slot.bin")
        with open(slot, "wb") as f:
            f.write(fl.build_slot_file(lc.SLOT_BASE[1], version=3, length=0x400))
        self.answers.append(None)
        mp.start_boot_update(slot)
        self.settle()
        title, text = self.asked[-1]
        self.assertEqual(title, "Slot file not from a release")
        self.assertIn("APP_OWN.bin, APP_OWN.uf2, STARTER.bin", text)
        self.assertIn("no release publishes a bootloader slot file", text)
        self.assertNotIn(lc.BOOT_BEGIN, self.lc_meter.cmds())
        mp.start_boot_update(slot)
        self.settle(30)
        self.assertIn("backup file first", self.asked[-1][1])
        codes = self.lc_meter.cmds()
        self.assertLess(codes.index(lc.CAL_READ), codes.index(lc.BOOT_BEGIN))
        self.assertIn(lc.BOOT_INSTALL, codes)

    def test_meter_screenshot_diagnostics(self):
        mp = self.page("Meter")
        self.done(mp.read_battery())
        self.assertIn("3.90 V", mp.battery_label.text())
        self.done(mp.read_info())
        self.assertEqual(mp.info_labels["uid"].text(), LC_SERIAL)
        self.done(mp.read_boot_status())
        self.assertIn("free slot: A", mp.boot_text.text())
        bad = os.path.join(self.tmp.name, "app.bin")
        with open(bad, "wb") as f:
            f.write(f2.build_app_image())
        mp.start_update(bad, None)
        self.assertIn("refused before anything was sent", mp.status.text())
        self.assertNotIn(lc.UPDATE_MODE, self.lc_meter.cmds())
        self.done(mp.reset())
        self.assertEqual(self.lc_meter.restarts, 1)
        self.done(mp.check_releases(False))
        self.assertEqual(mp.release_table.rowCount(), 0)
        self.assertEqual(mp.no_releases.text(), "no releases published")
        shot = self.page("Screenshot")
        self.done(shot.capture(), 20)
        self.assertEqual((shot.image.width(), shot.image.height()), (320, 240))
        png = os.path.join(self.tmp.name, "s.png")
        self.assertTrue(shot.save_png(png))
        self.assertTrue(os.path.getsize(png) > 0)
        dg = self.page("Diagnostics")
        self.done(dg.ping())
        self.assertIn("the same", dg.out.toPlainText())
        dg.addr.setText("0x20000000")
        dg.length.setValue(16)
        self.done(dg.mem_read())
        self.assertIn("20000000", dg.out.toPlainText())
        self.done(dg.factory())
        self.assertIn("FACTORY_MODE: 0", dg.out.toPlainText())
        self.done(dg.measure())
        self.assertIn("main: C 100.00 nF", dg.out.toPlainText())

    def test_events_and_preferences(self):
        self.win.active.link.event_frame.emit(
            fl.Frame(1, 4, lc.EV_CALIBRATION, bytes([1, 2, 0]), 0.0))
        self.assertTrue(wait(lambda: "status busy" in self.page("Calibration").progress.text()))
        self.assertTrue(any("event CALIBRATION" in t for t in self.win.events.lines()))
        pp = self.win.prefs
        pp.csv.setCurrentIndex(1)
        self.assertEqual(settings_mod.Settings(self.settings.path).get("csv_separator"), ";")
        pp.theme.setCurrentIndex(pp.theme.findData("dark"))
        self.assertEqual(self.settings.get("theme"), "dark")

    def test_calibration_started_on_the_meter_shows_its_end(self):
        # SL 5.3.3 / 7.3 / 10: a calibration started by KEY 0x008 on item 5 or the meter's keys
        # has no CALIBRATE answer; its CALIBRATION event with status 2 or 3 is the result
        cal = self.page("Calibration")
        link = self.win.active.link
        link.event_frame.emit(fl.Frame(1, 4, lc.EV_CALIBRATION, bytes([1, 13, 11]), 0.0))
        self.assertTrue(wait(lambda: "status busy" in cal.progress.text()))
        self.assertEqual(cal.result.text(), "—")                   # status 1: no end yet
        link.event_frame.emit(fl.Frame(2, 4, lc.EV_CALIBRATION, bytes([3, 13, 11]), 0.0))
        self.assertTrue(wait(lambda: "calibration started on the meter ended: status fail"
                             in cal.result.text()))
        self.assertIn("type 11", cal.result.text())
        link.event_frame.emit(fl.Frame(3, 4, lc.EV_CALIBRATION, bytes([2, 45, 0]), 0.0))
        self.assertTrue(wait(lambda: "status finish" in cal.result.text()))
        self.assertIn("SAVE_RESULT", cal.result.text())


# ----------------------------------------------------------------------------------------------
class Lc1020eNewFirmwarePages(Bench):
    """The LC1020E with the capability bits 22 .. 24: the Compare page shows the judgement and
    sets the tolerance mode, the secondary value shows "unspecified"; old firmware shows "-"."""

    def connect(self, caps):
        self.lc_meter.caps = caps
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        return self.page("Compare")

    def test_compare_tail_and_tolerance_form(self):
        cp = self.connect(fl.NEW_CAPS)
        self.done(cp.refresh())
        self.assertEqual(cp.labels["result"].text(), "IN")
        self.assertEqual(cp.labels["signed"].text(), "+1.5 %")
        self.assertTrue(cp.tol_button.isEnabled())
        cp.tol_mode.setCurrentIndex(1)
        cp.tol_lower.setValue(-2.5)
        cp.tol_upper.setValue(1.0)
        cp.tol_kind.setCurrentIndex(3)
        cp.tol_value.setValue(0.5)
        self.done(cp.apply_tolerance())
        self.assertIn((lc.TOLERANCE_SET, struct.pack("<BiiBBf", 1, -250, 100, 0, 3, 0.5)),
                      self.lc_meter.log)
        self.assertEqual(cp.labels["result"].text(), "HI")
        self.assertEqual(cp.labels["limits"].text(), "-2.5 .. 1 %")
        self.assertEqual(cp.labels["secondary_limit"].text(), "ESR max 0.5")

    def test_old_firmware_rows_and_form(self):
        cp = self.connect(fl.ALL_CAPS)
        self.done(cp.refresh())
        self.assertEqual(cp.labels["result"].text(), "-")
        self.assertFalse(cp.tol_button.isEnabled())

    def test_secondary_unspecified_on_the_live_page(self):
        self.connect(fl.NEW_CAPS)
        self.lc_meter.measure_markers = 2
        self.lc_meter.pending.append((lc.MEASURE, fl.build_measure(markers=2)))
        self.done(self.page("Diagnostics").ping())
        live = self.win.live
        self.assertTrue(wait(lambda: live.last is not None))
        self.assertTrue(live.last.secondary.unspecified)
        self.assertIn("unspecified", live.secondary.text())


# ----------------------------------------------------------------------------------------------
class Lc1020eOcalPages(Bench):
    """The own calibration area (OWN_CAL_DESIGN.md 6.3): the Gain calibration page, the LCR page's
    Vac / Iac and gain cal rows and the Backup page's area card, on a meter with capability bit 26."""

    def setUp(self):
        super().setUp()
        self.lc_meter.caps = fl.OCAL_CAPS
        self.lc_meter.window_step = 2          # a window closed per OCAL_RAW read, two at a time: no waiting
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        self.dev = self.win.active.device
        self.gp = self.page("Gain calibration")
        self.assertTrue(wait(lambda: self.gp.info is not None))

    def sections(self, slot=0):
        rec = lc.parse_ocal_record(bytes(self.lc_meter.slots[slot]), slot)
        self.assertTrue(rec.valid)
        return rec, lc.parse_body(rec.body)

    def test_the_page_is_registered_and_shows_the_empty_area(self):
        titles = [p.title for p in self.win.active.pages]
        self.assertEqual(titles[3:6], ["Calibration", "Gain calibration", "Backup"])
        self.assertEqual(self.win.nav_titles(), ["Live", "Trend", "Log", "Compare", "Meter settings",
                                                 "Meter", "Screen", "Diagnostics", "Settings"])
        gp = self.gp
        self.assertEqual((gp.info_label.text(), gp.sections_label.text(), gp.retake_label.text()),
                         ("none", "none", "not needed"))
        self.assertEqual(gp.table.rowCount(), 6)
        self.assertFalse(gp.write_button.isEnabled())              # nothing solved yet
        self.assertTrue(gp.start_button.isEnabled())

    def test_resistor_list(self):
        gp = self.gp
        res = gp.resistors()
        self.assertEqual([r.value for r in res], [10.0, 100.0, 1e3, 1e4, 1e5, 1e6])
        for r in res:
            self.assertAlmostEqual(r.rel_unc, 0.001, places=12)           # 0.1 % typed
        self.assertEqual(gp.table.item(0, 0).text(), "10 ohm")
        gp.add_resistor("47 ohm", 47.0, 0.5)
        self.assertEqual(gp.table.rowCount(), 7)
        self.assertAlmostEqual(gp.resistors()[6].rel_unc, 0.005, places=12)
        gp.table.setCurrentCell(6, 0)
        gp.remove_resistor()
        self.assertEqual(gp.table.rowCount(), 6)
        gp.table.item(1, 1).setText("abc")
        with self.assertRaises(ValueError):
            gp.resistors()
        self.assertIsNone(gp.start_run(1))
        self.assertIn("not a number", gp.status.text())
        gp.table.item(1, 1).setText("-5")
        with self.assertRaises(ValueError):
            gp.resistors()
        self.assertIsNone(gp.start_run(99))
        self.assertIsNone(gp.solve())
        self.assertNotIn(lc.OCAL_LOCK, self.lc_meter.cmds())

    def test_run_one_resistor_then_the_short(self):
        gp = self.gp
        self.lc_meter.dut_fn = lambda hz: 1000 + 0j
        self.done(gp.start_run(2), 90)
        # 6 frequencies x 2 levels, the autoranged cell (2, 2, 1) and its 10 neighbours each
        self.assertEqual(len(gp.windows), 132)
        self.assertEqual(gp.table.item(2, 3).text(), "132")
        self.assertEqual(gp.progress.value(), 1000)
        self.assertIn("132 windows of resistor 3", gp.status.text())
        self.assertIsNone(self.lc_meter.lock)                              # released at the end
        self.lc_meter.dut_fn = lambda hz: 0.05 + 0.01j
        self.done(gp.run_shortopen("short"), 90)
        self.assertEqual((gp.short_takes, len(gp.shortopen)), (1, 96))
        gp.clear_data()
        self.assertEqual((len(gp.windows), len(gp.shortopen), gp.short_takes), (0, 0, 0))
        self.assertEqual(gp.table.item(2, 3).text(), "0")

    def test_solve_and_write_gain(self):
        gp = self.gp
        while gp.table.rowCount() > 3:
            gp.table.removeRow(3)
        for r, v in enumerate((100.0, 1000.0, 10000.0)):
            gp.table.item(r, 1).setText("%g" % v)
        gp.windows = ts.decade_run()                      # injected factors, three decades
        self.done(gp.solve())
        self.assertEqual(gp.result.written_frequencies(), [1000])
        self.assertIn("1000 Hz: GAIN written", gp.report.toPlainText())
        self.assertTrue(gp.write_button.isEnabled())
        self.done(gp.write_gain(), 30)
        title, text = self.asked[-1]
        self.assertEqual(title, "Write the gain factors")
        self.assertIn("1000 Hz", text)
        self.assertIn(".ocal backup", text)
        rec, secs = self.sections()
        self.assertEqual((rec.sequence, sorted(secs)), (1, [0, 1]))
        gain = lc.unpack_gain(secs[lc.SEC_GAIN])
        self.assertEqual((gain[2].zeta_mag_mask, gain[2].nu_phase_mask), (ts.MASK_Z, ts.MASK_N))
        self.assertTrue(lc.unpack_meta(secs[lc.SEC_META])[1].startswith("gain cal "))
        self.assertTrue(list((self.tmp_backups() / ("LC1020E-" + LC_SERIAL)).glob("*.ocal")))
        self.assertTrue(wait(lambda: "slot 0, sequence 1" in gp.info_label.text()))
        self.assertTrue(wait(lambda: "GAIN at 1000 Hz" in gp.sections_label.text()))

    def tmp_backups(self):
        from pathlib import Path
        return Path(self.tmp.name) / "backups"

    def test_write_gain_not_confirmed_sends_nothing(self):
        gp = self.gp
        while gp.table.rowCount() > 3:
            gp.table.removeRow(3)
        for r, v in enumerate((100.0, 1000.0, 10000.0)):
            gp.table.item(r, 1).setText("%g" % v)
        gp.windows = ts.decade_run()
        self.done(gp.solve())
        self.answers.append(None)
        self.assertIsNone(gp.write_gain())
        self.assertNotIn(lc.OCAL_WRITE, self.lc_meter.cmds())
        self.assertIn("not confirmed", gp.status.text())

    def test_constants_clip_and_settle(self):
        gp = self.gp
        gp.clip_obs = {(0, 3): (5, 4095), (1, 2): (40, 4010)}
        gp.propose_clip()
        self.assertEqual(gp.clip_values()[3], (21, 4095))
        self.assertEqual(gp.clip_values()[10], (56, 3994))
        gp.settle_table.item(1, 3).setText("40")
        self.done(gp.write_constants(), 30)
        _rec, secs = self.sections()
        self.assertEqual(sorted(secs), [0, 2, 3])
        self.assertEqual(lc.unpack_clip(secs[lc.SEC_CLIP])[3], (21, 4095))
        table = lc.unpack_settle(secs[lc.SEC_SETTLE])
        self.assertEqual((table[0], table[1][3], table[5], table[6]), ([26] * 6, 40, [5] * 6, [5] * 6))
        gp.clip_table.item(3, 1).setText("0")                              # edit away, then read back
        gp.settle_table.item(1, 3).setText("26")
        self.done(gp.read_constants())
        self.assertEqual((gp.clip_table.item(3, 1).text(), gp.settle_table.item(1, 3).text()), ("21", "40"))

    def test_constants_the_meter_would_refuse_are_not_sent(self):
        gp = self.gp
        gp.clip_table.item(0, 2).setText("100")                            # hi - lo = 100: not above 3600
        self.assertIsNone(gp.write_constants())
        self.assertIn("would refuse", gp.status.text())
        gp.clip_table.item(0, 2).setText("4095")
        gp.settle_table.item(2, 2).setText("abc")
        self.assertIsNone(gp.write_constants())
        self.assertIn("not a number", gp.status.text())
        self.assertNotIn(lc.OCAL_WRITE, self.lc_meter.cmds())

    def test_a_settle_trace_fills_its_cell(self):
        gp = self.gp
        gp.kind_combo.setCurrentIndex(gp.kind_combo.findData(2))
        gp.trace_freq.setCurrentIndex(2)                                   # 1000 Hz
        self.done(gp.measure_trace(), 60)
        self.assertEqual(gp.settle_table.item(2, 2).text(), "55")          # derived in test_ocal_solve
        self.assertIn("kind 2 at 1000 Hz: 55 ms", gp.status.text())
        self.assertEqual(self.lc_meter.trace["state"], 0)

    def test_level_monitor(self):
        gp = self.gp
        gp.mon_table.item(2, 1).setText("0.5")                             # DMM: 0.5 V at 1 kHz
        self.assertEqual(gp.compute_kv(), {})                              # nothing captured yet
        self.done(gp.capture(2), 60)
        a_v, c5, nu = gp.captures[2]
        self.assertEqual((a_v, c5, nu), (2000.0, 2, 1.0))                  # the fake's window, no GAIN yet
        kv = gp.compute_kv()
        self.assertAlmostEqual(kv[2], 0.5 * 4 / 2000.0, places=12)         # V x G[2] / A_V
        self.assertEqual(gp.mon_table.item(2, 3).text(), "0.001")
        gp.bandwidth.setValue(0.5)                                         # a 500 Hz DMM: 1 kHz is above it
        self.assertEqual(gp.compute_kv(), {})
        self.assertEqual(gp.mon_table.item(2, 3).text(), "above the DMM's bandwidth")
        gp.bandwidth.setValue(100.0)
        self.done(gp.write_monitor(), 30)
        self.assertIn("1000 Hz", self.asked[-1][1])
        _rec, secs = self.sections()
        mask, kvs = lc.unpack_monitor(secs[lc.SEC_MONITOR])
        self.assertEqual(mask, 1 << 2)
        self.assertAlmostEqual(kvs[2], 0.001, places=9)
        # a second frequency: the first one stays in the new record
        gp.mon_table.item(2, 1).setText("")
        gp.mon_table.item(3, 1).setText("0.4")
        self.done(gp.capture(3), 60)
        self.done(gp.write_monitor(), 30)
        rec, secs = self.sections(1)
        self.assertEqual(rec.sequence, 2)
        mask, kvs = lc.unpack_monitor(secs[lc.SEC_MONITOR])
        self.assertEqual(mask, (1 << 2) | (1 << 3))
        self.assertAlmostEqual(kvs[2], 0.001, places=9)
        self.assertAlmostEqual(kvs[3], 0.4 * 4 / 2000.0, places=9)

    def test_nothing_to_write_without_a_capture(self):
        gp = self.gp
        self.assertIsNone(gp.write_monitor())
        self.assertIn("no kV to write", gp.status.text())
        self.assertIsNone(gp.capture(7))
        self.assertNotIn(lc.OCAL_WRITE, self.lc_meter.cmds())

    def test_the_current_check_line(self):
        gp = self.gp
        gp.dmm_ma.setValue(10.0)
        self.done(gp.compare_current())
        self.assertIn("no calibrated current", gp.current_label.text())
        self.lc_meter.monitor_values = dict(v=1.0, i=0.01, a_v=250.0, a_i=4.0, flags=3)
        self.done(gp.compare_current())
        self.assertEqual(gp.current_label.text(), "I by the meter 10 mA / I by the DMM 10 mA = 1.0000")

    def test_the_event_refreshes_the_record(self):
        n = self.lc_meter.cmds().count(lc.OCAL_INFO)
        self.win.active.link.event_frame.emit(
            fl.Frame(1, 4, lc.EV_OCAL_CHANGED, struct.pack("<BII", 0, 1, 3), 0.0))
        self.assertTrue(wait(lambda: self.lc_meter.cmds().count(lc.OCAL_INFO) > n))
        self.assertTrue(any("event OCAL_CHANGED" in t for t in self.win.events.lines()))

    def test_lcr_page_rows(self):
        p = self.page("LCR")
        self.assertTrue(p.ocal)
        self.assertFalse(p.vac_row.isHidden() or p.gain_row.isHidden())

        def reading(flags):
            return lc.measure_to_reading(lc.parse_measure(fl.build_measure(markers=0, ocal=flags), True),
                                         1.0, 2.0)
        p.lcd_flags(reading(0b0001))
        self.assertEqual((p.gain_row.value.text(), p.gain_warning.text()), ("applied", ""))
        self.done(p._monitor_job)                                           # MONITOR: uncalibrated
        self.assertTrue(p.vac_row.value.text().startswith("uncalibrated"))
        self.lc_meter.monitor_values = dict(v=1.0, i=0.01, a_v=250.0, a_i=4.0, flags=3)
        p._monitor_at = 0.0
        p.lcd_flags(reading(0b0010))                                        # GAIN here, but not at this cell
        self.assertEqual(p.gain_row.value.text(), "not applied")
        self.assertTrue(p.gain_warning.text().startswith("Warning:"))
        self.done(p._monitor_job)
        self.assertEqual(p.vac_row.value.text(), "1 V / 10 mA")
        p._monitor_at = 0.0
        p.lcd_flags(reading(0b1100))
        self.assertEqual(p.gain_row.value.text(), "not applied, lock active, retake short / open")
        self.assertEqual(p.gain_warning.text(), "")
        self.done(p._monitor_job)
        n = self.lc_meter.cmds().count(lc.MONITOR)
        p.lcd_flags(reading(0b0001))                                        # within a second: no new request
        self.assertEqual(self.lc_meter.cmds().count(lc.MONITOR), n)
        p.lcd_flags(None)                                                   # no reading: nothing changes
        no_gain = lc.measure_to_reading(lc.parse_measure(fl.build_measure(markers=0)), 1.0, 2.0)
        p.lcd_flags(no_gain)                                                # 65-byte record: gain_flags None
        self.assertEqual(self.lc_meter.cmds().count(lc.MONITOR), n)

    def test_lcr_monitor_without_a_reading_text(self):
        p = self.page("LCR")
        p.show_monitor(lc.parse_monitor(fl.build_monitor(flags=lc.MON_NO_READING | lc.MON_V_CALIBRATED)))
        self.assertEqual(p.vac_row.value.text(), "no reading")
        p.show_monitor(lc.parse_monitor(fl.build_monitor(1.5, 0.002, 90.0, 3.0, lc.MON_V_CALIBRATED
                                                         | lc.MON_LOCK)))
        self.assertEqual(p.vac_row.value.text(), "1.5 V / 2 mA (lock)")
        p.show_gain(None)
        self.assertEqual(p.gain_row.value.text(), "—")

    def test_backup_page_area_card(self):
        bp = self.page("Backup")
        self.assertFalse(bp.ocal_restore_button.isEnabled())                # no file selected
        self.done(bp.ocal_info())
        self.assertEqual(bp.ocal_state.text(), "flags 0x0000: none; no active record; other slot sequence 0")
        body = ts_body("A")
        self.lc_meter.slots[0] = fl.ocal_slot(fl.ocal_record(body, 1))
        self.done(bp.ocal_backup())
        self.assertEqual(bp.ocal_files.count(), 1)
        bp.ocal_files.setCurrentRow(0)
        self.assertEqual(bp.ocal_records.text().splitlines()[0],
                         "slot 0: format 1, sequence 1, 100 bytes, UID %s" % LC_SERIAL)
        self.assertTrue(bp.ocal_records.text().splitlines()[1].startswith("slot 1: no record"))
        self.assertTrue(bp.ocal_restore_button.isEnabled())
        bp.ocal_slot.setCurrentIndex(0)
        self.done(bp.ocal_restore(), 30)
        self.assertEqual(self.asked[-1][0], "Restore record 0")
        self.assertTrue(bp.ocal_result.text().startswith("commit OK, slot 1, sequence 2"))
        self.assertEqual(lc.parse_ocal_record(bytes(self.lc_meter.slots[1])).body, body)
        self.done(bp.ocal_rollback(0), 30)
        self.assertEqual(bp.ocal_result.text(), "record in slot 0, sequence 1, flags 0x0007 active")
        self.done(bp.ocal_info())
        self.assertIn("active slot 0", bp.ocal_state.text())
        self.answers.append(None)
        self.assertIsNone(bp.ocal_rollback(1))                              # not confirmed: nothing sent
        self.assertEqual(self.lc_meter.cmds().count(lc.OCAL_ROLLBACK), 1)


def ts_body(note: str) -> bytes:
    return lc.build_body({lc.SEC_META: lc.pack_meta(1700000000, note)})


# ----------------------------------------------------------------------------------------------
class Lc1020eNoOcalPages(Bench):
    """A meter without capability bit 26: the area's pages are built, their controls are off and
    nothing is sent to the area."""

    def test_controls_are_off_and_nothing_is_sent(self):
        self.plug(LC_PORT)
        self.assertTrue(self.connected("LC1020E"))
        gp = self.page("Gain calibration")
        for w in (gp.start_button, gp.info_button, gp.write_button, gp.capture_button):
            self.assertFalse(w.isEnabled())
        self.assertIn("capability bit 26", gp.start_button.toolTip())
        lcr = self.page("LCR")
        self.assertFalse(lcr.ocal)
        self.assertTrue(lcr.vac_row.isHidden() and lcr.gain_row.isHidden())
        bp = self.page("Backup")
        self.assertFalse(bp.ocal_backup_button.isEnabled())
        self.assertFalse(bp.ocal_restore_button.isEnabled())
        self.assertEqual([c for c in self.lc_meter.cmds() if 0x30 <= c <= 0x37], [])


class C2c53tPages(Bench):
    def setUp(self):
        super().setUp()
        self.plug(C2_PORT)
        self.assertTrue(self.connected("2C53T"))
        self.dev = self.win.active.device

    def test_multimeter_generator_and_the_live_reading(self):
        mp = self.page("Multimeter")
        self.done(mp._function(6))
        self.assertIn((c2.DMM_FUNCTION, bytes([6])), self.c2_meter.log)
        self.assertEqual(mp.function.currentText(), "C")
        self.done(mp._toggle("DMM_HOLD", self.dev.dmm_hold, True, mp.hold))
        self.assertTrue(mp.hold.isChecked())
        gp = self.page("Generator")
        gp.on.setChecked(True)
        gp.frequency.setValue(2500)
        self.win.publish_state(c2.parse_state(self.c2_meter.state()))
        self.assertEqual(gp.frequency.value(), 2500)              # not overwritten while edited
        self.assertIn("1000 Hz", gp.shown.text())                 # the meter's value shown
        self.done(gp.apply())
        self.assertEqual(self.c2_meter.state_kw["gen"], (1, 0, 2500, 50, 30))   # duty, amplitude
                                                                               # as STATE had them
        self.assertIn("2500 Hz", gp.shown.text())
        self.c2_meter.pending.append((c2.STATE, self.c2_meter.state()))
        self.done(self.page("Diagnostics").ping())
        self.assertTrue(wait(lambda: self.win.live.stats.n >= 1))
        self.assertEqual(self.win.live.big.text(), "4.70 kΩ")

    def test_oscilloscope(self):
        sp = self.page("Oscilloscope")
        self.assertTrue(wait(lambda: sp.timebase.count() == 29))
        self.c2_meter.state_kw["page"] = 2
        self.done(self.page("Diagnostics").state())
        self.win.show_page(sp)
        self.assertEqual(self.win.current_view(), "scope")
        self.assertTrue(wait(lambda: sp.waves.get(0) is not None))
        self.assertIn(c2.SCOPE_WAVE, self.c2_meter.cmds())
        self.assertIn("trace age", sp.age.text())
        self.done(sp._timebase(12))
        self.assertEqual(sp.timebase.currentText(), "TB12")
        sp.ch[1]["enabled"].setChecked(True)
        self.done(sp.apply_channel(1))
        self.assertEqual(self.c2_meter.state_kw["ch2"][0], 1)
        sp.t_level.setValue(-30)
        self.done(sp.apply_trigger())
        self.assertEqual(self.c2_meter.state_kw["trigger"][2], -30)
        for b in (sp.run_button, sp.stop_button, sp.autoset, sp.autocorrect):
            b.click()
        self.settle()
        for code in (c2.SCOPE_RUN, c2.SCOPE_AUTOSET, c2.SCOPE_AUTOCORRECT):
            self.assertIn(code, self.c2_meter.cmds())
        self.win.select_index(0)
        self.assertFalse(sp.timer.isActive())                     # polling stops when hidden
        self.settle()

    def test_files(self):
        fp = self.page("Files")
        self.done(fp.list_dir())
        self.assertEqual(sorted(e.name for e in fp.entries), ["BOOTINST.TXT", "README.TXT"])
        dest = os.path.join(self.tmp.name, "readme.txt")
        self.done(fp.read_to("2:/README.TXT", dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self.done(fp.write_from(dest, "2:/COPY.TXT"))
        self.assertEqual(self.c2_meter.files["2:/COPY.TXT"], b"hello")
        self.settle()
        self.answers.append(None)                                 # overwrite refused
        self.assertIsNone(fp.write_from(dest, "2:/README.TXT"))
        self.assertIn("replaces 2:/README.TXT", self.asked[-1][1])
        self.assertIsNone(fp.write_from(dest, "2:/update/app.bin"))
        self.assertIn("start-up file", fp.status.text())
        self.assertIsNone(fp.delete_path("2:/BOOTINST.TXT"))
        self.done(fp.delete_path("2:/COPY.TXT"))
        self.assertNotIn("2:/COPY.TXT", self.c2_meter.files)
        self.answers.append(None)
        self.assertIsNone(fp.share(True))
        self.assertNotIn(c2.DRIVE, self.c2_meter.cmds())
        self.assertIn("BOOTINST.TXT", self.asked[-1][1])

    def test_end_sharing_asks_for_the_eject_and_checks_the_volume(self):
        fp = self.page("Files")
        self.volumes[:] = [Volume("/Volumes/2C53T", "2C53T", 0x2E3C, 0x2C53, True)]
        self.done(fp.share(False))
        self.assertIn("Eject the meter's drive", self.asked[-1][1])
        self.assertNotIn(c2.DRIVE, self.c2_meter.cmds())
        self.assertIn("still mounted at /Volumes/2C53T", fp.status.text())
        self.answers.append(None)
        self.assertIsNone(fp.share(False))                     # not confirmed
        self.volumes[:] = [Volume("/dev/sdb", None, 0x2E3C, 0x2C53, False),     # unmounted node
                           Volume("/Volumes/STICK", None, 0x0781, 0x5567, True)]  # another device
        self.done(fp.share(False))
        self.assertEqual(self.c2_meter.log[-1], (c2.DRIVE, b"\x00"))
        self.assertIn("no volume of the meter was mounted", fp.status.text())

    def test_settings_keys_diagnostics(self):
        st = self.page("Settings")
        self.assertTrue(wait(lambda: st.last is not None))
        st.backlight.setValue(60)
        self.done(st.apply())
        self.assertEqual(self.c2_meter.settings[0], 60)
        old = self.dev.link
        self.done(st.save(), 20)
        self.settle()
        self.assertEqual(self.c2_meter.saved_settings[0], 60)
        self.assertIsNot(self.win.active.link, old)              # reconnected after the restart
        kp = self.page("Keys")
        self.done(kp.press(15))
        self.assertEqual(self.c2_meter.keys, [15])
        self.answers.append(None)
        self.assertIsNone(kp.press(1))
        self.assertFalse(self.c2_meter.powered_off)
        dg = self.page("Diagnostics")
        self.done(dg.flash_id())
        self.assertIn("0xEF", dg.out.toPlainText())
        self.done(dg.fault())
        self.assertIn("valid", dg.out.toPlainText())
        self.done(dg.log_on())
        self.done(dg.log_get())
        self.assertIn("40 bytes", dg.out.toPlainText())
        self.done(dg.cal_read())
        self.assertIn("2048 bytes", dg.out.toPlainText())
        self.done(dg.text_colour())
        self.assertEqual(self.c2_meter.text_colour, 0)
        self.done(dg.meter_send())
        self.assertEqual(self.c2_meter.sent_to_chip, [(0, 0)])

    def test_screenshot_dtr_and_releases(self):
        shot = self.page("Screenshot")
        self.done(shot.capture(), 20)
        r, g, b = c2.rgb565_to_rgb([f2.pixel(200, 5)])
        self.assertEqual(shot.image.pixel(200, 5) & 0xFFFFFF, r << 16 | g << 8 | b)
        self.assertEqual((shot.image.width(), shot.image.height()), (320, 240))
        self.win._show_stats()
        self.assertIn("DTR set", self.win.dtr_label.text())
        self.win.prefs.sleep.setChecked(True)                     # typed OK (default answer)
        self.assertTrue(self.settings.get("let_2c53t_sleep"))
        self.win.idle_limit = 0.0
        self.win._idle_check()
        link = self.win.active.link
        self.assertTrue(wait(lambda: not link.dtr))
        self.settle()
        self.assertGreaterEqual(self.c2_meter.cmds().count(c2.CAL_READ), 16)   # backup first
        self.win._show_stats()
        self.assertIn("DTR clear", self.win.dtr_label.text())
        self.win.user_action()
        self.assertTrue(wait(lambda: link.dtr))
        mp = self.page("Meter")
        self.win.release_get = test_releases.FakeGitHub()
        self.done(mp.check_releases(False))
        tag, rel = mp.release_rows[0][0].tag, mp.release_rows[0][1]
        self.assertEqual((tag, rel), ("v1.4.17", "same"))
        self.assertEqual(mp.release_table.item(0, 0).text(), "v1.4.17")
        self.assertTrue(mp.release_table.cellWidget(0, 3).findChild(
            QPushButton, "install_v1.4.17").isEnabled())
        self.assertIn("none is newer than 1.4.17", mp.release_note.text())

    def test_application_update_with_the_foreign_image_warning(self):
        mp = self.page("Meter")
        img = os.path.join(self.tmp.name, "APP_2C53T_V1.4.18_261120.bin")
        with open(img, "wb") as f:
            f.write(f2.build_app_image())
        self.answers += [None]                                    # refuse the 13.26 warning
        mp.start_update(img, None)
        self.settle()
        self.assertIn("not one of the published releases", self.asked[-1][1])
        self.assertNotIn(c2.FW_BEGIN, self.c2_meter.cmds())
        mp.start_update(img, None)                                # both answered OK now
        self.settle(30)
        self.assertEqual(self.c2_meter.installed, f2.build_app_image())
        self.assertEqual(mp.info_labels["firmware"].text(), "1.4.18")
        self.assertIn("1 (OK)", mp.info_labels["update"].text())
        lc_file = os.path.join(self.tmp.name, "a.uf2")
        with open(lc_file, "wb") as f:
            f.write(fl.build_uf2())
        mp.start_update(lc_file, None)
        self.assertIn("UF2", mp.status.text())


# ----------------------------------------------------------------------------------------------
class UpdateDrive(Bench):
    def test_2c53t_drive_after_a_failed_install(self):
        drive = os.path.join(self.tmp.name, "IAP")
        os.makedirs(drive)
        info = DriveInfo("2C53T", drive, 0x2E3C, 0x5720, "failed")
        self.watcher.drives_changed.emit([info])
        self.assertTrue(wait(lambda: self.win.drive_page is not None))
        dp = self.win.drive_page
        self.assertFalse(dp.copy_button.isEnabled())             # 13.14: no 2C53T seen yet
        self.assertIn("Refused", dp.reason.text())
        self.plug(C2_PORT)                                        # the meter, slot bootloader v2
        self.assertTrue(self.connected("2C53T"))
        self.assertIn("still connected", self.win.c2_drive_owner()[1])
        self.plug()                                               # its port goes: update mode
        self.watcher.drives_changed.emit([info])
        self.assertTrue(wait(lambda: self.win.drive_page is not None
                             and self.win.drive_page.slot_meter is not None))
        dp = self.win.drive_page
        self.assertEqual(dp.slot_meter[:2], (C2_SERIAL, f2.INVENTED_UID.hex().upper()))
        self.assertTrue(dp.copy_button.isEnabled())
        img = os.path.join(self.tmp.name, "APP_2C53T_V1.4.18_261120.bin")
        with open(img, "wb") as f:
            f.write(f2.build_app_image())
        dp.copy_path(img)
        self.settle()
        with open(os.path.join(drive, os.path.basename(img)), "rb") as f:
            self.assertEqual(f.read(), f2.build_app_image())
        self.assertIn("serial %s" % C2_SERIAL, self.asked[-1][1])

    def test_2c53t_drive_refused_when_another_2c53t_came_after(self):
        # review_r1 finding 8: the slot meter #1, then a 2C53T #2 (stock bootloader, never
        # identified here) appears and goes: the drive may be #2's and is refused
        drive = os.path.join(self.tmp.name, "IAP")
        os.makedirs(drive)
        self.plug(C2_PORT)
        self.plug()
        other = PortInfo("2C53T", "/dev/cu.fake-2c-2", "0A0B0C0D0E0F", 0x2E3C, 0x2C53)
        self.denied.add(other.device)                             # never opened in this test
        self.plug(other)
        self.plug()
        self.watcher.drives_changed.emit([DriveInfo("2C53T", drive, 0x2E3C, 0x5720, "ready")])
        self.assertTrue(wait(lambda: self.win.drive_page is not None))
        dp = self.win.drive_page
        self.assertIsNone(dp.slot_meter)
        self.assertFalse(dp.copy_button.isEnabled())
        self.assertIn("0A0B0C0D0E0F", dp.reason.text())
        dp.copy_path(os.path.join(self.tmp.name, "x.bin"))
        self.assertEqual(os.listdir(drive), [])

    def test_lc1020e_drive_refuses_a_2c53t_image(self):
        drive = os.path.join(self.tmp.name, "LC1020E-IAP")
        os.makedirs(drive)
        self.watcher.drives_changed.emit([DriveInfo("LC1020E", drive, 0x19F5, 0x5740, "ready")])
        self.assertTrue(wait(lambda: self.win.drive_page is not None))
        img = os.path.join(self.tmp.name, "app.bin")
        with open(img, "wb") as f:
            f.write(f2.build_app_image())
        self.win.drive_page.copy_path(img)
        self.assertIn("refused", self.win.drive_page.status.text())
        self.assertEqual(os.listdir(drive), [])
        self.watcher.drives_changed.emit([])
        self.assertTrue(wait(lambda: self.win.drive_page is None))


# ----------------------------------------------------------------------------------------------
class Look(Bench):
    """DESIGN_UI 6.16: every sidebar view of each model in both themes at 1100 x 720, the theme
    switch, the seven-segment characters, and the LCD's annunciators from the meter's records."""

    def test_scale_box_short_numbers_and_small_values(self):
        from PySide6.QtGui import QValidator
        from fnirsi_companion.ui.common.data_pages import ScaleBox
        sb = ScaleBox(0.0)
        self.assertEqual(sb.text(), "0")
        sb.setValue(1.5e-7)
        self.assertEqual(sb.text(), "1.5e-07")
        sb.setValue(2.2e-12)                       # a pF value is kept, not rounded to 0
        self.assertEqual(sb.value(), 2.2e-12)
        self.assertEqual(sb.valueFromText("4.7e3"), 4700.0)
        self.assertEqual(sb.validate("1e", 2)[0], QValidator.State.Intermediate)
        self.assertEqual(sb.validate("abc", 3)[0], QValidator.State.Invalid)

    def test_mono_families_are_installed_ones(self):
        from PySide6.QtGui import QFontDatabase
        have = set(QFontDatabase.families())
        fams = style.mono_families()
        self.assertTrue(fams)
        self.assertTrue(all(f in have for f in fams) or fams == style.MONO_FAMILIES)

    def show_every_view(self):
        shown = []
        for theme in ("dark", "light"):
            self.settings.set("theme", theme)
            self.win.apply_theme()
            self.assertEqual(style.name(), theme)
            self.win.resize(1100, 720)
            self.win.show()
            for key in self.win.view_order():
                self.win.select_view(key)
                QApplication.processEvents()
                self.assertEqual(self.win.current_view(), key)
                img = self.win.grab()
                self.assertFalse(img.isNull())
                self.assertEqual((img.width(), img.height()), (1100, 720))
                shown.append(key)
        self.win.hide()
        return shown

    def test_every_view_without_a_meter(self):
        self.assertEqual(self.show_every_view(),
                         ["live", "trend", "log", "diagnostics", "settings"] * 2)

    def test_every_view_of_the_lc1020e(self):
        self.plug(LC_PORT)
        self.lc_meter.pending.append((lc.MEASURE, fl.build_measure()))
        self.done(self.page("Diagnostics").ping())
        self.assertEqual(len(self.show_every_view()), 18)

    def test_every_view_of_the_2c53t(self):
        self.plug(C2_PORT)
        self.assertEqual(len(self.show_every_view()), 22)

    def test_update_drive_view(self):
        drive = os.path.join(self.tmp.name, "LC1020E-IAP")
        os.makedirs(drive)
        self.watcher.drives_changed.emit([DriveInfo("LC1020E", drive, 0x19F5, 0x5740, "ready")])
        self.assertTrue(wait(lambda: self.win.drive_page is not None))
        self.assertIn("Update", self.win.nav_titles())
        self.assertEqual(self.win.current_view(), "update")
        self.assertEqual(len(self.show_every_view()), 12)
        self.win.select_view("update")
        self.watcher.drives_changed.emit([])
        self.assertTrue(wait(lambda: "update" not in self.win.views))
        self.assertEqual(self.win.current_view(), "live")

    def test_sidebar_keys_follow_the_order(self):
        self.plug(C2_PORT)
        self.win.show()
        self.win.activateWindow()
        QTest.qWaitForWindowActive(self.win)
        for i, key in enumerate(self.win.view_order()[:9]):
            QTest.keyClick(self.win, Qt.Key_1 + i, Qt.ControlModifier)
            self.assertEqual(self.win.current_view(), key)

    def test_theme_switch_rebuilds_the_sheet_and_the_plots(self):
        app = QApplication.instance()
        self.settings.set("theme", "dark")
        self.win.apply_theme()
        dark_sheet = app.styleSheet()
        dark_bg = self.win.trend.plot.backgroundBrush().color().name()
        self.assertEqual(dark_bg, style.DARK["panel-2"].name())
        self.assertIn(style.qss(style.DARK["body"]), dark_sheet)
        self.win.prefs.theme.setCurrentIndex(self.win.prefs.theme.findData("light"))
        self.assertEqual(self.settings.get("theme"), "light")
        self.assertNotEqual(app.styleSheet(), dark_sheet)
        self.assertIn(style.qss(style.LIGHT["body"]), app.styleSheet())
        self.assertEqual(self.win.trend.plot.backgroundBrush().color().name(),
                         style.LIGHT["panel-2"].name())
        self.assertEqual(self.win.live.mini.backgroundBrush().color().name(),
                         style.LIGHT["panel-2"].name())
        self.assertEqual(app.palette().color(app.palette().ColorRole.Window).name(),
                         style.LIGHT["body"].name())
        self.assertEqual(style._state["style"].name(), "fusion")
        self.assertEqual(app.style().metaObject().className(), "QStyleSheetStyle")

    def test_seven_segment_characters(self):
        for ch, segs in widgets.SEGMENTS.items():
            self.assertEqual(widgets.segments_of(ch), segs)
            self.assertTrue(set(segs) <= set("abcdefg"))
            self.assertTrue(segs or ch == " ", ch)
        self.assertEqual(widgets.segments_of("T"), widgets.SEGMENTS["t"])    # the other case
        self.assertIsNone(widgets.segments_of("Ω"))                          # a blank
        self.assertIsNone(widgets.segments_of("K"))
        self.assertEqual(widgets.cells_of("1.5"), [("1", True), ("5", False)])
        self.assertEqual(widgets.cells_of(".OL"), [(" ", True), ("O", False), ("L", False)])
        self.assertEqual(widgets.split_reading("100.00 nF"), ("100.00", "nF"))
        self.assertEqual(widgets.split_reading("4.70 kΩ"), ("4.70", "kΩ"))
        self.assertEqual(widgets.split_reading(".OL"), (".OL", ""))
        seg = widgets.SevenSegment(4)
        seg.resize(400, 60)
        for theme in ("dark", "light"):
            style.apply(None, theme)
            for ch in widgets.SEGMENTS:
                seg.setText(ch + "." + ch)
                self.assertFalse(seg.grab().isNull())
        self.win.apply_theme()

    def test_lc1020e_annunciators_from_the_records(self):
        self.plug(LC_PORT)
        live = self.win.live
        self.assertEqual(live.big.words, ["AUTO", "HOLD", "SER", "PAR"])
        self.lc_meter.pending.append((lc.MEASURE, fl.build_measure(parallel=1)))
        self.done(self.page("Diagnostics").ping())
        self.assertTrue(wait(lambda: live.last is not None))
        lcr = self.page("LCR")
        self.assertTrue(wait(lambda: 2 in lcr.combos and lcr.state is not None))
        # STATE circuit 0 (auto): series / parallel from the MEASURE record (parallel 1)
        self.assertEqual(live.big.lit, {"PAR"})
        self.assertEqual(live.big.mode, "1kHz 0.6V")              # FREQ and AMP choices
        self.done(lcr._hold(True))
        self.assertIn("HOLD", live.big.lit)
        self.assertNotIn("AUTO", live.big.lit)                     # no RANGE list answered
        self.assertEqual(live.big.secondary[:2], ("D", "0.0012"))
        self.assertEqual(live.big.quantity, "C")
        self.assertEqual(len(self.page("Keys").live_buttons), 12)
        self.done(self.page("Keys").press(9, 0))
        self.assertIn((lc.KEY, struct.pack("<BH", 9, 0)), self.lc_meter.log)

    def test_2c53t_annunciators_from_the_records(self):
        self.plug(C2_PORT)
        live = self.win.live
        self.assertEqual(live.big.words, ["AUTO", ".OL", "CAL", "PAS", "Er"])
        self.c2_meter.state_kw["status"] = 6                       # Er1
        self.done(self.page("Diagnostics").state())
        self.assertEqual(live.big.lit, {"Er"})
        self.assertEqual(live.big.mode, "R")
        self.assertIsNone(live.big.secondary)
        keys = self.page("Keys")
        self.assertEqual(sorted(keys.live_buttons), [2, 4, 7, 13, 14, 15, 22])
        self.assertEqual(keys.live_buttons[15].text(), "hold")
        self.assertEqual(keys.live_buttons[22].sub, "long press")
        self.done(keys.press(15))
        self.assertEqual(self.c2_meter.keys, [15])

    def test_header_chips(self):
        self.assertTrue(self.win.fw_chip.isHidden())
        self.plug(LC_PORT)
        w = self.win
        self.assertEqual(w.fw_chip.text(), "Firmware V1.1 OWN")
        self.assertEqual(w.boot_chip.text(), "Bootloader A·v2")
        self.assertEqual(w.port_chip.text(), LC_PORT.device)
        self.assertIn(LC_SERIAL, w.port_chip.toolTip())
        self.assertTrue(w.connect_button.isHidden() and not w.disconnect_button.isHidden())
        self.assertTrue(w.device_list.isHidden())                  # one meter, no failure
        self.win.disconnect_active()
        self.settle()
        self.assertFalse(w._chip_wanted[w.fail_chip])              # closed by hand
        self.denied.add(C2_PORT.device)
        self.plug(LC_PORT, C2_PORT)
        self.assertTrue(w._chip_wanted[w.fail_chip])
        self.assertIn("permission denied", w.fail_chip.toolTip())
        self.assertFalse(w.device_list.isHidden())


if __name__ == "__main__":
    unittest.main()
