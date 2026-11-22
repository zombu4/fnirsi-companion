"""The look of the program: colour tokens, fonts, the application style sheet, the palette and the
theming of the pyqtgraph plots (DESIGN_UI sections 1, 3 and 6).

One token dict per theme (dark, light); every colour the program paints comes from the dict that is
current. apply(app, theme) builds the whole style sheet from the dict, sets it and the palette on
the application (Fusion style on every system, so the sheet looks the same on macOS, Windows and
Linux), re-themes every registered plot and calls the registered theme listeners, then repaints
the widgets that paint themselves (they read the tokens in their paintEvent).

"system" follows QStyleHints.colorScheme(): Dark gives the dark tokens, anything else the light
ones. The main window calls apply() again on QStyleHints.colorSchemeChanged while the setting is
"system".
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import weakref

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QGuiApplication, QImage, QPainter,
                           QPalette, QPen)
from PySide6.QtWidgets import QApplication

# ----------------------------------------------------------------------------------------------
# tokens (DESIGN_UI section 1; the cursor colours of section 6 item 9)
# ----------------------------------------------------------------------------------------------


def _c(text: str, alpha: float | None = None) -> QColor:
    """A QColor from "#rrggbb", with an alpha 0..1 when given."""
    c = QColor(text)
    if alpha is not None:
        c.setAlphaF(alpha)
    return c


DARK = {
    "body": _c("#121416"), "panel": _c("#1a1d20"), "panel-2": _c("#212529"),
    "line": _c("#2b3035"), "line-2": _c("#3a4047"),
    "ink": _c("#eef0f2"), "ink-2": _c("#a7aeb6"), "ink-3": _c("#6b737c"),
    "hol": _c("#ffc20e"), "hol-ink": _c("#1a1400"), "hol-soft": _c("#ffc20e", .12),
    "hol-icon": _c("#ffc20e"),
    "trace": _c("#ffc20e"), "ok": _c("#3ecf8e"), "bad": _c("#ff6b5b"),
    "ok-soft": _c("#3ecf8e", .14), "bad-soft": _c("#ff6b5b", .14),
    "lcd": _c("#0d1210"), "lcd-hi": _c("#141b18"), "seg": _c("#f0b93a"),
    "seg-off": _c("#f0b93a", .06),
    "seg-pill": _c("#1a1d20"),                 # selected option of a Segmented control
    "cursor-a": _c("#4cc3ff"), "cursor-b": _c("#ff8a5c"),
}

LIGHT = {
    "body": _c("#e9e7e1"), "panel": _c("#f7f6f2"), "panel-2": _c("#efede7"),
    "line": _c("#dcd9d0"), "line-2": _c("#c4c0b4"),
    "ink": _c("#15171a"), "ink-2": _c("#4a4f56"), "ink-3": _c("#74787f"),
    "hol": _c("#f5b800"), "hol-ink": _c("#1a1400"), "hol-soft": _c("#f5b800", .20),
    "hol-icon": _c("#8a6500"),                 # the reference's darker holster yellow for strokes
    "trace": _c("#d9a000"), "ok": _c("#138a55"), "bad": _c("#c8321f"),
    "ok-soft": _c("#138a55", .14), "bad-soft": _c("#c8321f", .12),
    "lcd": _c("#e4e2d8"), "lcd-hi": _c("#eeede6"), "seg": _c("#15171a"),
    "seg-off": _c("#15171a", .035),
    "seg-pill": _c("#ffffff"),
    "cursor-a": _c("#0a7bbd"), "cursor-b": _c("#c8541f"),
}

THEMES = {"dark": DARK, "light": LIGHT}
MONO_FAMILIES = ["SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono"]
_mono_installed: list = []


def mono_families() -> list:
    """MONO_FAMILIES that this computer has, in their order (asking Qt for a family it does not
    have makes it build its whole alias table, about 60 ms, and log a warning; "SF Mono" is not an
    installed family on every macOS). Falls back to the full list when none is installed."""
    if not _mono_installed:
        have = set(QFontDatabase.families())
        _mono_installed.extend([f for f in MONO_FAMILIES if f in have] or MONO_FAMILIES)
    return _mono_installed

_state = {"name": "dark"}
_plots: "weakref.WeakSet" = weakref.WeakSet()
_listeners: list = []


def name() -> str:
    """"dark" or "light": the tokens in use."""
    return _state["name"]


def is_dark() -> bool:
    return _state["name"] == "dark"


def tokens() -> dict:
    """The token dict in use (QColor values)."""
    return THEMES[_state["name"]]


def color(token: str) -> QColor:
    """A copy of one token's colour."""
    return QColor(tokens()[token])


def qss(c: QColor) -> str:
    """A colour for the style sheet: rgba(r,g,b,A) with A in 0..255 (DESIGN_UI 6.8)."""
    return "rgba(%d,%d,%d,%d)" % (c.red(), c.green(), c.blue(), c.alpha())


