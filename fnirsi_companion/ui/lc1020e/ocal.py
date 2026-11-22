"""LC1020E "Gain calibration" page: the own calibration area of the new firmware (OWN_CAL_DESIGN.md
"OCD" 3, 6.3), with four cards.

Own calibration area  OCAL_INFO of the area: the active record, its sections, the retake prompt.
Gain calibration      the reference resistors (value and uncertainty typed from the DMM), "connect, then
                      Start" per resistor (OCD 3.3 step 2, a progress bar over frequencies, levels and
                      cells), the short and the open step (step 2a), the solve with its report (OCD 3.4:
                      residuals per frequency and level, anchors, identifiability, steps, the cells and
                      checks that blocked a frequency), Write GAIN with the typed OK (the .ocal backup of
                      both slots first), and the prompt to retake short and open while OF_RETAKE is set.
Constants             CLIP from lock sweeps over every gain code on an open, a short and 1 kohm, SETTLE
                      from settle traces; proposals the user edits, written with CLIP and SETTLE through
                      OCAL_WRITE while every other section of the active record is kept.
Level monitor         per frequency the DMM's AC voltage reading (the DMM's bandwidth leaves higher
                      frequencies uncalibrated), kV = V_dmm * G[c5] / (A_V * |nu|), written into MONITOR;
                      the check line "I by the meter / I by the DMM".

Everything is sent only when the meter's capability bit 26 (CAP_OCAL) is set; the controls are
greyed out otherwise (Page.control).
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem)

from ...core import ocal_run, ocal_solve
from ...devices import lc1020e as lc
from ..base import Page, mono_text, row
from ..common.data_pages import fmt_si
from ..widgets import Card, StatRow, label, set_kind

DEFAULT_RESISTORS = ((10.0, 0.1), (100.0, 0.1), (1e3, 0.1), (1e4, 0.1), (1e5, 0.1), (1e6, 0.1))
TABLE_H = 220
SETTLE_COLUMNS = ["%d Hz" % f for f in lc.OCAL_FREQ]
CAPTURE_COLUMNS = ["Frequency", "DMM V rms", "A_V / c5 / |nu|", "kV"]
LEVEL_NOTE = ("A level monitor value is only as good as the DMM: type its AC volts reading across the "
              "open terminals at the frequency of the row.")


def _item(text: str, editable: bool = True) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def _number(item: QTableWidgetItem | None, what: str) -> float:
    text = item.text().strip().replace(",", ".") if item is not None else ""
    try:
        v = float(text)
    except ValueError:
        raise ValueError("%s: %r is not a number" % (what, text)) from None
    if not math.isfinite(v):
        raise ValueError("%s: %r is not finite" % (what, text))
    return v


class OcalPage(Page):
    title = "Gain calibration"
    status_names = dict(lc.STATUS_NAME)

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.windows: list = []                 # solver Window of every resistor
        self.shortopen: list = []               # solver ShortOpenWindow
        self.short_takes = 0
        self.result: ocal_solve.SolveResult | None = None
        self.clip_obs: dict = {}                # (channel, code) -> (minimum, maximum)
        self.captures: dict = {}                # fi -> (a_v, c5, |nu|)
        self.info: lc.OcalInfo | None = None
        self._build_record_card()
        self._build_gain_card()
        self._build_constants_card()
        self._build_monitor_card()
        self.body.addStretch(1)
        if device.has(lc.OCAL_INFO):
            self.refresh_info()

    # ------------------------------------------------------------------------------------------
    # record card
    # ------------------------------------------------------------------------------------------
    def _build_record_card(self) -> None:
        card = Card("Own calibration area")
        self.info_button = self.control(set_kind(QPushButton("Read OCAL_INFO"), "small"),
                                        lc.OCAL_INFO)
        self.info_button.clicked.connect(self.refresh_info)
        card.add_right(self.info_button)
        r1, r2, r3 = StatRow("Active record"), StatRow("Sections"), StatRow("Retake", line=False)
        self.info_label, self.sections_label, self.retake_label = r1.value, r2.value, r3.value
        self.info_label.setObjectName("ocal_info")
        self.sections_label.setObjectName("ocal_sections")
        self.retake_label.setObjectName("ocal_retake")
        for r in (r1, r2, r3):
            card.add(r)
        card.add(label("Gain factors per range, gain code and frequency, constants read from the "
                       "meter and the level monitor live in a flash area of their own (two record "
                       "slots); with the area empty the meter reads as without them. Every write "
                       "backs up both slots into a .ocal file first.", "hint", word_wrap=True))
        self.body.addWidget(card)

    def refresh_info(self):
        dev = self.dev
        return self.run("OCAL_INFO", lambda p: dev.ocal_info(), self.show_info, quiet=True)

    def show_info(self, info: lc.OcalInfo) -> None:
        self.info = info
        if info.active:
            self.info_label.setText("slot %d, sequence %d, %d bytes, CRC-32 0x%08X; %s" % (
                info.active_slot, info.active_sequence, info.active_length, info.active_crc,
                info.meta_note or "no note"))
        else:
            names = lc.flag_names(info.flags, lc.OCAL_INFO_FLAGS)
            self.info_label.setText("none" + (" (%s)" % ", ".join(names) if names else ""))
        text = ", ".join(lc.SECTION_NAME[i] for i in lc.SECTION_NAME if info.has_section(i)) or "none"
        if info.gain_frequencies:
            text += "; GAIN at %s Hz" % ", ".join(str(h) for h in info.gain_hz())
        if info.monitor_frequencies:
            text += "; MONITOR at %s Hz" % ", ".join(str(h) for h in info.monitor_hz())
        if info.sections_rejected:
            text += "; REJECTED: %s" % ", ".join(info.rejected_names())
        self.sections_label.setText(text)
        if info.flag(lc.OF_RETAKE):
            self.retake_label.setText("The short and open were taken before this GAIN: run the open "
                                      "and short calibration again (Calibration page).")
        else:
            self.retake_label.setText("not needed")

    def on_event(self, ev) -> None:
        if isinstance(ev, lc.Event) and ev.cmd == lc.EV_OCAL_CHANGED and self.dev.has(lc.OCAL_INFO):
            self.refresh_info()

    # ------------------------------------------------------------------------------------------
    # gain card: resistors, runs, solve, write
    # ------------------------------------------------------------------------------------------
    def _build_gain_card(self) -> None:
        card = Card("Gain calibration")
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("resistors")
        self.table.setHorizontalHeaderLabels(["Name", "Reference (ohm)", "Uncertainty (%)", "Windows"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(TABLE_H)
        for value, unc in DEFAULT_RESISTORS:
            self.add_resistor(None, value, unc)
        self.add_button = set_kind(QPushButton("Add resistor"), "small")
        self.add_button.clicked.connect(lambda: self.add_resistor())
        self.remove_button = set_kind(QPushButton("Remove selected"), "small")
        self.remove_button.clicked.connect(self.remove_resistor)
        codes = (lc.OCAL_LOCK, lc.OCAL_RAW, lc.LCR_SET, lc.LCR_TABLES, lc.MEASURE)
        self.start_button = self.control(set_kind(QPushButton("Connect it, then Start"), "primary"),
                                         *codes)
        self.start_button.setObjectName("ocal_start")
        self.start_button.clicked.connect(lambda: self.start_run())
        self.short_button = self.control(QPushButton("Short bar: take"), *codes)
        self.short_button.clicked.connect(lambda: self.run_shortopen("short"))
        self.open_button = self.control(QPushButton("Open terminals: take"), *codes)
        self.open_button.clicked.connect(lambda: self.run_shortopen("open"))
        self.clear_button = set_kind(QPushButton("Clear the data"), "small")
        self.clear_button.clicked.connect(self.clear_data)
        self.solve_button = set_kind(QPushButton("Solve"), "primary")
        self.solve_button.clicked.connect(self.solve)
        self.write_button = self.control(set_kind(QPushButton("Write GAIN…"), "danger"),
                                         lc.OCAL_WRITE, lc.OCAL_READ, lc.OCAL_INFO)
        self.write_button.clicked.connect(self.write_gain)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setObjectName("ocal_progress")
        self.progress_text = label("", "hint", word_wrap=True)
        self.report = mono_text()
        self.report.setObjectName("ocal_report")
        self.report.setMinimumHeight(180)
        card.add(self.table)
        card.add(row(self.add_button, self.remove_button))
        card.add(row(self.start_button, self.short_button, self.open_button, self.clear_button))
        card.add(self.progress)
        card.add(self.progress_text)
        card.add(row(self.solve_button, self.write_button))
        card.add(self.report)
        card.add(label("Type each resistor's DC reference (four-wire where you have it) and its "
                       "uncertainty. Put the part in the terminals and press Start: per frequency "
                       "and level the meter's autoranged cell and its neighbours are read through "
                       "the lock (OCAL_LOCK, OCAL_RAW). Take the short bar twice (re-insert it) and "
                       "the open once. Solve fits the factors; a frequency is written only when "
                       "every cell the meter chose has all four bits and the checks pass.",
                       "hint", word_wrap=True))
        self.body.addWidget(card)

    # -- the resistor list --------------------------------------------------------------------
    def add_resistor(self, name: str | None = None, value: float = 1000.0, unc: float = 0.1):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, _item(name or "%g ohm" % value))
        self.table.setItem(r, 1, _item("%g" % value))
        self.table.setItem(r, 2, _item("%g" % unc))
        self.table.setItem(r, 3, _item("0", editable=False))

    def remove_resistor(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        self.table.removeRow(r)
        self.windows = [w for w in self.windows if w.resistor != r]
        for w in self.windows:
            if w.resistor > r:
                w.resistor -= 1
        self.result = None
        self._count_windows()
        self.refresh_controls()

    def resistors(self) -> list:
        """The table as solver Resistor objects; ValueError for a cell that is not a positive
        number."""
        out = []
        for r in range(self.table.rowCount()):
            name = self.table.item(r, 0).text().strip() or "R%d" % (r + 1)
            value = _number(self.table.item(r, 1), "%s reference" % name)
            unc = _number(self.table.item(r, 2), "%s uncertainty" % name)
            if value <= 0 or unc <= 0:
                raise ValueError("%s: reference and uncertainty must be above 0" % name)
            out.append(ocal_solve.Resistor(name, value, unc / 100.0))
        return out

    def _count_windows(self) -> None:
        for r in range(self.table.rowCount()):
            n = sum(1 for w in self.windows if w.resistor == r)
            self.table.item(r, 3).setText(str(n))

    def clear_data(self) -> None:
        self.windows.clear()
        self.shortopen.clear()
        self.short_takes = 0
        self.result = None
        self.report.setPlainText("")
        self._count_windows()
        self.refresh_controls()
        self.say("data cleared")

    def _progress(self, frac: float, text: str) -> None:
        self.progress.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.progress_text.setText(text)

    # -- runs ---------------------------------------------------------------------------------
    def start_run(self, row_index: int | None = None):
        r = self.table.currentRow() if row_index is None else row_index
        if r < 0 or r >= self.table.rowCount():
            self.say("select a resistor in the list first", error=True)
            return None
        try:
            self.resistors()
        except ValueError as e:
            self.say(str(e), error=True)
            return None
        dev = self.dev

        def job(progress):
            with dev.ocal_session(), dev._cancellable():
                return ocal_run.collect_resistor(dev, r, progress)

        def done(res):
            windows, notes = res
            self.windows = [w for w in self.windows if w.resistor != r] + windows
            self._count_windows()
            self.result = None
            self.refresh_controls()
            self.say("%d windows of resistor %d%s" % (len(windows), r + 1,
                                                       "; " + "; ".join(notes) if notes else ""))

        return self.run("gain calibration run", job, done, exclusive=True, progress=self._progress)

    def run_shortopen(self, kind: str):
        dev = self.dev
        take = self.short_takes + 1 if kind == "short" else 1
        cells = self._calibrated_cells()

        def job(progress):
            with dev.ocal_session(), dev._cancellable():
                return ocal_run.collect_shortopen(dev, kind, take, cells, progress)

        def done(res):
            windows, notes = res
            if kind == "short":
                self.short_takes = take
            else:
                self.shortopen = [w for w in self.shortopen if w.kind != "open"]
            self.shortopen += windows
            self.result = None
            self.refresh_controls()
            self.say("%s: %d windows%s" % (kind, len(windows), "; " + "; ".join(notes) if notes else ""))

        return self.run("%s step" % kind, job, done, exclusive=True, progress=self._progress)

    def _calibrated_cells(self) -> set:
        """Cells the last solve identified (all four bits at some frequency), the cells a short or
        open is read at when the autoranged one is not among them (OCD 3.3 step 2a)."""
        cells: set = set()
        if self.result is None:
            return cells
        for fr in self.result.freqs.values():
            g = fr.fitted
            cells |= {(c5, c6, c8) for c8 in range(4) for c6 in range(8) for c5 in range(8)
                      if g.cell_calibrated(c5, c6, c8)}
        return cells

    # -- solve and write ----------------------------------------------------------------------
    def solve(self):
        try:
            resistors = self.resistors()
        except ValueError as e:
            self.say(str(e), error=True)
            return None
        if not self.windows:
            self.say("no windows: run a resistor first", error=True)
            return None
        windows, shortopen = list(self.windows), list(self.shortopen)

        def done(res):
            self.result = res
            self.report.setPlainText(res.report())
            self.refresh_controls()
            ok = res.written_frequencies()
            self.say("solved: GAIN for %s Hz%s" % (", ".join(str(f) for f in ok) or "no frequency",
                                                   "" if ok else " (every frequency blocked, see the report)"))

        return self.run("solve", lambda p: ocal_solve.solve(windows, resistors, shortopen), done)

    def extra_enabled(self, widget) -> bool:
        if widget is getattr(self, "write_button", None):
            return getattr(self, "result", None) is not None
        return True

    def write_gain(self):
        if self.result is None:
            return None
        dev, section = self.dev, self.result.section()
        freqs = ", ".join(str(f) for f in self.result.written_frequencies()) or "no frequency"
        word = self.confirm("Write the gain factors",
                            "OCAL_WRITE writes a new record into the meter's own calibration "
                            "area with the GAIN section for %s Hz (other frequencies get masks 0; "
                            "every other section of the active record is kept). Both area slots "
                            "are read into a .ocal backup file first. The previous record stays as "
                            "the rollback copy." % freqs)
        if word is None:
            return None
        note = "gain cal %s" % self._today()

        def done(res):
            self.say("GAIN written: %s" % res.describe())
            self.refresh_info()

        return self.run("OCAL_WRITE GAIN", lambda p: dev.ocal_update_sections(
            {lc.SEC_GAIN: section}, word, note), done, exclusive=True)

    def _today(self) -> str:
        return self.dev._now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------------------------------
    # constants card: CLIP and SETTLE
    # ------------------------------------------------------------------------------------------
    def _build_constants_card(self) -> None:
        card = Card("Constants")
        self.clip_table = QTableWidget(16, 3)
        self.clip_table.setObjectName("clip_table")
        self.clip_table.setHorizontalHeaderLabels(["Channel / code", "lo", "hi"])
        self.clip_table.setMaximumHeight(TABLE_H)
        for i in range(16):
            ch, code = divmod(i, 8)
            self.clip_table.setItem(i, 0, _item("%s code %d" % (("V (ADC1)", "I (ADC2)")[ch], code),
                                                editable=False))
            self.clip_table.setItem(i, 1, _item("0"))
            self.clip_table.setItem(i, 2, _item("4095"))
        self.part_combo = QComboBox()
        self.part_combo.addItems(["open terminals", "short bar", "1 kohm"])
        self.sweep_button = self.control(QPushButton("Sweep every gain code"), lc.OCAL_LOCK,
                                         lc.OCAL_RAW, lc.MEASURE)
        self.sweep_button.clicked.connect(lambda: self.sweep_clip())
        self.propose_button = set_kind(QPushButton("Propose lo / hi"), "small")
        self.propose_button.clicked.connect(self.propose_clip)
        self.margin = QDoubleSpinBox()
        self.margin.setDecimals(0)
        self.margin.setRange(0, 500)
        self.margin.setValue(16)
        self.settle_table = QTableWidget(7, 6)
        self.settle_table.setObjectName("settle_table")
        self.settle_table.setHorizontalHeaderLabels(SETTLE_COLUMNS)
        self.settle_table.setVerticalHeaderLabels(["%d %s" % (k, n) for k, n in
                                                   enumerate(lc.SETTLE_KIND_NAME)])
        self.settle_table.setMaximumHeight(TABLE_H)
        for k in range(7):
            for f in range(6):
                self.settle_table.setItem(k, f, _item(str(lc.DEFAULT_SETTLE_MS[k])))
        self.kind_combo = QComboBox()
        for k in (1, 2, 3, 4, 6):
            self.kind_combo.addItem("%d %s" % (k, lc.SETTLE_KIND_NAME[k]), k)
        self.trace_freq = QComboBox()
        for fi, hz in enumerate(lc.OCAL_FREQ):
            self.trace_freq.addItem("%d Hz" % hz, fi)
        self.trace_button = self.control(QPushButton("Measure a trace"), lc.OCAL_TRACE, lc.OCAL_RAW,
                                         lc.OCAL_LOCK, lc.LCR_SET, lc.LCR_TABLES)
        self.trace_button.clicked.connect(lambda: self.measure_trace())
        self.read_const_button = self.control(set_kind(QPushButton("Read from the meter"), "small"),
                                              lc.OCAL_READ, lc.OCAL_INFO)
        self.read_const_button.clicked.connect(self.read_constants)
        self.write_const_button = self.control(set_kind(QPushButton("Write CLIP and SETTLE…"),
                                                        "danger"), lc.OCAL_WRITE, lc.OCAL_READ,
                                               lc.OCAL_INFO)
        self.write_const_button.clicked.connect(self.write_constants)
        card.add_right(self.read_const_button)
        card.add(label("CLIP: the ADC counts from which a channel counts as saturated, per gain "
                       "code (defaults 0 / 4095). Put an open, then a short, then 1 kohm in the "
                       "terminals and sweep each; Propose takes the observed minimum and maximum "
                       "over the parts with the margin you see.", "hint", word_wrap=True))
        card.add(self.clip_table)
        card.add(row("Part", self.part_combo, self.sweep_button, "Margin", self.margin,
                     self.propose_button))
        card.add(label("SETTLE: milliseconds after a change of each kind (rows) at each frequency "
                       "(columns); defaults 26 ms, screen and codes 5 ms. A trace sets the cell "
                       "for its kind and frequency.", "hint", word_wrap=True))
        card.add(self.settle_table)
        card.add(row("Kind", self.kind_combo, "Frequency", self.trace_freq, self.trace_button))
        card.add(row(self.write_const_button))
        self.body.addWidget(card)

    def sweep_clip(self):
        dev = self.dev
        part = self.part_combo.currentText()

        def job(progress):
            with dev.ocal_session(), dev._cancellable():
                return ocal_run.sweep_clip(dev, progress)

        def done(obs):
            ocal_run.merge_clip(self.clip_obs, obs)
            self.say("%s swept: %d code observations in all" % (part, len(self.clip_obs)))

        return self.run("CLIP sweep (%s)" % part, job, done, exclusive=True, progress=self._progress)

    def propose_clip(self) -> list:
        prop = ocal_solve.propose_clip(self.clip_obs, int(self.margin.value()))
        for i, (lo, hi) in enumerate(prop):
            self.clip_table.item(i, 1).setText(str(lo))
            self.clip_table.item(i, 2).setText(str(hi))
        self.say("proposed from %d observations (margin %d counts)" % (len(self.clip_obs),
                                                                       self.margin.value()))
        return prop

    def clip_values(self) -> list:
        return [(int(_number(self.clip_table.item(i, 1), "CLIP lo %d" % i)),
                 int(_number(self.clip_table.item(i, 2), "CLIP hi %d" % i))) for i in range(16)]

    def settle_values(self) -> list:
        return [[int(_number(self.settle_table.item(k, f), "SETTLE %d/%d" % (k, f)))
                 for f in range(6)] for k in range(7)]

    def measure_trace(self):
        dev = self.dev
        kind, fi = self.kind_combo.currentData(), self.trace_freq.currentData()

        def job(progress):
            with dev.ocal_session(), dev._cancellable():
                return ocal_run.measure_trace(dev, kind, fi, progress)

        def done(res):
            trace, ms = res
            if ms is None:
                self.say("trace of %d entries gives no settle time (no transient)" % len(trace.entries))
                return
            self.settle_table.item(kind, fi).setText(str(ms))
            self.say("kind %d at %d Hz: %d ms from a trace of %d entries" % (
                kind, lc.OCAL_FREQ[fi], ms, len(trace.entries)))

        return self.run("settle trace", job, done, exclusive=True, progress=self._progress)

    def read_constants(self):
        dev = self.dev

        def done(res):
            _info, sections = res
            if lc.SEC_CLIP in sections:
                for i, (lo, hi) in enumerate(lc.unpack_clip(sections[lc.SEC_CLIP])):
                    self.clip_table.item(i, 1).setText(str(lo))
                    self.clip_table.item(i, 2).setText(str(hi))
            if lc.SEC_SETTLE in sections:
                for k, vals in enumerate(lc.unpack_settle(sections[lc.SEC_SETTLE])):
                    for f, v in enumerate(vals):
                        self.settle_table.item(k, f).setText(str(v))
            self.say("constants read: %s" % (", ".join(lc.SECTION_NAME[s] for s in sections
                                                       if s in lc.SECTION_NAME) or "no active record"))

        return self.run("read constants", lambda p: dev.ocal_active_sections(), done)

    def write_constants(self):
        dev = self.dev
        try:
            clip, settle = lc.pack_clip(self.clip_values()), lc.pack_settle(self.settle_values())
            lc.validate_section(lc.SEC_CLIP, clip)
            lc.validate_section(lc.SEC_SETTLE, settle)
        except (ValueError, lc.OcalValidationError) as e:
            self.say("the meter would refuse these values: %s" % e, error=True)
            return None
        word = self.confirm("Write CLIP and SETTLE",
                            "OCAL_WRITE writes a new record with these CLIP and SETTLE sections; "
                            "every other section of the active record is kept. Both area slots "
                            "are read into a .ocal backup file first.")
        if word is None:
            return None
        note = "constants %s" % self._today()
        return self.run("OCAL_WRITE CLIP/SETTLE", lambda p: dev.ocal_update_sections(
            {lc.SEC_CLIP: clip, lc.SEC_SETTLE: settle}, word, note),
            lambda r: (self.say("written: %s" % r.describe()), self.refresh_info()),
            exclusive=True)

    # ------------------------------------------------------------------------------------------
    # level monitor card
    # ------------------------------------------------------------------------------------------
    def _build_monitor_card(self) -> None:
        card = Card("Level monitor")
        self.mon_table = QTableWidget(6, 4)
        self.mon_table.setObjectName("monitor_table")
        self.mon_table.setHorizontalHeaderLabels(CAPTURE_COLUMNS)
        self.mon_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mon_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.mon_table.setMaximumHeight(TABLE_H)
        for fi, hz in enumerate(lc.OCAL_FREQ):
            self.mon_table.setItem(fi, 0, _item("%d Hz" % hz, editable=False))
            self.mon_table.setItem(fi, 1, _item(""))
            self.mon_table.setItem(fi, 2, _item("", editable=False))
            self.mon_table.setItem(fi, 3, _item("", editable=False))
        self.bandwidth = QDoubleSpinBox()
        self.bandwidth.setDecimals(1)
        self.bandwidth.setRange(0.1, 1000.0)
        self.bandwidth.setValue(100.0)
        self.bandwidth.setSuffix(" kHz")
        self.capture_button = self.control(QPushButton("Capture at the selected frequency"),
                                           lc.OCAL_RAW, lc.OCAL_LOCK, lc.LCR_SET, lc.LCR_TABLES,
                                           lc.MEASURE, lc.OCAL_READ, lc.OCAL_INFO)
        self.capture_button.clicked.connect(lambda: self.capture())
        self.kv_button = set_kind(QPushButton("Compute kV"), "small")
        self.kv_button.clicked.connect(self.compute_kv)
        self.write_kv_button = self.control(set_kind(QPushButton("Write MONITOR…"), "danger"),
                                            lc.OCAL_WRITE, lc.OCAL_READ, lc.OCAL_INFO)
        self.write_kv_button.clicked.connect(self.write_monitor)
        self.dmm_ma = QDoubleSpinBox()
        self.dmm_ma.setDecimals(4)
        self.dmm_ma.setRange(0.0, 1000.0)
        self.dmm_ma.setSuffix(" mA")
        self.current_button = self.control(QPushButton("Compare the current"), lc.MONITOR)
        self.current_button.clicked.connect(lambda: self.compare_current())
        self.current_label = label("", "mono", word_wrap=True)
        self.current_label.setObjectName("monitor_current")
        card.add(label(LEVEL_NOTE, "hint", word_wrap=True))
        card.add(self.mon_table)
        card.add(row("DMM AC bandwidth", self.bandwidth, self.capture_button, self.kv_button,
                     self.write_kv_button))
        card.add(row("DMM current in series", self.dmm_ma, self.current_button))
        card.add(self.current_label)
        card.add(label("kV = V_dmm x G[c5] / (A_V x |nu|), volts rms per count of peak amplitude "
                       "at V gain 1. The current needs no stored value: I = kV x A_I,ref follows "
                       "from kV and the gain factors, so the DMM's current is only a check.",
                       "hint", word_wrap=True))
        self.body.addWidget(card)

    def capture(self, fi: int | None = None):
        fi = self.mon_table.currentRow() if fi is None else fi
        if not 0 <= fi < 6:
            self.say("select a frequency row first", error=True)
            return None
        dev = self.dev

        def job(_p):
            with dev.ocal_session():
                by_fi, levels = ocal_run.meter_points(dev)
                if fi not in by_fi:
                    raise lc.DeviceError("the meter's frequency list has no %d Hz" % lc.OCAL_FREQ[fi])
                raw = ocal_run.settle_point(dev, by_fi[fi], levels[-1][1])
                _info, sections = dev.ocal_active_sections()
            nu = 1.0
            if lc.SEC_GAIN in sections:
                g = lc.unpack_gain(sections[lc.SEC_GAIN])[fi]
                if g.cell_calibrated(*raw.cell):
                    nu = abs(g.nu[raw.c5])
            return raw, nu

        def done(res):
            raw, nu = res
            self.captures[fi] = (raw.a_v, raw.c5, nu)
            self.mon_table.item(fi, 2).setText("%.5g / %d / %.5g" % (raw.a_v, raw.c5, nu))
            self.say("%d Hz captured: A_V %.5g counts, c5 %d" % (lc.OCAL_FREQ[fi], raw.a_v, raw.c5))

        return self.run("monitor capture", job, done, exclusive=True)

    def compute_kv(self) -> dict:
        """kV of every row that has a DMM reading and a capture and lies inside the DMM's
        bandwidth; the others stay empty. Returns {fi: kV}."""
        out = {}
        for fi in range(6):
            self.mon_table.item(fi, 3).setText("")
            text = self.mon_table.item(fi, 1).text().strip()
            if not text or fi not in self.captures:
                continue
            if lc.OCAL_FREQ[fi] > self.bandwidth.value() * 1000.0:
                self.mon_table.item(fi, 3).setText("above the DMM's bandwidth")
                continue
            try:
                v = _number(self.mon_table.item(fi, 1), "DMM reading at %d Hz" % lc.OCAL_FREQ[fi])
                a_v, c5, nu = self.captures[fi]
                kv = lc.monitor_kv(v, a_v, c5, nu)
            except ValueError as e:
                self.say(str(e), error=True)
                continue
            out[fi] = kv
            self.mon_table.item(fi, 3).setText("%.6g" % kv)
        self.say("kV computed for %d frequencies" % len(out))
        return out

    def write_monitor(self):
        kvs = self.compute_kv()
        if not kvs:
            self.say("no kV to write: capture a frequency and type the DMM reading", error=True)
            return None
        dev = self.dev
        word = self.confirm("Write the level monitor",
                            "OCAL_WRITE writes a new record with the MONITOR section for %s Hz "
                            "(frequencies already calibrated in the active record are kept; every "
                            "other section is kept). Both area slots are read into a .ocal backup "
                            "file first." % ", ".join(str(lc.OCAL_FREQ[f]) for f in sorted(kvs)))
        if word is None:
            return None
        note = "monitor %s" % self._today()

        def job(_p):
            _info, sections = dev.ocal_active_sections()
            mask, kv = (lc.unpack_monitor(sections[lc.SEC_MONITOR]) if lc.SEC_MONITOR in sections
                        else (0, [0.0] * 6))
            for fi, v in kvs.items():
                mask |= 1 << fi
                kv[fi] = v
            return dev.ocal_update_sections({lc.SEC_MONITOR: lc.pack_monitor(mask, kv)}, word, note)

        return self.run("OCAL_WRITE MONITOR", job,
                        lambda r: (self.say("MONITOR written: %s" % r.describe()), self.refresh_info()),
                        exclusive=True)

    def compare_current(self):
        dev = self.dev
        dmm = self.dmm_ma.value()

        def done(m: lc.Monitor):
            if not math.isfinite(m.i_rms) or not m.v_calibrated:
                self.current_label.setText("the meter shows no calibrated current (%s)" % (
                    "uncalibrated" if not m.v_calibrated else "no reading"))
                return
            text = "I by the meter %s / I by the DMM %s" % (fmt_si(m.i_rms, "A"),
                                                              fmt_si(dmm / 1000.0, "A"))
            if dmm > 0:
                text += " = %.4f" % (m.i_rms / (dmm / 1000.0))
            self.current_label.setText(text)

        return self.run("MONITOR", lambda p: dev.monitor(), done)
