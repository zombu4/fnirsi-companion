"""Live, Trend and Log: the pages fed with the active meter's readings (devices.base.Reading).

They belong to the window, not to a meter, so a recording and the trend go on across a reconnect of
the same meter; a change of Reading.key restarts the statistics and breaks the trend (core.record).

Live    the reading as the meter shows it, the secondary reading, validity, statistics (count,
        minimum, maximum, mean, standard deviation; core.record.Stats), Reset, and a limit test
        (low / high in the reading's SI unit, verdict PASS / HI / LO as the 121GW companion).
Trend   pyqtgraph plot of core.record.Trend: auto or fixed scale, two cursors with their times and
        values, freeze, clear, export of the current run as CSV.
Log     CSV recording (core.record.Recorder) with the log trigger (every reading, interval, change,
        limit verdict), the recordings folder's files.
"""

from __future__ import annotations

import math
import os
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox,
                               QVBoxLayout)

from ...core import record
from ..base import Page, group, row

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


# ----------------------------------------------------------------------------------------------
# Live
# ----------------------------------------------------------------------------------------------

class LivePage(Page):
    title = "Live"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.stats = record.Stats()
        self.last = None
        self.lo: float | None = None
        self.hi: float | None = None
        self.last_verdict: str | None = None

        self.quantity = QLabel("—")
        self.big = QLabel("no meter")
        self.big.setObjectName("live_reading")
        f = QFont(self.big.font())
        f.setPointSize(42)
        f.setBold(True)
        self.big.setFont(f)
        self.value = QLabel("—")
        self.validity = QLabel("—")
        self.secondary = QLabel("—")
        self.note_raw = QLabel("")
        self.note_raw.setWordWrap(True)
        top = QVBoxLayout()
        top.addWidget(self.quantity)
        top.addWidget(self.big)
        top.addLayout(row("Value (SI):", self.value, "Validity:", self.validity))
        top.addLayout(row("Secondary:", self.secondary))
        top.addWidget(self.note_raw)
        self.body.addWidget(group("Reading", top))

        grid = QGridLayout()
        self.stat_labels = {}
        for i, (key, name) in enumerate((("n", "Count"), ("min", "Minimum"), ("max", "Maximum"),
                                          ("mean", "Mean"), ("sd", "Standard deviation"))):
            grid.addWidget(QLabel(name), i, 0)
            lab = QLabel("—")
            lab.setObjectName("stat_" + key)
            grid.addWidget(lab, i, 1)
            self.stat_labels[key] = lab
        self.reset_button = QPushButton("Reset statistics")
        self.reset_button.clicked.connect(self.reset)
        grid.addWidget(self.reset_button, 5, 0)
        self.body.addWidget(group("Statistics (since the last change of quantity / range)", grid))

        self.lo_edit = QLineEdit()
        self.lo_edit.setPlaceholderText("low")
        self.hi_edit = QLineEdit()
        self.hi_edit.setPlaceholderText("high")
        self.apply_limit = QPushButton("Set limit")
        self.apply_limit.clicked.connect(self._set_limit)
        self.verdict_label = QLabel("no limit set")
        self.verdict_label.setObjectName("verdict")
        lim = QVBoxLayout()
        lim.addLayout(row("Low:", self.lo_edit, "High:", self.hi_edit, self.apply_limit))
        lim.addLayout(row("Verdict:", self.verdict_label))
        lim.addWidget(QLabel("Limits in the SI unit of the reading (F, H, Ω, V, A ...); "
                             "an empty field is no limit on that side."))
        self.body.addWidget(group("Limit", lim))
        self.body.addStretch(1)

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
            self.verdict_label.setText("no limit set")
        else:
            self.verdict_label.setText(v or "— (no number)")

    def add(self, reading) -> str | None:
        """Shows one reading and counts it; returns its limit verdict."""
        self.last = reading
        self.stats.add_reading(reading)
        self.quantity.setText(reading.quantity or "—")
        self.big.setText(reading.text or ("—" if reading.validity == "valid" else reading.validity))
        self.value.setText(fmt_si(record.numeric(reading), reading.unit))
        self.validity.setText(reading.validity)
        s = reading.secondary
        if s is None:
            self.secondary.setText("—")
        else:
            self.secondary.setText("%s  %s  (%s)" % (s.quantity, s.text or "—", s.validity))
        if reading.validity == "unknown":
            self.note_raw.setText("Raw meter data: the meter's record carries no unit prefix and "
                                  "no reading status (DESIGN 13.4); the value is the displayed "
                                  "digits.")
        else:
            self.note_raw.setText("")
        snap = self.stats.snapshot()
        self.stat_labels["n"].setText(str(snap["n"]))
        for k in ("min", "max", "mean", "sd"):
            self.stat_labels[k].setText(fmt_si(snap[k], reading.unit))
        v = self.verdict(reading)
        self.last_verdict = v
        self._show_verdict(v)
        return v

    def reset(self) -> None:
        self.stats.reset(self.last.key if self.last is not None else None)
        for k, lab in self.stat_labels.items():
            lab.setText("0" if k == "n" else "—")

    def meter_gone(self) -> None:
        self.big.setText("no meter")


# ----------------------------------------------------------------------------------------------
# Trend
# ----------------------------------------------------------------------------------------------

