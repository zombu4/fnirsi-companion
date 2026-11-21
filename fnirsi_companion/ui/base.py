"""What every page has: the context it works in, its command controls and their enabled state,
and the way it runs device calls as jobs.

A command control is registered with the command codes it sends. It is disabled while an exclusive
job holds the port (DESIGN 13.11) and greyed out with the reason when the meter's INFO has the
capability bit of one of its commands clear (DESIGN 13.5).

The context (the main window) offers:
  jobs                      the JobRunner
  confirm(title, text)      the typed word of the confirmation dialog, or None
  ask_open(title, filter)   a file to read, or None;  ask_save(title, name, filter)  a file to write
  ask_folder(title, start)  a folder, or None
  settings                  core.settings.Settings
  note(text)                a line in the Events page, the event log and the status bar
  publish_state(record)     hands a STATE record that a command answered to every page
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import style
from .widgets import Card, Segmented
from ..devices.base import (AnswerTimeout, Cancelled, CapabilityError, IdentifyError, PortLost,
                            SafetyRefused, StatusError)

STATUS_NAMES = {          # status codes common to both protocols (LC SL 3, 2C S2 3)
    1: "unknown command", 2: "bad length or argument", 3: "not allowed in the current state",
    4: "busy", 6: "refused for safety", 7: "CRC or length mismatch",
}


def describe_error(exc: BaseException, status_names: dict | None = None) -> str:
    """One line for a failed device call."""
    if isinstance(exc, StatusError):
        names = status_names or STATUS_NAMES
        text = "the meter answered status %d (%s)" % (exc.status, names.get(exc.status, "?"))
        rec = getattr(exc, "record", None)
        if rec is not None and hasattr(rec, "describe"):
            text += ": " + rec.describe()
        detail = getattr(exc, "detail", None)
        if detail:
            text += ": " + detail
        return text
    if isinstance(exc, AnswerTimeout):
        return "no answer in time: %s" % exc
    if isinstance(exc, PortLost):
        return "the port is gone: %s" % exc
    if isinstance(exc, CapabilityError):
        return "not offered by this meter: %s" % exc
    if isinstance(exc, Cancelled):
        return "cancelled: %s" % exc
    if isinstance(exc, SafetyRefused):
        return "refused: %s" % exc
    if isinstance(exc, IdentifyError):
        return str(exc)
    return "%s: %s" % (type(exc).__name__, exc)


def hexdump(data: bytes, base: int = 0) -> str:
    """16 bytes per line: address, hex, ASCII."""
    out = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        out.append("%08X  %-47s  %s" % (base + i, " ".join("%02X" % b for b in row),
                                        "".join(chr(b) if 32 <= b < 127 else "." for b in row)))
    return "\n".join(out)


def mono_text() -> QPlainTextEdit:
    """A read-only text box in the mono font (the outputs of the diagnostics)."""
    t = QPlainTextEdit()
    t.setReadOnly(True)
    t.setFont(style.mono(12))
    return t


def group(title: str, layout) -> Card:
    """A card with the title and the layout as its body (DESIGN_UI 3: cards replace every group
    box)."""
    c = Card(title)
    c.add(layout)
    return c


def row(*widgets, stretch: bool = True) -> QHBoxLayout:
    """A row of widgets; a string becomes a label in ink-2."""
    h = QHBoxLayout()
    h.setSpacing(8)
    for w in widgets:
        if isinstance(w, str):
            lab = QLabel(w)
            lab.setProperty("role", "label")
            h.addWidget(lab)
        else:
            h.addWidget(w)
    if stretch:
        h.addStretch(1)
    return h


class Page(QWidget):
    """Base of every page. `device` is the Device whose commands the page sends (None for the
    pages that need no meter). Subclasses build their widgets in __init__ and override on_state /
    on_event / on_shown / on_hidden as they need."""

    title = ""
    status_names: dict | None = None

    def __init__(self, ctx, device=None):
        super().__init__()
        self.ctx = ctx
        self.dev = device
        self._controls: list[tuple[QWidget, tuple]] = []
        self._busy = False
        self.retired = False
        self._dirty: dict[str, bool] = {}
        self.extras: list[QWidget] = []     # widgets of the page placed on another view
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(8)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        self.outer.addLayout(self.body, 1)
        self.status = QLabel("")
        self.status.setObjectName("page_status")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.status.hide()                  # shown once the page has something to say
        self.outer.addWidget(self.status)

    # -- controls -----------------------------------------------------------------------------
    def control(self, widget: QWidget, *codes: int) -> QWidget:
        """Registers a widget that sends the commands `codes`; returns it."""
        self._controls.append((widget, codes))
        self._apply(widget, codes)
        return widget

    def _missing(self, codes) -> list[int]:
        if self.dev is None:
            return []
        return [c for c in codes if not self.dev.has(c)]

    def _apply(self, widget: QWidget, codes) -> None:
        missing = self._missing(codes)
        widget.setEnabled(not self._busy and not missing and self.extra_enabled(widget))
        if missing:
            names = []
            for c in missing:
                cmd = self.dev.COMMANDS.get(c)
                names.append("%s (capability bit %s)" % (cmd.name, cmd.cap_bit) if cmd
                             else "0x%02X" % c)
            widget.setToolTip("Not offered by this meter: %s clear in its INFO" % ", ".join(names))

    def extra_enabled(self, widget: QWidget) -> bool:
        """A page's own condition for a control (for example a selection); default True."""
        return True

    def refresh_controls(self) -> None:
        for w, codes in self._controls:
            self._apply(w, codes)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.refresh_controls()

    @property
    def busy(self) -> bool:
        return self._busy

    # -- forms the user edits -----------------------------------------------------------------
    def edits(self, key: str, *widgets) -> None:
        """A form whose editors the meter's records fill only while the user has not changed
        them since the last answer (a 200 ms stream would else overwrite what is typed).
        The labels next to the form always show the meter's values."""
        from PySide6.QtWidgets import QAbstractSpinBox, QCheckBox, QComboBox
        self._dirty.setdefault(key, False)
        for w in widgets:
            if isinstance(w, QCheckBox):
                sig = w.toggled
            elif isinstance(w, (QComboBox, Segmented)):
                sig = w.currentIndexChanged
            elif isinstance(w, QAbstractSpinBox):
                sig = w.valueChanged
            else:
                raise TypeError("no change signal known for %r" % w)
            sig.connect(lambda *_a, k=key: self._dirty.__setitem__(k, True))

    def dirty(self, key: str) -> bool:
        return self._dirty.get(key, False)

    def clean(self, key: str) -> None:
        self._dirty[key] = False

    @staticmethod
    def quietly(widget, setter, value) -> None:
        """Sets a widget's value without its change signal (a value the meter answered)."""
        widget.blockSignals(True)
        setter(value)
        widget.blockSignals(False)

    # -- jobs ---------------------------------------------------------------------------------
    def run(self, what: str, fn, done=None, *, exclusive: bool = False, failed=None,
            progress=None, quiet: bool = False):
        """Runs fn(progress) as a job; done(result) in the GUI thread; errors go to the page's
        status line (and the events unless quiet) and then to failed(exc). Returns the Job, or
        None when it was refused."""
        from .jobs import JobRefused

        def on_error(exc):
            if self.retired:
                return                   # the page was removed while the job ran
            self.say("%s failed: %s" % (what, describe_error(exc, self.status_names)), error=True,
                     log=not quiet)
            if failed is not None:
                failed(exc)

        def on_result(value):
            if not self.retired and done is not None:
                done(value)

        def on_progress(frac, text):
            if not self.retired and progress is not None:
                progress(frac, text)

        try:
            return self.ctx.jobs.submit(fn, what=what, exclusive=exclusive, on_result=on_result,
                                        on_error=on_error, on_progress=on_progress)
        except JobRefused as e:
            self.say(str(e), error=True)
            return None

    def say(self, text: str, error: bool = False, log: bool = True) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status.setProperty("error", bool(error))     # the sheet colours an error in bad
        style.repolish(self.status)
        if log:
            self.ctx.note("%s: %s" % (self.title, text))

    def confirm(self, title: str, consequence: str) -> str | None:
        word = self.ctx.confirm(title, consequence)
        if word is None:
            self.say("%s: not confirmed, nothing was sent" % title)
        return word

    # -- feeds from the window ----------------------------------------------------------------
    def on_state(self, state) -> None:
        """A STATE record of the device (stream or an answer)."""

    def on_event(self, event) -> None:
        """An event record of the device."""

    def on_shown(self) -> None:
        """The page became the visible tab."""

    def on_hidden(self) -> None:
        """Another tab became visible."""

    def stop(self) -> None:
        """The page is removed: stop its timers."""

    def retire(self) -> None:
        """The window removes the page: its jobs' callbacks are dropped from now on (they would
        hand an old meter's records to the new one's pages or touch deleted widgets)."""
        self.retired = True
        self.stop()
