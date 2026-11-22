"""Live, Trend and Log: the pages fed with the active meter's readings (devices.base.Reading).

They belong to the window, not to a meter, so a recording and the trend go on across a reconnect of
the same meter; a change of Reading.key restarts the statistics and breaks the trend (core.record).

Live    the reading on the painted LCD as the meter shows it (annunciators and mode text from the
        model's block, the quantity, the secondary reading), the meter's keys, the model's
        measurement controls (attached by the window while a meter is connected), statistics
        (count, minimum, maximum, mean, standard deviation; core.record.Stats), Reset, a limit
        test (low / high in the reading's SI unit, verdict PASS / HI / LO as the 121GW
        companion), the recording of the Log page and the last 10 minutes of the trend.
Trend   pyqtgraph plot of core.record.Trend: time window, auto or fixed scale, the grid's
        divisions, two cursors with their times, values and differences, freeze, clear, export
        of the current run as CSV.
Log     CSV recording (core.record.Recorder) with the log trigger (every reading, interval, change,
        limit verdict), the recordings folder's files.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QPainter, QValidator
from PySide6.QtWidgets import (QDoubleSpinBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ...core import record
from .. import style
from ..base import Page, row
from ..widgets import Card, FlowLayout, Lcd, Segmented, StatRow, label, set_kind

_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"),
             (1e-9, "n"), (1e-12, "p")]


def fmt_si(v, unit: str, digits: int = 5) -> str:
    """A number in its SI unit with an engineering prefix ("1.0000e-07 F" -> "100.00 nF"); units
    without a prefix use (empty, degrees, °C, °F) are printed plain."""
    if v is None:
        return "—"
    if not math.isfinite(v):
        return str(v)
    if unit in ("", "°", "°C", "°F"):
        return ("%.*g" % (digits, v)) + ((" " + unit) if unit else "")
    a = abs(v)
    for scale, p in _PREFIXES:
        if a >= scale or scale == 1e-12:
            if a == 0:
                scale, p = 1.0, ""
            return "%.*g %s%s" % (digits, v / scale, p, unit)
    return "%g %s" % (v, unit)


class ScaleBox(QDoubleSpinBox):
    """A limit of the fixed vertical scale in the SI unit of the reading. Shows the shortest
    form of the number ("0", "1.5e-07") instead of a fixed count of decimals, and takes any
    number Python's float() reads; keeps 15 decimals so that pF and nH values are not rounded."""

    def __init__(self, value: float):
        super().__init__()
        self.setRange(-1e12, 1e12)
        self.setDecimals(15)
        self.setValue(value)

    def textFromValue(self, v: float) -> str:
        return "%.6g" % v

    def valueFromText(self, text: str) -> float:
        try:
            return float(text)
        except ValueError:
            return self.value()

    def validate(self, text: str, pos: int):
        t = text.strip()
        try:
            float(t)
            return QValidator.State.Acceptable, text, pos
        except ValueError:
            pass
        if all(c in "0123456789+-.eE" for c in t):
            return QValidator.State.Intermediate, text, pos
        return QValidator.State.Invalid, text, pos


# ----------------------------------------------------------------------------------------------
# Live
# ----------------------------------------------------------------------------------------------

@dataclass
class LiveModel:
    """What the connected model adds to the Live page (built by the main window):
    words      the annunciator words of its display, in order (DESIGN_UI 6.5)
    flags      flags(reading) -> (set of lit words, mode text), from the model's records
    secondary  the model has a secondary reading (LC1020E)
    page       the page of its measurement controls, shown under the LCD
    keys       the row of its meter keys, shown in the LCD card"""
    words: list
    flags: object
    secondary: bool
    page: QWidget
    keys: QWidget
    extra: list = field(default_factory=list)


class Dot(QWidget):
    """A round status dot of 10 px in a token colour."""

    def __init__(self, token: str = "ink-3"):
        super().__init__()
        self.token = token
        self.setFixedSize(10, 10)

    def set_token(self, token: str) -> None:
        if token != self.token:
            self.token = token
            self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(style.tokens()[self.token])
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


