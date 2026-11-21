"""2C53T Settings, Keys and Diagnostics pages.

Settings (S2 4.8): SETTINGS_GET / SETTINGS_SET (RAM only; the form shows the settings the meter
answers) and SETTINGS_SAVE (the meter writes its settings page and restarts; the settings /
calibration page is backed up first and the typed "OK" is needed, DESIGN 13.1).
Keys (S2 4.7): key codes 1..23 with the functions S2 lists per page; key 1 powers the meter off and
saves its settings, so it needs the backup and the typed "OK". The page is the Keys card of the
Diagnostics view; its row of the keys with a meaning on the meter page is shown on the Live page.
Diagnostics (DESIGN 13.6): PING, STATE, PAGE, KEEP_AWAKE, CAL_READ, MEM_READ with the fault record,
FLASH_READ, METER_LOG, FLASH_ID, METER_SEND (backup + typed "OK", 13.7), TEXT_COLOUR.
"""

from __future__ import annotations

from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox,
                               QWidget)

from ...devices import c2c53t as c2
from ..base import Page, hexdump, mono_text, row
from ..widgets import Card, FlowLayout, KeyButton, KeyRow, Segmented, StatRow, label, set_kind

LONG_PRESS_FROM = 16       # key codes 16..23 are the long presses (S2 4.7)


class C2SettingsPage(Page):
    title = "Settings"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.backlight = QSpinBox()
        self.backlight.setRange(25, 100)
        self.sound = QSpinBox()
        self.sound.setRange(0, 10)
        for sb in (self.backlight, self.sound):
            sb.setMinimumWidth(110)
            sb.setMaximumWidth(160)
        self.shutdown = Segmented(["0 off", "1 (the menu's first time)", "2 (second time)",
                                   "3 (third time)"])
        self.startup = Segmented(["0 (the menu's first entry)", "1 (second entry)",
                                  "2 (third entry)"])
        self.read_button = self.control(set_kind(QPushButton("SETTINGS_GET"), "small"),
                                        c2.SETTINGS_GET)
        self.read_button.clicked.connect(self.read)
        self.set_button = self.control(QPushButton("SETTINGS_SET (RAM)"), c2.SETTINGS_SET)
        self.set_button.clicked.connect(self.apply)
        self.save_button = self.control(set_kind(QPushButton("SETTINGS_SAVE…"), "danger"),
                                        c2.SETTINGS_SAVE)
        self.save_button.clicked.connect(self.save)
        card = Card("Settings of the meter")
        card.add_right(self.read_button)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        g.setColumnStretch(2, 1)
        for r, (name, w) in enumerate((("Backlight (25..100)", self.backlight),
                                       ("Sound (0..10)", self.sound),
                                       ("Auto shutdown", self.shutdown),
                                       ("Startup option", self.startup))):
            g.addWidget(label(name), r, 0)
            g.addWidget(w, r, 1)
        card.add(g)
        card.add(row(self.set_button, self.save_button))
        shown_row = StatRow("Meter", line=False)
        self.shown = shown_row.value
        self.shown.setObjectName("c2_settings")
        card.add(shown_row)
        card.add(label("SETTINGS_SET changes RAM only; SETTINGS_SAVE writes the settings page and "
                       "restarts the meter, after a backup of the settings / calibration page.",
                       "hint", word_wrap=True))
        self.body.addWidget(card)
        self.body.addStretch(1)
        if device.has(c2.SETTINGS_GET):
            self.read()

    def show_settings(self, s: c2.Settings) -> None:
        self.backlight.setValue(s.backlight)
        self.sound.setValue(s.sound)
        self.shutdown.setCurrentIndex(s.auto_shutdown if s.auto_shutdown <= 3 else -1)
        self.startup.setCurrentIndex(s.startup if s.startup <= 2 else -1)
        self.shown.setText("backlight %d, sound %d, auto shutdown %d, startup %d"
                           % (s.backlight, s.sound, s.auto_shutdown, s.startup))
        self.last = s

    last = None

    def read(self):
        dev = self.dev
        return self.run("SETTINGS_GET", lambda p: dev.settings_get(), self.show_settings)

    def apply(self):
        dev = self.dev
        args = (self.backlight.value(), self.sound.value(), self.shutdown.currentIndex(),
                self.startup.currentIndex())
        return self.run("SETTINGS_SET", lambda p: dev.settings_set(*args),
                        lambda s: (self.show_settings(s), self.say("in effect (RAM, not saved)")),
                        failed=lambda e: self.last is not None and self.show_settings(self.last))

    def save(self):
        dev = self.dev
        word = self.confirm("Save the settings",
                            "SETTINGS_SAVE: the meter writes its settings page (the same routine "
                            "as when it powers off) and restarts; its port goes away and comes "
                            "back (S2 4.8). The settings / calibration page is read into a backup "
                            "file first.")
        if word is None:
            return None
        return self.run("SETTINGS_SAVE", lambda p: dev.settings_save(word),
                        lambda r: self.say("saved; the meter restarted"), exclusive=True)


