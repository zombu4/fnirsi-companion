"""LC1020E Settings, Calibration and Backup pages (SL 5.3.3, 5.4, 8.2, 9; DESIGN 13.8, 13.9).

Settings     every SETTINGS_TABLES setting (range: a number on its step; choices: the meter's own
             texts), SETTINGS_SET (RAM only), each row then read again from the meter;
             SETTINGS_SAVE with the backup first and the typed "OK".
Calibration  CALIBRATE open (type 11) and short (type 0), backup first, typed "OK"; the progress
             from the CALIBRATION events; the answer's calibration status and save status.
Backup       both settings copies into a dated file bound to the meter's UID (LC1020E.backup), the
             files of this meter, comparison of a file with the meter's copies now, restore of one
             copy (LC1020E.restore: same UID only, the copy checked, backup first), and the
             recovery commands 0x54 .. 0x5A with the conditions SL 9.3 gives for them. With the
             own calibration area (CAP_OCAL, OWN_CAL_DESIGN.md 6.1): OCAL_INFO, the .ocal backups of
             both area slots (LC1020E.ocal_backup) listed with their records, the restore of one
             record (LC1020E.ocal_restore: same UID in the name and the record header only, backup
             first) and OCAL_ROLLBACK.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QGridLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSpinBox)

from ...devices import lc1020e as lc
from ..base import Page, describe_error, row
from ..widgets import Card, Segmented, StatRow, label, set_kind

SEGMENTED_MAX = 6            # settings of up to this many choices get a segmented control
LIST_MAX_H = 320             # the backup list scrolls inside its card above this height


class LcSettingsPage(Page):
    title = "Settings"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.settings: dict[int, lc.Setting] = {}
        self.editors: dict[int, object] = {}
        self.current: dict[int, QLabel] = {}
        card = Card("Settings")
        self.reload_button = self.control(set_kind(QPushButton("Read the settings again"),
                                                   "small"), lc.SETTINGS_TABLES)
        self.reload_button.clicked.connect(self.load)
        card.add_right(self.reload_button)
        card.add(label("Each setting as the meter lists it; Set changes it in RAM "
                       "(SETTINGS_SET), SETTINGS_SAVE writes them into the meter's flash.",
                       "hint", word_wrap=True))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(3, 1)
        card.add(self.grid)
        self.save_button = self.control(set_kind(QPushButton("SETTINGS_SAVE…"), "danger"),
                                        lc.SETTINGS_SAVE)
        self.save_button.clicked.connect(self.save)
        card.add(row(self.save_button))
        self.body.addWidget(card)
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
        # the meter's own labels: setting 1 (volume) and setting 4 (warn sound) are both "Volume"
        # (menu item and sorting-dialog row, PROTOCOL.md 8.2), so a repeated label gets its id
        names = [st.name for _, st in items if not isinstance(st, Exception)]
        for r, (sid, st) in enumerate(items):
            if isinstance(st, Exception):
                self.grid.addWidget(label("setting %d" % sid), r, 0)
                self.grid.addWidget(label("not answered: %s" % describe_error(st, lc.STATUS_NAME),
                                          "hint", word_wrap=True), r, 1, 1, 3)
                continue
            self.settings[sid] = st
            self.grid.addWidget(label(st.name if names.count(st.name) == 1
                                      else "%s (setting %d)" % (st.name, sid)), r, 0)
            if st.kind == 0:
                ed = Segmented() if len(st.choices) <= SEGMENTED_MAX else QComboBox()
                for i, text in enumerate(st.choices):
                    ed.addItem(text, st.min + i * st.step)
            else:
                ed = QSpinBox()
                ed.setRange(st.min, st.max)
                ed.setSingleStep(max(st.step, 1))
                ed.setMinimumWidth(110)
                ed.setMaximumWidth(160)
            ed.setObjectName("setting_%d" % sid)
            self.grid.addWidget(ed, r, 1)
            btn = self.control(set_kind(QPushButton("Set"), "small"), lc.SETTINGS_SET)
            btn.setObjectName("setting_set_%d" % sid)
            btn.clicked.connect(lambda _=False, sid=sid: self.set_value(sid))
            self.grid.addWidget(btn, r, 2)
            cur = label("", "mono")
            self.grid.addWidget(cur, r, 3)
            self.editors[sid] = ed
            self.current[sid] = cur
            self._show(st)
        self.say("%d settings" % len(self.settings))

    def _show(self, st: lc.Setting) -> None:
        self.settings[st.id] = st
        ed = self.editors[st.id]
        ed.blockSignals(True)
        if isinstance(ed, (QComboBox, Segmented)):
            ed.setCurrentIndex(ed.findData(st.current))
            text = ed.currentText() if ed.currentIndex() >= 0 else str(st.current)
        else:
            ed.setValue(st.current)
            text = str(st.current)
        ed.blockSignals(False)
        self.current[st.id].setText("meter: %s" % text)

    def value_of(self, sid: int) -> int:
        ed = self.editors[sid]
        return ed.currentData() if isinstance(ed, (QComboBox, Segmented)) else ed.value()

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
        self.open_button = self.control(set_kind(QPushButton("Open calibration (type 11)…"),
                                                 "danger"), lc.CALIBRATE)
        self.open_button.clicked.connect(lambda: self.calibrate(11))
        self.short_button = self.control(set_kind(QPushButton("Short calibration (type 0)…"),
                                                  "danger"), lc.CALIBRATE)
        self.short_button.clicked.connect(lambda: self.calibrate(0))
        card = Card("Calibration")
        card.add(row(self.open_button, self.short_button))
        r_prog = StatRow("Progress (CALIBRATION events)")
        r_res = StatRow("Result", line=False)
        self.progress, self.result = r_prog.value, r_res.value
        self.progress.setObjectName("cal_progress")
        self.result.setObjectName("cal_result")
        card.add(r_prog)
        card.add(r_res)
        card.add(label("The meter runs its own calibration procedure and saves the result itself "
                       "(SL 5.3.3). Types 1..10 (factory references, factory mode only) are on "
                       "the Diagnostics page.", "hint", word_wrap=True))
        self.body.addWidget(card)

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
            sweep = self.dev is not None and self.dev.info.has(lc.CAP_CAL_SWEEP)
            self.progress.setText(lc.cal_progress(ev.fields, sweep))


def run_calibration(page: Page, dev, cal_type: int, word: str, label: QLabel):
    def done(r: lc.CalResult):
        text = r.describe()
        label.setText(text)
        page.say(text)

    def failed(e):
        rec = getattr(e, "record", None)
        if isinstance(rec, lc.CalResult):
            label.setText("status %d: %s" % (rec.status, rec.describe()))
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
        self.backup_button = self.control(set_kind(QPushButton("Back up both copies now"),
                                                   "primary"), lc.CAL_READ)
        self.backup_button.clicked.connect(self.backup)
        self.files = QListWidget()
        self.files.setObjectName("backups")
        self.files.setMaximumHeight(LIST_MAX_H)
        self.files.setMinimumHeight(120)
        self.files.currentItemChanged.connect(lambda *_: self.refresh_controls())
        self.compare_button = self.control(QPushButton("Compare with the meter"), lc.CAL_READ)
        self.compare_button.clicked.connect(self.compare)
        self.copy_combo = Segmented(["A", "B"])
        self.restore_button = self.control(set_kind(QPushButton("Restore this copy…"), "danger"),
                                           lc.SETTINGS_WRITE_BLOCK, lc.CAL_READ)
        self.restore_button.clicked.connect(self.restore)
        self.result = label("", "mono", word_wrap=True)
        b = Card("Backup and restore")
        b.add_right(self.backup_button)
        b.add(label("Backups of this meter (UID %s), newest first (SL 9.4):"
                    % device.info.uid_hex, "hint", word_wrap=True))
        b.add(self.files)
        b.add(row(self.compare_button, "Copy", self.copy_combo, self.restore_button))
        b.add(self.result)
        self.body.addWidget(b)

        rec = Card("Recovery commands")
        rec.add(label("Each one reads both settings copies into a backup file first.", "hint"))
        self.recovery_buttons = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        for r, (code, method, title, text) in enumerate(RECOVERY):
            btn = self.control(set_kind(QPushButton(title + "…"), "danger"), code, lc.CAL_READ)
            btn.clicked.connect(lambda _=False, m=method, t=title, x=text: self.recovery(m, t, x))
            grid.addWidget(btn, r, 0)
            grid.addWidget(label(text, "hint", word_wrap=True), r, 1)
            self.recovery_buttons[code] = btn
        rec.add(grid)
        state_row = StatRow("Meter state", line=False)
        self.flags = state_row.value
        rec.add(state_row)
        self.body.addWidget(rec)
        self._build_ocal_card()
        self.list_backups()

    def extra_enabled(self, widget) -> bool:
        if widget in (getattr(self, "compare_button", None), getattr(self, "restore_button", None)):
            return self.selected() is not None
        if widget is getattr(self, "ocal_restore_button", None):
            return self.ocal_selected() is not None
        return True

    def folder(self) -> Path:
        return Path(self.ctx.settings.backups_folder())

    # -- the own calibration area (OCD 6.1) -----------------------------------------------------
    def _build_ocal_card(self) -> None:
        dev = self.dev
        card = Card("Own calibration area")
        self.ocal_info_button = self.control(set_kind(QPushButton("Read OCAL_INFO"), "small"),
                                             lc.OCAL_INFO)
        self.ocal_info_button.clicked.connect(self.ocal_info)
        self.ocal_backup_button = self.control(set_kind(QPushButton("Back up the area now"),
                                                        "primary"), lc.OCAL_READ)
        self.ocal_backup_button.clicked.connect(self.ocal_backup)
        card.add_right(self.ocal_backup_button)
        self.ocal_state = label("", "mono", word_wrap=True)
        self.ocal_state.setObjectName("ocal_state")
        self.ocal_files = QListWidget()
        self.ocal_files.setObjectName("ocal_backups")
        self.ocal_files.setMaximumHeight(LIST_MAX_H)
        self.ocal_files.setMinimumHeight(90)
        self.ocal_files.currentItemChanged.connect(lambda *_: self.ocal_selection_changed())
        self.ocal_records = label("", "mono", word_wrap=True)
        self.ocal_records.setObjectName("ocal_records")
        self.ocal_slot = Segmented(["slot 0", "slot 1"])
        self.ocal_restore_button = self.control(set_kind(QPushButton("Restore this record…"),
                                                         "danger"), lc.OCAL_WRITE, lc.OCAL_READ)
        self.ocal_restore_button.clicked.connect(self.ocal_restore)
        self.ocal_rollback_button = self.control(set_kind(QPushButton("Roll back the active record…"),
                                                          "danger"), lc.OCAL_ROLLBACK, lc.OCAL_READ)
        self.ocal_rollback_button.clicked.connect(lambda: self.ocal_rollback(0))
        self.ocal_clear_button = self.control(set_kind(QPushButton("Revoke every record…"),
                                                       "danger"), lc.OCAL_ROLLBACK, lc.OCAL_READ)
        self.ocal_clear_button.clicked.connect(lambda: self.ocal_rollback(1))
        self.ocal_result = label("", "mono", word_wrap=True)
        self.ocal_result.setObjectName("ocal_result")
        card.add(label(".ocal files of this meter (UID %s), newest first: both slots of the "
                       "area as read before each write." % dev.info.uid_hex, "hint", word_wrap=True))
        card.add(self.ocal_files)
        card.add(self.ocal_records)
        card.add(row(self.ocal_info_button, "Record", self.ocal_slot, self.ocal_restore_button))
        card.add(row(self.ocal_rollback_button, self.ocal_clear_button))
        card.add(self.ocal_state)
        card.add(self.ocal_result)
        self.body.addWidget(card)
        self.list_ocal_backups()

    def ocal_selected(self) -> Path | None:
        it = self.ocal_files.currentItem() if hasattr(self, "ocal_files") else None
        return Path(it.data(256)) if it is not None else None

    def list_ocal_backups(self) -> None:
        self.ocal_files.clear()
        for p in self.dev.list_ocal_backups(self.folder()):
            it = QListWidgetItem(p.name)
            it.setData(256, str(p))
            self.ocal_files.addItem(it)
        self.ocal_selection_changed()

    def ocal_selection_changed(self) -> None:
        """The records of the selected file, as the meter's load would see them."""
        path = self.ocal_selected()
        text = ""
        if path is not None:
            try:
                data = lc.LC1020E.load_ocal_file(path, self.dev.info.uid_hex)
                text = "\n".join(r.describe() for r in lc.parse_ocal_file(data))
            except lc.BackupMismatch as e:
                text = "unreadable: %s" % e
        self.ocal_records.setText(text)
        self.refresh_controls()

    def ocal_info(self):
        dev = self.dev
        return self.run("OCAL_INFO", lambda p: dev.ocal_info(), self.show_ocal_info)

    def show_ocal_info(self, i: lc.OcalInfo) -> None:
        text = i.describe()
        if i.sections_present:
            text += "; sections " + ", ".join(lc.SECTION_NAME[k] for k in lc.SECTION_NAME
                                              if i.has_section(k))
        if i.sections_rejected:
            text += "; rejected " + ", ".join(i.rejected_names())
        if i.active:
            text += '; META %d "%s"' % (i.meta_time, i.meta_note)
        self.ocal_state.setText(text)
        self.say("OCAL_INFO read")

    def ocal_backup(self):
        dev, folder = self.dev, self.folder()

        def done(path):
            self.list_ocal_backups()
            self.say("area backup: %s" % path)

        return self.run("area backup", lambda p: dev.ocal_backup(folder), done, exclusive=True)

    def ocal_restore(self):
        path = self.ocal_selected()
        if path is None:
            return None
        slot, dev = self.ocal_slot.currentIndex(), self.dev
        word = self.confirm("Restore record %d" % slot,
                            "OCAL_WRITE writes the record of slot %d of %s into the meter's own "
                            "calibration area as a new record (the meter gives it a new sequence "
                            "and checks its values), only when the file and the record belong to "
                            "this meter's UID. Both slots are read into a .ocal backup file first."
                            % (slot, path.name))
        if word is None:
            return None

        def done(r):
            self.ocal_result.setText(r.describe())
            self.list_ocal_backups()
            self.say("restored slot %d: %s" % (slot, r.describe()))

        return self.run("area restore", lambda p: dev.ocal_restore(path, slot, word), done,
                        exclusive=True)

    def ocal_rollback(self, mode: int):
        dev = self.dev
        what = ("the active record is revoked and the other slot's record becomes active"
                if mode == 0 else "every record of this meter is revoked: the meter reads as "
                                   "without the area")
        word = self.confirm("OCAL_ROLLBACK mode %d" % mode,
                            "OCAL_ROLLBACK: %s. Both slots are read into a .ocal backup file "
                            "first." % what)
        if word is None:
            return None

        def done(r):
            self.ocal_result.setText(r.describe())
            self.list_ocal_backups()
            self.say("rollback: %s" % r.describe())

        return self.run("OCAL_ROLLBACK", lambda p: dev.ocal_rollback(mode, word), done,
                        exclusive=True)

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
