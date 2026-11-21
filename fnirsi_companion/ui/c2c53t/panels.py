"""2C53T Multimeter and Generator pages (S2 4.3, 4.5, 4.6).

Multimeter: DMM_FUNCTION (the 13 functions of S2 4.3), DMM_HOLD, DMM_RELATIVE; the meter page is
opened with PAGE 1 (the DMM commands answer status 3 elsewhere). The controls and the record view
follow the STATE record (stream and answers).
Generator: GEN_SET (output, waveform index 0..12 of the meter's list, frequency 1..50000 Hz, duty
0..100 %, amplitude 1..30 = 0.1..3.0 V); the form shows the generator part of STATE.
"""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel, QPushButton, QSpinBox)

from ...devices import c2c53t as c2
from ..base import Page, group, row


class MultimeterPage(Page):
    title = "Multimeter"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.page_button = self.control(QPushButton("Open the meter page (PAGE 1)"), c2.PAGE)
        self.page_button.clicked.connect(lambda: self._send("PAGE 1", lambda: self.dev.page(1)))
        self.function = self.control(QComboBox(), c2.DMM_FUNCTION)
        self.function.setObjectName("dmm_function")
        for i, name in enumerate(c2.DMM_FUNCTION_NAME):
            self.function.addItem(name, i)
        self.function.activated.connect(self._function)
        self.hold = self.control(QCheckBox("Hold"), c2.DMM_HOLD)
        self.hold.clicked.connect(lambda on: self._toggle("DMM_HOLD", self.dev.dmm_hold, on,
                                                          self.hold))
        self.relative = self.control(QCheckBox("Relative"), c2.DMM_RELATIVE)
        self.relative.clicked.connect(lambda on: self._toggle("DMM_RELATIVE", self.dev.dmm_relative,
                                                              on, self.relative))
        self.body.addLayout(row(self.page_button))
        self.body.addLayout(row("Function:", self.function, self.hold, self.relative))
        self.fields = {}
        form = QFormLayout()
        for key, name in (("page", "Page"), ("function", "Function"), ("status", "Status"),
                          ("value", "Value (as the meter has it)"), ("decimals", "Decimals"),
                          ("unit", "Unit (from the function)"), ("shown", "Unit shown"),
                          ("exponent", "Prefix exponent"), ("length", "Record length")):
            lab = QLabel("—")
            lab.setObjectName("dmm_" + key)
            form.addRow(name, lab)
            self.fields[key] = lab
        self.body.addWidget(group("STATE: multimeter", form))
        self.body.addWidget(QLabel("DMM_RELATIVE is refused by the meter in auto, diode / "
                                   "continuity, temperature and without a reading (status 3)."))
        self.body.addStretch(1)

    def _send(self, what, call):
        return self.run(what, lambda p: call(), self.ctx.publish_state)

    def _function(self, idx: int):
        f = self.function.itemData(idx)
        return self._send("DMM_FUNCTION", lambda: self.dev.dmm_function(f))

    def _toggle(self, what, fn, on, box):
        box.setChecked(not on)                       # shown from the answer
        return self._send(what, lambda: fn(on))

    def on_state(self, st) -> None:
        if not isinstance(st, c2.State):
            return
        d = st.dmm
        self.function.blockSignals(True)
        self.function.setCurrentIndex(d.function if d.function < len(c2.DMM_FUNCTION_NAME) else -1)
        self.function.blockSignals(False)
        self.hold.setChecked(st.flag("hold"))
        self.relative.setChecked(st.flag("relative"))
        F = self.fields
        F["page"].setText(st.page_name)
        F["function"].setText(d.function_name)
        F["status"].setText("—" if d.status is None else "%d %s" % (d.status, d.status_text or ""))
        F["value"].setText("%.*f" % (d.decimals, d.value) if d.value == d.value else "NaN")
        F["decimals"].setText(str(d.decimals))
        F["unit"].setText(c2.UNIT_SYMBOL.get(d.unit, str(d.unit)) or "none")
        F["shown"].setText("—" if d.shown_unit is None else
                           (c2.UNIT_SYMBOL.get(d.shown_unit, str(d.shown_unit)) or "none"))
        F["exponent"].setText("—" if d.exponent is None else str(d.exponent))
        F["length"].setText("%d bytes%s" % (st.length, "" if d.has_si_view else
                                            " (no prefix / status: raw view, DESIGN 13.4)"))


class GeneratorPage(Page):
    title = "Generator"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.page_button = self.control(QPushButton("Open the generator page (PAGE 3)"), c2.PAGE)
        self.page_button.clicked.connect(
            lambda: self.run("PAGE 3", lambda p: self.dev.page(3), self.ctx.publish_state))
        self.on = QCheckBox("Output on")
        self.waveform = QComboBox()
        for i in range(c2.GEN_WAVEFORMS):
            self.waveform.addItem("waveform %d" % i, i)
        self.frequency = QSpinBox()
        self.frequency.setRange(1, 50000)
        self.frequency.setSuffix(" Hz")
        self.duty = QSpinBox()
        self.duty.setRange(0, 100)
        self.duty.setSuffix(" %")
        self.amplitude = QSpinBox()
        self.amplitude.setRange(1, 30)
        self.amplitude.setSuffix(" × 0.1 V")
        self.apply_button = self.control(QPushButton("GEN_SET"), c2.GEN_SET)
        self.apply_button.clicked.connect(self.apply)
        form = QFormLayout()
        form.addRow(self.on)
        form.addRow("Waveform (index in the meter's list)", self.waveform)
        form.addRow("Frequency", self.frequency)
        form.addRow("Duty", self.duty)
        form.addRow("Amplitude", self.amplitude)
        form.addRow(self.apply_button)
        self.body.addLayout(row(self.page_button))
        self.body.addWidget(group("Signal generator", form))
        self.shown = QLabel("—")
        self.shown.setObjectName("gen_state")
        self.body.addLayout(row("Meter:", self.shown))
        self.body.addStretch(1)
        self.edits("gen", self.on, self.waveform, self.frequency, self.duty, self.amplitude)

    def apply(self):
        dev = self.dev
        args = (self.on.isChecked(), self.waveform.currentData(), self.frequency.value(),
                self.duty.value(), self.amplitude.value())
        if args[1] is None:
            self.say("choose a waveform", error=True)
            return None

        def answered(st):
            self.clean("gen")
            self.ctx.publish_state(st)

        def failed(_e):
            self.clean("gen")
            if self.last is not None:
                self.on_state(self.last)

        return self.run("GEN_SET", lambda p: dev.gen_set(*args), answered, failed=failed)

    last = None

    def on_state(self, st) -> None:
        if not isinstance(st, c2.State):
            return
        self.last = st
        g = st.gen
        if not self.dirty("gen"):
            self.quietly(self.on, self.on.setChecked, g.on)
            self.quietly(self.waveform, self.waveform.setCurrentIndex,
                         g.waveform if g.waveform < c2.GEN_WAVEFORMS else -1)
            for w, v in ((self.frequency, g.frequency), (self.duty, g.duty),
                         (self.amplitude, g.amplitude)):
                self.quietly(w, w.setValue, v)
        self.shown.setText("output %s, waveform %d, %d Hz, duty %d %%, amplitude %.1f V" % (
            "on" if g.on else "off", g.waveform, g.frequency, g.duty, g.amplitude / 10))
