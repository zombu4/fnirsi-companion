"""The typed confirmation of the dangerous actions (DESIGN 9, 13.1, 13.7, 13.8, 13.26).

The dialog states what the action does to the meter (the consequence), and its confirm button is
enabled only while the field holds exactly the word "OK". The word typed is what the caller passes
on to the device module, whose methods refuse anything but "OK" themselves.
"""

from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout,
                               QWidget)

from ..devices.base import CONFIRM_WORD


class ConfirmDialog(QDialog):
    """Title, the consequence text, a field for the typed word, Confirm / Cancel."""

    def __init__(self, parent: QWidget | None, title: str, consequence: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        lay = QVBoxLayout(self)
        text = QLabel(consequence)
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addWidget(QLabel('Type %s to go ahead:' % CONFIRM_WORD))
        self.field = QLineEdit()
        self.field.setObjectName("confirm_word")
        lay.addWidget(self.field)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Confirm")
        self.ok_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.field.textChanged.connect(self._check)
        self.consequence = consequence

    def _check(self, text: str) -> None:
        self.ok_button.setEnabled(text == CONFIRM_WORD)

    def accept(self) -> None:
        if self.field.text() != CONFIRM_WORD:
            return                       # Enter in the field with another word does nothing
        super().accept()

    def word(self) -> str | None:
        """The typed word after the dialog was accepted, None after Cancel."""
        return self.field.text() if self.result() == QDialog.Accepted else None


def ask(parent: QWidget | None, title: str, consequence: str) -> str | None:
    """Shows the dialog modally; the typed word, or None when it was cancelled."""
    dlg = ConfirmDialog(parent, title, consequence)
    dlg.exec()
    return dlg.word()
