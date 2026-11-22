"""LC1020E measurement controls (the "Measurement" card of the Live page) and the Compare page.

LCR (SL 5.3, 8.1): one selector per LCR_TABLES list, built from the meter's own lists (names,
units, entry texts): a segmented control for lists of up to 6 entries, a combo box for longer
ones. A choice sends LCR_SET with the list id as the setting (the LCR_SET setting numbers are the
list ids, SL 5.3) and the selectors then show the meter's STATE: setting 0 Func, 1 secondary,
2 frequency, 3 level, 4 bias, 5 range, 6 speed, 7 circuit mode, compared by the list's value type
(index, u32, f32). HOLD (SL 5.3) and the screen (SCREEN, SL 5.2) show STATE too.

The page also gives the Live page's LCD its annunciators (DESIGN_UI 6.5): HOLD from the STATE flag
"hold"; SER / PAR from STATE's circuit mode (1 series, 2 parallel) or, in auto (0), from the
series / parallel byte of the MEASURE record (PROTOCOL.md: 0 series, 1 parallel; the last field of
the reading's key, devices.lc1020e.measure_to_reading); AUTO when the current choice of the meter's
RANGE list (setting 5) reads "AUTO"; the mode text is the current choices of FREQ (2) and AMP (3).

With CAP_OCAL (OWN_CAL_DESIGN.md "OCD" 6.3) the Measurement card has two more rows: "Vac / Iac" from
the MONITOR command (read at most once a second while readings arrive; "uncalibrated" with the counts
in place of the unit when flag bit 0 is clear) and "gain cal" from byte 65 of the MEASURE record (the
gain correction applied at this cell; a warning when bit 1 says the frequency has GAIN but this cell
is not calibrated).

Compare (SL 5.3, 6.3): the COMPARE record (refreshed while the page is shown, one request
outstanding), and COMPARE_SET with the limits of SL 5.3.1; the form shows what the meter stored.
With CAP_TOLERANCE the record's judgement (IN / HI / LO, signed deviation, limits in force, secondary
limit) is shown and the TOLERANCE_SET form sets the mode (RAM only); older firmware leaves those
rows at "-" and the form disabled.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton)

from ...devices import lc1020e as lc
from ..base import Page, describe_error
from ..common.data_pages import SECONDARY_LABEL, fmt_si
from ..widgets import Card, Segmented, StatRow, label, set_kind

# STATE field of each LCR_SET setting / LCR_TABLES list id (SL 5.3 table "Settings of LCR_SET")
STATE_FIELD = {0: "func", 1: "secondary", 2: "frequency", 3: "level", 4: "bias", 5: "range",
               6: "speed", 7: "circuit"}
SEGMENTED_MAX = 6          # lists of up to this many entries get a segmented control
LCD_WORDS = ["AUTO", "HOLD", "SER", "PAR"]
KEY_PARALLEL = 9           # index of the series / parallel byte in the reading's key
MONITOR_PERIOD = 1.0       # seconds between two MONITOR requests while readings arrive


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
        self.combos: dict[int, object] = {}
        self.notes: dict[int, QLabel] = {}
        card = Card("Measurement")
        self.reload_button = self.control(set_kind(QPushButton("Read the lists again"), "small"),
                                          lc.LCR_TABLES)
        self.reload_button.clicked.connect(self.load)
        card.add_right(self.reload_button)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(2, 1)
        card.add(self.grid)
        self.hold = self.control(QPushButton("HOLD"), lc.HOLD)
        self.hold.setCheckable(True)
        self.hold.clicked.connect(self._hold)
        self.screen_combo = self.control(Segmented(), lc.SCREEN)
        for n, name in lc.SCREEN_NAME.items():
            self.screen_combo.addItem(name, n)
        self.screen_combo.activated.connect(self._screen)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.hold)
        bottom.addSpacing(12)
        bottom.addWidget(label("Screen", "label"))
        bottom.addWidget(self.screen_combo)
        bottom.addStretch(1)
        card.add(bottom)
        self.flags = label("—", "hint", word_wrap=True)
        card.add(self.flags)
        self.vac_row = StatRow("Vac / Iac", "—")
        self.vac_row.value.setObjectName("lcr_vac")
        self.gain_row = StatRow("gain cal", "—", line=False)
        self.gain_row.value.setObjectName("lcr_gain")
        self.gain_warning = label("", "hint", word_wrap=True)
        self.gain_warning.setObjectName("lcr_gain_warning")
        self.ocal = device.has(lc.MONITOR)
        for w in (self.vac_row, self.gain_row, self.gain_warning):
            w.setVisible(self.ocal)
            card.add(w)
        self._monitor_at = 0.0
        self._monitor_job = None
        self.body.addWidget(card)
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
                self.grid.addWidget(label("list %d" % lid), r, 0)
                self.grid.addWidget(label("not answered: %s" % describe_error(lst, lc.STATUS_NAME),
                                          "hint", word_wrap=True), r, 1, 1, 2)
                continue
            self.lists[lid] = lst
            self.grid.addWidget(label(lst.name + (" (%s)" % lst.unit if lst.unit else "")), r, 0)
            combo = Segmented() if len(lst.entries) <= SEGMENTED_MAX else QComboBox()
            combo.setObjectName("lcr_%d" % lid)
            for v, text in lst.entries:
                # list 1 is the secondary parameter (SL 8.1): shown with the manual's names
                combo.addItem(SECONDARY_LABEL.get(text, text) if lid == 1 else text, v)
            combo.activated.connect(lambda idx, lid=lid: self.set_value(lid, idx))
            self.control(combo, lc.LCR_SET)
            self.grid.addWidget(combo, r, 1)
            note = label("", "hint")
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

    # -- level monitor and gain mark (CAP_OCAL, OCD 6.3) ---------------------------------------------
    def show_gain(self, flags: int | None) -> None:
        """MEASURE byte 65: bit 0 gain applied, bit 1 GAIN present for this frequency but not
        applied at this cell, bit 2 lock active, bit 3 retake short / open."""
        if flags is None:
            self.gain_row.value.setText("—")
            self.gain_warning.setText("")
            return
        parts = ["applied" if flags & lc.OCAL_FLAG_GAIN else "not applied"]
        if flags & lc.OCAL_FLAG_LOCK:
            parts.append("lock active")
        if flags & lc.OCAL_FLAG_RETAKE:
            parts.append("retake short / open")
        self.gain_row.value.setText(", ".join(parts))
        self.gain_warning.setText(
            "Warning: this frequency has gain factors but this range / gain cell is not "
            "calibrated; the reading is without the gain correction." if flags & lc.OCAL_FLAG_GAIN_MISSING
            else "")

    def show_monitor(self, m: lc.Monitor) -> None:
        if m.no_reading:
            text = "no reading"
        elif not m.v_calibrated:
            text = "uncalibrated: A_V,ref %.5g, A_I,ref %.5g counts" % (m.a_v_ref, m.a_i_ref)
        else:
            text = "%s / %s" % (fmt_si(m.v_rms, "V"), fmt_si(m.i_rms, "A"))
        self.vac_row.value.setText(text + (" (lock)" if m.lock else ""))

    def _follow_reading(self, reading) -> None:
        """Called with each reading the Live page shows: the gain mark from the reading, the
        monitor row from MONITOR at most once a second and one request at a time."""
        if not self.ocal or reading is None or reading.gain_flags is None:
            return
        self.show_gain(reading.gain_flags)
        now = time.monotonic()
        if self.busy or now - self._monitor_at < MONITOR_PERIOD:
            return
        if self._monitor_job is not None and not self._monitor_job.done.is_set():
            return
        self._monitor_at = now
        dev = self.dev
        self._monitor_job = self.run("MONITOR", lambda p: dev.monitor(), self.show_monitor,
                                     quiet=True)

    # -- the Live page's LCD ------------------------------------------------------------------
    def _choice_text(self, lid: int) -> str | None:
        """The text of the meter's current choice of list lid (from STATE), or None."""
        lst, st = self.lists.get(lid), self.state
        field = STATE_FIELD.get(lid)
        if lst is None or st is None or field is None:
            return None
        i = entry_index(lst, getattr(st, field))
        return lst.entries[i][1] if i >= 0 else None

    def lcd_flags(self, reading) -> tuple[set, str]:
        """(lit annunciator words, mode text) for the Live page (module docstring)."""
        self._follow_reading(reading)
        lit: set = set()
        st = self.state
        if st is not None:
            if st.flag("hold"):
                lit.add("HOLD")
            if st.circuit == 1:
                lit.add("SER")
            elif st.circuit == 2:
                lit.add("PAR")
            elif st.circuit == 0 and reading is not None and len(reading.key) > KEY_PARALLEL:
                parallel = reading.key[KEY_PARALLEL]
                if parallel == 0:
                    lit.add("SER")
                elif parallel == 1:
                    lit.add("PAR")
        if self._choice_text(5) == "AUTO":
            lit.add("AUTO")
        mode = " ".join(t for t in (self._choice_text(2), self._choice_text(3)) if t)
        return lit, mode


