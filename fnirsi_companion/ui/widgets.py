"""The program's own widgets (DESIGN_UI sections 2, 3 and 6): cards, chips, segmented controls,
button kinds, meter keys, statistic rows, badges, a flow layout, the seven-segment digits and the
LCD, the sidebar items with their painted icons, the logo tile and the device drawing.

The widgets that paint themselves read their colours from style.tokens() in paintEvent, so a theme
change only needs a repaint (style.apply repaints every widget).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPolygonF)
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
                               QWidgetItem)

from . import style

# ----------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------


def set_kind(button: QPushButton, kind: str) -> QPushButton:
    """Gives a push button one of the kinds of the sheet: "primary" (holster yellow), "danger"
    (red text: saves, calibrations, installs, restores, power off) or "small"."""
    button.setProperty("kind", kind)
    style.repolish(button)
    return button


def label(text: str = "", role: str = "label", word_wrap: bool = False) -> QLabel:
    """A QLabel with a role of the sheet: "label" (ink-2), "hint" (ink-3, small), "value" (mono),
    "mono", "caption", "big", "name"."""
    lab = QLabel(text)
    lab.setProperty("role", role)
    lab.setWordWrap(word_wrap)
    return lab


# ----------------------------------------------------------------------------------------------
# Card
# ----------------------------------------------------------------------------------------------

class Card(QFrame):
    """A rounded panel (objectName "card") with an optional title row (title on the left, widgets
    added with add_right on the right) and a body layout."""

    def __init__(self, title: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 14, 16, 16)
        self._lay.setSpacing(10)
        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(8)
        self.title_label = QLabel(title or "")
        self.title_label.setObjectName("cardTitle")
        self.title_row.addWidget(self.title_label)
        self.title_row.addStretch(1)
        self.title_label.setVisible(bool(title))
        self._lay.addLayout(self.title_row)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self._lay.addLayout(self.body, 1)
        self._tail = True                   # a trailing stretch keeps the body at the top
        self.body.addStretch(1)
        self._has_title_row = bool(title)
        self._update_title_row()

    def _update_title_row(self) -> None:
        self.title_row.setContentsMargins(0, 0, 0, 0)
        if not self._has_title_row:
            self._lay.setSpacing(0)
        else:
            self._lay.setSpacing(10)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)
        self.title_label.setVisible(bool(text))
        self._has_title_row = True
        self._update_title_row()

    def add_right(self, widget: QWidget) -> QWidget:
        """A widget at the right end of the title row."""
        self.title_row.addWidget(widget)
        self._has_title_row = True
        self._update_title_row()
        return widget

    def add(self, item, stretch: int = 0):
        """Adds a widget or a layout to the body (before the trailing stretch; an item with a
        stretch takes the card's spare height instead of it); returns it."""
        if stretch and self._tail:
            self.body.takeAt(self.body.count() - 1)
            self._tail = False
        at = self.body.count() - 1 if self._tail else self.body.count()
        if isinstance(item, QLayout):
            self.body.insertLayout(at, item, stretch)
        else:
            self.body.insertWidget(at, item, stretch)
        return item


# ----------------------------------------------------------------------------------------------
# Chip
# ----------------------------------------------------------------------------------------------

class Chip(QWidget):
    """A pill of the header: an optional status dot, a label in ink-2 and an optional value in
    mono ink. Off: dot and text ink-3. Bad: text and dot in the bad colour. The value is elided
    when the chip is narrower than its text."""

    def __init__(self, text: str = "", value: str = "", dot: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._text, self._value, self._dot = text, value, dot
        self._on, self._bad = True, False
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_text(self, text: str, value: str = "") -> None:
        if (text, value) != (self._text, self._value):
            self._text, self._value = text, value
            self.updateGeometry()
            self.update()

    def set_dot(self, token: str | None) -> None:
        self._dot = token
        self.update()

    def set_on(self, on: bool) -> None:
        if on != self._on:
            self._on = on
            self.update()

    def set_bad(self, bad: bool) -> None:
        self._bad = bad
        self.update()

    def text(self) -> str:
        return (self._text + (" " + self._value if self._value else "")).strip()

    def _fonts(self):
        return style.sans(12, QFont.Weight.Medium), style.mono(12, QFont.Weight.DemiBold)

    def _natural(self) -> int:
        f1, f2 = self._fonts()
        w = 26                       # padding 11 + 11, and room for the rounding of the advances
        if self._dot is not None:
            w += 13
        if self._text:
            w += QFontMetrics(f1).horizontalAdvance(self._text)
        if self._value:
            w += QFontMetrics(f2).horizontalAdvance(self._value) + (6 if self._text else 0)
        return w

    def sizeHint(self) -> QSize:
        return QSize(self._natural(), 28)

    def minimumSizeHint(self) -> QSize:
        return QSize(min(self._natural(), 90), 28)

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(t["line"], 1))
        p.setBrush(t["panel-2"])
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        x = 11.0
        ink2 = t["bad"] if self._bad else (t["ink-2"] if self._on else t["ink-3"])
        ink = t["bad"] if self._bad else (t["ink"] if self._on else t["ink-3"])
        if self._dot is not None:
            dot = t["bad"] if self._bad else (t[self._dot] if self._on else t["ink-3"])
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QPointF(x + 3.5, r.center().y()), 3.5, 3.5)
            x += 13
        f1, f2 = self._fonts()
        right = self.width() - 11
        if self._text:
            p.setFont(f1)
            p.setPen(ink2)
            fm = QFontMetrics(f1)
            avail = int(right - x)
            text = fm.elidedText(self._text, Qt.ElideRight, avail)
            p.drawText(QRectF(x, 0, avail, self.height()), Qt.AlignVCenter | Qt.AlignLeft, text)
            x += fm.horizontalAdvance(text) + 6
        if self._value and x < right:
            p.setFont(f2)
            p.setPen(ink)
            fm = QFontMetrics(f2)
            avail = int(right - x)
            text = fm.elidedText(self._value, Qt.ElideRight, avail)
            p.drawText(QRectF(x, 0, avail, self.height()), Qt.AlignVCenter | Qt.AlignLeft, text)