# The LC1020E's secondary names as its manual writes them (5.2.7 "X/D/Q/θ/ESR"): the protocol's
# "R" (secondary 0, the series resistance, NOTES "0 R/ESR") is ESR, "theta" is θ.
SECONDARY_LABEL = {"R": "ESR", "theta": "θ"}
TREND_WINDOW = 600.0                      # seconds of the Live page's trend card


class LivePage(Page):
    title = "Live"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.stats = record.Stats()
        self.last = None
        self.state = None
        self.lo: float | None = None
        self.hi: float | None = None
        self.last_verdict: str | None = None
        self.model: LiveModel | None = None
        self.last_reading_at: float | None = None

        grid = QGridLayout()
        grid.setSpacing(16)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 1)
        left = QVBoxLayout()
        left.setSpacing(16)
        right = QVBoxLayout()
        right.setSpacing(16)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        self.body.addLayout(grid)

        # LCD card: the display, the meter's keys, the SI value and validity
        self.lcd_card = Card()
        self.big = Lcd()
        self.big.setObjectName("live_reading")
        self.lcd_card.add(self.big)
        self.connect_hint = label("Connect a meter (USB)", "hint")
        self.connect_hint.setAlignment(Qt.AlignCenter)
        self.lcd_card.add(self.connect_hint)
        self.keys_box = QVBoxLayout()
        self.keys_box.setContentsMargins(0, 0, 0, 0)
        self.lcd_card.add(self.keys_box)
        self.value = label("—", "mono")
        self.validity = label("—", "mono")
        self.secondary = label("", "mono")
        info = QHBoxLayout()
        info.setSpacing(6)
        info.addWidget(label("SI value", "caption"))
        info.addWidget(self.value)
        info.addSpacing(10)
        info.addWidget(label("validity", "caption"))
        info.addWidget(self.validity)
        info.addSpacing(10)
        info.addWidget(self.secondary)
        info.addStretch(1)
        self.info_row = QWidget()
        self.info_row.setLayout(info)
        info.setContentsMargins(0, 0, 0, 0)
        self.lcd_card.add(self.info_row)
        self.note_raw = label("", "hint", word_wrap=True)
        self.note_raw.hide()
        self.lcd_card.add(self.note_raw)
        left.addWidget(self.lcd_card)

        # the model's measurement controls (attach / detach)
        self.model_box = QVBoxLayout()
        self.model_box.setSpacing(16)
        left.addLayout(self.model_box)

        # limit
        lim = Card("Limit")
        self.lo_edit = QLineEdit()
        self.lo_edit.setPlaceholderText("low")
        self.hi_edit = QLineEdit()
        self.hi_edit.setPlaceholderText("high")
        self.apply_limit = QPushButton("Set limit")
        self.apply_limit.clicked.connect(self._set_limit)
        self.verdict_label = QLabel("no limit set")
        self.verdict_label.setObjectName("verdict")
        self.verdict_label.setProperty("role", "caption")     # "big" once there is a verdict
        lim_row = QHBoxLayout()
        lim_row.setSpacing(8)
        lim_row.addWidget(self.lo_edit, 1)
        lim_row.addWidget(self.hi_edit, 1)
        lim_row.addWidget(self.apply_limit)
        lim_row.addSpacing(16)
        lim_row.addWidget(self.verdict_label, 1)
        lim.add(lim_row)
        lim.add(label("Low and high in the SI unit of the reading (F, H, Ω, V, A ...); an empty "
                      "field is no limit on that side.", "hint", word_wrap=True))
        left.addWidget(lim)
        left.addStretch(1)

        # statistics
        st = Card("Statistics")
        self.reset_button = set_kind(QPushButton("Reset"), "small")
        self.reset_button.clicked.connect(self.reset)
        st.add_right(self.reset_button)
        mean_row = QHBoxLayout()
        mean_row.setSpacing(10)
        self.mean_label = QLabel("—")
        self.mean_label.setObjectName("stat_mean")
        self.mean_label.setProperty("role", "big")          # mono 28 px (DESIGN_UI 6.7)
        self.mean_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        mean_row.addWidget(self.mean_label)
        mean_cap = label("mean", "caption")
        mean_row.addWidget(mean_cap, 0, Qt.AlignBottom)
        mean_row.addStretch(1)
        st.add(mean_row)
        self.stat_labels = {"mean": self.mean_label}
        rows = (("min", "Minimum"), ("max", "Maximum"), ("sd", "Std. deviation"), ("n", "Readings"))
        for i, (key, name) in enumerate(rows):
            r = StatRow(name, line=i < len(rows) - 1)
            r.value.setObjectName("stat_" + key)
            st.add(r)
            self.stat_labels[key] = r.value
        st.add(label("Since the last change of quantity or range.", "hint", word_wrap=True))
        right.addWidget(st)

        # recording (the Log page's recorder)
        rc = Card("Recording")
        rec_row = QHBoxLayout()
        rec_row.setSpacing(10)
        self.rec_dot = Dot("ink-3")
        rec_row.addWidget(self.rec_dot)
        self.rec_time = QLabel("00:00:00")
        self.rec_time.setProperty("role", "big")
        rec_row.addWidget(self.rec_time)
        rec_row.addStretch(1)
        rc.add(rec_row)
        self.rec_button = set_kind(QPushButton("Record"), "primary")
        self.rec_button.setObjectName("live_record")
        self.rec_button.clicked.connect(lambda: self.ctx.log.toggle())
        rc.add(self.rec_button)
        self.rec_file = label("", "hint", word_wrap=True)
        rc.add(self.rec_file)
        right.addWidget(rc)
        right.addStretch(1)

        # the last 10 minutes of the trend
        tc = Card("Trend · last 10 minutes")
        self.trend_card = tc
        self.mini = pg.PlotWidget()
        self.mini.setMinimumHeight(150)
        self.mini.setMaximumHeight(200)
        self.mini.setMouseEnabled(False, False)
        self.mini.hideButtons()
        self.mini.setMenuEnabled(False)
        self.mini.getPlotItem().hideAxis("bottom")
        self.mini.setXRange(-TREND_WINDOW, 0, padding=0)
        style.theme_plot(self.mini)
        self.mini_curve = self.mini.plot([], [], connect="finite")
        tc.add(self.mini)
        ends = QHBoxLayout()
        ends.addWidget(label("-600 s", "mono"))
        ends.addStretch(1)
        ends.addWidget(label("now", "mono"))
        tc.add(ends)
        grid.addWidget(tc, 1, 0, 1, 2)
        self._pens()
        style.on_theme(self._pens)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.meter_gone()

    def _pens(self) -> None:
        self.mini_curve.setPen(pg.mkPen(style.color("trace"), width=1.5))

    # -- the model's block --------------------------------------------------------------------
    def attach(self, model: LiveModel) -> None:
        """Shows a connected model's keys and measurement controls, and its annunciators."""
        self.detach()
        self.model = model
        self.big.set_words(model.words)
        self.big.set_flags(set())
        self.keys_box.addWidget(model.keys)
        self.model_box.addWidget(model.page)
        model.keys.show()
        model.page.show()
        self.connect_hint.hide()
        self.refresh_flags()

    def detach(self) -> None:
        """Takes the model's widgets off the page (their pages delete them)."""
        m, self.model = self.model, None
        if m is not None:
            for w in (m.keys, m.page):
                self.keys_box.removeWidget(w)
                self.model_box.removeWidget(w)
                w.setParent(None)
        self.big.set_words([])
        self.big.set_flags(set())
        self.big.set_secondary(None)

    def refresh_flags(self) -> None:
        """The annunciators and the mode text from the model's records and the last reading."""
        if self.model is None:
            return
        lit, mode = self.model.flags(self.last)
        self.big.set_flags(lit, mode)

    def on_state(self, st) -> None:
        self.state = st
        self.refresh_flags()

    # -- limit --------------------------------------------------------------------------------
    def _set_limit(self) -> None:
        vals = []
        for edit in (self.lo_edit, self.hi_edit):
            t = edit.text().strip()
            if not t:
                vals.append(None)
                continue
            try:
                v = float(t)
            except ValueError:
                self.say("limit: %r is not a number" % t, error=True)
                return
            if not math.isfinite(v):
                self.say("limit: a finite number is needed", error=True)
                return
            vals.append(v)
        if vals[0] is not None and vals[1] is not None and vals[0] > vals[1]:
            self.say("limit: low is above high", error=True)
            return
        self.lo, self.hi = vals
        self.say("limit %s .. %s" % ("—" if self.lo is None else self.lo,
                                     "—" if self.hi is None else self.hi))
        if self.last is not None:
            self._show_verdict(self.verdict(self.last))
        else:
            self._show_verdict(None)

    def verdict(self, reading) -> str | None:
        """PASS / HI / LO of the reading against the limit; None without a limit or a number."""
        if self.lo is None and self.hi is None:
            return None
        v = record.numeric(reading)
        if v is None or reading.validity in record.NOT_COUNTED:
            return None
        if self.lo is not None and v < self.lo:
            return "LO"
        if self.hi is not None and v > self.hi:
            return "HI"
        return "PASS"

    def _show_verdict(self, v) -> None:
        if self.lo is None and self.hi is None:
            text = "no limit set"
        else:
            text = v or "— (no number)"
        if text != self.verdict_label.text():
            self.verdict_label.setText(text)
            self.verdict_label.setProperty("verdict", v or "")
            self.verdict_label.setProperty("role", "big" if v else "caption")
            style.repolish(self.verdict_label)

    # -- readings -----------------------------------------------------------------------------
    def add(self, reading) -> str | None:
        """Shows one reading and counts it; returns its limit verdict."""
        self.last = reading
        self.last_reading_at = time.monotonic()
        self.stats.add_reading(reading)
        self.big.set_quantity(reading.quantity or "")
        if reading.text:
            self.big.setText(reading.text)
        else:
            self.big.set_dashes("—" if reading.validity == "valid" else reading.validity)
        self.value.setText(fmt_si(record.numeric(reading), reading.unit))
        self.validity.setText(reading.validity + (", unspecified" if reading.unspecified else ""))
        s = reading.secondary
        if s is None or self.model is None or not self.model.secondary:
            self.big.set_secondary(None)
            self.secondary.setText("")
        else:
            self.big.set_secondary(SECONDARY_LABEL.get(s.quantity, s.quantity), s.text)
            self.secondary.setText("secondary %s %s%s" % (
                SECONDARY_LABEL.get(s.quantity, s.quantity), s.validity,
                ", unspecified" if s.unspecified else ""))
        if reading.validity == "unknown":
            self.note_raw.setText("Raw meter data: the meter's record carries no unit prefix and "
                                  "no reading status (DESIGN 13.4); the value is the displayed "
                                  "digits.")
            self.note_raw.show()
        elif self.note_raw.isVisible() or self.note_raw.text():
            self.note_raw.setText("")
            self.note_raw.hide()
        snap = self.stats.snapshot()
        self.stat_labels["n"].setText(str(snap["n"]))
        for k in ("min", "max", "mean", "sd"):
            self.stat_labels[k].setText(fmt_si(snap[k], reading.unit))
        v = self.verdict(reading)
        self.last_verdict = v
        self._show_verdict(v)
        self.refresh_flags()
        return v

    def reset(self) -> None:
        self.stats.reset(self.last.key if self.last is not None else None)
        for k, lab in self.stat_labels.items():
            lab.setText("0" if k == "n" else "—")

    def meter_gone(self) -> None:
        """No meter: the empty display with dashes and the hint to connect one."""
        self.big.set_dashes()
        self.big.set_quantity("")
        self.big.set_secondary(None)
        self.big.set_flags(set())
        self.connect_hint.show()
        self.detach()

    # -- recording card and trend card --------------------------------------------------------
    def show_recording(self, *_a) -> None:
        log = getattr(self.ctx, "log", None)
        if log is None:
            return
        r = log.recorder
        on = r.recording
        self.rec_dot.set_token("bad" if on else "ink-3")
        self.rec_button.setText("Stop" if on else "Record")
        if on and r.started:
            s = int(time.time() - r.started)
            self.rec_time.setText("%02d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60))
        elif not on:
            self.rec_time.setText("00:00:00")
        self.rec_file.setText(r.path or "")

    def _tick(self) -> None:
        self.show_recording()
        if self.isVisible():
            self.redraw_trend()

    def redraw_trend(self) -> None:
        trend = getattr(self.ctx, "trend", None)
        if trend is None:
            return
        now = time.monotonic()
        pts = trend.trend.points(since=now - TREND_WINDOW)
        x = np.array([t - now for t, _ in pts], dtype=float)
        y = np.array([np.nan if v is None else v for _, v in pts], dtype=float)
        self.mini_curve.setData(x, y, connect="finite")
        unit = trend.unit
        self.trend_card.set_title("Trend · last 10 minutes" + (" · %s" % unit if unit else ""))

    def stop(self) -> None:
        self.timer.stop()


# ----------------------------------------------------------------------------------------------
# Trend
# ----------------------------------------------------------------------------------------------

TIME_WINDOWS = [("All", None), ("10 s", 10.0), ("30 s", 30.0), ("1 min", 60.0),
                ("5 min", 300.0), ("10 min", 600.0)]


class CursorReadout(QWidget):
    """The cursor row of the reference's scope page: A t, B t, Δt, Δ value, 1/Δt, each in its
    cell with the caption on the left and the value in mono on the right."""

    NAMES = ("A t", "B t", "Δt", "Δ", "1/Δt")

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.caps, self.vals = [], []
        for i, name in enumerate(self.NAMES):
            cell = QWidget()
            cell.setObjectName("cursorcell")
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(12 if i else 0, 6, 12, 6)
            cap = label(name, "caption")
            val = label("—", "value")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            cl.addWidget(cap)
            cl.addWidget(val, 1)
            lay.addWidget(cell, 1)
            self.caps.append(cap)
            self.vals.append(val)

    def set_unit(self, unit: str) -> None:
        self.caps[3].setText("Δ%s" % (unit or "value"))

    def set_values(self, values) -> None:
        for lab, v in zip(self.vals, values):
            lab.setText(v)

    def text(self) -> str:
        return "   ".join("%s %s" % (c.text(), v.text()) for c, v in zip(self.caps, self.vals))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setPen(style.tokens()["line"])
        p.drawLine(0, 0, self.width(), 0)
        n = len(self.NAMES)
        for i in range(1, n):
            x = int(self.width() * i / n)
            p.drawLine(x, 0, x, self.height())


class TrendPage(Page):
    title = "Trend"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.trend = record.Trend()
        self.unit = ""
        self.t0: float | None = None
        self.frozen = False
        card = Card()
        self.body.addWidget(card, 1)

        bar = FlowLayout(spacing=8)
        bar_w = QWidget()
        bar_w.setLayout(bar)
        self.span = Segmented(TIME_WINDOWS)
        self.span.setObjectName("trend_window")
        self.span.currentIndexChanged.connect(lambda _i: self.redraw(force=True))
        self.auto = Segmented(["Auto", "Fixed"])
        self.auto.currentIndexChanged.connect(self._scale)
        self.ymin = ScaleBox(0.0)
        self.ymax = ScaleBox(1.0)
        for sb in (self.ymin, self.ymax):
            sb.setFixedWidth(130)
            sb.valueChanged.connect(self._scale)
        self.cursors = QPushButton("Cursors")
        self.cursors.setCheckable(True)
        self.cursors.toggled.connect(self._toggle_cursors)
        self.freeze = QPushButton("Freeze")
        self.freeze.setCheckable(True)
        self.freeze.toggled.connect(self._freeze)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self.export)
        for w in (self._pair("Time", self.span), self._pair("Vertical", self.auto), self.ymin,
                  self.ymax, self.cursors, self.freeze, self.clear_button, self.export_button):
            bar.addWidget(w)
        card.add(bar_w)
        self.divs = label("", "mono")
        card.add(self.divs)

        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(220)
        style.theme_plot(self.plot)
        self.plot.setLabel("bottom", "time", "s")
        self.curve = self.plot.plot([], [], connect="finite")
        self.c1 = pg.InfiniteLine(angle=90, movable=True)
        self.c2 = pg.InfiniteLine(angle=90, movable=True)
        self.c1.sigPositionChanged.connect(self._cursor_text)
        self.c2.sigPositionChanged.connect(self._cursor_text)
        self.plot.getViewBox().sigRangeChanged.connect(self._show_divs)
        card.add(self.plot, 1)
        self.cursor_info = CursorReadout()
        card.add(self.cursor_info)
        self._pens()
        style.on_theme(self._pens)
        self._scale()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.redraw)
        self.timer.start()

    @staticmethod
    def _pair(name: str, w: QWidget) -> QWidget:
        """A caption and a control as one item of the top bar."""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 4, 0)
        h.setSpacing(8)
        h.addWidget(label(name, "caption"))
        h.addWidget(w)
        return box

    def _pens(self) -> None:
        self.curve.setPen(pg.mkPen(style.color("trace"), width=1.5))
        self.c1.setPen(pg.mkPen(style.color("cursor-a"), width=1.5))
        self.c2.setPen(pg.mkPen(style.color("cursor-b"), width=1.5))

    def add(self, reading) -> None:
        if self.t0 is None:
            self.t0 = reading.t
        if reading.key != self.trend._key:
            self.unit = reading.unit
            self.plot.setLabel("left", reading.quantity, reading.unit)
            self.cursor_info.set_unit(reading.unit)
        self.trend.add(reading)

    def data(self):
        """(x seconds since the first reading, y values with NaN gaps) of the plot."""
        pts = self.trend.points()
        if not pts:
            return np.array([]), np.array([])
        x = np.array([t - self.t0 for t, _ in pts], dtype=float)
        y = np.array([np.nan if v is None else v for _, v in pts], dtype=float)
        return x, y

    def redraw(self, force: bool = False) -> None:
        if self.frozen or (not self.isVisible() and not force):
            return
        x, y = self.data()
        self.curve.setData(x, y, connect="finite")
        span = self.span.currentData()
        if span is None:
            self.plot.enableAutoRange(axis="x")
        elif len(x):
            self.plot.setXRange(x[-1] - span, x[-1], padding=0)
        self._cursor_text()

    def _show_divs(self, *_a) -> None:
        """The spacing of the grid's major lines as "x s/div · y unit/div" (the plot's own tick
        spacing, so the text matches the grid that is drawn)."""
        item = self.plot.getPlotItem()
        (x0, x1), (y0, y1) = item.getViewBox().viewRange()
        bx, ly = item.getAxis("bottom"), item.getAxis("left")
        try:
            sx = bx.tickSpacing(x0, x1, max(bx.geometry().width(), 1.0))
            sy = ly.tickSpacing(y0, y1, max(ly.geometry().height(), 1.0))
        except (ZeroDivisionError, ValueError, OverflowError):
            return
        if not sx or not sy:
            return
        self.divs.setText("%s/div · %s/div" % (fmt_si(sx[0][0], "s", 3),
                                               fmt_si(sy[0][0], self.unit, 3)))

    def _scale(self, *_):
        fixed = self.auto.currentIndex() == 1
        self.ymin.setEnabled(fixed)
        self.ymax.setEnabled(fixed)
        if fixed:
            lo, hi = self.ymin.value(), self.ymax.value()
            if hi <= lo:
                self.say("fixed scale: max must be above min", error=True)
                return
            self.plot.disableAutoRange(axis="y")
            self.plot.setYRange(lo, hi, padding=0)
        else:
            self.plot.enableAutoRange(axis="y")

    def _toggle_cursors(self, on: bool) -> None:
        if on:
            x, _ = self.data()
            a, b = (x[0], x[-1]) if len(x) else (0.0, 1.0)
            self.c1.setValue(a + (b - a) / 3)
            self.c2.setValue(a + 2 * (b - a) / 3)
            self.plot.addItem(self.c1)
            self.plot.addItem(self.c2)
        else:
            self.plot.removeItem(self.c1)
            self.plot.removeItem(self.c2)
        self._cursor_text()

    def value_at(self, xs: float):
        """(time, value) of the point nearest to time xs, or None."""
        x, y = self.data()
        if not len(x):
            return None
        i = int(np.argmin(np.abs(x - xs)))
        return x[i], (None if np.isnan(y[i]) else float(y[i]))

    def _cursor_text(self, *_):
        if not self.cursors.isChecked():
            self.cursor_info.set_values(["—"] * 5)
            return
        a, b = self.value_at(self.c1.value()), self.value_at(self.c2.value())
        if a is None or b is None:
            self.cursor_info.set_values(["no data"] + ["—"] * 4)
            return
        dt = b[0] - a[0]
        dv = (b[1] - a[1]) if a[1] is not None and b[1] is not None else None
        self.cursor_info.set_values([
            "%.3f s · %s" % (a[0], fmt_si(a[1], self.unit)),
            "%.3f s · %s" % (b[0], fmt_si(b[1], self.unit)),
            "%.3f s" % dt, fmt_si(dv, self.unit),
            fmt_si(1.0 / dt, "Hz") if dt else "—"])

    def _freeze(self, on: bool) -> None:
        self.frozen = on
        if not on:
            self.redraw()

    def clear(self) -> None:
        self.trend.clear()
        self.t0 = None
        self.curve.setData([], [])

    def export(self) -> None:
        folder = self.ctx.settings.recordings_folder()
        try:
            path, n = self.trend.export_csv(folder, self.unit, self.ctx.file_prefix())
        except (ValueError, OSError) as e:
            self.say("export: %s" % e, error=True)
            return
        self.say("exported %d points to %s" % (n, path))

    def stop(self) -> None:
        self.timer.stop()