# ----------------------------------------------------------------------------------------------
# fonts (DESIGN_UI section 1 and 6.4: sizes in px, tabular digits from the mono family)
# ----------------------------------------------------------------------------------------------

def mono(px: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The mono font (values, versions, counters) at px pixels."""
    f = QFont()
    f.setFamilies(mono_families())
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def sans(px: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The system UI font at px pixels."""
    f = QFont(QGuiApplication.font())
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


# ----------------------------------------------------------------------------------------------
# the small arrows and the check mark of the sheet: painted with QPainter into PNG files of a
# temporary folder of this run (a style sheet takes images only as files), removed at exit
# ----------------------------------------------------------------------------------------------

_icon_dir: list = []


def _folder() -> str:
    if not _icon_dir:
        d = tempfile.mkdtemp(prefix="fnirsi-companion-ui-")
        atexit.register(shutil.rmtree, d, True)
        _icon_dir.append(d)
    return _icon_dir[0]


def _paint_png(path: str, w: int, h: int, points, colour: QColor, width: float) -> None:
    """A polyline in `colour` on a transparent image of w x h logical px, at twice the size
    (the sheet scales it down, so it stays sharp on high-density screens)."""
    img = QImage(w * 2, h * 2, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(colour, width * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(x * 2, y * 2) for x, y in points])
    p.end()
    img.save(path, "PNG")


def icon_files(t: dict, theme: str) -> dict:
    """Paths of the arrow and check-mark images in the colours of token dict t."""
    d = _folder()
    out = {}
    for key, (w, h, pts, tok, width) in {
            "down": (10, 6, [(1, 1), (5, 5), (9, 1)], "ink-2", 1.6),
            "down-off": (10, 6, [(1, 1), (5, 5), (9, 1)], "ink-3", 1.6),
            "up": (10, 6, [(1, 5), (5, 1), (9, 5)], "ink-2", 1.6),
            "check": (12, 10, [(1.5, 5), (4.5, 8), (10.5, 2)], "hol-ink", 2.0)}.items():
        path = os.path.join(d, "%s-%s.png" % (key, theme))
        if not os.path.exists(path):
            _paint_png(path, w, h, pts, t[tok], width)
        out[key] = path.replace(os.sep, "/")
    return out


# ----------------------------------------------------------------------------------------------
# the style sheet
# ----------------------------------------------------------------------------------------------

def stylesheet(t: dict, theme: str = "dark") -> str:
    """The application style sheet built from the token dict t."""
    c = {k: qss(v) for k, v in t.items()}
    c.update({"icon-" + k: v for k, v in icon_files(t, theme).items()})
    mono_family = ", ".join('"%s"' % f for f in mono_families())
    return """
QWidget { color: %(ink)s; font-size: 13px; }
QMainWindow, QDialog, QMessageBox { background: %(body)s; }
QWidget#central { background: %(body)s; }
QScrollArea, QScrollArea > QWidget > QWidget#viewcontent { background: transparent; border: 0; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QStackedWidget { background: transparent; }

/* frame of the window */
QWidget#sidebar { background: %(panel)s; border-right: 1px solid %(line)s; }
QWidget#navlist { background: transparent; }
QWidget#header { background: %(body)s; border-bottom: 1px solid %(line)s; }
QLabel#title { font-size: 17px; }
QLabel#bar_status { color: %(ink-3)s; font-size: 12px; }
QWidget#statusline { background: %(body)s; border-top: 1px solid %(line)s; }
QLabel#status_message { color: %(ink-2)s; font-family: %(mono)s; font-size: 11px; }
QLabel#link_stats, QLabel#dtr { color: %(ink-3)s; font-family: %(mono)s; font-size: 11px; }

/* cards */
QFrame#card { background: %(panel)s; border: 1px solid %(line)s; border-radius: 14px; }
QLabel#cardTitle { color: %(ink)s; font-size: 14px; font-weight: 600; }
QLabel[role="label"] { color: %(ink-2)s; }
QLabel[role="hint"] { color: %(ink-3)s; font-size: 12px; }
QLabel[role="value"] { font-family: %(mono)s; }
QLabel[role="mono"] { font-family: %(mono)s; font-size: 12px; color: %(ink-2)s; }
QLabel[role="caption"] { color: %(ink-3)s; font-size: 12px; }
QLabel[role="big"] { font-family: %(mono)s; font-size: 28px; font-weight: 300; }
QLabel[role="name"] { font-size: 18px; font-weight: 700; }
QLabel#page_status { color: %(ink-3)s; font-size: 12px; }
QLabel#page_status[error="true"] { color: %(bad)s; }
QLabel[verdict="PASS"] { color: %(ok)s; }
QLabel[verdict="HI"], QLabel[verdict="LO"] { color: %(bad)s; }
QLabel[badge] { border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600; }
QLabel[badge="newer"] { background: %(hol-soft)s; color: %(hol-icon)s; }
QLabel[badge="installed"] { background: %(ok-soft)s; color: %(ok)s; }
QLabel[badge="older"], QLabel[badge="unknown"] { background: %(panel-2)s; color: %(ink-3)s; }
QFrame#screenframe { background: %(lcd)s; border: 1px solid %(line)s; border-radius: 12px; }
QFrame#screenframe QScrollArea, QFrame#screenframe QLabel,
QFrame#screenframe QWidget#qt_scrollarea_viewport { background: transparent; border: 0; }

/* buttons (DESIGN_UI section 3) */
QPushButton { background: %(panel-2)s; color: %(ink)s; border: 1px solid %(line-2)s;
              border-radius: 8px; padding: 0 12px; min-height: 28px; font-weight: 600; }
QPushButton:hover { border-color: %(ink-3)s; }
QPushButton:pressed { background: %(line)s; }
QPushButton:checked { background: %(hol-soft)s; border-color: %(hol)s; }
QPushButton:disabled { color: %(ink-3)s; border-color: %(line)s; }
QPushButton[kind="primary"] { background: %(hol)s; color: %(hol-ink)s; border-color: %(hol)s; }
QPushButton[kind="primary"]:hover { border-color: %(hol-ink)s; }
QPushButton[kind="primary"]:disabled { background: %(hol-soft)s; color: %(ink-3)s;
                                       border-color: %(line)s; }
QPushButton[kind="danger"] { color: %(bad)s; }
QPushButton[kind="danger"]:disabled { color: %(ink-3)s; }
QPushButton[kind="small"] { min-height: 22px; padding: 0 9px; font-size: 12px; }

/* inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: %(panel-2)s; color: %(ink)s; border: 1px solid %(line-2)s; border-radius: 8px;
    padding: 0 8px; min-height: 28px; selection-background-color: %(hol)s;
    selection-color: %(hol-ink)s; }
QLineEdit, QSpinBox, QDoubleSpinBox { font-family: %(mono)s; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: %(hol)s; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    color: %(ink-3)s; border-color: %(line)s; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
                       width: 22px; border: 0; }
QComboBox::down-arrow { image: url(%(icon-down)s); width: 10px; height: 6px; }
QComboBox::down-arrow:disabled { image: url(%(icon-down-off)s); }
QComboBox QAbstractItemView { background: %(panel)s; color: %(ink)s; border: 1px solid %(line-2)s;
                              selection-background-color: %(hol-soft)s;
                              selection-color: %(ink)s; outline: 0; padding: 4px; }
QSpinBox, QDoubleSpinBox { padding-right: 18px; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::down-button { subcontrol-origin: border; width: 18px; border: 0;
                              background: transparent; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(%(icon-up)s); width: 8px;
                                               height: 5px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(%(icon-down)s); width: 8px;
                                                   height: 5px; }
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled { image: none; }
QCheckBox { spacing: 8px; color: %(ink)s; }
QCheckBox:disabled { color: %(ink-3)s; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px;
                       border: 1px solid %(line-2)s; background: %(panel-2)s; }
QCheckBox::indicator:checked { background: %(hol)s; border-color: %(hol)s;
                               image: url(%(icon-check)s); }
QCheckBox::indicator:hover { border-color: %(ink-3)s; }

/* lists, tables, text */
QTableWidget, QListWidget { background: transparent; border: 0; outline: 0;
                            alternate-background-color: transparent;
                            selection-background-color: %(hol-soft)s; selection-color: %(ink)s; }
QTableWidget { gridline-color: transparent; font-family: %(mono)s; }
QTableWidget::item, QListWidget::item { border-bottom: 1px solid %(line)s; padding: 0 8px; }
QTableWidget::item:selected, QListWidget::item:selected { background: %(hol-soft)s;
                                                          color: %(ink)s; }
QListWidget { font-family: %(mono)s; font-size: 12px; }
QHeaderView { background: transparent; border: 0; }
QHeaderView::section { background: %(panel-2)s; color: %(ink-2)s; border: 0;
                       border-bottom: 1px solid %(line)s; padding: 6px 8px; font-size: 12px;
                       font-weight: 600; }
QTableCornerButton::section { background: %(panel-2)s; border: 0; }
QPlainTextEdit { background: %(panel-2)s; color: %(ink)s; border: 1px solid %(line)s;
                 border-radius: 8px; font-family: %(mono)s; font-size: 12px; padding: 6px; }
QProgressBar { background: %(panel-2)s; border: 1px solid %(line)s; border-radius: 5px;
               max-height: 10px; min-height: 10px; color: transparent; text-align: center; }
QProgressBar::chunk { background: %(hol)s; border-radius: 4px; }

/* scroll bars */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(line-2)s; border-radius: 3px; min-height: 28px; }
QScrollBar::handle:horizontal { background: %(line-2)s; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:hover { background: %(ink-3)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QToolTip { background: %(panel-2)s; color: %(ink)s; border: 1px solid %(line-2)s;
           padding: 4px 6px; }
QMenu { background: %(panel)s; color: %(ink)s; border: 1px solid %(line-2)s; padding: 4px; }
QMenu::item { padding: 5px 14px; border-radius: 6px; }
QMenu::item:selected { background: %(hol-soft)s; }
""" % dict(c, mono=mono_family)


def palette(t: dict) -> QPalette:
    """The QPalette of the theme (native dialogs and the roles the sheet does not cover)."""
    p = QPalette()
    roles = {
        QPalette.Window: "body", QPalette.WindowText: "ink", QPalette.Base: "panel-2",
        QPalette.AlternateBase: "panel", QPalette.Text: "ink", QPalette.Button: "panel-2",
        QPalette.ButtonText: "ink", QPalette.Highlight: "hol", QPalette.HighlightedText: "hol-ink",
        QPalette.ToolTipBase: "panel-2", QPalette.ToolTipText: "ink",
        QPalette.PlaceholderText: "ink-3", QPalette.BrightText: "bad", QPalette.Link: "hol-icon",
        QPalette.Light: "line-2", QPalette.Midlight: "line", QPalette.Mid: "line",
        QPalette.Dark: "body", QPalette.Shadow: "body",
    }
    for role, tok in roles.items():
        p.setColor(role, t[tok])
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, t["ink-3"])
    return p


