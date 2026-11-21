"""2C53T Files page (S2 4.10; DESIGN 13.2, 13.25).

FILE_LIST of a directory (drive 2: and drive 3:), FILE_READ into a file on the computer,
FILE_WRITE of a file from the computer, FILE_DELETE, and DRIVE sharing. The guards are those of the
device module (c2c53t.guard_file_path): the start-up files 2:/BOOTINST.TXT, 2:/BOOTTEST.TXT,
2:/BOOTDUMP.TXT and 2:/UPDATE/APP.BIN are never written or deleted, drive 3: takes only the
pictures 3:/System file/N.jpg (N 1..199), which are read back and kept before they change and need
the typed "OK". Before DRIVE sharing starts, a warning names the start-up files. Before it ends,
a dialog asks for the drive to be ejected on the computer first (S2 4.10: the host must eject
it, or its file system can be left inconsistent), and where the system can be asked (the volume
probes of link.discover) DRIVE 0 is refused while a volume of the meter's USB device
2E3C:2C53 is still mounted.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHeaderView, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem)

from ...devices import c2c53t as c2
from ..base import Page, row
from ..common.data_pages import fit_table, setup_table
from ..widgets import Card, label, set_kind

ATTR_DIRECTORY = 0x10        # FAT directory attribute bit of the FILE_LIST entry


def join(directory: str, name: str) -> str:
    return directory.rstrip("/") + "/" + name if not directory.endswith(":/") else directory + name


class FilesPage(Page):
    title = "Files"
    status_names = c2.STATUS_NAME

    def __init__(self, ctx, device):
        super().__init__(ctx, device)
        self.entries: list[c2.FileEntry] = []
        self.listed = ""
        self.dir = QLineEdit("2:/")
        self.dir.setObjectName("files_dir")
        self.list_button = self.control(QPushButton("List"), c2.FILE_LIST)
        self.list_button.clicked.connect(self.list_dir)
        self.up_button = self.control(QPushButton("Up"), c2.FILE_LIST)
        self.up_button.clicked.connect(self.up)
        self.drive3_button = self.control(QPushButton("3:/System file"), c2.FILE_LIST)
        self.drive3_button.clicked.connect(lambda: (self.dir.setText("3:/System file"),
                                                    self.list_dir()))
        set_kind(self.list_button, "primary")
        bar = Card()
        bar.add(row(self.dir, self.list_button, self.up_button, self.drive3_button,
                    stretch=False))
        self.body.addWidget(bar)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("files")
        self.table.setHorizontalHeaderLabels(["Name", "Size B", "Attribute"])
        setup_table(self.table)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._open)
        self.table.itemSelectionChanged.connect(self.refresh_controls)
        self.read_button = self.control(QPushButton("Read to the computer…"), c2.FILE_READ)
        self.read_button.clicked.connect(self.read_selected)
        self.write_button = self.control(QPushButton("Write a file here…"), c2.FILE_WRITE)
        self.write_button.clicked.connect(self.write_file)
        self.delete_button = self.control(set_kind(QPushButton("Delete…"), "danger"),
                                          c2.FILE_DELETE)
        self.delete_button.clicked.connect(self.delete_selected)
        files = Card("Files")
        self.files_card = files
        files.add(self.table)
        files.add(row(self.read_button, self.write_button, self.delete_button))
        files.add(label("Double-click a directory to open it. The start-up files of drive 2: "
                        "are never written or deleted; drive 3: takes only the pictures "
                        "3:/System file/N.jpg.", "hint", word_wrap=True))
        self.body.addWidget(files)
        self.share_button = self.control(set_kind(QPushButton("Share the drive with the "
                                                              "computer…"), "danger"), c2.DRIVE)
        self.share_button.clicked.connect(lambda: self.share(True))
        self.unshare_button = self.control(QPushButton("End drive sharing"), c2.DRIVE)
        self.unshare_button.clicked.connect(lambda: self.share(False))
        share = Card("Drive sharing")
        share.add(row(self.share_button, self.unshare_button))
        share.add(label("DRIVE shares drive 2: with this computer; eject it on the computer "
                        "before the sharing ends (S2 4.10).", "hint", word_wrap=True))
        self.body.addWidget(share)
        self.body.addStretch(1)

    def extra_enabled(self, widget) -> bool:
        if widget is getattr(self, "read_button", None):
            e = self.selected()
            return e is not None and not e.attribute & ATTR_DIRECTORY
        if widget is getattr(self, "delete_button", None):
            return self.selected() is not None
        return True

    def selected(self) -> c2.FileEntry | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.entries[rows[0].row()] if rows and rows[0].row() < len(self.entries) else None

    def list_dir(self):
        path = self.dir.text().strip()
        try:
            cp = c2.canonical_path(path)
        except (ValueError, c2.SafetyRefused) as e:
            self.say(str(e), error=True)
            return None
        dev = self.dev

        def done(entries):
            self.listed = cp
            self.dir.setText(cp)
            self.entries = entries
            self.table.setRowCount(len(entries))
            for i, e in enumerate(entries):
                for col, text in enumerate((e.name + ("/" if e.attribute & ATTR_DIRECTORY else ""),
                                            str(e.size), "0x%02X" % e.attribute)):
                    it = QTableWidgetItem(text)
                    if col == 1:
                        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, col, it)
            fit_table(self.table)
            self.files_card.set_title("Files · %s · %d entries" % (cp, len(entries)))
            self.refresh_controls()
            self.say("%s: %d entries" % (cp, len(entries)))

        return self.run("FILE_LIST", lambda p: dev.file_list(cp), done)

    def up(self):
        cp = self.listed or self.dir.text()
        if cp.endswith(":/"):
            return None
        self.dir.setText(cp.rsplit("/", 1)[0] or cp[:3])
        if self.dir.text().endswith(":"):
            self.dir.setText(self.dir.text() + "/")
        return self.list_dir()

    def _open(self, r: int, _c: int) -> None:
        e = self.entries[r]
        if e.attribute & ATTR_DIRECTORY:
            self.dir.setText(join(self.listed, e.name))
            self.list_dir()

    def read_selected(self):
        e = self.selected()
        if e is None:
            return None
        src = join(self.listed, e.name)
        dest = self.ctx.ask_save("Save %s" % src, e.name, "All files (*)")
        if not dest:
            return None
        return self.read_to(src, dest)

    def read_to(self, src: str, dest: str):
        dev = self.dev

        def job(_p):
            data = dev.file_read(src)
            with open(dest, "wb") as f:
                f.write(data)
            return len(data)

        return self.run("FILE_READ", job, lambda n: self.say("%s: %d bytes to %s" % (src, n, dest)),
                        exclusive=True)

    def write_file(self):
        src = self.ctx.ask_open("File to write to the meter", "All files (*)")
        if not src:
            return None
        return self.write_from(src, join(self.listed or self.dir.text(), os.path.basename(src)))

    def write_from(self, src: str, target: str):
        try:
            cp = c2.guard_file_path(target, write=True)
            with open(src, "rb") as f:
                data = f.read()
            if cp.startswith("3:"):
                c2.check_picture(data)
        except (OSError, ValueError, c2.SafetyRefused, c2.DeviceError) as e:
            self.say("not written: %s" % e, error=True)
            return None
        word = None
        parent, name = cp.rsplit("/", 1)
        if parent.endswith(":"):
            parent += "/"
        listed_here = parent == self.listed and any(e.name.lower() == name.lower()
                                                    for e in self.entries)
        if cp.startswith("2:") and listed_here:
            if self.confirm("Overwrite a file",
                            "FILE_WRITE replaces %s on the meter with %s (%d bytes)."
                            % (cp, os.path.basename(src), len(data))) is None:
                return None
        if cp.startswith("3:"):
            word = self.confirm("Replace a user-interface picture",
                                "FILE_WRITE replaces the meter's picture %s with %s (%d bytes, a "
                                "baseline JPEG). The picture on the meter is read back and kept in "
                                "the backup folder first." % (cp, os.path.basename(src), len(data)))
            if word is None:
                return None
        dev = self.dev
        return self.run("FILE_WRITE", lambda p: dev.file_write(cp, data, confirm=word),
                        lambda r: (self.say("%s written (%d bytes)" % (cp, len(data))),
                                   self.list_dir()), exclusive=True)

    def delete_selected(self):
        e = self.selected()
        if e is None:
            return None
        return self.delete_path(join(self.listed, e.name))

    def delete_path(self, path: str):
        try:
            cp = c2.guard_file_path(path, write=False)
        except (ValueError, c2.SafetyRefused) as e:
            self.say("not deleted: %s" % e, error=True)
            return None
        text = "FILE_DELETE removes %s from the meter." % cp
        if cp.startswith("3:"):
            text += " The picture is read back and kept in the backup folder first."
        word = self.confirm("Delete a file", text)
        if word is None:
            return None
        dev = self.dev
        return self.run("FILE_DELETE",
                        lambda p: dev.file_delete(cp, confirm=word if cp.startswith("3:") else None),
                        lambda r: (self.say("%s deleted" % cp), self.list_dir()), exclusive=True)

    def share(self, on: bool):
        dev = self.dev
        if not on:
            return self.end_sharing()
        word = self.confirm(
            "Share the drive",
            "DRIVE shares the meter's drive 2: with this computer; the meter's port goes away "
            "and comes back (the program reconnects). While it is shared, do not change or "
            "delete these start-up files: %s. Eject the drive on the computer before ending "
            "the sharing (S2 4.10)." % ", ".join(
                "2:/" + n.upper() for n in sorted(c2.PROTECTED_FILES)))
        if word is None:
            return None
        return self.run("DRIVE", lambda p: dev.drive(True),
                        lambda r: self.say("drive sharing started"))

    def end_sharing(self):
        """DRIVE 0 after the eject dialog and, where possible, the check that no volume of the
        meter is mounted any more."""
        dev = self.dev
        word = self.confirm(
            "End drive sharing",
            "Eject the meter's drive on this computer first (Finder: Eject; Windows: Eject in the "
            "Explorer; Linux: unmount it). The computer writes changed files back lazily; ending "
            "the sharing before the eject can leave the drive's file system damaged, and drive 2: "
            "holds the meter's start-up files and the staged update (S2 4.10). The program "
            "checks that no volume of the meter (USB 2E3C:2C53) is mounted before it sends "
            "DRIVE 0. Have you ejected it?")
        if word is None:
            return None
        check = self.ctx.mounted_meter_volumes

        def job(_p):
            mounted = check(c2.USB_VID, c2.USB_PID)
            if mounted:
                raise c2.SafetyRefused("the meter's drive is still mounted at %s: eject it first, "
                                       "then end the sharing again" % ", ".join(mounted))
            dev.drive(False)
            return mounted

        def done(mounted):
            self.say("drive sharing ended" + (
                " (this system offers no volume check: the eject was not verified)"
                if mounted is None else " (no volume of the meter was mounted)"))

        return self.run("DRIVE", job, done)
