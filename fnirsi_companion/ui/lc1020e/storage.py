"""LC1020E Settings, Calibration and Backup pages (SL 5.3.3, 5.4, 8.2, 9; DESIGN 13.8, 13.9).

Settings     every SETTINGS_TABLES setting (range: a number on its step; choices: the meter's own
             texts), SETTINGS_SET (RAM only), each row then read again from the meter;
             SETTINGS_SAVE with the backup first and the typed "OK".
Calibration  CALIBRATE open (type 11) and short (type 0), backup first, typed "OK"; the progress
             from the CALIBRATION events; the answer's calibration status and save status.
Backup       both settings copies into a dated file bound to the meter's UID (LC1020E.backup), the
             files of this meter, comparison of a file with the meter's copies now, restore of one
             copy (LC1020E.restore: same UID only, the copy checked, backup first), and the
             recovery commands 0x54 .. 0x5A with the conditions SL 9.3 gives for them.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QGridLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSpinBox, QVBoxLayout)

from ...devices import lc1020e as lc
from ..base import Page, describe_error, group, row


class LcSettingsPage(Page):
    title = "Settings"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.settings: dict[int, lc.Setting] = {}
        self.editors: dict[int, object] = {}
        self.current: dict[int, QLabel] = {}
        self.grid = QGridLayout()
        self.body.addWidget(group("Settings of the meter (SETTINGS_SET changes RAM only)", self.grid))
        self.reload_button = self.control(QPushButton("Read the settings again"), lc.SETTINGS_TABLES)
        self.reload_button.clicked.connect(self.load)
        self.save_button = self.control(QPushButton("SETTINGS_SAVE…"), lc.SETTINGS_SAVE)
        self.save_button.clicked.connect(self.save)
        self.body.addLayout(row(self.reload_button, self.save_button))
        self.body.addStretch(1)
        if device.has(lc.SETTINGS_TABLES):
            self.load()

    def load(self):
        dev = self.dev

        def job(_p):
            out = []
            for i in range(dev.settings_count()):
                try:
                    out.append((i, dev.settings_tables(i)))
                except lc.StatusError as e:
                    out.append((i, e))
            return out

        return self.run("SETTINGS_TABLES", job, self._built)

    def _built(self, items) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                self._controls = [(c, k) for c, k in self._controls if c is not w]
                w.deleteLater()
        self.settings.clear()
        self.editors.clear()
        self.current.clear()
        for r, (sid, st) in enumerate(items):
            if isinstance(st, Exception):
                self.grid.addWidget(QLabel("setting %d" % sid), r, 0)
                self.grid.addWidget(QLabel("not answered: %s" % describe_error(st, lc.STATUS_NAME)),
                                    r, 1)
                continue
            self.settings[sid] = st
            self.grid.addWidget(QLabel(st.name), r, 0)
            if st.kind == 0:
                ed = QComboBox()
                for i, text in enumerate(st.choices):
                    ed.addItem(text, st.min + i * st.step)
            else:
                ed = QSpinBox()
                ed.setRange(st.min, st.max)
                ed.setSingleStep(max(st.step, 1))
            ed.setObjectName("setting_%d" % sid)
            self.grid.addWidget(ed, r, 1)
            btn = self.control(QPushButton("Set"), lc.SETTINGS_SET)
            btn.setObjectName("setting_set_%d" % sid)
            btn.clicked.connect(lambda _=False, sid=sid: self.set_value(sid))
            self.grid.addWidget(btn, r, 2)
            cur = QLabel("")
            self.grid.addWidget(cur, r, 3)
            self.editors[sid] = ed
            self.current[sid] = cur
            self._show(st)
        self.say("%d settings" % len(self.settings))

    def _show(self, st: lc.Setting) -> None:
        self.settings[st.id] = st
        ed = self.editors[st.id]
        ed.blockSignals(True)
        if isinstance(ed, QComboBox):
            ed.setCurrentIndex(ed.findData(st.current))
            text = ed.currentText() if ed.currentIndex() >= 0 else str(st.current)
        else:
            ed.setValue(st.current)
            text = str(st.current)
        ed.blockSignals(False)
        self.current[st.id].setText("meter: %s" % text)

    def value_of(self, sid: int) -> int:
        ed = self.editors[sid]
        return ed.currentData() if isinstance(ed, QComboBox) else ed.value()

    def set_value(self, sid: int):
        dev = self.dev
        value = self.value_of(sid)
        st = self.settings[sid]
        if st.kind == 1 and st.step and (value - st.min) % st.step:
            self.say("%s: %d is not on the step %d from %d" % (st.name, value, st.step, st.min),
                     error=True)
            return None

        def job(_p):
            state = dev.settings_set(sid, value)
            return state, dev.settings_tables(sid)

        def done(res):
            state, setting = res
            self._show(setting)
            self.ctx.publish_state(state)
            self.say("%s set to %s (RAM)" % (setting.name, self.current[sid].text()[7:]))

        return self.run("SETTINGS_SET %s" % st.name, job, done,
                        failed=lambda e: self._show(self.settings[sid]))

    def save(self):
        dev = self.dev
        word = self.confirm("Save the settings",
                            "SETTINGS_SAVE writes the meter's settings block into its flash "
                            "copies (SL 9). Both settings copies are read into a backup file of "
                            "this meter first.")
        if word is None:
            return None
        return self.run("SETTINGS_SAVE", lambda p: dev.settings_save(word),
                        lambda r: self.say(r.describe()), exclusive=True)


CAL_TYPES = {11: "open", 0: "short"}


class CalibrationPage(Page):
    title = "Calibration"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.open_button = self.control(QPushButton("Open calibration (type 11)…"), lc.CALIBRATE)
        self.open_button.clicked.connect(lambda: self.calibrate(11))
        self.short_button = self.control(QPushButton("Short calibration (type 0)…"), lc.CALIBRATE)
        self.short_button.clicked.connect(lambda: self.calibrate(0))
        self.progress = QLabel("—")
        self.progress.setObjectName("cal_progress")
        self.result = QLabel("—")
        self.result.setObjectName("cal_result")
        self.result.setWordWrap(True)
        box = QVBoxLayout()
        box.addLayout(row(self.open_button, self.short_button))
        box.addLayout(row("Progress (CALIBRATION events):", self.progress))
        box.addLayout(row("Result:", self.result))
        box.addWidget(QLabel("The meter runs its own calibration procedure and saves the result "
                             "itself (SL 5.3.3). Types 1..10 (factory references, factory mode "
                             "only) are on the Diagnostics page."))
        self.body.addWidget(group("CALIBRATE", box))
        self.body.addStretch(1)

    def calibrate(self, cal_type: int):
        dev = self.dev
        name = CAL_TYPES.get(cal_type, "type %d" % cal_type)
        word = self.confirm("%s calibration" % name.capitalize(),
                            "CALIBRATE type %d (%s): the meter runs its own calibration and saves "
                            "the result into its settings (the calibration table changes). Both "
                            "settings copies are read into a backup file of this meter first."
                            % (cal_type, name))
        if word is None:
            return None
        self.progress.setText("started")
        return run_calibration(self, dev, cal_type, word, self.result)

    def on_event(self, ev) -> None:
        if isinstance(ev, lc.Event) and ev.cmd == lc.EV_CALIBRATION and "status" in ev.fields:
            f = ev.fields
            self.progress.setText("status %s, step %d, type %d" % (
                lc.CAL_STATUS.get(f["status"], f["status"]), f["step"], f["type"]))


def run_calibration(page: Page, dev, cal_type: int, word: str, label: QLabel):
    def done(r: lc.CalResult):
        text = "calibration status %s, type %d, save status 0x%08X" % (
            lc.CAL_STATUS.get(r.cal_status, r.cal_status), r.cal_type, r.save_status)
        label.setText(text)
        page.say(text)

    def failed(e):
        rec = getattr(e, "record", None)
        if isinstance(rec, lc.CalResult):
            label.setText("status %d: calibration status %s, save status 0x%08X" % (
                rec.status, lc.CAL_STATUS.get(rec.cal_status, rec.cal_status), rec.save_status))
        else:
            label.setText(describe_error(e, lc.STATUS_NAME))

    return page.run("CALIBRATE %d" % cal_type, lambda p: dev.calibrate(cal_type, word), done,
                    exclusive=True, failed=failed)


RECOVERY = [  # (code, method, title, consequence: SL 9.3 / 5.4 / DESIGN 13.8)
    (lc.RESTORE_DEFAULTS, "restore_defaults", "RESTORE_DEFAULTS",
     "Runs the meter's defaults routine, which saves once itself: the user calibration table "
     "becomes the factory one (SL 5.4). Refused while saves are held, a restart is pending or a "
     "calibration runs."),
    (lc.SETTINGS_RESTORE_B, "settings_restore_b", "SETTINGS_RESTORE_B",
     "Writes copy B's block to copy A with B's sequence. Allowed when saves are not held and the "
     "backup differs or B was loaded; the meter restarts after result 0 (SL 9.3)."),
    (lc.SETTINGS_ACCEPT_A, "settings_accept_a", "SETTINGS_ACCEPT_A",
     "Writes copy A's block to copy B. Allowed when saves are not held and the backup differs or "
     "B was loaded; restarts when B was the loaded copy (SL 9.3)."),
    (lc.SETTINGS_ACCEPT_B, "settings_accept_b", "SETTINGS_ACCEPT_B",
     "Writes copy B's block to copy A with B's sequence. Allowed when copy A is damaged (SL 9.3)."),
    (lc.SETTINGS_ACCEPT_DEFAULTS, "settings_accept_defaults", "SETTINGS_ACCEPT_DEFAULTS",
     "Writes the RAM block (the defaults, plus any change since) to B then A. Allowed when no "
     "valid copy was found at load (SL 9.3)."),
]


class BackupPage(Page):
    title = "Backup"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.backup_button = self.control(QPushButton("Back up both copies now"), lc.CAL_READ)
        self.backup_button.clicked.connect(self.backup)
        self.files = QListWidget()
        self.files.setObjectName("backups")
        self.files.currentItemChanged.connect(lambda *_: self.refresh_controls())
        self.compare_button = self.control(QPushButton("Compare with the meter"), lc.CAL_READ)
        self.compare_button.clicked.connect(self.compare)
        self.copy_combo = QComboBox()
        self.copy_combo.addItems(["A", "B"])
        self.restore_button = self.control(QPushButton("Restore this copy…"),
                                           lc.SETTINGS_WRITE_BLOCK, lc.CAL_READ)
        self.restore_button.clicked.connect(self.restore)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        b = QVBoxLayout()
        b.addLayout(row(self.backup_button))
        b.addWidget(QLabel("Backups of this meter (UID %s):" % device.info.uid_hex))
        b.addWidget(self.files)
        b.addLayout(row(self.compare_button, "Copy:", self.copy_combo, self.restore_button))
        b.addWidget(self.result)
        self.body.addWidget(group("Backup and restore (SL 9.4)", b), 1)

        rec = QVBoxLayout()
        self.recovery_buttons = {}
        for code, method, title, text in RECOVERY:
            btn = self.control(QPushButton(title + "…"), code, lc.CAL_READ)
            btn.clicked.connect(lambda _=False, m=method, t=title, x=text: self.recovery(m, t, x))
            lab = QLabel(text)
            lab.setWordWrap(True)
            rec.addLayout(row(btn, lab, stretch=False))
            self.recovery_buttons[code] = btn
        self.flags = QLabel("—")
        self.flags.setWordWrap(True)
        rec.addLayout(row("Meter state:", self.flags))
        self.body.addWidget(group("Recovery commands (each makes a backup first)", rec))
        self.list_backups()

    def extra_enabled(self, widget) -> bool:
        if widget in (getattr(self, "compare_button", None), getattr(self, "restore_button", None)):
            return self.selected() is not None
        return True

    def folder(self) -> Path:
        return Path(self.ctx.settings.backups_folder())

    def selected(self) -> Path | None:
        it = self.files.currentItem()
        return Path(it.data(256)) if it is not None else None

    def list_backups(self) -> None:
        out_dir = self.dev.backup_dir(self.folder())
        self.files.clear()
        paths = sorted(out_dir.glob("*.json"), reverse=True) if out_dir.is_dir() else []
        for p in paths:
            try:
                doc = lc.LC1020E.load_backup(p)
                text = "%s  %s  " % (p.name, doc.get("date", "?")) + "  ".join(
                    "%s %s %s" % (c["name"], c["crc32"], "valid" if c.get("valid") else "not valid")
                    for c in doc["copies"])
            except lc.BackupMismatch as e:
                text = "%s  (unreadable: %s)" % (p.name, e)
            it = QListWidgetItem(text)
            it.setData(256, str(p))
            self.files.addItem(it)
        self.refresh_controls()

    def backup(self):
        dev, folder = self.dev, self.folder()

        def done(path):
            self.list_backups()
            self.say("backup: %s" % path)

        return self.run("backup", lambda p: dev.backup(folder), done, exclusive=True)

    def compare(self):
        path = self.selected()
        if path is None:
            return None
        dev = self.dev

        def job(_p):
            doc = lc.LC1020E.load_backup(path)
            with dev.link.exclusive():
                now = [dev.read_copy(0), dev.read_copy(1)]
            return doc, now

        def done(res):
            doc, now = res
            parts = []
            for c, data in zip(doc["copies"], now):
                parts.append("copy %s: %s" % (c["name"], "same as the meter" if c["data"] == data
                                              else "differs from the meter (meter CRC-32 0x%08X)"
                                              % lc.crc32(data)))
            if doc["uid"] != dev.info.uid_hex:
                parts.append("the file is of meter %s" % doc["uid"])
            self.result.setText("; ".join(parts))
            self.say("compared %s" % path.name)

        return self.run("compare backup", job, done, exclusive=True)

    def restore(self):
        path = self.selected()
        if path is None:
            return None
        copy = self.copy_combo.currentText()
        dev = self.dev
        word = self.confirm("Restore copy %s" % copy,
                            "SETTINGS_WRITE_BLOCK writes copy %s of %s into the meter's settings "
                            "(the settings and calibration of this copy replace the meter's), "
                            "only when the file belongs to this meter's UID and the copy "
                            "passes its check. The meter's current copies are backed up first; "
                            "the meter restarts after the last chunk (SL 9.4)." % (copy, path.name))
        if word is None:
            return None
        return self.run("restore", lambda p: dev.restore(path, copy, word),
                        lambda r: (self.result.setText(r.describe()), self.list_backups(),
                                   self.say("restored copy %s: %s" % (copy, r.describe()))),
                        exclusive=True)

    def recovery(self, method: str, title: str, text: str):
        dev = self.dev
        word = self.confirm(title, text + " Both settings copies are read into a backup file "
                                          "first.")
        if word is None:
            return None
        return self.run(title, lambda p: getattr(dev, method)(word),
                        lambda r: (self.result.setText(r.describe()), self.list_backups(),
                                   self.say("%s: %s" % (title, r.describe()))),
                        exclusive=True)

    def on_state(self, st) -> None:
        if isinstance(st, lc.State):
            names = [n for n in lc.flag_names(st.flags, lc.STATE_FLAGS)
                     if n in ("save failed", "backup differs", "saves held", "restart pending",
                              "calibration running")]
            self.flags.setText(", ".join(names) or "no save flags set")