# ----------------------------------------------------------------------------------------------
# Log
# ----------------------------------------------------------------------------------------------

LOG_MODE_TEXT = {"all": "Every reading", "interval": "Interval", "change": "On change",
                 "limit": "On limit verdict"}


class LogPage(Page):
    title = "Log"
    recording_changed = Signal(bool)

    def __init__(self, ctx):
        super().__init__(ctx)
        self.recorder = record.Recorder()
        top = QHBoxLayout()
        top.setSpacing(16)
        self.body.addLayout(top)

        rec = Card("Recording")
        self.record_button = set_kind(QPushButton("Start recording"), "primary")
        self.record_button.setObjectName("record")
        self.record_button.clicked.connect(self.toggle)
        rec.add(self.record_button)
        r_time, r_rows, r_file = StatRow("Time"), StatRow("Rows"), StatRow("File", line=False)
        self.elapsed, self.rows, self.path = r_time.value, r_rows.value, r_file.value
        for r in (r_time, r_rows, r_file):
            rec.add(r)
        rec.body.addStretch(1)
        top.addWidget(rec, 1)

        trig = Card("Log trigger")
        self.mode = Segmented()
        for m in record.LOG_MODES:
            self.mode.addItem(LOG_MODE_TEXT[m], m)
        self.interval = QSpinBox()
        self.interval.setRange(0, record.LOG_INTERVAL_MAX)
        self.interval.setValue(record.LOG_DEFAULT[1])
        self.interval.setSuffix(" s")
        self.change = QSpinBox()
        self.change.setRange(1, record.LOG_CHANGE_MAX)
        self.change.setValue(record.LOG_DEFAULT[2])
        self.change.setSuffix(" counts")
        self.mode.currentIndexChanged.connect(self._explain)
        self.interval.valueChanged.connect(self._explain)
        self.change.valueChanged.connect(self._explain)
        self.explain = label("", "hint", word_wrap=True)
        trig.add(self.mode)
        trig.add(row("Interval", self.interval, "Change", self.change))
        trig.add(self.explain)
        trig.add(label("The display, statistics and trend get every reading; the trigger chooses "
                       "the rows of the file.", "hint", word_wrap=True))
        trig.body.addStretch(1)
        top.addWidget(trig, 1)

        files = Card("Recordings folder")
        self.refresh_button = set_kind(QPushButton("Refresh list"), "small")
        self.refresh_button.clicked.connect(self.refresh_files)
        self.open_folder = set_kind(QPushButton("Open folder"), "small")
        self.open_folder.clicked.connect(self._open_folder)
        files.add_right(self.refresh_button)
        files.add_right(self.open_folder)
        self.files = QTableWidget(0, 3)
        self.files.setObjectName("recordings")
        self.files.setHorizontalHeaderLabels(["Name", "Size B", "Modified"])
        setup_table(self.files)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        files.add(self.files)
        self.body.addWidget(files)
        self.body.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self._explain()
        self.refresh_files()

    def trigger(self):
        return (self.mode.currentData(), self.interval.value(), self.change.value())

    def _explain(self, *_):
        m, n, c = self.trigger()
        self.explain.setText({
            "all": "A row for every reading.",
            "interval": "A row every %d s." % n if n else "Interval 0: a row for every reading.",
            "change": "A row when the displayed digits change by at least %d counts (tested once "
                      "per second)%s." % (c, " and every %d s" % n if n else ""),
            "limit": "A row when the limit verdict of the Live page (PASS, HI, LO) changes%s."
                     % (" and every %d s while it is not PASS" % n if n else ""),
        }[m])

    def toggle(self) -> None:
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> str | None:
        sep, dec = self.ctx.settings.csv_format()
        try:
            path = self.recorder.start(self.ctx.settings.recordings_folder(), self.ctx.file_prefix(),
                                       sep, dec, self.trigger(), self.ctx.value_header())
        except (ValueError, OSError) as e:
            self.say("recording not started: %s" % e, error=True)
            return None
        for w in (self.mode, self.interval, self.change):
            w.setEnabled(False)
        self.record_button.setText("Stop recording")
        self.path.setText(path)
        self.timer.start()
        self._tick()
        self.say("recording to %s" % path)
        self.recording_changed.emit(True)
        return path

    def stop_recording(self) -> str | None:
        path = self.recorder.stop()
        self.timer.stop()
        for w in (self.mode, self.interval, self.change):
            w.setEnabled(True)
        self.record_button.setText("Start recording")
        self._tick()
        self.refresh_files()
        if path:
            self.say("recording stopped: %d rows in %s" % (self.recorder.rows, path))
        self.recording_changed.emit(False)
        return path

    def add(self, reading, verdict) -> None:
        if self.recorder.recording:
            self.recorder.add(reading, verdict)

    def _tick(self) -> None:
        r = self.recorder
        self.rows.setText(str(r.rows) if r.path else "—")
        if r.recording and r.started:
            s = int(time.time() - r.started)
            self.elapsed.setText("%02d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60))

    def refresh_files(self) -> None:
        folder = self.ctx.settings.recordings_folder()
        try:
            names = sorted((n for n in os.listdir(folder) if n.lower().endswith(".csv")),
                           key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
        except OSError:
            names = []
        self.files.setRowCount(0)
        for n in names:
            p = os.path.join(folder, n)
            try:
                size, mtime = os.path.getsize(p), os.path.getmtime(p)
            except OSError:
                continue                    # removed between the listing and now
            r = self.files.rowCount()
            self.files.insertRow(r)
            for col, text in enumerate((n, "%d" % size,
                                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)))):
                it = QTableWidgetItem(text)
                if col == 1:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.files.setItem(r, col, it)
        fit_table(self.files)

    def _open_folder(self) -> None:
        folder = self.ctx.settings.recordings_folder()
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def stop(self) -> None:
        self.timer.stop()
        if self.recorder.recording:
            self.recorder.stop()


# ----------------------------------------------------------------------------------------------
# tables (DESIGN_UI 3 and 6.15)
# ----------------------------------------------------------------------------------------------

TABLE_MAX_H = 320


def setup_table(t: QTableWidget) -> None:
    """No grid, header in panel-2, rows 40 px with a line under each, selection of whole rows,
    no editing, at most TABLE_MAX_H px high (it scrolls inside its card)."""
    t.setShowGrid(False)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(40)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setWordWrap(False)
    t.setFocusPolicy(Qt.NoFocus)
    t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    fit_table(t)


def fit_table(t: QTableWidget) -> None:
    """Height of the table: its rows, at most TABLE_MAX_H px."""
    h = t.horizontalHeader().sizeHint().height() + 40 * max(t.rowCount(), 1) + 4
    t.setFixedHeight(min(h, TABLE_MAX_H))