# S2 4.7: code -> functions per page (menu, meter, scope, generator, settings)
KEY_TABLE = {
    1: "power off (saves settings), every page",
    2: "meter: auto function; scope: autoset",
    4: "meter: DC/AC (or °C/°F); scope: other channel",
    5: "scope: next selection (V/div + timebase, offset, trigger level, cursors)",
    7: "meter: relative",
    9: "scope: screenshot to the drive",
    10: "scope: open / close the scope menu; generator, settings: back",
    11: "scope: selected value + (menu: item up); generator, settings: value up",
    12: "scope: selected value - (menu: item down); generator, settings: value down",
    13: "menu: item up; meter: function back; scope: timebase +; generator: digit left; "
        "settings: field edit",
    14: "menu: item down; meter: function next; scope: timebase -; generator: digit right; "
        "settings: field edit",
    15: "menu: open item; meter: hold; scope: run / stop (menu: enter / apply); generator: next "
        "field; settings: apply",
    16: "scope: auto-correction",
    17: "menu: meter page",
    18: "menu: scope page",
    19: "menu: generator page",
    22: "meter, scope, generator, settings: menu",
    23: "generator: output on / off",
}


def meter_meaning(text: str) -> str | None:
    """The part of a KEY_TABLE text that names the key's function on the meter page ("meter:
    hold" -> "hold"), or None when the table lists none."""
    for part in text.split(";"):
        if ":" not in part:
            continue
        pages, what = part.split(":", 1)
        if "meter" in [p.strip() for p in pages.split(",")]:
            return what.strip()
    return None


