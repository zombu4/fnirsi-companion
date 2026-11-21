"""Events, Preferences and Screenshot.

Events       every meter event (LC1020E SL 10, 2C53T S2 4.12) and every program note as it arrives,
             and the lines of the previous runs from the event log (core.eventlog).
Preferences  the keys of core.settings, each stored as soon as it is changed.
Screenshot   the meter's screen (SCREENSHOT, both models; the port is held while it runs), shown
             and saved as PNG.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QListWidget,
                               QProgressBar, QPushButton, QScrollArea)

from ...core import eventlog, settings as settings_mod
from ..base import Page, group, row


class EventsPage(Page):
    title = "Events"
    MAX_LINES = 5000

    def __init__(self, ctx, previous: list[str] | None = None):
        super().__init__(ctx)
        self.list = QListWidget()
        self.list.setObjectName("events")
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.list.clear)
        self.body.addWidget(self.list, 1)
        self.body.addLayout(row(self.clear_button))
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


THEME_TEXT = {"system": "System", "light": "Light", "dark": "Dark"}
CHECK_TEXT = {"connect": "On connect", "daily": "Daily", "never": "Never"}


class PreferencesPage(Page):
    title = "Preferences"

    def __init__(self, ctx):
        super().__init__(ctx)
        s = ctx.settings
        form = QFormLayout()
        self.rec_folder = QLineEdit(s.recordings_folder())
        self.rec_browse = QPushButton("Choose…")
        self.rec_browse.clicked.connect(lambda: self._browse("recordings_folder", self.rec_folder))
        self.rec_folder.editingFinished.connect(
            lambda: self._set_folder("recordings_folder", self.rec_folder))
        form.addRow("Recordings folder", row(self.rec_folder, self.rec_browse, stretch=False))
        self.bak_folder = QLineEdit(s.backups_folder())
        self.bak_browse = QPushButton("Choose…")
        self.bak_browse.clicked.connect(lambda: self._browse("backups_folder", self.bak_folder))
        self.bak_folder.editingFinished.connect(
            lambda: self._set_folder("backups_folder", self.bak_folder))
        form.addRow("Backups folder", row(self.bak_folder, self.bak_browse, stretch=False))

        self.csv = QComboBox()
        self.csv.addItem("1.5 , separated", ",")
        self.csv.addItem("1,5 ; separated", ";")
        self.csv.setCurrentIndex(settings_mod.CSV_SEPARATORS.index(s.get("csv_separator")))
        self.csv.currentIndexChanged.connect(lambda: self._set("csv_separator", self.csv.currentData()))
        form.addRow("CSV format", self.csv)

        self.switch = QCheckBox("Switch to a meter plugged in while another one is active")
        self.switch.setChecked(s.get("switch_to_new_meter"))
        self.switch.toggled.connect(lambda v: self._set("switch_to_new_meter", v))
        form.addRow("New meter", self.switch)

        self.check = QComboBox()
        for k in settings_mod.UPDATE_CHECKS:
            self.check.addItem(CHECK_TEXT[k], k)
        self.check.setCurrentIndex(settings_mod.UPDATE_CHECKS.index(s.get("update_check")))
        self.check.currentIndexChanged.connect(lambda: self._set("update_check", self.check.currentData()))
        form.addRow("Release check", self.check)

        self.theme = QComboBox()
        for k in settings_mod.THEMES:
            self.theme.addItem(THEME_TEXT[k], k)
        self.theme.setCurrentIndex(settings_mod.THEMES.index(s.get("theme")))
        self.theme.currentIndexChanged.connect(self._theme)
        form.addRow("Theme", self.theme)

        self.sleep = QCheckBox("Let the 2C53T switch itself off when idle (DTR is dropped after "
                               "10 minutes without user action)")
        self.sleep.setChecked(s.get("let_2c53t_sleep"))
        self.sleep.toggled.connect(self._sleep)
        form.addRow("2C53T", self.sleep)

        self.reset_button = QPushButton("Reset all preferences")
        self.reset_button.clicked.connect(self.reset_all)
        form.addRow(self.reset_button)
        self.body.addWidget(group("Preferences (stored in %s)" % s.path, form))
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
        self.csv.setCurrentIndex(settings_mod.CSV_SEPARATORS.index(s.get("csv_separator")))
        self.check.setCurrentIndex(settings_mod.UPDATE_CHECKS.index(s.get("update_check")))
        self.theme.setCurrentIndex(settings_mod.THEMES.index(s.get("theme")))
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
        self.view = QLabel("no screenshot yet")
        self.view.setAlignment(Qt.AlignCenter)
        area = QScrollArea()
        area.setWidget(self.view)
        area.setWidgetResizable(True)
        self.body.addWidget(area, 1)
        self.capture_button = self.control(QPushButton("Capture"), screenshot_code)
        self.capture_button.clicked.connect(self.capture)
        self.save_button = self.control(QPushButton("Save PNG…"))
        self.save_button.clicked.connect(self.save)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.body.addLayout(row(self.capture_button, self.save_button, self.progress))

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
