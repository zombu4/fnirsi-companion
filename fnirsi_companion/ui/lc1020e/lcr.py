"""LC1020E LCR and Compare pages.

LCR (SL 5.3, 8.1): one selector per LCR_TABLES list, built from the meter's own lists (names,
units, entry texts); a choice sends LCR_SET with the list id as the setting (the LCR_SET setting
numbers are the list ids, SL 5.3) and the selectors then show the meter's STATE: setting 0 Func,
1 secondary, 2 frequency, 3 level, 4 bias, 5 range, 6 speed, 7 circuit mode, compared by the list's
value type (index, u32, f32). HOLD (SL 5.3) and the screen (SCREEN, SL 5.2) show STATE too.

Compare (SL 5.3, 6.3): the COMPARE record (refreshed while the page is shown, one request
outstanding), and COMPARE_SET with the limits of SL 5.3.1; the form shows what the meter stored.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QLabel, QPushButton)

from ...devices import lc1020e as lc
from ..base import Page, describe_error, group, row

# STATE field of each LCR_SET setting / LCR_TABLES list id (SL 5.3 table "Settings of LCR_SET")
STATE_FIELD = {0: "func", 1: "secondary", 2: "frequency", 3: "level", 4: "bias", 5: "range",
               6: "speed", 7: "circuit"}


def entry_index(lst: lc.LcrList, value) -> int:
    """Index of the list entry that stands for the STATE value, -1 when none does."""
    for i, (v, _text) in enumerate(lst.entries):
        if lst.value_type == 2:
            if isinstance(value, float) and math.isfinite(value) and abs(v - value) <= 1e-6:
                return i
        elif v == value:
            return i
    return -1


class LcrPage(Page):
    title = "LCR"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.lists: dict[int, lc.LcrList] = {}
        self.combos: dict[int, QComboBox] = {}
        self.notes: dict[int, QLabel] = {}
        self.grid = QGridLayout()
        self.body.addWidget(group("Measurement settings (the meter's own lists)", self.grid))
        self.hold = self.control(QCheckBox("HOLD"), lc.HOLD)
        self.hold.clicked.connect(self._hold)
        self.screen_combo = self.control(QComboBox(), lc.SCREEN)
        for n, name in lc.SCREEN_NAME.items():
            self.screen_combo.addItem(name, n)
        self.screen_combo.activated.connect(self._screen)
        self.reload_button = self.control(QPushButton("Read the lists again"), lc.LCR_TABLES)
        self.reload_button.clicked.connect(self.load)
        self.body.addLayout(row(self.hold, "Screen:", self.screen_combo, self.reload_button))
        self.flags = QLabel("—")
        self.flags.setWordWrap(True)
        self.body.addLayout(row("Meter state:", self.flags))
        self.body.addStretch(1)
        self.state = None
        if device.has(lc.LCR_TABLES):
            self.load()

    def load(self):
        dev = self.dev

        def job(_p):
            count = dev.lcr_table_count()
            out = []
            for i in range(count):
                try:
                    out.append((i, dev.lcr_tables(i)))
                except lc.StatusError as e:
                    out.append((i, e))
            st = dev.read_state() if dev.has(lc.STATE) else None
            return out, st

        return self.run("LCR_TABLES", job, self._built)

    def _built(self, res) -> None:
        lists, st = res
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                self._controls = [(c, k) for c, k in self._controls if c is not w]
                w.deleteLater()
        self.lists.clear()
        self.combos.clear()
        self.notes.clear()
        for r, (lid, lst) in enumerate(lists):
            if isinstance(lst, Exception):
                self.grid.addWidget(QLabel("list %d" % lid), r, 0)
                self.grid.addWidget(QLabel("not answered: %s" % describe_error(lst, lc.STATUS_NAME)),
                                    r, 1)
                continue
            self.lists[lid] = lst
            self.grid.addWidget(QLabel(lst.name + (" (%s)" % lst.unit if lst.unit else "")), r, 0)
            combo = QComboBox()
            combo.setObjectName("lcr_%d" % lid)
            for v, text in lst.entries:
                combo.addItem(text, v)
            combo.activated.connect(lambda idx, lid=lid: self.set_value(lid, idx))
            self.control(combo, lc.LCR_SET)
            self.grid.addWidget(combo, r, 1)
            note = QLabel("")
            self.grid.addWidget(note, r, 2)
            self.combos[lid] = combo
            self.notes[lid] = note
        if st is not None:
            self.ctx.publish_state(st)
        self.say("%d lists" % len(self.lists))

    def set_value(self, lid: int, index: int):
        dev = self.dev

        def job(_p):
            dev.lcr_set(lid, index)
            return dev.read_state()

        def failed(_e):
            if self.state is not None:
                self.on_state(self.state)          # back to what the meter has

        return self.run("LCR_SET %s" % self.lists[lid].name, job, self.ctx.publish_state,
                        failed=failed)

    def _hold(self, on: bool):
        dev = self.dev
        self.hold.setChecked(not on)               # shown from the answer only
        return self.run("HOLD", lambda p: dev.hold(on), self.ctx.publish_state)

    def _screen(self, idx: int):
        dev = self.dev
        n = self.screen_combo.itemData(idx)
        return self.run("SCREEN", lambda p: dev.screen(n), self.ctx.publish_state,
                        failed=lambda e: self.state and self.on_state(self.state))

    def on_state(self, st) -> None:
        if not isinstance(st, lc.State):
            return
        self.state = st
        for lid, combo in self.combos.items():
            field = STATE_FIELD.get(lid)
            if field is None:
                continue
            value = getattr(st, field)
            i = entry_index(self.lists[lid], value)
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            self.notes[lid].setText("" if i >= 0 else "meter value %r is not in the list" % value)
        self.hold.setChecked(st.flag("hold"))
        i = self.screen_combo.findData(st.screen)
        self.screen_combo.blockSignals(True)
        self.screen_combo.setCurrentIndex(i)
        self.screen_combo.blockSignals(False)
        names = lc.flag_names(st.flags, lc.STATE_FLAGS)
        self.flags.setText("screen %s; flags: %s; dropped stream frames %d" % (
            lc.SCREEN_NAME.get(st.screen, st.screen), ", ".join(names) or "none", st.dropped))


class ComparePage(Page):
    title = "Compare"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.labels = {}
        form = QFormLayout()
        for key, name in (("quantity", "Quantity"), ("nominal", "Nominal"),
                          ("tolerance", "Tolerance %"), ("deviation", "Deviation %"),
                          ("ok", "Verdict"), ("passed", "Passed"), ("failed", "Failed"),
                          ("value", "Value"), ("warn", "Warn type / sound / LED")):
            lab = QLabel("—")
            lab.setObjectName("cmp_" + key)
            form.addRow(name, lab)
            self.labels[key] = lab
        self.refresh_button = self.control(QPushButton("Read COMPARE"), lc.COMPARE)
        self.refresh_button.clicked.connect(self.refresh)
        form.addRow(self.refresh_button)
        self.body.addWidget(group("COMPARE (the meter's counters and verdict)", form))

        self.nominal = QDoubleSpinBox()
        self.nominal.setDecimals(3)
        self.nominal.setRange(0.001, 999999999.0)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setDecimals(1)
        self.tolerance.setRange(0.0, 99.9)
        self.warn_type = QComboBox()
        self.warn_type.addItems(["0 NG", "1 OK"])        # SL 8.2 setting 3 choices
        self.warn_sound = QComboBox()
        self.warn_sound.addItems(["0", "1"])
        self.warn_led = QComboBox()
        self.warn_led.addItems(["0", "1"])
        self.set_button = self.control(QPushButton("COMPARE_SET"), lc.COMPARE_SET)
        self.set_button.clicked.connect(self.apply)
        f2 = QFormLayout()
        f2.addRow("Nominal (0.001 .. < 1e9)", self.nominal)
        f2.addRow("Tolerance % (0 .. 99.9)", self.tolerance)
        f2.addRow("Warn type", self.warn_type)
        f2.addRow("Warn sound", self.warn_sound)
        f2.addRow("Warn LED", self.warn_led)
        f2.addRow(self.set_button)
        f2.addRow(QLabel("The meter stores the nominal as a scale and five digits and the "
                         "tolerance in tenths of a percent (SL 5.3.1); the form shows what it "
                         "stored. RAM only, not saved."))
        self.body.addWidget(group("COMPARE_SET", f2))
        self.body.addStretch(1)
        self.pending = None
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        self.edits("cmp", self.nominal, self.tolerance, self.warn_type, self.warn_sound,
                   self.warn_led)

    def refresh(self):
        dev = self.dev
        self.pending = self.run("COMPARE", lambda p: dev.compare(), self.show_compare)
        return self.pending

    def _poll(self) -> None:
        if self.busy or not self.dev.has(lc.COMPARE):
            return
        if self.pending is not None and not self.pending.done.is_set():
            return                     # one request outstanding
        self.refresh()

    def show_compare(self, c: lc.Compare) -> None:
        L = self.labels
        L["quantity"].setText(lc.MAIN_NAME.get(c.quantity, str(c.quantity)))
        L["nominal"].setText("%g" % c.nominal)
        L["tolerance"].setText("%g" % c.tolerance)
        L["deviation"].setText("%g" % c.deviation)
        L["ok"].setText("pass" if c.ok else "fail")
        L["passed"].setText(str(c.passed))
        L["failed"].setText(str(c.failed))
        L["value"].setText("%g" % c.value)
        L["warn"].setText("%d / %d / %d" % (c.warn_type, c.warn_sound, c.warn_led))
        if self.dirty("cmp"):
            return                       # the user is editing the form
        for w, v in ((self.nominal, c.nominal), (self.tolerance, c.tolerance)):
            self.quietly(w, w.setValue, v)
        for w, v in ((self.warn_type, c.warn_type), (self.warn_sound, c.warn_sound),
                     (self.warn_led, c.warn_led)):
            self.quietly(w, w.setCurrentIndex, v if v in (0, 1) else -1)

    def apply(self):
        dev = self.dev
        args = (self.nominal.value(), self.tolerance.value(), self.warn_type.currentIndex(),
                self.warn_sound.currentIndex(), self.warn_led.currentIndex())
        if min(args[2:]) < 0:
            self.say("choose warn type, sound and LED", error=True)
            return None

        def answered(c):
            self.clean("cmp")
            self.show_compare(c)
            self.say("stored by the meter")

        return self.run("COMPARE_SET", lambda p: dev.compare_set(*args), answered,
                        failed=lambda e: self.clean("cmp"))

    def on_shown(self) -> None:
        self.timer.start()
        self._poll()

    def on_hidden(self) -> None:
        self.timer.stop()

    def stop(self) -> None:
        self.timer.stop()