# ----------------------------------------------------------------------------------------------
# Segmented: a row of options in a pill, with the API of a QComboBox the pages use
# ----------------------------------------------------------------------------------------------

class Segmented(QWidget):
    """2..6 choices as a row of options (the reference's "Auto | Fixed"). It offers the parts of
    the QComboBox API the pages use (addItem, itemText, itemData, currentIndex, setCurrentIndex,
    currentText, currentData, findData, findText, count, clear, activated, currentIndexChanged),
    so a page can take it where a combo box was. Index -1 shows no option selected (a meter value
    that is not in the list)."""

    activated = Signal(int)
    currentIndexChanged = Signal(int)

    PAD = 2
    H = 32

    def __init__(self, options=(), parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, object]] = []
        self._index = -1
        self._hover = -1
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        for o in options:
            if isinstance(o, tuple):
                self.addItem(*o)
            else:
                self.addItem(o)

    # -- the combo-box API --------------------------------------------------------------------
    def addItem(self, text: str, data=None) -> None:
        self._items.append((str(text), data))
        if self._index < 0 and len(self._items) == 1:
            self._set(0, emit=True)
        self._resized()

    def addItems(self, texts) -> None:
        for t in texts:
            self.addItem(t)

    def clear(self) -> None:
        self._items.clear()
        changed = self._index != -1
        self._index = -1
        self._resized()
        if changed and not self.signalsBlocked():
            self.currentIndexChanged.emit(-1)

    def count(self) -> int:
        return len(self._items)

    def itemText(self, i: int) -> str:
        return self._items[i][0] if 0 <= i < len(self._items) else ""

    def itemData(self, i: int):
        return self._items[i][1] if 0 <= i < len(self._items) else None

    def currentIndex(self) -> int:
        return self._index

    def currentText(self) -> str:
        return self.itemText(self._index)

    def currentData(self):
        return self.itemData(self._index)

    def findData(self, data) -> int:
        for i, (_t, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def findText(self, text: str) -> int:
        for i, (t, _d) in enumerate(self._items):
            if t == text:
                return i
        return -1

    def setCurrentIndex(self, i: int) -> None:
        if not -1 <= i < len(self._items):
            i = -1
        self._set(i, emit=True)

    def _set(self, i: int, emit: bool) -> None:
        if i == self._index:
            return
        self._index = i
        self.update()
        if emit and not self.signalsBlocked():
            self.currentIndexChanged.emit(i)

    # -- geometry -----------------------------------------------------------------------------
    def _font(self) -> QFont:
        return style.sans(12, QFont.Weight.DemiBold)

    def _widths(self) -> list[int]:
        fm = QFontMetrics(self._font())
        return [fm.horizontalAdvance(t) + 22 for t, _d in self._items]

    def _rects(self) -> list[QRectF]:
        out, x = [], float(self.PAD)
        for w in self._widths():
            out.append(QRectF(x, self.PAD, w, self.H - 2 * self.PAD))
            x += w + 2
        return out

    def _resized(self) -> None:
        w = sum(self._widths()) + 2 * max(len(self._items) - 1, 0) + 2 * self.PAD
        self.setFixedWidth(max(w, 2 * self.PAD + 20))
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.H)

    # -- input --------------------------------------------------------------------------------
    def _at(self, pos) -> int:
        for i, r in enumerate(self._rects()):
            if r.contains(QPointF(pos)):
                return i
        return -1

    def mouseMoveEvent(self, ev) -> None:
        i = self._at(ev.position().toPoint())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, _ev) -> None:
        self._hover = -1
        self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton or not self.isEnabled():
            return
        i = self._at(ev.position().toPoint())
        if i >= 0:
            self._choose(i)

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key_Left, Qt.Key_Right) and self._items:
            step = -1 if ev.key() == Qt.Key_Left else 1
            i = max(0, min(len(self._items) - 1, self._index + step))
            self._choose(i)
            return
        super().keyPressEvent(ev)

    def _choose(self, i: int) -> None:
        """A choice of the user: currentIndexChanged when it changes, activated always (as a
        QComboBox does)."""
        self._set(i, emit=True)
        if not self.signalsBlocked():
            self.activated.emit(i)

    # -- painting -----------------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(t["line"], 1))
        p.setBrush(t["panel-2"])
        p.drawRoundedRect(outer, 10, 10)
        p.setFont(self._font())
        for i, r in enumerate(self._rects()):
            if i == self._index:
                # the reference's "lift": a line-coloured edge one pixel under the pill
                p.setPen(Qt.NoPen)
                p.setBrush(t["line-2"] if not style.is_dark() else QColor(0, 0, 0, 90))
                p.drawRoundedRect(r.translated(0, 1), 8, 8)
                p.setBrush(t["seg-pill"])
                p.drawRoundedRect(r, 8, 8)
                p.setPen(t["ink"])
            else:
                p.setPen(t["ink"] if i == self._hover and self.isEnabled() else t["ink-2"])
            p.drawText(r, Qt.AlignCenter, self._items[i][0])
        if self.hasFocus():
            p.setPen(QPen(t["hol"], 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(outer, 10, 10)


# ----------------------------------------------------------------------------------------------
# KeyButton, StatRow, Badge
# ----------------------------------------------------------------------------------------------

class KeyButton(QAbstractButton):
    """A meter key as the reference draws it: panel-2, 44 px high, the label semibold and an
    optional second line small in ink-3 (for example "long: compare screen")."""

    def __init__(self, text: str, sub: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setText(text)
        self.sub = sub
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._hover = False

    def sizeHint(self) -> QSize:
        w1 = QFontMetrics(style.sans(13, QFont.Weight.DemiBold)).horizontalAdvance(self.text())
        w2 = QFontMetrics(style.sans(10)).horizontalAdvance(self.sub) if self.sub else 0
        return QSize(max(84, max(w1, w2) + 24), 44)

    def minimumSizeHint(self) -> QSize:
        return QSize(56, 44)

    def enterEvent(self, _ev) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, _ev) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        down = self.isDown()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -1.5)
        if down:
            r.translate(0, 1)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(t["line-2"])
            p.drawRoundedRect(r.translated(0, 1), 10, 10)
        en = self.isEnabled()
        border = t["hol"] if self._hover and en else t["line"]
        p.setPen(QPen(border, 1))
        p.setBrush(t["panel-2"])
        p.drawRoundedRect(r, 10, 10)
        p.setPen(t["ink"] if en else t["ink-3"])
        room = int(r.width()) - 8
        f, text = self._fit(self.text(), (13, 12, 11, 10), QFont.Weight.DemiBold, room)
        p.setFont(f)
        if self.sub:
            top = QRectF(r.x(), r.y() + 5, r.width(), r.height() / 2)
            p.drawText(top, Qt.AlignHCenter | Qt.AlignVCenter, text)
            sf, sub = self._fit(self.sub, (10, 9, 8), QFont.Weight.Normal, room)
            p.setFont(sf)
            p.setPen(t["ink-3"])
            p.drawText(QRectF(r.x(), r.center().y() + 1, r.width(), r.height() / 2 - 5),
                       Qt.AlignHCenter | Qt.AlignVCenter, sub)
        else:
            p.drawText(r, Qt.AlignCenter, text)

    @staticmethod
    def _fit(text: str, sizes, weight, room: int):
        """The largest of `sizes` (px) at which `text` fits into `room` pixels; at the smallest
        size a text that still does not fit is cut with "…" (the tooltip has it in full)."""
        for px in sizes:
            f = style.sans(px, weight)
            fm = QFontMetrics(f)
            if fm.horizontalAdvance(text) <= room:
                return f, text
        return f, fm.elidedText(text, Qt.ElideRight, room)


class KeyRow(QWidget):
    """The Live page's meter keys in rows of equal columns, never more than two rows (DESIGN_UI
    6.10): as many keys per row as fit with their full labels, at least half of them."""

    GAP = 8

    def __init__(self, buttons, parent: QWidget | None = None):
        super().__init__(parent)
        self.buttons = list(buttons)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(self.GAP)
        self.cols = 0
        self._place(max(1, math.ceil(len(self.buttons) / 2)))

    def _cols_for(self, width: int) -> int:
        n = len(self.buttons)
        if not n:
            return 1
        need = max(b.sizeHint().width() for b in self.buttons)
        fit = max(1, (width + self.GAP) // (need + self.GAP))
        return max(math.ceil(n / 2), min(n, fit))

    def _place(self, cols: int) -> None:
        if cols == self.cols:
            return
        for b in self.buttons:
            self.grid.removeWidget(b)
        for c in range(max(self.cols, cols)):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        self.cols = cols
        for i, b in enumerate(self.buttons):
            self.grid.addWidget(b, i // cols, i % cols)

    def resizeEvent(self, ev) -> None:
        self._place(self._cols_for(ev.size().width()))
        super().resizeEvent(ev)


class StatRow(QWidget):
    """Label in ink-2 on the left, value in mono on the right, a separator line under it. The
    value QLabel is `value` (the pages keep it to set the text)."""

    def __init__(self, name: str, value: str = "—", line: bool = True,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.line = line
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 7, 0, 8)
        lay.setSpacing(12)
        self.name = label(name, "label")
        self.value = label(value, "value")
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value.setWordWrap(True)
        self.value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.name, 0, Qt.AlignTop)
        lay.addWidget(self.value, 1)

    def paintEvent(self, _ev) -> None:
        if not self.line:
            return
        p = QPainter(self)
        p.setPen(QPen(style.tokens()["line"], 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)


class ElidedLabel(QLabel):
    """A one-line label that ends its text with "…" when it is narrower than the text (the
    status line); the whole text is its tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, text)


class Badge(QLabel):
    """A small pill: kind "newer" (hol), "installed" (ok), "older" or "unknown" (ink-3)."""

    def __init__(self, text: str, kind: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setProperty("badge", kind)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


# ----------------------------------------------------------------------------------------------
# FlowLayout: widgets in rows that wrap
# ----------------------------------------------------------------------------------------------

class FlowLayout(QLayout):
    """Lays its widgets out left to right and wraps to the next row when the width is used up."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def addWidget(self, w: QWidget) -> None:
        self.addChildWidget(w)
        self.addItem(QWidgetItem(w))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() is not None and it.widget().isHidden():
                continue                  # a hidden widget takes no room
            hint = it.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > r.right() + 1 and line_h > 0:
                x = r.x()
                y += line_h + self._spacing
                nx = x + hint.width() + self._spacing
                line_h = 0
            if apply:
                it.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# ----------------------------------------------------------------------------------------------
# seven-segment digits
# ----------------------------------------------------------------------------------------------

# segments a (top), b (top right), c (bottom right), d (bottom), e (bottom left), f (top left),
# g (middle) of every character the display claims (DESIGN_UI section 3)
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg",
    "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
    "A": "abcefg", "b": "cdefg", "C": "adef", "c": "deg", "d": "bcdeg", "E": "adefg",
    "F": "aefg", "H": "bcefg", "L": "def", "n": "ceg", "o": "cdeg", "O": "abcdef",
    "P": "abefg", "r": "eg", "S": "acdfg", "t": "defg", "U": "bcdef", "u": "cde",
    "Y": "bcdfg", "°": "abfg",
}


def segments_of(ch: str) -> str | None:
    """The lit segments of a character: its own entry, else the entry of its other case (the
    display's "t" stands for T, "b" for B); None when the display cannot show it (a blank)."""
    if ch in SEGMENTS:
        return SEGMENTS[ch]
    other = ch.swapcase()
    if other != ch and other in SEGMENTS:
        return SEGMENTS[other]
    return None


def cells_of(text: str) -> list[tuple[str, bool]]:
    """The text as display cells (character, decimal point lit): "." and "," light the point of
    the cell before them (a blank cell when there is none)."""
    out: list[list] = []
    for ch in text:
        if ch in ".,":
            if out and not out[-1][1]:
                out[-1][1] = True
            else:
                out.append([" ", True])
        else:
            out.append([ch, False])
    return [(c, d) for c, d in out]


SKEW = 0.07          # italic slant of the digits (x per unit of height)
CELL_W = 0.54        # digit width / height
CELL_GAP = 0.16      # space after a digit (holds the decimal point) / height


def segment_width(cells: int, height: float) -> float:
    """Width of `cells` digit cells at the digit height."""
    return cells * height * (CELL_W + CELL_GAP) + height * SKEW


def _segment_polys(x0: float, y0: float, h: float) -> dict:
    """The seven segment polygons (and the decimal point square) of a cell at (x0, y0)."""
    w = h * CELL_W
    t = h * 0.105
    g = t * 0.14
    half = t / 2

    def hseg(xa, xb, y):
        return [(xa, y), (xa + half, y - half), (xb - half, y - half), (xb, y),
                (xb - half, y + half), (xa + half, y + half)]

    def vseg(x, ya, yb):
        return [(x, ya), (x + half, ya + half), (x + half, yb - half), (x, yb),
                (x - half, yb - half), (x - half, ya + half)]

    pts = {
        "a": hseg(half + g, w - half - g, half),
        "g": hseg(half + g, w - half - g, h / 2),
        "d": hseg(half + g, w - half - g, h - half),
        "f": vseg(half, half + g, h / 2 - g),
        "b": vseg(w - half, half + g, h / 2 - g),
        "e": vseg(half, h / 2 + g, h - half - g),
        "c": vseg(w - half, h / 2 + g, h - half - g),
    }
    dp = w + h * CELL_GAP * 0.30
    pts["dp"] = [(dp, h - t), (dp + t, h - t), (dp + t, h), (dp, h)]
    out = {}
    for k, poly in pts.items():
        out[k] = QPolygonF([QPointF(x0 + x + (h - y) * SKEW, y0 + y) for x, y in poly])
    return out


def paint_segments(p: QPainter, x: float, y: float, height: float, text: str, cells: int,
                   lit: QColor, off: QColor, glow: bool) -> float:
    """Paints `text` right-aligned in `cells` digit cells (more when the text needs them) with
    the top left at (x, y); every unlit segment in `off` (the ghost 8s). Returns the width."""
    chars = cells_of(text)
    n = max(cells, len(chars))
    chars = [(" ", False)] * (n - len(chars)) + chars
    step = height * (CELL_W + CELL_GAP)
    lit_polys, off_polys = [], []
    for i, (ch, dot) in enumerate(chars):
        polys = _segment_polys(x + i * step, y, height)
        segs = segments_of(ch) or ""
        for name, poly in polys.items():
            on = dot if name == "dp" else name in segs
            (lit_polys if on else off_polys).append(poly)
    p.setPen(Qt.NoPen)
    p.setBrush(off)
    for poly in off_polys:
        p.drawPolygon(poly)
    if glow and lit_polys:
        g = QColor(lit)
        g.setAlpha(46)
        p.setPen(QPen(g, max(2.0, height * 0.05), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(g)
        for poly in lit_polys:
            p.drawPolygon(poly)
        p.setPen(Qt.NoPen)
    p.setBrush(lit)
    for poly in lit_polys:
        p.drawPolygon(poly)
    return segment_width(n, height)


class SevenSegment(QWidget):
    """A string of seven-segment characters with decimal points, scaled to the widget's height
    (and narrowed to its width), lit segments in seg, every unlit segment in seg-off."""

    def __init__(self, cells: int = 5, parent: QWidget | None = None):
        super().__init__(parent)
        self.cells = cells
        self._text = ""
        self.setMinimumHeight(24)

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        h = 48
        return QSize(int(segment_width(max(self.cells, len(cells_of(self._text))), h)) + 2, h)

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = max(self.cells, len(cells_of(self._text)))
        h = min(float(self.height()), self.width() / max(segment_width(n, 1.0), 0.01))
        w = segment_width(n, h)
        paint_segments(p, self.width() - w, (self.height() - h) / 2, h, self._text, self.cells,
                       t["seg"], t["seg-off"], style.is_dark())


# ----------------------------------------------------------------------------------------------
# Lcd
# ----------------------------------------------------------------------------------------------

def split_reading(text: str) -> tuple[str, str]:
    """A meter text ("100.00 nF", "4.70 kΩ", ".OL", "") as (digits, unit): the unit is the part
    after the last space when that part holds no digit."""
    text = (text or "").strip()
    if " " in text:
        digits, unit = text.rsplit(" ", 1)
        if not any(ch.isdigit() for ch in unit):
            return digits.strip(), unit
    return text, ""


class Lcd(QWidget):
    """The meter display, painted (DESIGN_UI 3 and 6.2, 6.5, 6.6): a vertical gradient from
    lcd-hi to lcd with an inset edge; the annunciator row (ghost words on the left, lit ones in
    seg, the mode text on the right); the quantity as a lit word left of the main digits; the main
    SevenSegment digits with the unit in sans (40 % of the digit height); and, when set, the
    secondary area on the right (label, smaller digits, unit). In the dark theme the lit segments
    get a glow."""

    MAIN_MIN, MAIN_MAX = 70, 150
    PAD_X, PAD_TOP, PAD_BOTTOM, FLAGS_H, GAP = 24, 16, 18, 14, 12

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.words: list[str] = []
        self.lit: set[str] = set()
        self.mode = ""
        self.quantity = ""
        self._text = ""
        self.digits, self.unit = "", ""
        self.secondary: tuple[str, str, str] | None = None
        self.main_cells, self.sub_cells = 5, 4
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumHeight(self.MAIN_MIN + self.PAD_TOP + self.PAD_BOTTOM + self.FLAGS_H
                              + self.GAP)

    # -- content ------------------------------------------------------------------------------
    def set_words(self, words) -> None:
        self.words = list(words)
        self.update()

    def set_flags(self, lit, mode: str = "") -> None:
        lit = set(lit)
        if lit != self.lit or mode != self.mode:
            self.lit, self.mode = lit, mode
            self.update()

    def set_quantity(self, text: str) -> None:
        if text != self.quantity:
            self.quantity = text
            self.update()

    def setText(self, text: str) -> None:
        """The main reading as the meter shows it (digits and unit)."""
        self._text = text
        self.digits, self.unit = split_reading(text)
        self.update()

    def set_dashes(self, text: str = "") -> None:
        """An empty display: dashes in every cell, no unit; `text` is what text() returns (the
        reading's validity when it carries no digits)."""
        self._text = text
        self.digits, self.unit = "-" * self.main_cells, ""
        self.update()

    def text(self) -> str:
        return self._text

    def set_secondary(self, name: str | None, text: str = "") -> None:
        """The secondary area: label `name` with the reading `text`; None hides it."""
        if name is None:
            self.secondary = None
        else:
            d, u = split_reading(text)
            self.secondary = (name, d, u)
        self.update()

    # -- size ---------------------------------------------------------------------------------
    def hasHeightForWidth(self) -> bool:
        return True

    def _digit_h_for(self, width: int) -> float:
        return max(self.MAIN_MIN, min(self.MAIN_MAX, width * 0.16))

    def heightForWidth(self, width: int) -> int:
        return int(self._digit_h_for(width) + self.PAD_TOP + self.PAD_BOTTOM + self.FLAGS_H
                   + self.GAP)

    def sizeHint(self) -> QSize:
        return QSize(600, self.heightForWidth(600))

    # -- painting -----------------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        dark = style.is_dark()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        grad = QLinearGradient(0, 0, 0, r.height())
        grad.setColorAt(0.0, t["lcd-hi"])
        grad.setColorAt(0.6, t["lcd"])
        grad.setColorAt(1.0, t["lcd"])
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(r, 12, 12)
        # inset edge: a 1 px dark line and a 2 px inner shade along the top
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 64 if dark else 26), 1))
        p.drawRoundedRect(r, 12, 12)
        shade = QColor(0, 0, 0, 70 if dark else 22)
        p.setPen(QPen(shade, 2))
        p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), 11, 11)
        seg, off = t["seg"], t["seg-off"]

        # annunciator row
        x = self.PAD_X
        y = self.PAD_TOP
        f = style.sans(11, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.PercentageSpacing, 104)
        p.setFont(f)
        fm = QFontMetrics(f)
        for w in self.words:
            p.setPen(seg if w in self.lit else off)
            p.drawText(QRectF(x, y, fm.horizontalAdvance(w) + 2, self.FLAGS_H),
                       Qt.AlignLeft | Qt.AlignVCenter, w)
            x += fm.horizontalAdvance(w) + 12
        if self.mode:
            mc = QColor(seg)
            mc.setAlphaF(0.75)
            p.setPen(mc)
            # the meter's own texts as it writes them ("1kHz", not "1KHZ")
            mw = fm.horizontalAdvance(self.mode) + 2
            p.drawText(QRectF(r.right() - self.PAD_X - mw, y, mw, self.FLAGS_H),
                       Qt.AlignRight | Qt.AlignVCenter, self.mode)

        # the value row: quantity, main digits, unit, secondary area
        top = y + self.FLAGS_H + self.GAP
        avail_h = r.height() - top - self.PAD_BOTTOM
        right = r.right() - self.PAD_X
        sub_w = 0.0
        sub_h = 0.0
        if self.secondary is not None:
            sub_h = max(24.0, min(avail_h * 0.36, 52.0))
        qf = style.sans(14, QFont.Weight.Bold)
        q_w = QFontMetrics(qf).horizontalAdvance(self.quantity) + 12 if self.quantity else 0
        main_h = max(24.0, min(float(self.MAIN_MAX), avail_h))
        uf = style.sans(int(max(12, main_h * 0.40)), QFont.Weight.Light)
        u_w = QFontMetrics(uf).horizontalAdvance(self.unit) + 14 if self.unit else 0
        n_main = max(self.main_cells, len(cells_of(self.digits)))
        if self.secondary is not None:
            sf2 = style.sans(int(max(10, sub_h * 0.5)), QFont.Weight.Medium)
            n_sub = max(self.sub_cells, len(cells_of(self.secondary[1])))
            sub_w = segment_width(n_sub, sub_h) + QFontMetrics(sf2).horizontalAdvance(
                self.secondary[2]) + 12
        room = right - self.PAD_X - q_w - u_w - (sub_w + 24 if sub_w else 0)
        if segment_width(n_main, main_h) > room:
            main_h = max(18.0, room / segment_width(n_main, 1.0))
            uf = style.sans(int(max(12, main_h * 0.40)), QFont.Weight.Light)
            u_w = QFontMetrics(uf).horizontalAdvance(self.unit) + 14 if self.unit else 0
        base = top + avail_h                  # bottom line of the digits
        mx = self.PAD_X
        if self.quantity:
            p.setFont(qf)
            p.setPen(seg)
            p.drawText(QRectF(mx, base - main_h, q_w, 20), Qt.AlignLeft | Qt.AlignTop,
                       self.quantity)
            mx += q_w
        mw = paint_segments(p, mx, base - main_h, main_h, self.digits, self.main_cells, seg, off,
                            dark)
        if self.unit:
            p.setFont(uf)
            p.setPen(seg)
            p.drawText(QRectF(mx + mw + 6, base - main_h, u_w + 40, main_h),
                       Qt.AlignLeft | Qt.AlignBottom, self.unit)
        if self.secondary is not None:
            name, d, u = self.secondary
            sf2 = style.sans(int(max(10, sub_h * 0.5)), QFont.Weight.Medium)
            uw2 = QFontMetrics(sf2).horizontalAdvance(u) + 6 if u else 0
            n_sub = max(self.sub_cells, len(cells_of(d)))
            dw = segment_width(n_sub, sub_h)
            sx = right - uw2 - dw
            lf = style.sans(11, QFont.Weight.Bold)
            lf.setLetterSpacing(QFont.PercentageSpacing, 106)
            p.setFont(lf)
            lc = QColor(seg)
            lc.setAlphaF(0.7)
            p.setPen(lc)
            p.drawText(QRectF(sx - 40, base - sub_h - 22, dw + uw2 + 40, 16),
                       Qt.AlignRight | Qt.AlignVCenter, name)   # as given: "θ", not "Θ"
            paint_segments(p, sx, base - sub_h, sub_h, d, self.sub_cells, seg, off, dark)
            if u:
                p.setFont(sf2)
                p.setPen(seg)
                p.drawText(QRectF(right - uw2 + 6, base - sub_h, uw2, sub_h),
                           Qt.AlignLeft | Qt.AlignBottom, u)


