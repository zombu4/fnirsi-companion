"""Events, Preferences and Screenshot.

Events       every meter event (LC1020E SL 10, 2C53T S2 4.12) and every program note as it arrives,
             and the lines of the previous runs from the event log (core.eventlog); the Events
             card of the Diagnostics view.
Preferences  the keys of core.settings, each stored as soon as it is changed (the Settings view:
             Folders, Recording, Connection, Updates, Appearance).
Screenshot   the meter's screen (SCREENSHOT, both models; the port is held while it runs), shown
             and saved as PNG.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QLabel, QLineEdit, QListWidget,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout)

from ...core import eventlog, settings as settings_mod
from ..base import Page, row
from ..widgets import Card, Segmented, label, set_kind

EVENTS_MAX_H = 320           # the event list scrolls inside its card above this height


class EventsPage(Page):
    title = "Events"
    MAX_LINES = 5000

    def __init__(self, ctx, previous: list[str] | None = None):
        super().__init__(ctx)
        card = Card("Events")
        self.list = QListWidget()
        self.list.setObjectName("events")
        self.list.setMinimumHeight(200)
        self.list.setMaximumHeight(EVENTS_MAX_H)
        self.list.setWordWrap(False)
        self.clear_button = set_kind(QPushButton("Clear"), "small")
        self.clear_button.clicked.connect(self.list.clear)
        card.add_right(self.clear_button)
        card.add(self.list)
        card.add(label("Meter events and the program's notes of this run, after the lines of the "
                       "earlier runs from the event log.", "hint", word_wrap=True))
        self.body.addWidget(card)
        if previous:
            self.list.addItem("--- earlier runs (event log) ---")
            self.list.addItems(previous)
            self.list.addItem("--- this run ---")

    def add(self, text: str) -> None:
        self.list.addItem(eventlog.line(time.time(), text))
        while self.list.count() > self.MAX_LINES:
            self.list.takeItem(0)
        self.list.scrollToBottom()

    def lines(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def say(self, text: str, error: bool = False, log: bool = True) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))


THEME_TEXT = {"system": "System", "light": "Light", "dark": "Dark"}
THEME_ORDER = ("system", "dark", "light")          # DESIGN_UI 4: System / Dark / Light
CHECK_TEXT = {"connect": "On connect", "daily": "Daily", "never": "Never"}


class PreferencesPage(Page):
    title = "Preferences"

    def __init__(self, ctx):
        super().__init__(ctx)
        s = ctx.settings
        grid = QGridLayout()
        grid.setSpacing(16)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)

        folders = Card("Folders")
        self.rec_folder = QLineEdit(s.recordings_folder())
        self.rec_browse = QPushButton("Choose…")
        self.rec_browse.clicked.connect(lambda: self._browse("recordings_folder", self.rec_folder))
        self.rec_folder.editingFinished.connect(
            lambda: self._set_folder("recordings_folder", self.rec_folder))
        self.bak_folder = QLineEdit(s.backups_folder())
        self.bak_browse = QPushButton("Choose…")
        self.bak_browse.clicked.connect(lambda: self._browse("backups_folder", self.bak_folder))
        self.bak_folder.editingFinished.connect(
            lambda: self._set_folder("backups_folder", self.bak_folder))
        fg = QGridLayout()
        fg.setHorizontalSpacing(12)
        fg.setVerticalSpacing(10)
        fg.setColumnStretch(1, 1)
        for r, (name, edit, browse) in enumerate((("Recordings", self.rec_folder, self.rec_browse),
                                                  ("Backups", self.bak_folder, self.bak_browse))):
            fg.addWidget(label(name), r, 0)
            fg.addWidget(edit, r, 1)
            fg.addWidget(browse, r, 2)
        folders.add(fg)
        folders.add(label("Preferences are stored in %s." % s.path, "hint", word_wrap=True))
        grid.addWidget(folders, 0, 0, 1, 2)

        rec = Card("Recording")
        self.csv = Segmented()
        self.csv.addItem("1.5 , separated", ",")
        self.csv.addItem("1,5 ; separated", ";")
        self.csv.setCurrentIndex(self.csv.findData(s.get("csv_separator")))
        self.csv.currentIndexChanged.connect(lambda: self._set("csv_separator", self.csv.currentData()))
        rec.add(row("CSV format", self.csv))
        rec.add(label("Decimal mark and column separator of the recordings and trend exports.",
                      "hint", word_wrap=True))
        rec.body.addStretch(1)
        grid.addWidget(rec, 1, 0)

        upd = Card("Updates")
        self.check = Segmented()
        for k in settings_mod.UPDATE_CHECKS:
            self.check.addItem(CHECK_TEXT[k], k)
        self.check.setCurrentIndex(self.check.findData(s.get("update_check")))
        self.check.currentIndexChanged.connect(lambda: self._set("update_check", self.check.currentData()))
        upd.add(row("Release check", self.check))
        upd.add(label("When the program asks GitHub for the meter's releases.", "hint",
                      word_wrap=True))
        upd.body.addStretch(1)
        grid.addWidget(upd, 1, 1)

        conn = Card("Connection")
        self.switch = QCheckBox("Switch to a meter plugged in while another one is active")
        self.switch.setChecked(s.get("switch_to_new_meter"))
        self.switch.toggled.connect(lambda v: self._set("switch_to_new_meter", v))
        conn.add(self.switch)
        self.sleep = QCheckBox("Let the 2C53T switch itself off when idle")
        self.sleep.setChecked(s.get("let_2c53t_sleep"))
        self.sleep.toggled.connect(self._sleep)
        conn.add(self.sleep)
        conn.add(label("2C53T: DTR is dropped after 10 minutes without user action, after a backup "
                       "of its settings / calibration page.", "hint", word_wrap=True))
        conn.body.addStretch(1)
        grid.addWidget(conn, 2, 0)

        app = Card("Appearance")
        self.theme = Segmented()
        for k in THEME_ORDER:
            self.theme.addItem(THEME_TEXT[k], k)
        self.theme.setCurrentIndex(self.theme.findData(s.get("theme")))
        self.theme.currentIndexChanged.connect(self._theme)
        app.add(row("Theme", self.theme))
        app.add(label("System follows the light or dark setting of the computer.", "hint",
                      word_wrap=True))
        app.body.addStretch(1)
        grid.addWidget(app, 2, 1)

        self.reset_button = QPushButton("Reset all preferences")
        self.reset_button.clicked.connect(self.reset_all)
        self.body.addLayout(row(self.reset_button))
        if s.problem:
            self.say(s.problem, error=True)
        self.body.addStretch(1)

    def _set(self, key, value) -> bool:
        try:
            self.ctx.settings.set(key, value)
        except (ValueError, OSError) as e:
            self.say("%s: %s" % (key, e), error=True)
            return False
        self.say("%s = %r" % (key, value))
        self.ctx.preferences_changed(key)
        return True

    def _set_folder(self, key, edit) -> None:
        text = edit.text().strip()
        if self._set(key, text or None):
            edit.setText(getattr(self.ctx.settings, key)())

    def _browse(self, key, edit) -> None:
        folder = self.ctx.ask_folder("Choose the folder", edit.text())
        if folder:
            edit.setText(folder)
            self._set_folder(key, edit)

    def _sleep(self, on: bool) -> None:
        if on and self.ctx.confirm(
                "Let the 2C53T switch itself off",
                "After 10 minutes without user action the program reads the 2C53T's settings / "
                "calibration page into a backup file and then drops DTR, which no longer holds the "
                "meter's auto power-off (DESIGN 13.1, 13.23): the meter may then switch itself "
                "off.") is None:
            self.sleep.blockSignals(True)
            self.sleep.setChecked(False)
            self.sleep.blockSignals(False)
            self.say("not confirmed: DTR stays set")
            return
        self._set("let_2c53t_sleep", on)

    def _theme(self) -> None:
        if self._set("theme", self.theme.currentData()):
            self.ctx.apply_theme()

    def reset_all(self) -> None:
        try:
            self.ctx.settings.reset()
        except OSError as e:
            self.say("reset: %s" % e, error=True)
            return
        s = self.ctx.settings
        for w in (self.csv, self.check, self.theme, self.switch, self.sleep):
            w.blockSignals(True)
        self.csv.setCurrentIndex(self.csv.findData(s.get("csv_separator")))
        self.check.setCurrentIndex(self.check.findData(s.get("update_check")))
        self.theme.setCurrentIndex(self.theme.findData(s.get("theme")))
        self.switch.setChecked(s.get("switch_to_new_meter"))
        self.sleep.setChecked(s.get("let_2c53t_sleep"))
        for w in (self.csv, self.check, self.theme, self.switch, self.sleep):
            w.blockSignals(False)
        self.rec_folder.setText(s.recordings_folder())
        self.bak_folder.setText(s.backups_folder())
        self.ctx.apply_theme()
        self.say("all preferences back to their defaults")


class ScreenshotPage(Page):
    title = "Screenshot"

    def __init__(self, ctx, device, screenshot_code: int):
        super().__init__(ctx, device)
        self.image = None
        card = Card("Screen")
        self.capture_button = self.control(set_kind(QPushButton("Capture"), "primary"),
                                           screenshot_code)
        self.capture_button.clicked.connect(self.capture)
        self.save_button = self.control(QPushButton("Save PNG…"))
        self.save_button.clicked.connect(self.save)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(220)
        card.add(row(self.capture_button, self.save_button, self.progress))
        frame = QFrame()
        frame.setObjectName("screenframe")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 12, 12, 12)
        self.view = QLabel("no screenshot yet")
        self.view.setProperty("role", "hint")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumHeight(240)
        area = QScrollArea()
        area.setWidget(self.view)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setMinimumHeight(500)
        fl.addWidget(area)
        card.add(frame, 1)
        card.add(label("The meter's screen at twice its size; the port is held while it is read.",
                       "hint", word_wrap=True))
        self.body.addWidget(card, 1)

    def extra_enabled(self, widget) -> bool:
        return widget is not getattr(self, "save_button", None) or self.image is not None

    def capture(self):
        dev = self.dev
        self.progress.setValue(0)
        return self.run("screenshot", lambda p: dev.screenshot(p), self._shown, exclusive=True,
                        progress=lambda f, t: self.progress.setValue(int(f * 100)))

    def _shown(self, img) -> None:
        self.image = img
        self.view.setPixmap(QPixmap.fromImage(img).scaled(img.width() * 2, img.height() * 2))
        self.progress.setValue(100)
        self.refresh_controls()
        self.say("screenshot %dx%d" % (img.width(), img.height()))

    def save(self) -> None:
        if self.image is None:
            return
        name = "%s-screen-%s.png" % (self.dev.model, time.strftime("%Y-%m-%d_%H-%M-%S"))
        path = self.ctx.ask_save("Save screenshot", name, "PNG image (*.png)")
        if path:
            self.save_png(path)

    def save_png(self, path: str) -> bool:
        ok = self.image is not None and self.image.save(path, "PNG")
        self.say(("saved %s" % path) if ok else ("could not save %s" % path), error=not ok)
        return ok