class ComparePage(Page):
    title = "Compare"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        top = QHBoxLayout()
        top.setSpacing(16)
        self.body.addLayout(top)
        self.labels = {}
        rec = Card("COMPARE")
        self.refresh_button = self.control(set_kind(QPushButton("Read COMPARE"), "small"),
                                           lc.COMPARE)
        self.refresh_button.clicked.connect(self.refresh)
        rec.add_right(self.refresh_button)
        items = (("quantity", "Quantity"), ("nominal", "Nominal"),
                 ("tolerance", "Tolerance %"), ("deviation", "Deviation %"),
                 ("ok", "Verdict"), ("passed", "Passed"), ("failed", "Failed"),
                 ("value", "Value"), ("warn", "Warn type / sound / LED"),
                 ("result", "Result"), ("signed", "Signed deviation"),
                 ("mode", "Mode in force / as set"), ("limits", "Limits in force"),
                 ("secondary_limit", "Secondary limit"))
        for i, (key, name) in enumerate(items):
            r = StatRow(name, line=i < len(items) - 1)
            r.value.setObjectName("cmp_" + key)
            rec.add(r)
            self.labels[key] = r.value
        rec.add(label("The meter's counters and verdict, read every second while this page is "
                      "shown.", "hint", word_wrap=True))
        top.addWidget(rec, 1)

        form = Card("COMPARE_SET")
        self.nominal = QDoubleSpinBox()
        self.nominal.setDecimals(3)
        self.nominal.setRange(0.001, 999999999.0)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setDecimals(1)
        self.tolerance.setRange(0.0, 99.9)
        self.warn_type = Segmented(["0 NG", "1 OK"])        # SL 8.2 setting 3 choices
        self.warn_sound = Segmented(["0", "1"])
        self.warn_led = Segmented(["0", "1"])
        self.set_button = self.control(QPushButton("COMPARE_SET"), lc.COMPARE_SET)
        self.set_button.clicked.connect(self.apply)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        for r, (name, w) in enumerate((("Nominal (0.001 .. < 1e9)", self.nominal),
                                       ("Tolerance % (0 .. 99.9)", self.tolerance),
                                       ("Warn type", self.warn_type),
                                       ("Warn sound", self.warn_sound),
                                       ("Warn LED", self.warn_led))):
            g.addWidget(label(name), r, 0)
            g.addWidget(w, r, 1)
        g.setColumnStretch(2, 1)
        form.add(g)
        b = QHBoxLayout()
        b.addWidget(self.set_button)
        b.addStretch(1)
        form.add(b)
        form.add(label("The meter stores the nominal as a scale and five digits and the "
                       "tolerance in tenths of a percent (SL 5.3.1); the form shows what it "
                       "stored. RAM only, not saved.", "hint", word_wrap=True))
        form.body.addStretch(1)
        top.addWidget(form, 1)
        tol = Card("TOLERANCE_SET")
        self.tol_mode = Segmented(["0 vendor", "1 %", "2 pF", "3 IEC"])
        self.tol_lower = QDoubleSpinBox()
        self.tol_upper = QDoubleSpinBox()
        for w in (self.tol_lower, self.tol_upper):
            w.setDecimals(2)                                  # 0.01 % / 0.01 pF steps
            w.setRange(-999.99, 999.99)
        self.tol_letter = QComboBox()
        self.tol_letter.addItems(list(lc.TOL_LETTERS))
        self.tol_kind = Segmented(["none", "D max", "Q min", "ESR max"])
        self.tol_value = QDoubleSpinBox()
        self.tol_value.setDecimals(4)
        self.tol_value.setRange(0.0, 999999.0)
        self.tol_button = self.control(QPushButton("TOLERANCE_SET"), lc.TOLERANCE_SET)
        self.tol_button.clicked.connect(self.apply_tolerance)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        for r, (name, w) in enumerate((("Mode", self.tol_mode),
                                       ("Lower limit (% or pF)", self.tol_lower),
                                       ("Upper limit (% or pF)", self.tol_upper),
                                       ("IEC 60062 letter", self.tol_letter),
                                       ("Secondary limit", self.tol_kind),
                                       ("Secondary value", self.tol_value))):
            g.addWidget(label(name), r, 0)
            g.addWidget(w, r, 1)
        g.setColumnStretch(2, 1)
        tol.add(g)
        b = QHBoxLayout()
        b.addWidget(self.tol_button)
        b.addStretch(1)
        tol.add(b)
        tol.add(label("Mode 0 is the vendor tolerance of COMPARE_SET; mode 2 works with the "
                      "compare quantity C only. RAM only; any later change of the meter's "
                      "tolerance returns the mode to 0.", "hint", word_wrap=True))
        tol.body.addStretch(1)
        top.addWidget(tol, 1)
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
        if c.result is None:                      # old firmware: 29-byte record, no tail
            for k in ("result", "signed", "mode", "limits", "secondary_limit"):
                L[k].setText("-")
        else:
            unit = "pF" if c.mode_in_force == 2 else "%"
            L["result"].setText(c.result_name)
            L["signed"].setText("%+g %s" % (c.signed_deviation, unit))
            L["mode"].setText("%s / %s%s" % (
                lc.COMPARE_MODE_IN_FORCE_NAME.get(c.mode_in_force, str(c.mode_in_force)),
                lc.TOLERANCE_MODE_NAME.get(c.mode_set, str(c.mode_set)),
                " " + c.letter if c.letter else ""))
            L["limits"].setText("%g .. %g %s" % (c.lower, c.upper, unit))
            L["secondary_limit"].setText("%s %g" % (
                lc.SECONDARY_LIMIT_NAME.get(c.secondary_kind, str(c.secondary_kind)),
                c.secondary_value) if c.secondary_kind else "none")
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

    def apply_tolerance(self):
        dev = self.dev
        mode = self.tol_mode.currentIndex()
        if mode < 0 or self.tol_kind.currentIndex() < 0:
            self.say("choose the mode and the secondary limit", error=True)
            return None
        args = (mode, round(self.tol_lower.value() * 100), round(self.tol_upper.value() * 100),
                self.tol_letter.currentText(), self.tol_kind.currentIndex(),
                self.tol_value.value())          # the call checks the ranges (ValueError)

        def answered(c):
            self.show_compare(c)
            self.say("tolerance set by the meter")

        return self.run("TOLERANCE_SET", lambda p: dev.tolerance_set(*args), answered)

    def on_shown(self) -> None:
        self.timer.start()
        self._poll()

    def on_hidden(self) -> None:
        self.timer.stop()

    def stop(self) -> None:
        self.timer.stop()
