"""LC1020E Keys and Diagnostics pages.

Keys (SL 5.2, 7.1, 7.3): the 14 key bits with what SL 7.1 lists for them on the measure and the
settings screen; a short press or a hold of the given milliseconds (KEY). The meter itself refuses
the injections of SL 7.3 (status 6), for example a hold of the power key.

Diagnostics (DESIGN 13.6): PING, STATE, FACTORY_MODE, a single MEASURE, MEM_READ, and CALIBRATE
types 1..10 (the factory resistor references, accepted by the meter in factory mode only).
"""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox

from ...devices import lc1020e as lc
from ..base import Page, hexdump, mono_text, row
from ..widgets import Card, KeyButton, KeyRow, StatRow, label, set_kind
from .storage import run_calibration

# SL 7.1: (pin, measure screen, settings screen)
KEYS = {
    0: ("PC6", "long: power off", ""),
    1: ("PC7 (up)", "value +1 of the parameter under the cursor",
        "previous menu item; in the sorting editor: steps the digit"),
    2: ("PC8", "cursor +1", "value of menu items 2..4; in the sorting dialog: row, digit "
                            "position, alarm value"),
    3: ("PC9 (OK)", "long: settings screen", "opens the item / dialog, starts editing a row, "
                                             "starts the calibration (item 5); long: leaves editing, "
                                             "closes the dialog, leaves the menu"),
    4: ("PC10", "cursor -1", "as 0x0004, other direction"),
    5: ("PA15", "value -1 of the parameter under the cursor",
        "next menu item; in the sorting editor: steps the digit"),
    6: ("PC11", "secondary +1", ""),
    7: ("PC12", "cursor to RANGE, then step it", ""),
    8: ("PB5", "cursor to BIAS, then step it", ""),
    9: ("PB7", "Func +1", ""),
    10: ("PE6", "hold on / off; long: compare screen", ""),
    11: ("PB8", "cursor to AMP (level), then step it", ""),
    12: ("PB9", "speed +1; long: circuit mode +1", ""),
    13: ("PE0", "cursor to FREQ, then step it", ""),
}

# The Live page's key row (DESIGN_UI 6.10): the keys whose measure-screen meaning above has a
# short-press part, labelled with the words of that meaning; the second line is its long-press
# part. Keys 0 and 3 act on the measure screen only when held, so they are on the Keys card only.
LIVE_KEYS = {
    9: ("Func +1", ""), 6: ("secondary +1", ""), 13: ("FREQ", ""), 11: ("AMP", ""),
    8: ("BIAS", ""), 7: ("RANGE", ""), 12: ("speed +1", "long: circuit mode +1"),
    10: ("hold", "long: compare screen"), 2: ("cursor +1", ""), 4: ("cursor -1", ""),
    1: ("value +1", ""), 5: ("value -1", ""),
}


class LcKeysPage(Page):
    title = "Keys"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        card = Card("Keys")
        self.hold_ms = QSpinBox()
        self.hold_ms.setRange(0, 65535)
        self.hold_ms.setSuffix(" ms")
        self.hold_ms.setMinimumWidth(110)
        card.add(row("Hold time", self.hold_ms,
                     label("0: a short press; from 1000 ms on: a hold (SL 7.1)", "hint")))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        for col, text in enumerate(("Key", "Measure screen", "Settings screen")):
            grid.addWidget(label(text, "caption"), 0, col)
        self.buttons = {}
        for bit, (pin, meas, sett) in KEYS.items():
            b = self.control(QPushButton("bit %d  0x%04X  %s" % (bit, 1 << bit, pin)), lc.KEY)
            b.setObjectName("key_%d" % bit)
            b.clicked.connect(lambda _=False, bit=bit: self.press(bit))
            grid.addWidget(b, bit + 1, 0)
            for col, text in ((1, meas), (2, sett)):
                grid.addWidget(label(text, "label", word_wrap=True), bit + 1, col)
            self.buttons[bit] = b
        card.add(grid)
        answer_row = StatRow("Meter state after the key", line=False)
        self.answer = answer_row.value
        card.add(answer_row)
        self.body.addWidget(card)

        # the Live page's row of keys: a short press each, the meaning as tooltip
        self.live_buttons = {}
        for bit, (text, sub) in LIVE_KEYS.items():
            k = self.control(KeyButton(text, sub), lc.KEY)
            k.setObjectName("live_key_%d" % bit)
            k.setToolTip("%s (bit %d, %s): %s" % (text, bit, KEYS[bit][0], KEYS[bit][1]))
            k.clicked.connect(lambda _=False, bit=bit: self.press(bit, 0))
            self.live_buttons[bit] = k
        self.live_keys = KeyRow(list(self.live_buttons.values()))
        self.extras.append(self.live_keys)

    def press(self, bit: int, ms: int | None = None):
        """KEY: bit, held `ms` milliseconds (None: the hold time of the Keys card)."""
        dev = self.dev
        ms = self.hold_ms.value() if ms is None else ms

        def done(st):
            self.ctx.publish_state(st)
            self.say("key bit %d %s" % (bit, "held %d ms" % ms if ms else "pressed"))

        return self.run("KEY %d" % bit, lambda p: dev.key(bit, ms), done)

    def on_state(self, st) -> None:
        if isinstance(st, lc.State):
            self.answer.setText("screen %s, cursor %d, dialog %d, menu item %d; flags: %s" % (
                lc.SCREEN_NAME.get(st.screen, st.screen), st.cursor, st.dialog, st.menu_item,
                ", ".join(lc.flag_names(st.flags, lc.STATE_FLAGS)) or "none"))