class C2KeysPage(Page):
    title = "Keys"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        card = Card("Keys")
        card.add(label("Key codes 1..15 are short presses, 16..23 long presses (S2 4.7). Key 1 "
                       "switches the meter off and saves its settings: backup and typed OK "
                       "first.", "hint", word_wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.buttons = {}
        for code in range(c2.KEY_MIN, c2.KEY_MAX + 1):
            b = self.control(QPushButton("%d%s" % (code, "…" if code == c2.KEY_POWER_OFF else "")),
                             c2.KEY)
            if code == c2.KEY_POWER_OFF:
                set_kind(b, "danger")
            b.setObjectName("key_%d" % code)
            b.setFixedWidth(54)
            b.clicked.connect(lambda _=False, c=code: self.press(c))
            lab = label(KEY_TABLE.get(code, "no function listed in S2 4.7"),
                        "label" if code in KEY_TABLE else "hint", word_wrap=True)
            r = code - 1
            grid.addWidget(b, r % 12, (r // 12) * 2)
            grid.addWidget(lab, r % 12, (r // 12) * 2 + 1)
            self.buttons[code] = b
        card.add(grid)
        answer_row = StatRow("Meter after the key", line=False)
        self.answer = answer_row.value
        card.add(answer_row)
        self.body.addWidget(card)

        # the Live page's row: the keys with a function on the meter page (key 1, power off on
        # every page, stays on the Keys card with its confirmation)
        self.live_buttons = {}
        for code, text in KEY_TABLE.items():
            what = meter_meaning(text)
            if what is None:
                continue
            k = self.control(KeyButton(what, "long press" if code >= LONG_PRESS_FROM else ""),
                             c2.KEY)
            k.setObjectName("live_key_%d" % code)
            k.setToolTip("key %d: %s" % (code, text))
            k.clicked.connect(lambda _=False, c=code: self.press(c))
            self.live_buttons[code] = k
        self.live_keys = KeyRow(list(self.live_buttons.values()))
        self.extras.append(self.live_keys)

    def press(self, code: int):
        dev = self.dev
        word = None
        if code == c2.KEY_POWER_OFF:
            word = self.confirm("Key 1: power off",
                                "Key 1 switches the meter off and it saves its settings (S2 4.7). "
                                "The settings / calibration page is read into a backup file "
                                "first.")
            if word is None:
                return None

        def done(st):
            self.ctx.publish_state(st)
            self.say("key %d sent" % code)

        return self.run("KEY %d" % code, lambda p: dev.key(code, word), done,
                        exclusive=code == c2.KEY_POWER_OFF)

    def on_state(self, st) -> None:
        if isinstance(st, c2.State):
            self.answer.setText("page %s; flags: %s" % (st.page_name,
                                                        ", ".join(c2.flag_names(st.flags)) or "none"))


class C2DiagnosticsPage(Page):
    title = "Diagnostics"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.out = mono_text()
        self.out.setObjectName("diag_out")
        self.out.setMinimumHeight(240)
        self.ping_text = QLineEdit("FNIRSI")
        self.ping_text.setMaximumWidth(200)
        self.ping_button = self._b("PING", c2.PING, self.ping)
        self.state_button = self._b("STATE", c2.STATE, self.state)
        self.page_combo = Segmented()
        for n in (0, 1, 2, 3):
            self.page_combo.addItem(c2.PAGE_NAME[n], n)
        self.page_button = self._b("PAGE", c2.PAGE, self.page)
        self.awake_on = self._b("KEEP_AWAKE on", c2.KEEP_AWAKE, lambda: self.keep_awake(True))
        self.awake_off = self._b("KEEP_AWAKE off", c2.KEEP_AWAKE, lambda: self.keep_awake(False))
        devc = Card("Device")
        devc.add(row("Bytes", self.ping_text, self.ping_button, self.state_button))
        devc.add(row("Page", self.page_combo, self.page_button))
        devc.add(row(self.awake_on, self.awake_off))
        top = QHBoxLayout()
        top.setSpacing(16)
        self.body.addLayout(top)
        top.addWidget(devc, 1)

        self.addr = QLineEdit("0x08000000")
        self.addr.setMaximumWidth(160)
        self.length = QSpinBox()
        self.length.setRange(1, 65536)
        self.length.setValue(256)
        self.cal_button = self._b("CAL_READ (settings page)", c2.CAL_READ, self.cal_read)
        self.mem_button = self._b("MEM_READ", c2.MEM_READ, self.mem_read)
        self.fault_button = self._b("Fault record", c2.MEM_READ, self.fault)
        self.flash_button = self._b("FLASH_READ (SPI flash)", c2.FLASH_READ, self.flash_read)
        self.flash_id_button = self._b("FLASH_ID", c2.FLASH_ID, self.flash_id)
        mem = Card("Memory")
        mem.add(row("Address", self.addr, "Length", self.length))
        flow_w = QWidget()
        flow = FlowLayout(flow_w, spacing=8)
        for b in (self.mem_button, self.fault_button, self.flash_button, self.flash_id_button,
                  self.cal_button):
            flow.addWidget(b)
        mem.add(flow_w)
        mem.add(label("MEM_READ: internal flash 0x08000000..0x080FFFFF and RAM "
                      "0x20037FB0..0x20037FFF; FLASH_READ: drive 3: from 0, drive 2: from "
                      "0x200000, at most 16 MB (S2 4.13).", "hint", word_wrap=True))
        top.addWidget(mem, 1)

        self.log_start = self._b("METER_LOG start", c2.METER_LOG, self.log_on)
        self.log_stop = self._b("METER_LOG stop", c2.METER_LOG, self.log_off)
        self.log_read = self._b("METER_LOG read", c2.METER_LOG, self.log_get)
        self.m1 = QSpinBox()
        self.m1.setRange(0, 255)
        self.m0 = QSpinBox()
        self.m0.setRange(0, 255)
        self.send_button = self._b("METER_SEND…", c2.METER_SEND, self.meter_send)
        set_kind(self.send_button, "danger")
        self.colour = QLineEdit("0x0000")
        self.colour.setMaximumWidth(120)
        self.colour_button = self._b("TEXT_COLOUR", c2.TEXT_COLOUR, self.text_colour)
        chip = Card("Multimeter chip and display")
        chip.add(row(self.log_start, self.log_stop, self.log_read))
        chip.add(row("m1", self.m1, "m0", self.m0, self.send_button))
        chip.add(row("RGB565 (0 default, 0xFFFF white)", self.colour, self.colour_button))
        self.body.addWidget(chip)
        out = Card("Output")
        out.add(self.out, 1)
        self.body.addWidget(out, 1)

    def _b(self, text, code, slot) -> QPushButton:
        b = self.control(QPushButton(text), code)
        b.clicked.connect(lambda _=False: slot())
        return b

    def _show(self, title: str, text: str = "") -> None:
        self.out.setPlainText("%s\n%s" % (title, text))
        self.say(title)

    def _addr(self):
        try:
            return int(self.addr.text(), 0)
        except ValueError:
            self.say("address: a number (0x... for hex)", error=True)
            return None

    def ping(self):
        dev = self.dev
        data = self.ping_text.text().encode("utf-8")[:500]
        return self.run("PING", lambda p: dev.ping(data),
                        lambda a: self._show("PING: %d bytes back%s" % (
                            len(a), ", the same" if a == data else ", DIFFERENT"), hexdump(a)))

    def state(self):
        dev = self.dev

        def done(st):
            self.ctx.publish_state(st)
            lines = ["length %d, page %s, flags %s, battery %d" % (
                st.length, st.page_name, ", ".join(c2.flag_names(st.flags)) or "none", st.battery),
                "dmm %r" % (st.dmm,), "timebase %d" % st.timebase, "ch1 %r" % (st.ch1,),
                "ch2 %r" % (st.ch2,), "trigger %r" % (st.trigger,), "gen %r" % (st.gen,),
                "time %d ms" % st.time_ms, "", hexdump(st.raw)]
            self._show("STATE", "\n".join(lines))

        return self.run("STATE", lambda p: dev.read_state(), done)

    def page(self):
        dev, n = self.dev, self.page_combo.currentData()
        return self.run("PAGE", lambda p: dev.page(n),
                        lambda st: (self.ctx.publish_state(st), self._show("PAGE: %s" % st.page_name)))

    def keep_awake(self, on: bool):
        dev = self.dev
        return self.run("KEEP_AWAKE", lambda p: dev.keep_awake(on),
                        lambda r: self._show("KEEP_AWAKE %s" % ("on" if on else "off")))

    def cal_read(self):
        dev = self.dev
        return self.run("CAL_READ", lambda p: dev.read_calibration_page(),
                        lambda d: self._show("CAL_READ: %d bytes of 0x%08X" % (len(d), c2.CAL_BASE),
                                             hexdump(d, c2.CAL_BASE)), exclusive=True)

    def mem_read(self):
        addr = self._addr()
        if addr is None:
            return None
        n, dev = self.length.value(), self.dev
        return self.run("MEM_READ", lambda p: dev.mem_read(addr, n),
                        lambda d: self._show("MEM_READ 0x%08X, %d bytes" % (addr, len(d)),
                                             hexdump(d, addr)), exclusive=n > 256)

    def fault(self):
        dev = self.dev

        def done(f):
            text = "\n".join("%s = 0x%08X" % (k, v) for k, v in vars(f).items())
            self._show("Fault record: %s" % ("valid" if f.valid else "no valid record (magic)"), text)

        return self.run("fault record", lambda p: dev.fault_record(), done)

    def flash_read(self):
        addr = self._addr()
        if addr is None:
            return None
        n, dev = self.length.value(), self.dev
        return self.run("FLASH_READ", lambda p: dev.flash_read(addr, n),
                        lambda d: self._show("FLASH_READ 0x%06X, %d bytes" % (addr, len(d)),
                                             hexdump(d, addr)), exclusive=n > 256)

    def flash_id(self):
        dev = self.dev
        return self.run("FLASH_ID", lambda p: dev.flash_id(),
                        lambda t: self._show("FLASH_ID: manufacturer 0x%02X, memory type 0x%02X, "
                                             "capacity 0x%02X" % t))

    def log_on(self):
        dev = self.dev
        return self.run("METER_LOG start", lambda p: dev.meter_log_start(),
                        lambda r: self._show("METER_LOG recording"))

    def log_off(self):
        dev = self.dev
        return self.run("METER_LOG stop", lambda p: dev.meter_log_stop(),
                        lambda r: self._show("METER_LOG stopped"))

    def log_get(self):
        dev = self.dev
        return self.run("METER_LOG read", lambda p: dev.meter_log_read(),
                        lambda d: self._show("METER_LOG: %d bytes" % len(d), hexdump(d)))

    def meter_send(self):
        m1, m0, dev = self.m1.value(), self.m0.value(), self.dev
        word = self.confirm("METER_SEND",
                            "The multimeter chip gets the frame AA 55 %02X %02X 00 00 00 00 00 "
                            "(m1+m0) (S2 4.13); only on the meter page. The settings / calibration "
                            "page is read into a backup file first." % (m1, m0))
        if word is None:
            return None
        return self.run("METER_SEND", lambda p: dev.meter_send(m1, m0, word),
                        lambda r: self._show("METER_SEND %02X %02X sent" % (m1, m0)),
                        exclusive=True)

    def text_colour(self):
        try:
            v = int(self.colour.text(), 0)
        except ValueError:
            self.say("colour: a number (0x... for hex)", error=True)
            return None
        dev = self.dev
        return self.run("TEXT_COLOUR", lambda p: dev.text_colour(v),
                        lambda r: self._show("TEXT_COLOUR 0x%04X" % v))
