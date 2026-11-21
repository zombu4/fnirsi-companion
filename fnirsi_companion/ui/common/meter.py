"""Meter page (INFO, battery, updates, RESET, power off) and the page of a meter that is present
only as its update drive.

Updates (DESIGN 9, 13.10, 13.14, 13.17, 13.26):
- The file is checked against the attached model before anything else (LC1020E: check_uf2, a UF2
  of family 0xC6B3602B inside the application range; 2C53T: check_app_image, which refuses UF2
  files and wrong lengths / vectors); a file of the other meter is refused.
- An image whose SHA-256 is in no SHA256SUMS of the model's releases (or when the list cannot be
  read) gets its own warning with the typed "OK" (13.26); a release download is verified against
  its SHA256SUMS by core.releases.download.
- Then the typed confirmation of the update itself; the update runs as an exclusive job through
  the device module (LC1020E.update_app / boot_update, C2C53T.update_app), which reconnects by
  the USB serial number and checks the UID and the last update result.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QHeaderView, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...core import releases
from ...devices import c2c53t as c2
from ...devices import lc1020e as lc
from ..base import Page, describe_error, row
from ..widgets import Badge, Card, DeviceDrawing, StatRow, label, set_kind
from .data_pages import fit_table, setup_table

# the badge of a release by its relation to the meter (core.releases.relation)
RELATION_BADGE = {"newer": ("newer", "newer"), "same": ("installed", "installed"),
                  "older": ("older", "older"), "unknown": ("not known", "unknown")}


def pair(name: str, value: QWidget) -> QWidget:
    """A caption over a mono value (the reference meter page's Firmware / Bootloader row)."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(3)
    v.addWidget(label(name, "caption"))
    v.addWidget(value)
    return w


def _cell(*widgets_and_align) -> QWidget:
    """A table cell holding widgets in a row: _cell(w, align) or _cell(w1, align, w2)."""
    box = QWidget()
    h = QHBoxLayout(box)
    h.setContentsMargins(8, 0, 8, 0)
    h.setSpacing(6)
    ws = [w for w in widgets_and_align if isinstance(w, QWidget)]
    align = next((a for a in widgets_and_align if not isinstance(a, QWidget)), Qt.AlignLeft)
    if align == Qt.AlignRight:
        h.addStretch(1)
    for w in ws:
        h.addWidget(w, 0, Qt.AlignVCenter)
    if align != Qt.AlignRight:
        h.addStretch(1)
    return box


def header_card(title_label, sub_label) -> tuple[Card, QVBoxLayout]:
    """The meter page's top card: the device drawing, the model name and the line under it;
    returns the card and the column right of the drawing."""
    card = Card()
    top = QHBoxLayout()
    top.setSpacing(18)
    top.addWidget(DeviceDrawing(), 0, Qt.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(4)
    col.addSpacing(6)
    col.addWidget(title_label)
    col.addWidget(sub_label)
    col.addStretch(1)
    top.addLayout(col, 1)
    card.add(top)
    return card, col


def check_file(model: str, data: bytes) -> str:
    """A short description of an image that passes the model's file check; raises the module's
    error otherwise (DESIGN 9: a file of the other meter is refused)."""
    if model == "LC1020E":
        if len(data) >= 4 and struct.unpack_from("<I", data)[0] != lc.UF2_MAGIC0:
            raise lc.DeviceError("not a UF2 file: the LC1020E takes its application as .uf2")
        blocks = lc.check_uf2(data)
        return "UF2, %d blocks" % blocks
    img = c2.check_app_image(data)
    return "%d bytes, CRC-32 0x%08X, stack pointer 0x%08X, reset vector 0x%08X" % (
        img.length, img.crc32, img.stack_pointer, img.reset_vector)


def write_plain(target: str, data: bytes) -> None:
    """A plain open / write / flush of a new file: no extended attributes, so macOS writes no "._"
    companion (DESIGN 13.14); on macOS the flush goes through the drive cache (F_FULLFSYNC)."""
    with open(target, "wb") as g:
        g.write(data)
        g.flush()
        try:
            import fcntl
            fcntl.fcntl(g.fileno(), fcntl.F_FULLFSYNC)
        except (ImportError, AttributeError, OSError):
            os.fsync(g.fileno())


class ImageGate:
    """The two questions before an image goes to a meter: is its SHA-256 a published one (13.26),
    and the typed confirmation of the action. `page` is the asking page."""

    def __init__(self, page: Page, model: str):
        self.page, self.model = page, model

    def check_hash(self, data: bytes, then, slot: bool = False) -> None:
        """Looks the SHA-256 up in the releases' SHA256SUMS (a job: network) and calls
        then(sha256, known_set, confirm_foreign) in the GUI thread, or says why it stops.
        slot=True: an LC1020E bootloader slot file, looked up in every SHA256SUMS line of the
        releases (releases.listed_hashes), with its own warning text."""
        sha = hashlib.sha256(data).hexdigest()
        get = self.page.ctx.release_get
        repo = releases.REPOS[self.model]

        def done(found):
            known, reason = found
            if known is not None and sha in known:
                tag, name = known[sha]
                self.page.say("SHA-256 %s is %s of release %s" % (sha, name, tag))
                then(sha, set(known), None)
                return
            if slot:
                listed = sorted({name for _tag, name in (known or {}).values()})
                why = ("its SHA-256 %s is in no SHA256SUMS of the releases of %s. Those list %s; "
                       "no release publishes a bootloader slot file, so every slot file counts as "
                       "not ours" % (sha, repo, ", ".join(listed) or "no files")) \
                    if known is not None else \
                    ("the release list could not be read (%s), so the SHA-256 %s cannot be "
                     "checked" % (reason, sha))
                word = self.page.confirm(
                    "Slot file not from a release",
                    "This bootloader slot file is not one of the published releases: %s. A slot "
                    "that does not start leaves the meter to the starter's fallback (slot F). "
                    "Install it anyway?" % why)
            else:
                why = ("its SHA-256 %s is in no SHA256SUMS of the releases of %s" % (sha, repo)) \
                    if known is not None else \
                    ("the release list could not be read (%s), so the SHA-256 %s cannot be "
                     "checked" % (reason, sha))
                word = self.page.confirm(
                    "Image not from a release",
                    "This image is not one of the published releases: %s. An image that is not "
                    "ours may not run on the meter. Install it anyway?" % why)
            if word is None:
                return
            then(sha, set(known or ()), word)

        def lookup(_p):
            try:
                if slot:
                    return releases.listed_hashes(self.model, get), None
                return releases.known_image_hashes(self.model, get), None
            except (releases.ReleaseError, OSError) as e:
                return None, str(e)

        self.page.run("release hash check", lookup, done)


# ----------------------------------------------------------------------------------------------
# Meter page
# ----------------------------------------------------------------------------------------------

class MeterPage(Page):
    title = "Meter"

    def __init__(self, ctx, device, port):
        super().__init__(ctx, device)
        self.port = port
        self.is_lc = device.model == "LC1020E"
        self.status_names = lc.STATUS_NAME if self.is_lc else c2.STATUS_NAME
        mod = lc if self.is_lc else c2
        self.mod = mod

        grid = QGridLayout()
        grid.setSpacing(16)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 1)
        left = QVBoxLayout()
        left.setSpacing(16)
        right = QVBoxLayout()
        right.setSpacing(16)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        self.body.addLayout(grid)

        # header card: drawing, model, port, the identity pairs, the INFO rows, battery
        self.info_labels = {}
        name = label("—", "name")
        sub = label("—", "caption")
        card, _col = header_card(name, sub)
        self.info_labels["model"] = name
        self.info_labels["port"] = sub
        pairs = QGridLayout()
        pairs.setHorizontalSpacing(18)
        for col, (key, title, stretch) in enumerate((("firmware", "Firmware", 2),
                                                     ("slot", "Bootloader", 1),
                                                     ("uid", "Chip UID", 3),
                                                     ("companion", "Companion", 1))):
            v = label("—", "value")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            self.info_labels[key] = v
            pairs.addWidget(pair(title, v), 0, col)
            pairs.setColumnStretch(col, stretch)
        card.add(pairs)
        rows = (("protocol", "Protocol version"), ("caps", "Capability bits"),
                ("app_size", "Application size"), ("update", "Last update result"),
                ("serial", "USB serial number"))
        for key, title in rows:
            r = StatRow(title)
            self.info_labels[key] = r.value
            card.add(r)
        bat = StatRow("Battery", line=False)
        self.battery_label = bat.value
        card.add(bat)
        for key, lab in self.info_labels.items():
            lab.setObjectName("info_" + key)
        self.battery_label.setObjectName("battery")
        self.info_button = self.control(QPushButton("Read INFO again"), mod.INFO)
        self.info_button.clicked.connect(self.read_info)
        self.battery_button = self.control(QPushButton("Read battery"), mod.BATTERY)
        self.battery_button.clicked.connect(self.read_battery)
        card.add(row(self.info_button, self.battery_button))
        left.addWidget(card)

        # install a version: the releases as a table, a file of one's own
        update_code = lc.UPDATE_MODE if self.is_lc else c2.FW_BEGIN
        self.update_code = update_code
        inst = Card("Install a version")
        self.check_button = set_kind(QPushButton("Refresh list"), "small")
        self.check_button.clicked.connect(lambda: self.check_releases(False))
        inst.add_right(self.check_button)
        self.release_page = set_kind(QPushButton("Releases page"), "small")
        self.release_page.clicked.connect(self._open_release_page)
        inst.add_right(self.release_page)
        inst.add(label("The releases of the %s firmware; a release download is checked against "
                       "its SHA256SUMS, a file of your own gets its own warning."
                       % device.model, "hint", word_wrap=True))
        self.release_table = QTableWidget(0, 4)
        self.release_table.setObjectName("releases")
        self.release_table.setHorizontalHeaderLabels(["Version", "Released", "On this meter", ""])
        setup_table(self.release_table)
        hh = self.release_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.release_table.hide()
        inst.add(self.release_table)
        self.no_releases = label("Refresh list asks GitHub for the releases.", "hint")
        inst.add(self.no_releases)
        self.release_note = label("", "hint", word_wrap=True)
        inst.add(self.release_note)
        self.file_button = self.control(set_kind(QPushButton("Install a file…"), "danger"),
                                        update_code)
        self.file_button.clicked.connect(self.update_from_file)
        inst.add(row(self.file_button))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress_text = label("", "mono", word_wrap=True)
        inst.add(self.progress)
        inst.add(self.progress_text)
        left.addWidget(inst)
        left.addStretch(1)

        # bootloader slots (LC1020E)
        if self.is_lc:
            slots = Card("Bootloader slots")
            self.boot_status_button = self.control(set_kind(QPushButton("Read"), "small"),
                                                   lc.BOOT_STATUS)
            self.boot_status_button.clicked.connect(self.read_boot_status)
            slots.add_right(self.boot_status_button)
            self.boot_file_button = self.control(
                set_kind(QPushButton("Install a slot file…"), "danger"), lc.BOOT_BEGIN)
            self.boot_file_button.clicked.connect(self.boot_update)
            self.boot_text = label("Read shows the three slots and the record of the next "
                                   "start.", "mono", word_wrap=True)
            slots.add(self.boot_text)
            slots.add(row(self.boot_file_button))
            right.addWidget(slots)

        # the meter: restart, power off
        mc = Card("Meter")
        self.reset_button = self.control(QPushButton("RESET (restart)"), mod.RESET)
        self.reset_button.clicked.connect(self.reset)
        power_code = lc.POWER_OFF if self.is_lc else c2.KEY
        self.power_button = self.control(set_kind(QPushButton("Power off…"), "danger"),
                                         power_code)
        self.power_button.clicked.connect(self.power_off)
        mc.add(row(self.reset_button, self.power_button))
        mc.add(label("RESET restarts the meter: its port goes and comes back. Power off saves "
                     "the meter's settings after a backup.", "hint", word_wrap=True))
        right.addWidget(mc)
        right.addStretch(1)
        self.releases: list = []
        self._release_buttons: list = []
        self.show_info(device.info)

    # -- INFO / battery -----------------------------------------------------------------------
    def show_info(self, info) -> None:
        mod = self.mod
        L = self.info_labels
        L["model"].setText(self.dev.model)
        L["firmware"].setText(info.firmware)
        L["uid"].setText(info.uid_hex)
        L["serial"].setText(self.port.serial or "none reported by the system")
        L["port"].setText("Connected · %s" % self.port.device)
        L["protocol"].setText(str(info.protocol))
        L["caps"].setText("0x%08X" % info.capabilities)
        L["slot"].setText("%s · v%d" % (mod.SLOT_NAME.get(info.running_slot,
                                                          "%d" % info.running_slot),
                                        info.slot_version))
        L["companion"].setText(getattr(self.ctx, "version", "") or "—")
        L["app_size"].setText("%d bytes" % info.app_size)
        L["update"].setText("%d (%s)" % (info.update_result,
                                         mod.UPDATE_RESULT.get(info.update_result, "?")))

    def read_info(self):
        dev = self.dev
        return self.run("INFO", lambda p: dev.read_info(), self._info_read)

    def _info_read(self, info) -> None:
        self.show_info(info)
        self.say("INFO read")

    def read_battery(self):
        dev = self.dev
        return self.run("battery", lambda p: dev.battery(), self._battery)

    def _battery(self, b) -> None:
        if self.is_lc:
            text = "%.2f V, icon level %d of 4%s" % (b.volts, b.level,
                                                     ", low-voltage popup shown" if b.low_popup else "")
        else:
            text = "level %d (5..100 as the meter shows it), charging flag %d" % (
                b.level, 1 if b.charging else 0)
        self.battery_label.setText(text)
        self.say("battery: " + text)

    def on_shown(self) -> None:
        if not self.busy and self.dev.has(self.mod.BATTERY):
            self.read_battery()

    # -- releases -----------------------------------------------------------------------------
    def check_releases(self, auto: bool):
        model = self.dev.model
        get = self.ctx.release_get

        def done(found):
            self.releases = found
            info = self.dev.info
            installed = self.ctx.settings.installed_release(model, info.uid_hex)
            newer = None
            rows = []
            for r in found:
                rel = releases.relation(model, info.firmware, r, installed)
                if rel == "newer" and newer is None:
                    newer = r
                rows.append((r, rel))
            self.show_releases(rows)
            try:
                self.ctx.settings.mark_release_checked()
            except OSError as e:
                self.say("release check time not stored: %s" % e, error=True)
            self.release_note.setText(
                ("Release %s is newer than the meter's firmware." % newer.tag) if newer else
                ("%d releases; none is known to be newer (LC1020E: the INFO text carries no "
                 "release version, only a release this program installed is known)."
                 % len(found) if model == "LC1020E" else
                 "%d releases; none is newer than %s." % (len(found), info.firmware)))
            self.refresh_controls()

        return self.run("release check" + (" (automatic)" if auto else ""),
                        lambda p: releases.list_releases(model, get), done)

    def show_releases(self, rows) -> None:
        """The release table: version, date, the badge of its relation to the meter, and its
        Install and Page buttons (Install registered as a command control of the update)."""
        self.release_rows = list(rows)
        t = self.release_table
        for b in self._release_buttons:
            self._controls = [(c, k) for c, k in self._controls if c is not b]
        self._release_buttons = []
        t.setRowCount(0)
        for r, rel in rows:
            i = t.rowCount()
            t.insertRow(i)
            t.setItem(i, 0, QTableWidgetItem(r.tag))
            t.setItem(i, 1, QTableWidgetItem(r.published or "?"))
            text, kind = RELATION_BADGE.get(rel, (rel, "unknown"))
            t.setCellWidget(i, 2, _cell(Badge(text, kind), Qt.AlignLeft))
            inst = self.control(set_kind(QPushButton("Install"), "small"), self.update_code)
            if rel == "newer":
                set_kind(inst, "primary")
            inst.setObjectName("install_%s" % r.tag)
            inst.clicked.connect(lambda _=False, r=r: self.update_from_release(r))
            page = set_kind(QPushButton("Page"), "small")
            page.clicked.connect(lambda _=False, r=r: QDesktopServices.openUrl(QUrl(r.page_url)))
            self._release_buttons.append(inst)
            t.setCellWidget(i, 3, _cell(inst, Qt.AlignRight, page))
        t.setVisible(bool(rows))
        self.no_releases.setVisible(not rows)
        self.no_releases.setText("no releases published")
        fit_table(t)

    def _open_release_page(self) -> None:
        QDesktopServices.openUrl(QUrl("https://github.com/%s/releases"
                                      % releases.REPOS[self.dev.model]))

    # -- updates ------------------------------------------------------------------------------
    def update_from_file(self) -> None:
        path = self.ctx.ask_open("Application image for the %s" % self.dev.model,
                                 "LC1020E image (*.uf2)" if self.is_lc else "2C53T image (*.bin)")
        if path:
            self.start_update(path, None)

    def update_from_release(self, r=None):
        """Downloads release r (verified against its SHA256SUMS) and starts its update."""
        if r is None:
            return None
        get, cache = self.ctx.release_get, self.ctx.cache_dir

        def got(path):
            self.say("downloaded and verified %s" % path)
            self.start_update(path, r)

        return self.run("download %s" % r.tag, lambda p: releases.download(r, get, cache), got)

    def start_update(self, path: str, release) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
            desc = check_file(self.dev.model, data)
        except (OSError, lc.DeviceError, c2.DeviceError) as e:
            self.say("refused before anything was sent: %s" % e, error=True)
            return
        gate = ImageGate(self, self.dev.model)

        def go(sha, known, foreign):
            if self.is_lc:
                text = ("Both settings copies (calibration, serial, options) are read into a "
                        "backup file first. Then the LC1020E restarts into the slot bootloader's "
                        "update mode (UPDATE_MODE), the program copies %s (%s) onto its update "
                        "drive as a plain file, the "
                        "meter programs it and restarts. The program then waits for the port of "
                        "the same USB serial number and checks that INFO reports the last update "
                        "as OK." % (os.path.basename(path), desc))
            else:
                text = ("The settings / calibration page is read into a backup first. Then %s "
                        "(%s) is staged on the meter as 2:/UPDATE/APP.BIN (FW_BEGIN, FW_DATA, "
                        "FW_END), and FW_INSTALL restarts the meter into its bootloader, which "
                        "programs the staged image only if it is complete and correct. The "
                        "program then waits for the port and checks that INFO reports the last "
                        "update as OK." % (os.path.basename(path), desc))
            word = self.confirm("Update the %s application" % self.dev.model, text)
            if word is None:
                return
            self._run_update(path, word, known, foreign, release)

        gate.check_hash(data, go)

    def _run_update(self, path, word, known, foreign, release):
        dev = self.dev
        prog = self._progress
        if self.is_lc:
            finder = self.ctx.drives_snapshot

            def job(p):
                return dev.update_app(path, finder, p, confirm=word)
        else:
            def job(p):
                return dev.update_app(path, p, confirm=word, known_sha256=known or None,
                                      confirm_foreign=foreign)

        def done(res):
            info = res if self.is_lc else res.after
            self.show_info(info)
            if release is not None:
                try:
                    self.ctx.settings.record_install(dev.model, info.uid_hex, release.tag)
                except (ValueError, OSError) as e:
                    self.say("installed release not stored: %s" % e, error=True)
            self.say("updated: %s" % info.firmware)

        self.progress.setValue(0)
        return self.run("application update", job, done, exclusive=True, progress=prog)

    def _progress(self, frac: float, text: str) -> None:
        self.progress.setValue(int(frac * 100))
        self.progress_text.setText(text)

    def read_boot_status(self):
        dev = self.dev
        return self.run("BOOT_STATUS", lambda p: dev.boot_status(), self._boot_shown)

    def _boot_shown(self, st) -> None:
        parts = ["%s: version %d, %d bytes, CRC-32 0x%08X, %s" % (
            lc.SLOT_NAME[i], s.version, s.length, s.crc, "passes the check" if s.ok else
            "does not pass the check") for i, s in enumerate(st.slots)]
        parts.append("newest record: slot %s %s (sequence %d); next start: slot %s; free slot: %s"
                     % (lc.SLOT_NAME.get(st.newest_slot, st.newest_slot),
                        lc.RECORD_STATE.get(st.newest_state, st.newest_state), st.newest_sequence,
                        lc.SLOT_NAME.get(st.next_slot, st.next_slot),
                        lc.SLOT_NAME.get(st.free_slot, st.free_slot)))
        self.boot_text.setText("\n".join(parts))
        self.say("bootloader slots read")

    def boot_update(self) -> None:
        path = self.ctx.ask_open("Bootloader slot file for the LC1020E", "Slot file (*.bin)")
        if path:
            self.start_boot_update(path)

    def start_boot_update(self, path: str) -> None:
        """The slot file goes through the 13.26 gate (its SHA-256 against the releases), then the
        typed confirmation, then LC1020E.boot_update as an exclusive job."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.say("refused before anything was sent: %s" % e, error=True)
            return
        dev = self.dev

        def go(sha, known, foreign):
            word = self.confirm(
                "Install a bootloader slot",
                "Both settings copies (calibration, serial, options) are read into a backup file "
                "first. Then the slot file %s is checked for the free slot that BOOT_STATUS names, "
                "written into that slot (BOOT_BEGIN, BOOT_DATA, BOOT_END: every word read back, "
                "the slot checked as the starter checks it), recorded as INSTALL (BOOT_INSTALL), "
                "and the meter restarts and starts the new slot. The program then reconnects and "
                "checks that the slot runs." % os.path.basename(path))
            if word is None:
                return

            def done(res):
                st, info = res
                self._boot_shown(st)
                self.show_info(info)
                self.say("bootloader slot %s runs" % lc.SLOT_NAME.get(info.running_slot, "?"))

            self.progress.setValue(0)
            self.run("bootloader slot update",
                     lambda p: dev.boot_update(path, p, confirm=word, known_sha256=known,
                                               confirm_foreign=foreign),
                     done, exclusive=True, progress=self._progress)

        ImageGate(self, dev.model).check_hash(data, go, slot=True)

    # -- reset / power off --------------------------------------------------------------------
    def reset(self):
        dev = self.dev
        return self.run("RESET", lambda p: dev.reset(),
                        lambda r: self.say("RESET sent: the meter restarts, its port goes and "
                                           "comes back"))

    def power_off(self):
        dev = self.dev
        if self.is_lc:
            text = ("POWER_OFF runs the meter's own power-off path: the meter saves its settings, "
                    "beeps and switches off; on USB power it enters powered-off mode (SL 7.5). "
                    "Both settings copies are read into a backup file first.")
        else:
            text = ("Key 1: the meter saves its settings and switches off (S2 4.7). The settings / "
                    "calibration page is read into a backup file first.")
        word = self.confirm("Power off the %s" % dev.model, text)
        if word is None:
            return None
        return self.run("power off", lambda p: dev.power_off(word),
                        lambda r: self.say("power off sent"), exclusive=True)


# ----------------------------------------------------------------------------------------------
# A meter present only as its update drive
# ----------------------------------------------------------------------------------------------

DRIVE_STATUS_TEXT = {
    "ready": "ready for a file",
    "success": "the last file was installed",
    "failed": "the last install failed: the drive stays, the file can be copied again",
    "large": "the last file was too large",
    "unknown": "the bootloader reports Unkown",
    "jump": "the bootloader reports Jump",
    "unmounted": "not mounted: the file cannot be read yet",
}
COPY_STATES = {"LC1020E": ("ready",), "2C53T": ("ready", "failed")}


class DrivePage(Page):
    """The update page of a meter found only as its update drive (DESIGN 5, 13.3, 13.14).

    2C53T: slot_meter is (USB serial, UID, bootloader version) of the 2C53T this drive is taken
    to belong to: the 2C53T port seen last ran the slot bootloader, its port is gone and no other
    2C53T port was seen since (MainWindow.c2_drive_owner); None with why_unknown otherwise, and
    the copy is refused (13.14)."""

    title = "Update drive"

    def __init__(self, ctx, drive, slot_meter=None, why_unknown: str = ""):
        super().__init__(ctx)
        self.drive = drive
        self.slot_meter = slot_meter
        self.why_unknown = why_unknown
        name = label("%s in update mode" % drive.model, "name")
        sub = label("Update drive · %s" % drive.path, "caption")
        card, _col = header_card(name, sub)
        rows = [("Drive", drive.path), ("USB", "%04X:%04X" % (drive.vid, drive.pid))]
        for title, text in rows:
            card.add(StatRow(title, text))
        st = StatRow("Status", "%s (%s)" % (drive.status, DRIVE_STATUS_TEXT.get(drive.status, "?")),
                     line=drive.status == "unmounted")
        self.state = st.value
        self.state.setObjectName("drive_status")
        card.add(st)
        if drive.status == "unmounted":
            card.add(StatRow("Mount it", lc.MOUNT_COMMAND % drive.path, line=False))
        self.body.addWidget(card)
        inst = Card("Install a file")
        self.copy_button = self.control(set_kind(QPushButton("Copy an application file…"),
                                                 "danger"))
        self.copy_button.clicked.connect(self.copy_file)
        inst.add(row(self.copy_button))
        self.reason = label(self.refusal() or "", "label", word_wrap=True)
        self.reason.setObjectName("drive_refusal")
        inst.add(self.reason)
        inst.add(label("The meter's bootloader installs a file copied onto its update drive and "
                       "restarts.", "hint", word_wrap=True))
        self.body.addWidget(inst)
        self.body.addStretch(1)

    def refusal(self) -> str | None:
        """Why no file is copied to this drive, or None."""
        if self.drive.status not in COPY_STATES[self.drive.model]:
            return "The drive's status is %s: a file is copied only in the states %s." % (
                self.drive.status, ", ".join(COPY_STATES[self.drive.model]))
        if self.drive.model == "2C53T" and self.slot_meter is None:
            return ("Refused (DESIGN 13.14): %s, so this drive may be the stock bootloader's, "
                    "which did not take a plain copy on macOS (2C53T README). Connect the meter "
                    "once with its application running, or use the procedure of the 2C53T "
                    "README." % (self.why_unknown or "the meter of this drive is not known"))
        return None

    def extra_enabled(self, widget) -> bool:
        return self.refusal() is None

    def copy_file(self) -> None:
        model = self.drive.model
        path = self.ctx.ask_open("Application image for the %s" % model,
                                 "LC1020E image (*.uf2)" if model == "LC1020E" else
                                 "2C53T image (*.bin)")
        if path:
            self.copy_path(path)

    def copy_path(self, path: str) -> None:
        model, drive = self.drive.model, self.drive
        why = self.refusal()
        if why is not None:
            self.say(why, error=True)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            desc = check_file(model, data)
        except (OSError, lc.DeviceError, c2.DeviceError) as e:
            self.say("refused before anything was written: %s" % e, error=True)
            return
        target = os.path.join(drive.path, lc.DRIVE_FILE if model == "LC1020E"
                              else os.path.basename(path))

        def go(sha, known, foreign):
            text = ("%s (%s) is written as a plain file to %s. The meter's bootloader installs "
                    "it and restarts." % (os.path.basename(path), desc, target))
            if model == "2C53T":
                serial, uid, version = self.slot_meter
                text += (" The 2C53T this program saw last is serial %s (UID %s) with the slot "
                         "bootloader version %d; its port is gone and no other 2C53T port was "
                         "seen since, so this drive is taken as that meter's slot bootloader "
                         "drive." % (serial, uid, version))
            else:
                text += (" The drive was found by its USB VID:PID 19F5:5740, its label and its "
                         "Ready.TXT (DESIGN 13.3): confirm that it is your LC1020E's update "
                         "drive.")
            word = self.confirm("Copy to the update drive", text)
            if word is None:
                return
            self.run("copy to the update drive", lambda p: write_plain(target, data),
                     lambda r: self.say("copied to %s: the meter installs it and restarts; its "
                                        "port comes back when it runs" % target),
                     exclusive=True)

        ImageGate(self, model).check_hash(data, go)