# ----------------------------------------------------------------------------------------------
# sidebar: icons, items, logo
# ----------------------------------------------------------------------------------------------

def _poly(path: QPainterPath, *pts) -> None:
    path.moveTo(*pts[0])
    for pt in pts[1:]:
        path.lineTo(*pt)


def icon_path(kind: str) -> tuple[QPainterPath, QPainterPath | None]:
    """The stroke path of a sidebar icon in a 24 x 24 box, and an optional path to fill (knobs
    and dots). Drawn in code: no image files, no icon fonts (DESIGN_UI section 2)."""
    s = QPainterPath()
    fill = None
    if kind == "live":                       # gauge screen: a display with bars
        s.addRoundedRect(QRectF(3, 4, 18, 13), 2.5, 2.5)
        _poly(s, (7.5, 13.5), (7.5, 10.5))
        _poly(s, (11, 13.5), (11, 8))
        _poly(s, (14.5, 13.5), (14.5, 11))
        _poly(s, (9, 20.5), (15, 20.5))
    elif kind == "trend":                    # pulse
        _poly(s, (2, 12), (6, 12), (9, 5), (14, 19), (17, 12), (22, 12))
    elif kind == "log":                      # document with lines
        _poly(s, (6, 3), (14, 3), (19, 8), (19, 21), (6, 21), (6, 3))
        _poly(s, (14, 3), (14, 8), (19, 8))
        _poly(s, (9, 12.5), (16, 12.5))
        _poly(s, (9, 16.5), (16, 16.5))
    elif kind == "lcr":                      # resistor zig-zag
        _poly(s, (2, 12), (5.5, 12), (7, 8), (9.5, 16), (12, 8), (14.5, 16), (17, 8), (18.5, 12),
              (22, 12))
    elif kind == "compare":                  # two bars
        s.addRoundedRect(QRectF(5, 9, 5, 11), 1.2, 1.2)
        s.addRoundedRect(QRectF(14, 4, 5, 16), 1.2, 1.2)
        _poly(s, (3, 21.5), (21, 21.5))
    elif kind == "scope":                    # sine in a box
        s.addRoundedRect(QRectF(3, 4, 18, 16), 2.5, 2.5)
        s.moveTo(5.5, 12)
        s.cubicTo(7.5, 5.5, 10, 5.5, 12, 12)
        s.cubicTo(14, 18.5, 16.5, 18.5, 18.5, 12)
    elif kind == "generator":                # square wave
        _poly(s, (2, 16), (6, 16), (6, 8), (11, 8), (11, 16), (16, 16), (16, 8), (20, 8),
              (20, 16), (22, 16))
    elif kind == "files":                    # folder
        _poly(s, (3, 7), (3, 19), (21, 19), (21, 9), (12, 9), (10, 6), (4, 6), (3, 7))
    elif kind == "sliders":                  # meter settings
        for y, kx in ((6, 9), (12, 15), (18, 8)):
            _poly(s, (3, y), (kx - 2.2, y))
            _poly(s, (kx + 2.2, y), (21, y))
            s.addEllipse(QPointF(kx, y), 2.2, 2.2)
    elif kind == "meter":                    # the meter's outline
        s.addRoundedRect(QRectF(6, 2, 12, 20), 2.5, 2.5)
        s.addRoundedRect(QRectF(8.5, 4.5, 7, 5.5), 1, 1)
        s.addEllipse(QPointF(12, 15.5), 2.5, 2.5)
    elif kind == "screen":                   # monitor
        s.addRoundedRect(QRectF(2, 4, 20, 13), 2, 2)
        _poly(s, (12, 17), (12, 20.5))
        _poly(s, (8, 20.5), (16, 20.5))
    elif kind == "wrench":                   # diagnostics
        s.moveTo(14.5, 5.6)
        s.arcTo(QRectF(11, 3, 9.5, 9.5), 150, 270)
        s.lineTo(17.2, 10.2)
        _poly(s, (13.8, 10.8), (4.5, 20.1))
        s.moveTo(13.5, 7.5)
        s.lineTo(14.5, 5.6)
        fill = None
    elif kind == "gear":                     # settings
        pts = []
        for i in range(32):
            a = math.radians(i * 360 / 32 - 90 + 360 / 64)
            phase = i % 4
            rad = 9.5 if phase in (0, 1) else 7.2
            pts.append((12 + rad * math.cos(a), 12 + rad * math.sin(a)))
        _poly(s, *pts, pts[0])
        s.addEllipse(QPointF(12, 12), 3, 3)
    elif kind == "drive":                    # update drive
        s.addRoundedRect(QRectF(3, 11, 18, 8), 2, 2)
        _poly(s, (12, 2.5), (12, 8.5))
        _poly(s, (9.2, 5.8), (12, 8.6), (14.8, 5.8))
        fill = QPainterPath()
        fill.addEllipse(QPointF(17, 15), 1.1, 1.1)
    return s, fill