class LcDiagnosticsPage(Page):
    title = "Diagnostics"
    status_names = lc.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.out = mono_text()
        self.out.setObjectName("diag_out")

        self.out.setMinimumHeight(240)
        self.ping_text = QLineEdit("FNIRSI")
        self.ping_text.setMaximumWidth(220)
        self.ping_button = self.control(QPushButton("PING"), lc.PING)
        self.ping_button.clicked.connect(self.ping)
        self.state_button = self.control(QPushButton("STATE"), lc.STATE)
        self.state_button.clicked.connect(self.state)
        self.factory_button = self.control(QPushButton("FACTORY_MODE"), lc.FACTORY_MODE)
        self.factory_button.clicked.connect(self.factory)
        self.measure_button = self.control(QPushButton("MEASURE (single)"), lc.MEASURE)
        self.measure_button.clicked.connect(self.measure)
        top = QHBoxLayout()
        top.setSpacing(16)
        self.body.addLayout(top)
        devc = Card("Device")
        devc.add(row("Bytes", self.ping_text, self.ping_button))
        devc.add(row(self.state_button, self.factory_button, self.measure_button))
        devc.body.addStretch(1)
        top.addWidget(devc, 1)

        self.addr = QLineEdit("0x08000000")
        self.addr.setMaximumWidth(160)
        self.length = QSpinBox()
        self.length.setRange(1, 256)
        self.length.setValue(64)
        self.mem_button = self.control(QPushButton("MEM_READ"), lc.MEM_READ)
        self.mem_button.clicked.connect(self.mem_read)
        mem = Card("Memory")
        mem.add(row("Address", self.addr, "Length", self.length, self.mem_button))
        mem.add(label("Flash 0x08000000..0x0807FFFF, system memory 0x1FFF0000..0x1FFF3FFF, "
                      "configuration / option bytes 0x1FFFF000..0x1FFFF811, RAM "
                      "0x20000000..0x2001FFFF (SL 5.5); else status 2.", "hint", word_wrap=True))
        mem.body.addStretch(1)
        top.addWidget(mem, 1)

        self.cal_type = QSpinBox()
        self.cal_type.setRange(1, 10)
        self.cal_button = self.control(set_kind(QPushButton("CALIBRATE…"), "danger"),
                                       lc.CALIBRATE)
        self.cal_button.clicked.connect(self.calibrate)
        cal = Card("Factory calibration")
        cal.add(row("Type (factory reference)", self.cal_type, self.cal_button))
        res_row = StatRow("Result", line=False)
        self.cal_result = res_row.value
        cal.add(res_row)
        cal.add(label("Factory mode only (SL 5.3.3).", "hint"))
        self.body.addWidget(cal)
        out = Card("Output")
        out.add(self.out, 1)
        self.body.addWidget(out, 1)

    def _show(self, title: str, text: str) -> None:
        self.out.setPlainText("%s\n%s" % (title, text))
        self.say(title)

    def ping(self):
        dev = self.dev
        data = self.ping_text.text().encode("utf-8")[:511]
        return self.run("PING", lambda p: dev.ping(data),
                        lambda a: self._show("PING: %d bytes back%s" % (
                            len(a), ", the same" if a == data else ", DIFFERENT"), hexdump(a)))

    def state(self):
        dev = self.dev

        def done(st):
            self.ctx.publish_state(st)
            self._show("STATE", "\n".join("%s = %r" % (k, v) for k, v in vars(st).items()
                                          if k != "raw") + "\n\n" + hexdump(st.raw))

        return self.run("STATE", lambda p: dev.read_state(), done)

    def factory(self):
        dev = self.dev
        return self.run("FACTORY_MODE", lambda p: dev.factory_mode(),
                        lambda v: self._show("FACTORY_MODE: %d%s" % (
                            v, " (factory mode)" if v == 2 else ""), ""))

    def measure(self):
        dev = self.dev

        def job(_p):
            m = dev.measure()
            return m, lc.measure_to_reading(m, 0.0, 0.0)

        def done(res):
            m, r = res
            lines = ["result %d, time %d ms" % (m.result, m.time),
                     "main: %s %s (%s)" % (r.quantity, r.text, r.validity),
                     "secondary: %s %s (%s)" % (r.secondary.quantity, r.secondary.text,
                                                 r.secondary.validity),
                     "Z %g ohm, theta %g degrees, frequency %d Hz, level %g V, bias %g V" % (
                         m.z, m.theta, m.frequency, m.level, m.bias),
                     "range %d, speed %d, circuit %s, parallel %d" % (
                         m.range, m.speed, lc.CIRCUIT_NAME.get(m.circuit, m.circuit), m.parallel),
                     "", hexdump(m.raw)]
            self._show("MEASURE", "\n".join(lines))

        return self.run("MEASURE", job, done)

    def mem_read(self):
        try:
            addr = int(self.addr.text(), 0)
        except ValueError:
            self.say("address: a number (0x... for hex)", error=True)
            return None
        n = self.length.value()
        dev = self.dev
        return self.run("MEM_READ", lambda p: dev.mem_read(addr, n),
                        lambda d: self._show("MEM_READ 0x%08X, %d bytes" % (addr, len(d)),
                                             hexdump(d, addr)))

    def calibrate(self):
        t = self.cal_type.value()
        word = self.confirm("Factory calibration type %d" % t,
                            "CALIBRATE type %d: the meter runs its calibration against factory "
                            "reference %d and saves the result into its settings (the calibration "
                            "table changes). The meter accepts it only in factory mode. Both "
                            "settings copies are read into a backup file first." % (t, t))
        if word is None:
            return None
        return run_calibration(self, self.dev, t, word, self.cal_result)