# ----------------------------------------------------------------------------------------------
# plots and listeners
# ----------------------------------------------------------------------------------------------

def theme_plot(plot) -> None:
    """Background panel-2, axes line-2 with ink-3 text, grid alpha 0.25 (DESIGN_UI 6.9) on a
    pyqtgraph PlotWidget; the plot is kept (weakly) and themed again on every apply()."""
    import pyqtgraph as pg
    t = tokens()
    plot.setBackground(t["panel-2"])
    item = plot.getPlotItem()
    for side in ("left", "bottom", "right", "top"):
        ax = item.getAxis(side)
        ax.setPen(pg.mkPen(t["line-2"]))
        ax.setTextPen(pg.mkPen(t["ink-3"]))
        ax.setStyle(tickFont=sans(11))
    item.showGrid(x=True, y=True, alpha=0.25)
    item.setContentsMargins(4, 8, 16, 4)       # room for the last tick text at the right edge
    _plots.add(plot)


def on_theme(fn) -> None:
    """Calls fn() after every apply(); a bound method is held weakly (its object may go)."""
    if hasattr(fn, "__self__"):
        _listeners.append(weakref.WeakMethod(fn))
    else:
        _listeners.append(lambda: fn)


def scheme_of(setting: str) -> str:
    """The tokens a theme setting stands for: "dark", "light", or for "system" the colour
    scheme the system reports (Dark: dark, anything else: light)."""
    if setting in THEMES:
        return setting
    hints = QGuiApplication.styleHints()
    try:
        dark = hints.colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        dark = False
    return "dark" if dark else "light"