def paint_icon(p: QPainter, kind: str, rect: QRectF, colour: QColor) -> None:
    """Strokes the icon `kind` into rect (stroke 1.7 px of the 24 px box, round caps)."""
    path, fill = icon_path(kind)
    p.save()
    p.translate(rect.x(), rect.y())
    sc = rect.width() / 24.0
    p.scale(sc, sc)
    pen = QPen(colour, 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    if fill is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawPath(fill)
    p.restore()


class NavButton(QAbstractButton):
    """A sidebar item, 60 x 52 px, radius 12: the painted icon (22 px) and the name under it.
    Hover: panel-2. Active: hol-soft background, icon stroke hol, name ink (DESIGN_UI 6.1). The
    3 px bar at the sidebar's edge is painted by NavList."""

    def __init__(self, key: str, text: str, icon: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        self.icon_kind = icon
        self.setText(text)
        self.setCheckable(True)
        self.lines, self.font_px = self._fit(text)
        self.setFixedSize(60, 52 if len(self.lines) == 1 else 62)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(text)
        self._hover = False

    @staticmethod
    def _fit(text: str) -> tuple[list[str], int]:
        """The name in the 56 px under the icon: one line at 10 px; a two-word name that does
        not fit goes on two lines; one long word gets a smaller size (down to 8 px)."""
        room = 56

        def width(t, px):
            return QFontMetrics(style.sans(px, QFont.Weight.DemiBold)).horizontalAdvance(t)
        if width(text, 10) <= room:
            return [text], 10
        if " " in text:
            a, b = text.split(" ", 1)
            if width(a, 10) <= room and width(b, 10) <= room:
                return [a, b], 10
        for px in (9, 8):
            if width(text, px) <= room:
                return [text], px
        return [text], 8

    def enterEvent(self, _ev) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, _ev) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        on = self.isChecked()
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(t["hol-soft"])
            p.drawRoundedRect(r, 12, 12)
        elif self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(t["panel-2"])
            p.drawRoundedRect(r, 12, 12)
        stroke = t["hol-icon"] if on else (t["ink"] if self._hover else t["ink-3"])
        paint_icon(p, self.icon_kind, QRectF((r.width() - 22) / 2, 7, 22, 22), stroke)
        f = style.sans(self.font_px, QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(t["ink"] if on or self._hover else t["ink-3"])
        fm = QFontMetrics(f)
        for i, line in enumerate(self.lines):
            text = fm.elidedText(line, Qt.ElideRight, int(r.width()) - 2)
            p.drawText(QRectF(0, 33 + 12 * i, r.width(), 14), Qt.AlignHCenter | Qt.AlignVCenter,
                       text)


class NavList(QWidget):
    """The column of sidebar items; paints the 3 px hol bar at its left edge (x = 0 of the
    sidebar, right corners rounded 3 px) next to the active item."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("navlist")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(8, 2, 8, 2)
        self.lay.setSpacing(2)
        self.lay.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

    def paintEvent(self, _ev) -> None:
        for b in self.findChildren(NavButton):
            if b.isChecked() and b.isVisible():
                g = b.geometry()
                p = QPainter(self)
                p.setRenderHint(QPainter.Antialiasing)
                p.setPen(Qt.NoPen)
                p.setBrush(style.tokens()["hol"])
                path = QPainterPath()
                r = QRectF(-3, g.y() + 14, 6, g.height() - 28)
                path.addRoundedRect(r, 3, 3)
                p.drawPath(path)
                break


class Logo(QWidget):
    """The logo tile: 52 x 52, hol background, radius 12, the model's two lines in hol-ink."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self.lines = ("FN", "—")

    def set_lines(self, top: str, bottom: str) -> None:
        self.lines = (top, bottom)
        self.update()

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(t["hol"])
        p.drawRoundedRect(QRectF(self.rect()), 12, 12)
        p.setPen(t["hol-ink"])
        p.setFont(style.sans(17, QFont.Weight.ExtraBold))
        p.drawText(QRectF(0, 8, 52, 22), Qt.AlignHCenter | Qt.AlignVCenter, self.lines[0])
        c = QColor(t["hol-ink"])
        c.setAlphaF(0.72)
        p.setPen(c)
        p.setFont(style.sans(10, QFont.Weight.Bold))
        p.drawText(QRectF(0, 29, 52, 14), Qt.AlignHCenter | Qt.AlignVCenter, self.lines[1])


class DeviceDrawing(QWidget):
    """The meter page's drawing: a rounded body with a hol outline and the screen rectangle."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(72, 100)

    def paintEvent(self, _ev) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(3, 3, 66, 94)
        p.setPen(QPen(t["hol"], 5))
        p.setBrush(t["panel-2"])
        p.drawRoundedRect(body, 14, 14)
        p.setPen(QPen(t["line-2"], 1))
        p.setBrush(t["lcd"])
        p.drawRoundedRect(QRectF(14, 15, 44, 30), 4, 4)
