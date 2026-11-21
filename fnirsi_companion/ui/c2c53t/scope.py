"""2C53T Oscilloscope page (S2 4.4; DESIGN 13.22).

SCOPE_WAVE is a request: while the page is shown and no exclusive job holds the port, the page asks
for the trace of each enabled channel, at most one request outstanding, and shows the age of the
newest trace (a long key command of the meter delays the answers). The plot shows the 301 display
samples as the meter has them, 0 = the centre line (sample 128), in display rows.

Timebase and V/div selectors carry the meter's own texts from SCOPE_TABLES (the V/div text of a
channel is entry probe x 3 + step). Channel, trigger, run / stop, autoset and auto-correction send
their commands; every control shows the STATE record the meter answers or streams.
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QPushButton,
                               QSpinBox, QWidget)

from ...devices import c2c53t as c2
from .. import style
from ..base import Page, row
from ..widgets import Card, FlowLayout, Segmented, StatRow, label, set_kind

POLL_MS = 100
# STATE pages the trace is asked for on: 2 scope and 9 scope menu (S2 4.6). S2 4.4 requires the
# scope page; whether the scope menu counts is not stated, a status 3 there pauses the polling.
SCOPE_PAGES = (2, 9)
CHANNEL_TOKENS = ("trace", "cursor-a")       # CH1, CH2 colours (DESIGN_UI 6.9)


class ScopePage(Page):
    title = "Oscilloscope"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.tables: c2.ScopeTables | None = None
        self.state: c2.State | None = None
        self.waves: dict[int, c2.ScopeWave] = {}
        self.wave_time: float | None = None
        self.pending = None
        self.polls = 0
        self.pause_until = 0.0

        # the trace card: commands, plot, age of the newest trace
        tc = Card()
        self.page_button = self.control(QPushButton("Open the scope page (PAGE 2)"), c2.PAGE)
        self.page_button.clicked.connect(
            lambda: self.run("PAGE 2", lambda p: self.dev.page(2), self.ctx.publish_state))
        self.run_button = self.control(set_kind(QPushButton("Run"), "primary"), c2.SCOPE_RUN)
        self.run_button.clicked.connect(lambda: self._send("SCOPE_RUN", lambda: self.dev.scope_run(True)))
        self.stop_button = self.control(QPushButton("Stop"), c2.SCOPE_RUN)
        self.stop_button.clicked.connect(lambda: self._send("SCOPE_RUN", lambda: self.dev.scope_run(False)))
        self.autoset = self.control(QPushButton("Autoset"), c2.SCOPE_AUTOSET)
        self.autoset.clicked.connect(lambda: self._send("SCOPE_AUTOSET", self.dev.scope_autoset))
        self.autocorrect = self.control(QPushButton("Auto-correction"), c2.SCOPE_AUTOCORRECT)
        self.autocorrect.clicked.connect(lambda: self._send("SCOPE_AUTOCORRECT",
                                                            self.dev.scope_autocorrect))
        self.running = label("—", "mono")
        bar_w = QWidget()
        bar = FlowLayout(bar_w, spacing=8)
        for b in (self.run_button, self.stop_button, self.autoset, self.autocorrect,
                  self.page_button):
            bar.addWidget(b)
        tc.add(bar_w)
        tc.add(self.running)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(300)
        style.theme_plot(self.plot)
        self.plot.setLabel("bottom", "sample")
        self.plot.setLabel("left", "display rows from the centre line")
        self.plot.setXRange(0, c2.WAVE_SAMPLES - 1)
        self.plot.setYRange(-128, 127)
        self.curves = [self.plot.plot([], []) for _ in range(2)]
        tc.add(self.plot, 1)
        self.age = label("no trace yet", "mono", word_wrap=True)
        self.age.setObjectName("trace_age")
        tc.add(self.age)
        self.body.addWidget(tc, 1)
        self._pens()
        style.on_theme(self._pens)

        # timebase and channels
        self.timebase = self.control(QComboBox(), c2.SCOPE_TIMEBASE)
        self.timebase.setObjectName("timebase")
        self.timebase.setMinimumWidth(140)
        self.timebase.activated.connect(self._timebase)
        chc = Card("Timebase and channels")
        chc.add(row("Timebase", self.timebase))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for col, text in enumerate(("", "V/div", "Offset (-100..100)", "Coupling", "Probe", "",
                                    "")):
            if text:
                grid.addWidget(label(text, "caption"), 0, col)
        self.ch = []
        for n in range(2):
            w = {
                "enabled": QCheckBox("CH%d on" % (n + 1)),
                "vdiv": QComboBox(),
                "offset": QSpinBox(),
                "coupling": Segmented(["AC", "DC"]),
                "probe": Segmented(["x1", "x10"]),
                "fft": QCheckBox("FFT"),
                "apply": self.control(QPushButton("Apply CH%d" % (n + 1)), c2.SCOPE_CHANNEL),
            }
            w["vdiv"].setObjectName("vdiv_%d" % n)
            w["vdiv"].setMinimumWidth(110)
            w["offset"].setRange(-100, 100)
            w["probe"].currentIndexChanged.connect(lambda _i, n=n: self._fill_vdiv(n))
            w["apply"].clicked.connect(lambda _=False, n=n: self.apply_channel(n))
            r = n + 1
            for col, key in enumerate(("enabled", "vdiv", "offset", "coupling", "probe", "fft",
                                       "apply")):
                grid.addWidget(w[key], r, col)
            self.ch.append(w)
        grid.setColumnStretch(7, 1)
        chc.add(grid)

        # trigger
        self.t_source = Segmented(["CH1", "CH2"])
        self.t_edge = Segmented(["edge 0", "edge 1"])
        self.t_level = QSpinBox()
        self.t_level.setRange(-100, 100)
        self.t_mode = Segmented()
        for k, name in c2.TRIGGER_MODE.items():
            self.t_mode.addItem(name, k)
        self.t_apply = self.control(QPushButton("Apply trigger"), c2.SCOPE_TRIGGER)
        self.t_apply.clicked.connect(self.apply_trigger)
        trc = Card("Trigger")
        tg = QGridLayout()
        tg.setHorizontalSpacing(12)
        tg.setVerticalSpacing(10)
        tg.setColumnStretch(2, 1)
        for r, (name, w) in enumerate((("Source", self.t_source), ("Edge", self.t_edge),
                                       ("Level (-100..100)", self.t_level),
                                       ("Mode", self.t_mode))):
            tg.addWidget(label(name), r, 0)
            tg.addWidget(w, r, 1)
        trc.add(tg)
        trc.add(row(self.t_apply))
        trc.add(label("A source change needs both channels on (S2 4.4); a single shot is mode "
                      "single + Run.", "hint", word_wrap=True))
        lower = QHBoxLayout()
        lower.setSpacing(16)
        lower.addWidget(chc, 3)
        lower.addWidget(trc, 2)
        self.body.addLayout(lower)
        mv = StatRow("Meter", line=False)
        self.meter_values = mv.value
        self.meter_values.setObjectName("scope_meter_values")
        chc.add(mv)
        for n in range(2):
            w = self.ch[n]
            self.edits("ch%d" % n, w["enabled"], w["vdiv"], w["offset"], w["coupling"],
                       w["probe"], w["fft"])
        self.edits("trig", self.t_source, self.t_edge, self.t_level, self.t_mode)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        if device.has(c2.SCOPE_TABLES):
            self.run("SCOPE_TABLES", lambda p: device.scope_tables(), self._tables)

    def _pens(self) -> None:
        """CH1 in the trace colour, CH2 in the cursor A colour of the theme."""
        for curve, tok in zip(self.curves, CHANNEL_TOKENS):
            curve.setPen(pg.mkPen(style.color(tok), width=1.5))

    # -- tables -------------------------------------------------------------------------------
    def _tables(self, t: c2.ScopeTables) -> None:
        self.tables = t
        self.timebase.clear()
        for i, text in enumerate(t.timebase):
            self.timebase.addItem(text, i)
        for n in range(2):
            self._fill_vdiv(n)
        if self.state is not None:
            self.on_state(self.state)
        self.say("%d timebases, %d V/div texts" % (len(t.timebase), len(t.vdiv)))

    def _fill_vdiv(self, n: int) -> None:
        combo = self.ch[n]["vdiv"]
        keep = combo.currentIndex()
        combo.blockSignals(True)
        combo.clear()
        if self.tables is not None:
            probe = self.ch[n]["probe"].currentIndex()
            for step in range(10):
                i = probe * 3 + step
                if i < len(self.tables.vdiv):
                    combo.addItem(self.tables.vdiv[i], step)
        combo.setCurrentIndex(keep if keep < combo.count() else -1)
        combo.blockSignals(False)

    # -- commands -----------------------------------------------------------------------------
    def _send(self, what, call, form: str | None = None):
        def answered(st):
            if form:
                self.clean(form)
            self.ctx.publish_state(st)

        def failed(_e):
            if form:
                self.clean(form)
            if self.state is not None:
                self.on_state(self.state)

        return self.run(what, lambda p: call(), answered, failed=failed)

    def _timebase(self, idx: int):
        i = self.timebase.itemData(idx)
        return self._send("SCOPE_TIMEBASE", lambda: self.dev.scope_timebase(i))

    def apply_channel(self, n: int):
        w = self.ch[n]
        step = w["vdiv"].currentData()
        if step is None:
            self.say("CH%d: no V/div selected (SCOPE_TABLES not read)" % (n + 1), error=True)
            return None
        args = (n, w["enabled"].isChecked(), step, w["offset"].value(),
                w["coupling"].currentIndex(), w["probe"].currentIndex(), w["fft"].isChecked())
        if min(args[4:6]) < 0:
            self.say("CH%d: choose coupling and probe" % (n + 1), error=True)
            return None
        return self._send("SCOPE_CHANNEL %d" % (n + 1), lambda: self.dev.scope_channel(*args),
                          "ch%d" % n)

    def apply_trigger(self):
        args = (self.t_source.currentIndex(), self.t_edge.currentIndex(), self.t_level.value(),
                self.t_mode.currentData())
        if args[0] < 0 or args[1] < 0 or args[3] is None:
            self.say("trigger: choose source, edge and mode", error=True)
            return None
        return self._send("SCOPE_TRIGGER", lambda: self.dev.scope_trigger(*args), "trig")

    # -- the trace ----------------------------------------------------------------------------
    def channels(self) -> list[int]:
        if self.state is None:
            return [0]
        return [n for n, c in enumerate((self.state.ch1, self.state.ch2)) if c.enabled]

    def poll(self) -> None:
        self._show_age()
        if self.busy or not self.dev.has(c2.SCOPE_WAVE) or self.ctx.jobs.busy:
            return
        if self.pending is not None and not self.pending.done.is_set():
            return                                  # one request outstanding
        if time.monotonic() < self.pause_until:
            return
        if self.state is not None and self.state.page not in SCOPE_PAGES:
            self.age.setText("the meter shows the %s page: SCOPE_WAVE needs the scope page "
                             "(S2 4.4)" % self.state.page_name)
            return
        chans = self.channels()
        if not chans:
            return
        dev = self.dev
        self.polls += 1
        self.pending = self.run("SCOPE_WAVE", lambda p: [dev.scope_wave(c) for c in chans],
                                self._waves, failed=self._wave_failed, quiet=True)

    def _wave_failed(self, _exc) -> None:
        self.pause_until = time.monotonic() + 2.0       # no request storm while it fails

    def _waves(self, waves) -> None:
        self.wave_time = time.monotonic()
        shown = set()
        for w in waves:
            self.waves[w.channel] = w
            shown.add(w.channel)
            y = np.frombuffer(w.samples, dtype=np.uint8).astype(float) - c2.ScopeWave.CENTRE
            self.curves[w.channel].setData(np.arange(len(y)), y)
        for n in range(2):
            if n not in shown:
                self.curves[n].setData([], [])
        self._show_age()

    def _show_age(self) -> None:
        if self.wave_time is None:
            return
        age = time.monotonic() - self.wave_time
        parts = []
        for n in sorted(self.waves):
            w = self.waves[n]
            tb = self.tables.timebase[w.timebase] if self.tables and w.timebase < len(self.tables.timebase) else str(w.timebase)
            vi = w.probe * 3 + w.vdiv
            vd = self.tables.vdiv[vi] if self.tables and vi < len(self.tables.vdiv) else str(w.vdiv)
            parts.append("CH%d %s %s %s" % (n + 1, tb, vd, "running" if w.running else "stopped"))
        self.age.setText("trace age %.1f s; %s" % (age, "; ".join(parts)))

    def on_shown(self) -> None:
        self.timer.start()

    def on_hidden(self) -> None:
        self.timer.stop()

    def stop(self) -> None:
        self.timer.stop()

    # -- state --------------------------------------------------------------------------------
    def on_state(self, st) -> None:
        if not isinstance(st, c2.State):
            return
        self.state = st
        self.running.setText("scope %s, page %s" % ("running" if st.flag("scope running")
                                                    else "stopped", st.page_name))
        i = self.timebase.findData(st.timebase)
        self.timebase.blockSignals(True)
        self.timebase.setCurrentIndex(i)
        self.timebase.blockSignals(False)
        vd = []
        for n, c in enumerate((st.ch1, st.ch2)):
            vi = c.probe * 3 + c.vdiv
            vd.append("CH%d %s, %s, offset %d, %s, %s%s" % (
                n + 1, "on" if c.enabled else "off",
                self.tables.vdiv[vi] if self.tables and vi < len(self.tables.vdiv) else
                "V/div step %d" % c.vdiv, c.offset, "DC" if c.coupling else "AC",
                "x10" if c.probe else "x1", ", FFT" if c.fft else ""))
        t = st.trigger
        vd.append("trigger CH%d, edge %d, level %d, %s" % (t.source + 1, t.edge, t.level,
                                                           t.mode_name))
        self.meter_values.setText("; ".join(vd))
        for n, c in enumerate((st.ch1, st.ch2)):
            if self.dirty("ch%d" % n):
                continue                     # the user is editing this channel
            w = self.ch[n]
            for key in ("enabled", "fft", "offset", "coupling", "probe"):
                w[key].blockSignals(True)
            w["enabled"].setChecked(c.enabled)
            w["fft"].setChecked(c.fft)
            w["offset"].setValue(c.offset)
            w["coupling"].setCurrentIndex(c.coupling if c.coupling in (0, 1) else -1)
            w["probe"].setCurrentIndex(c.probe if c.probe in (0, 1) else -1)
            for key in ("enabled", "fft", "offset", "coupling", "probe"):
                w[key].blockSignals(False)
            self._fill_vdiv(n)
            combo = w["vdiv"]
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(c.vdiv))
            combo.blockSignals(False)
        if self.dirty("trig"):
            return
        self.quietly(self.t_source, self.t_source.setCurrentIndex,
                     t.source if t.source in (0, 1) else -1)
        self.quietly(self.t_edge, self.t_edge.setCurrentIndex, t.edge if t.edge in (0, 1) else -1)
        self.quietly(self.t_level, self.t_level.setValue, t.level)
        self.quietly(self.t_mode, self.t_mode.setCurrentIndex, self.t_mode.findData(t.mode))