def apply(app: QApplication | None, setting: str) -> str:
    """Sets the theme of `setting` ("system", "dark", "light") on the application: Fusion style,
    palette, style sheet, plots, listeners, repaint. Returns "dark" or "light"."""
    _state["name"] = scheme_of(setting)
    t = tokens()
    if app is not None:
        # Fusion once per application: with a sheet set, app.style() is Qt's style-sheet
        # wrapper (no name), so the style set here is remembered; a new style at every theme
        # change would polish every widget again
        if _state.get("style_app") is not app:
            _state["style"] = app.setStyle("Fusion")
            _state["style_app"] = app
        sheet = stylesheet(t, _state["name"])
        if app.styleSheet() != sheet:          # an unchanged sheet would repolish for nothing
            app.setPalette(palette(t))
            app.setStyleSheet(sheet)
    for plot in list(_plots):
        try:
            theme_plot(plot)
        except RuntimeError:          # its C++ object is gone
            _plots.discard(plot)
    alive = []
    for ref in _listeners:
        fn = ref()
        if fn is None:
            continue
        try:
            fn()
        except RuntimeError:          # a widget deleted on the C++ side
            continue
        alive.append(ref)
    _listeners[:] = alive
    if app is not None:
        for w in app.allWidgets():
            w.update()
    return _state["name"]


def repolish(widget) -> None:
    """Applies the sheet again to a widget whose dynamic property changed."""
    s = widget.style()
    s.unpolish(widget)
    s.polish(widget)
    widget.update()