class TrendPage(Page):
    title = "Trend"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.trend = record.Trend()
        self.unit = ""
        self.t0: float | None = None
        self.frozen = False
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "time", "s")
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2), connect="finite")
        self.c1 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#e67e22"))
        self.c2 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#16a085"))
        self.c1.sigPositionChanged.connect(self._cursor_text)
        self.c2.sigPositionChanged.connect(self._cursor_text)
        self.body.addWidget(self.plot, 1)

        self.auto = QComboBox()
        self.auto.addItems(["Auto scale", "Fixed scale"])
        self.auto.currentIndexChanged.connect(self._scale)
        self.ymin = QDoubleSpinBox()
        self.ymax = QDoubleSpinBox()
        for sb, v in ((self.ymin, 0.0), (self.ymax, 1.0)):
            sb.setRange(-1e12, 1e12)
            sb.setDecimals(9)
            sb.setValue(v)
            sb.valueChanged.connect(self._scale)
        self.cursors = QCheckBox("Cursors")
        self.cursors.toggled.connect(self._toggle_cursors)
        self.freeze = QCheckBox("Freeze")
        self.freeze.toggled.connect(self._freeze)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        self.export_button = QPushButton("Export trend CSV")
        self.export_button.clicked.connect(self.export)
        self.cursor_info = QLabel("")
        self.body.addLayout(row(self.auto, "min", self.ymin, "max", self.ymax, self.cursors,
                                self.freeze, self.clear_button, self.export_button))
        self.body.addWidget(self.cursor_info)
        self._scale()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.redraw)
        self.timer.start()

    def add(self, reading) -> None:
        if self.t0 is None:
            self.t0 = reading.t
        if reading.key != self.trend._key:
            self.unit = reading.unit
            self.plot.setLabel("left", reading.quantity, reading.unit)
        self.trend.add(reading)

    def data(self):
        """(x seconds since the first reading, y values with NaN gaps) of the plot."""
        pts = self.trend.points()
        if not pts:
            return np.array([]), np.array([])
        x = np.array([t - self.t0 for t, _ in pts], dtype=float)
        y = np.array([np.nan if v is None else v for _, v in pts], dtype=float)
        return x, y

    def redraw(self) -> None:
        if self.frozen or not self.isVisible():
            return
        x, y = self.data()
        self.curve.setData(x, y, connect="finite")
        self._cursor_text()

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
            self.cursor_info.setText("")
            return
        a, b = self.value_at(self.c1.value()), self.value_at(self.c2.value())
        if a is None or b is None:
            self.cursor_info.setText("cursors: no data")
            return
        dt = b[0] - a[0]
        dv = (b[1] - a[1]) if a[1] is not None and b[1] is not None else None
        self.cursor_info.setText("C1 %.3f s: %s   C2 %.3f s: %s   Δt %.3f s   Δ %s" % (
            a[0], fmt_si(a[1], self.unit), b[0], fmt_si(b[1], self.unit), dt,
            fmt_si(dv, self.unit)))

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

    def __init__(self, ctx):
        super().__init__(ctx)
        self.recorder = record.Recorder()
        self.record_button = QPushButton("Start recording")
        self.record_button.setObjectName("record")
        self.record_button.clicked.connect(self.toggle)
        self.elapsed = QLabel("—")
        self.rows = QLabel("—")
        self.path = QLabel("—")
        self.path.setWordWrap(True)
        rec = QFormLayout()
        rec.addRow(self.record_button)
        rec.addRow("Time", self.elapsed)
        rec.addRow("Rows", self.rows)
        rec.addRow("File", self.path)
        self.body.addWidget(group("Recording", rec))

        self.mode = QComboBox()
        for m in record.LOG_MODES:
            self.mode.addItem(LOG_MODE_TEXT[m], m)
        self.interval = QSpinBox()
        self.interval.setRange(0, record.LOG_INTERVAL_MAX)
        self.interval.setValue(record.LOG_DEFAULT[1])
        self.change = QSpinBox()
        self.change.setRange(1, record.LOG_CHANGE_MAX)
        self.change.setValue(record.LOG_DEFAULT[2])
        self.mode.currentIndexChanged.connect(self._explain)
        self.interval.valueChanged.connect(self._explain)
        self.change.valueChanged.connect(self._explain)
        self.explain = QLabel("")
        self.explain.setWordWrap(True)
        trig = QFormLayout()
        trig.addRow("Trigger", self.mode)
        trig.addRow("Interval (s)", self.interval)
        trig.addRow("Change (counts)", self.change)
        trig.addRow(self.explain)
        self.body.addWidget(group("Log trigger (the display, statistics and trend get every "
                                  "reading)", trig))

        self.files = QListWidget()
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self.refresh_files)
        self.open_folder = QPushButton("Open folder")
        self.open_folder.clicked.connect(self._open_folder)
        fl = QVBoxLayout()
        fl.addWidget(self.files)
        fl.addLayout(row(self.refresh_button, self.open_folder))
        self.body.addWidget(group("Recordings folder", fl), 1)
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
        self.files.clear()
        try:
            names = sorted((n for n in os.listdir(folder) if n.lower().endswith(".csv")),
                           key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
        except OSError:
            names = []
        for n in names:
            self.files.addItem("%s  (%d bytes)" % (n, os.path.getsize(os.path.join(folder, n))))

    def _open_folder(self) -> None:
        folder = self.ctx.settings.recordings_folder()
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def stop(self) -> None:
        self.timer.stop()
        if self.recorder.recording:
            self.recorder.stop()
