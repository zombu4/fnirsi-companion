"""The program's window: sidebar, header, the views of the active meter, status line.

Frame (DESIGN_UI sections 2 and 6): a sidebar (logo tile, one item per view, Settings at the bottom;
Ctrl+1..9 follow its order), a header (title, the chips of firmware, bootloader, port, link, live
readings, recording, update drive and a failed connection; the meter choice when there is a choice
to make; Connect / Disconnect), a stack of the views, and the status line (the last note, the
Cancel job button, the DTR state and the link counters). A view shows one or more pages; the
pages hold the commands and their logic (ui.base.Page), a view only places them:
  Live         LivePage, with the model's measurement controls (LcrPage / MultimeterPage) and its
               row of meter keys (LcKeysPage / C2KeysPage) attached while a meter is connected
  Trend, Log   TrendPage, LogPage
  Update       DrivePage, while an update drive is shown
  LC1020E      Compare (ComparePage); Meter settings (LcSettingsPage, CalibrationPage, BackupPage)
  2C53T        Oscilloscope (ScopePage); Generator; Files; Meter settings (C2SettingsPage)
  Meter        MeterPage;  Screen  ScreenshotPage
  Diagnostics  the model's diagnostics page, its Keys page and the EventsPage
  Settings     PreferencesPage

Detection and the switch of the active meter (DESIGN 5, 13.13, 13.19):
- The watcher (link.discover.Watcher, own threads) reports the meters' serial ports and the update
  drives; the device bar lists all of them. Nothing is opened that the watcher did not list.
- With auto-connect on, the first meter found is opened when none is active, and a meter plugged
  in while another one is active becomes the active one when the preference "switch to a new
  meter" is set. Never while an exclusive job holds the port: the switch waits for its end.
- Opening: SerialLink in the GUI thread (its signals are delivered to receivers there), INFO and
  identify() in a job; a mismatch ("meter answers as ...") or an open error ("permission denied",
  DESIGN 13.16) is shown in the device bar and the port is not tried again until it is re-plugged
  or chosen by hand. Then the model's Device object is created in the GUI thread, its pages
  replace the previous set, the live stream starts, and at the first identification of a 2C53T its
  settings / calibration page is backed up (DESIGN 13.1).
- A meter whose port goes (unplugged, RESET, restart after a save, drive sharing) is waited for by
  its USB serial number; when a port of the same model and serial number comes back it is opened
  again. Ports without a serial number are not followed (13.19: never by path).
- During an exclusive job a restart of the meter is followed by the device module through
  reconnect(uid, timeout), which waits for the port of the same serial number, opens it in the GUI
  thread and checks the UID with INFO.

Status bar: the link's counters, the LC1020E's dropped-frame counter of STATE, and for the 2C53T
the DTR state, which holds its auto power-off (DESIGN 13.23); with the preference set, DTR is
dropped after 10 minutes without user action and set again at the next one. A Cancel button is
enabled while the exclusive job is in a phase in which it may stop between requests (DESIGN 3);
closing the window during an exclusive job offers that cancel, or says what the job waits for.

2C53T update drives (DESIGN 13.14): the drive (VID:PID 2E3C:5720, label IAP) carries no serial
number. It is taken as a slot bootloader's drive only when the 2C53T port that appeared last ran
the slot bootloader (INFO bootloader version 1 or above), that port is gone and no other 2C53T
port appeared since (c2_drive_owner); otherwise the copy is refused.
"""

from __future__ import annotations

import concurrent.futures
import errno
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..core import paths
from ..devices import c2c53t as c2
from ..devices import lc1020e as lc
from ..devices.base import DeviceError, IdentifyError, PortLost, identify
from ..link import discover
from ..link.port import SerialLink
from . import confirm as confirm_mod
from . import style
from .base import describe_error
from .c2c53t import panels as c2_panels
from .c2c53t.files import FilesPage
from .c2c53t.panels import GeneratorPage, MultimeterPage
from .c2c53t.scope import ScopePage
from .c2c53t.tools import C2DiagnosticsPage, C2KeysPage, C2SettingsPage
from .common.data_pages import LiveModel, LivePage, LogPage, TrendPage
from .common.meter import DrivePage, MeterPage
from .common.misc import EventsPage, PreferencesPage, ScreenshotPage
from .jobs import JobRefused, JobRunner
from .lc1020e import lcr as lc_lcr
from .lc1020e.lcr import ComparePage, LcrPage
from .lc1020e.storage import BackupPage, CalibrationPage, LcSettingsPage
from .lc1020e.tools import LcDiagnosticsPage, LcKeysPage
from .widgets import Chip, ElidedLabel, Logo, NavButton, NavList, set_kind

IDLE_LIMIT = 600.0              # seconds without user action before DTR is dropped (13.23)
LC_STATE_PERIOD_MS = 1000       # STATE stream of the LC1020E, so the pages follow the meter's keys
LIVE_TIMEOUT = 3.0              # seconds without a reading after which the Live chip goes off

# sidebar items: key -> (name, icon); the order per model is MainWindow.view_order()
VIEW_INFO = {
    "live": ("Live", "live"), "trend": ("Trend", "trend"), "log": ("Log", "log"),
    "update": ("Update", "drive"), "compare": ("Compare", "compare"),
    "scope": ("Oscilloscope", "scope"), "generator": ("Generator", "generator"),
    "files": ("Files", "files"), "msettings": ("Meter settings", "sliders"),
    "meter": ("Meter", "meter"), "screen": ("Screen", "screen"),
    "diagnostics": ("Diagnostics", "wrench"), "settings": ("Settings", "gear"),
}
MODEL_VIEWS = {"LC1020E": ["compare", "msettings", "meter", "screen"],
               "2C53T": ["scope", "generator", "files", "msettings", "meter", "screen"]}
LOGO_LINES = {"LC1020E": ("LC", "1020E"), "2C53T": ("2C", "53T")}
FILL_VIEWS = ("trend", "scope", "screen")     # views whose page takes the window's height


class View:
    """One sidebar item: its button and a scroll area holding its pages in a column (16 px page
    margin and gap); the pages scroll vertically when the window is smaller than they are."""

    def __init__(self, key: str):
        self.key = key
        self.name, icon = VIEW_INFO[key]
        self.button = NavButton(key, self.name, icon)
        self.area = QScrollArea()
        self.area.setObjectName("view_" + key)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("viewcontent")
        self.lay = QVBoxLayout(self.content)
        self.lay.setContentsMargins(16, 16, 16, 16)
        self.lay.setSpacing(16)
        self.fill = key in FILL_VIEWS
        if not self.fill:
            self.lay.addStretch(1)
        self.area.setWidget(self.content)
        self.pages: list = []

    def add(self, page, index: int | None = None) -> None:
        """Puts a page into the column (before the trailing stretch; at `index` when given)."""
        n = len(self.pages)
        at = n if index is None else index
        self.lay.insertWidget(at, page, 1 if self.fill else 0)
        self.pages.insert(at, page)
        page.show()

    def remove(self, page) -> None:
        if page in self.pages:
            self.pages.remove(page)
            self.lay.removeWidget(page)
            page.setParent(None)


def port_key(p) -> tuple:
    """Identity of a port: model and USB serial number; a port without one is told apart by its
    device name within this listing only (it is never followed across a reconnect)."""
    return (p.model, p.serial) if p.serial else (p.model, None, p.device)


def open_error_text(port, exc: BaseException) -> str:
    cause = exc.__cause__
    if isinstance(cause, PermissionError) or getattr(cause, "errno", None) == errno.EACCES \
            or "ermission denied" in str(exc):
        return ("permission denied: this user may not open %s (on Linux the udev rule of the "
                "FNIRSI Companion package gives access)" % port.device)
    return "cannot open %s: %s" % (port.device, exc)


@dataclass
class Conn:
    """The active meter."""
    port: object
    link: object
    device: object
    pages: list = field(default_factory=list)
    meter_page: object = None
    views: dict = field(default_factory=dict)      # view key -> its pages
    diag: list = field(default_factory=list)       # pages of the Diagnostics view
    live: object = None                            # the LiveModel of the Live page


class _GuiCall(QObject):
    """Runs a function in the GUI thread for a job thread and hands its result back."""

    sig = Signal(object)

    def __init__(self):
        super().__init__()
        self.sig.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, item):
        fn, fut = item
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn())
        except BaseException as e:      # handed to the waiting job thread
            fut.set_exception(e)

    def __call__(self, fn, timeout: float = 30.0):
        if QThread.currentThread() == self.thread():
            return fn()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.sig.emit((fn, fut))
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            if fut.cancel():                 # not started: it never runs
                raise PortLost("the window did not answer within %.0f s" % timeout) from None
            return fut.result()              # it runs now: its result is owned here


class MainWindow(QMainWindow):
    def __init__(self, settings, *, watcher, eventlog=None, serial_factory=None,
                 autoconnect: bool = True, release_get=None, cache_dir: str | None = None,
                 confirmer=None, version: str = "", asker=None, volume_probe=None):
        super().__init__()
        self.settings = settings
        self.eventlog = eventlog
        self.watcher = watcher
        self.serial_factory = serial_factory
        self.autoconnect = autoconnect
        self.release_get = release_get
        self.cache_dir = cache_dir or paths.cache_dir()
        self.confirmer = confirmer or (lambda title, text: confirm_mod.ask(self, title, text))
        self.asker = asker or self._ask_yes_no
        self._volume_check = volume_probe is not None or discover.has_probe()
        self.volume_probe = volume_probe if volume_probe is not None else discover.default_probe()
        self.reconnect_gone_wait = 5.0     # seconds the old port may take to leave the list
        self.idle_limit = IDLE_LIMIT

        self.jobs = JobRunner()
        self.jobs.busy_changed.connect(self._busy_changed)
        self.jobs.cancellable_changed.connect(self._cancellable_changed)
        self.jobs.started.connect(self._job_started)
        self.jobs.failed.connect(lambda what, e: self.note("%s failed: %s" % (what, describe_error(e))))
        self._gui = _GuiCall()
        self._lock = threading.Lock()
        self._meter_list: list = []
        self._drive_list: list = []
        self.meters: list = []
        self.drives: list = []
        self.failed: dict = {}
        self._seen: set = set()
        self.active: Conn | None = None
        self.connecting = None
        self.wanted = None                  # (model, serial) to open again when it comes back
        self.wanted_uid: str | None = None  # the UID of the meter that left (13.13)
        self._expect_uid: str | None = None
        self._serial_of: dict[str, str | None] = {}
        self.slot_meters: dict[str, tuple] = {}   # 2C53T serial -> (UID, bootloader version)
        self._c2_present: set = set()             # port keys of the 2C53T ports listed now
        self._last_c2: str | None = None          # serial of the 2C53T port that appeared last
        self._last_c2_known = False               # a 2C53T port appeared in this run
        self._close_after_job = False
        self.backed_up: set[str] = set()
        self._pending_sync = False
        self._pending_drives = False
        self._last_state = None
        self._last_action = time.monotonic()
        self._dtr_dropped = False
        self.drive_page: DrivePage | None = None
        self.version = version
        self._closed_by_user = None          # port key of the meter disconnected by hand
        self._last_reading = None            # monotonic time of the last reading (Live chip)

        self.setWindowTitle("FNIRSI Companion" + (" %s" % version if version else ""))
        self.setMinimumSize(1000, 640)
        self._build_frame()

        previous = eventlog.previous(200) if eventlog is not None else []
        self.events = EventsPage(self, previous)
        self.live = LivePage(self)
        self.trend = TrendPage(self)
        self.log = LogPage(self)
        self.prefs = PreferencesPage(self)
        self.common = [self.live, self.trend, self.log, self.events, self.prefs]
        self.log.recording_changed.connect(self._recording_changed)
        self.views: dict[str, View] = {}
        for key, pages in (("live", [self.live]), ("trend", [self.trend]), ("log", [self.log]),
                           ("diagnostics", [self.events]), ("settings", [self.prefs])):
            v = self._add_view(key)
            for p in pages:
                v.add(p)
        self._current_view: View | None = None
        self._rebuild_nav()
        self.select_view("live")
        for i in range(1, 10):
            sc = QShortcut(QKeySequence("Ctrl+%d" % i), self)
            sc.activated.connect(lambda i=i: self.select_index(i - 1))

        watcher.meters_changed.connect(self._on_meters)
        watcher.drives_changed.connect(self._on_drives)
        watcher.scan_failed.connect(self._on_scan_failed)

        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self._show_stats)
        self.stats_timer.start()
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(30000)
        self.idle_timer.timeout.connect(self._idle_check)
        self.idle_timer.start()
        self.release_timer = QTimer(self)
        self.release_timer.setInterval(3600 * 1000)
        self.release_timer.timeout.connect(self._daily_release_check)
        self.release_timer.start()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)
        except AttributeError:
            pass                              # a Qt without colour schemes: no "system" follow
        style.on_theme(self._refresh_title)
        self._refresh_bar()
        self.apply_theme()

    # ------------------------------------------------------------------------------------------
    # the frame: sidebar, header, view stack, status line (DESIGN_UI 2, 6.1, 6.11, 6.12)
    # ------------------------------------------------------------------------------------------
    def _build_frame(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setAttribute(Qt.WA_StyledBackground, True)
        side.setFixedWidth(76)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 12, 1, 10)
        sl.setSpacing(10)
        self.logo = Logo()
        sl.addWidget(self.logo, 0, Qt.AlignHCenter)
        self.nav = NavList()
        nav_area = QScrollArea()
        nav_area.setWidgetResizable(True)
        nav_area.setFrameShape(QFrame.NoFrame)
        nav_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_area.setWidget(self.nav)
        sl.addWidget(nav_area, 1)
        self.nav_bottom = NavList()
        sl.addWidget(self.nav_bottom)
        outer.addWidget(side)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        outer.addWidget(main, 1)

        # header
        self.header = QWidget()
        self.header.setObjectName("header")
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        self.header.setFixedHeight(64)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(24, 0, 24, 0)
        hl.setSpacing(8)
        self.title_label = QLabel()
        self.title_label.setObjectName("title")
        hl.addWidget(self.title_label)
        hl.addSpacing(10)
        self.fw_chip = Chip("Firmware")
        self.fw_chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)   # elides when narrow
        self.boot_chip = Chip("Bootloader")
        self.port_chip = Chip()
        self.link_chip = Chip("Link", dot="ok")
        self.live_chip = Chip("Live", dot="hol")
        self.rec_chip = Chip("Rec", dot="bad")
        self.drive_chip = Chip("Drive", dot="hol")
        self.fail_chip = Chip("connection failed", dot="bad")
        self.fail_chip.set_bad(True)
        self.chips = [self.fw_chip, self.boot_chip, self.port_chip, self.link_chip, self.live_chip,
                      self.rec_chip, self.drive_chip, self.fail_chip]
        self._chip_wanted = {c: False for c in self.chips}
        for c in self.chips:
            hl.addWidget(c)
            c.hide()
        self.bar_status = QLabel("searching for meters")
        self.bar_status.setObjectName("bar_status")
        self.bar_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hl.addWidget(self.bar_status, 1)
        hl.addStretch(1)
        self.device_list = QComboBox()
        self.device_list.setObjectName("device_list")
        self.device_list.setMinimumWidth(200)
        self.device_list.setMaximumWidth(360)
        self.device_list.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_list.currentIndexChanged.connect(lambda _i: self._show_connect_button())
        hl.addWidget(self.device_list)
        self.connect_button = set_kind(QPushButton("Connect"), "primary")
        self.connect_button.clicked.connect(self.connect_selected)
        hl.addWidget(self.connect_button)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_active)
        hl.addWidget(self.disconnect_button)
        ml.addWidget(self.header)

        self.stack = QStackedWidget()
        ml.addWidget(self.stack, 1)

        # status line
        status = QWidget()
        status.setObjectName("statusline")
        status.setAttribute(Qt.WA_StyledBackground, True)
        status.setFixedHeight(28)
        st = QHBoxLayout(status)
        st.setContentsMargins(24, 0, 24, 0)
        st.setSpacing(14)
        self.status_message = ElidedLabel("")
        self.status_message.setObjectName("status_message")
        st.addWidget(self.status_message, 1)
        self.cancel_button = set_kind(QPushButton("Cancel job"), "small")
        self.cancel_button.setObjectName("cancel_job")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)
        st.addWidget(self.cancel_button)
        self.dtr_label = QLabel("")
        self.dtr_label.setObjectName("dtr")
        st.addWidget(self.dtr_label)
        self.stats_label = QLabel("no link")
        self.stats_label.setObjectName("link_stats")
        st.addWidget(self.stats_label)
        ml.addWidget(status)
        self.setCentralWidget(central)
        self.header.installEventFilter(self)

    def _add_view(self, key: str) -> View:
        v = View(key)
        v.button.clicked.connect(lambda _=False, k=key: self.select_view(k))
        self.views[key] = v
        self.stack.addWidget(v.area)
        return v

    def _drop_view(self, key: str) -> None:
        """Removes a view (its pages were taken out or are deleted by their owner)."""
        v = self.views.pop(key, None)
        if v is None:
            return
        if self._current_view is v:
            self._current_view = None
        self.stack.removeWidget(v.area)
        v.button.setParent(None)
        v.button.deleteLater()
        v.area.deleteLater()

    def view_order(self) -> list[str]:
        """The sidebar's items in their fixed order for the active model (Ctrl+1..9 follow it)."""
        keys = ["live", "trend", "log"]
        if self.drive_page is not None and "update" in self.views:
            keys.append("update")
        if self.active is not None:
            keys += [k for k in MODEL_VIEWS[self.active.device.model] if k in self.views]
        keys += ["diagnostics", "settings"]
        return keys

    def nav_titles(self) -> list[str]:
        return [self.views[k].name for k in self.view_order()]

    def _rebuild_nav(self) -> None:
        for lst in (self.nav, self.nav_bottom):
            while lst.lay.count():
                w = lst.lay.takeAt(0).widget()
                if w is not None:
                    w.setParent(None)
        for k in self.view_order():
            b = self.views[k].button
            (self.nav_bottom if k == "settings" else self.nav).lay.addWidget(b)
            b.show()
        if self._current_view is None or self._current_view.key not in self.views:
            self.select_view("live")
        self.nav.update()
        self.nav_bottom.update()

    def select_view(self, key: str) -> None:
        """Shows view `key`: its pages get on_shown, the pages of the view left on_hidden."""
        new = self.views.get(key)
        if new is None:
            return
        old = self._current_view
        self._current_view = new
        self.stack.setCurrentWidget(new.area)
        for v in self.views.values():
            v.button.setChecked(v is new)
        self.nav.update()
        self.nav_bottom.update()
        if old is new:
            return
        if old is not None and old.key in self.views:
            for p in old.pages:
                p.on_hidden()
        for p in new.pages:
            p.on_shown()

    def select_index(self, i: int) -> None:
        order = self.view_order()
        if 0 <= i < len(order):
            self.select_view(order[i])

    def current_view(self) -> str | None:
        return self._current_view.key if self._current_view is not None else None

    def show_page(self, page) -> None:
        """Shows the view that holds `page`."""
        for k, v in self.views.items():
            if page in v.pages:
                self.select_view(k)
                return

    def _refresh_title(self) -> None:
        ink2 = style.qss(style.color("ink-2"))
        if self.active is not None:
            self.title_label.setText('<span style="font-weight:700">%s</span> <span style="'
                                     'color:%s;font-weight:500">Companion</span>'
                                     % (self.active.device.model, ink2))
        else:
            self.title_label.setText('<span style="font-weight:700">FNIRSI</span> <span style="'
                                     'color:%s;font-weight:500">Companion</span>' % ink2)

    def _want_chip(self, chip: Chip, on: bool) -> None:
        self._chip_wanted[chip] = on

    def _refresh_chips(self) -> None:
        """The header's chips from the connection, the readings, the recording and the drives
        (DESIGN_UI 2, 6.12); then the header is fitted to its width."""
        conn = self.active
        if conn is not None:
            d, info = conn.device, conn.device.info
            fw = info.firmware
            if fw.startswith(d.model):
                fw = fw[len(d.model):].strip()
            self.fw_chip.set_text("Firmware", fw)
            self._want_chip(self.fw_chip, True)
            mod = lc if d.model == "LC1020E" else c2
            slot = mod.SLOT_NAME.get(info.running_slot)
            known = slot is not None and info.running_slot != 0xFF and info.slot_version >= 1
            if known:
                self.boot_chip.set_text("Bootloader", "%s·v%d" % (slot, info.slot_version))
            self._want_chip(self.boot_chip, known)
            self.port_chip.set_text("", conn.port.device)
            self.port_chip.setToolTip("USB serial number %s" % (conn.port.serial or "none"))
            self._want_chip(self.port_chip, True)
        else:
            for c in (self.fw_chip, self.boot_chip, self.port_chip):
                self._want_chip(c, False)
        self.link_chip.set_on(conn is not None and conn.device.link.is_open)
        self._want_chip(self.link_chip, True)
        live = self._last_reading is not None and conn is not None \
            and time.monotonic() - self._last_reading < LIVE_TIMEOUT
        self.live_chip.set_on(live)
        self._want_chip(self.live_chip, True)
        self.rec_chip.set_on(self.log.recorder.recording if hasattr(self, "log") else False)
        self._want_chip(self.rec_chip, True)
        self.drive_chip.set_on(bool(self.drives))
        self._want_chip(self.drive_chip, True)
        fails = {k: v for k, v in self.failed.items() if k != self._closed_by_user}
        if fails:
            self.fail_chip.setToolTip("\n".join(fails.values()))
        self._want_chip(self.fail_chip, bool(fails))
        self.device_list.setVisible(self.device_list.count() > 1 or bool(fails))
        self._show_connect_button()
        self._fit_header()

    def _show_connect_button(self) -> None:
        """One button: Disconnect for the active meter, Connect for any other choice."""
        cur = self.device_list.currentData()
        active_chosen = self.active is not None and (
            self.device_list.isHidden() or cur is None
            or (cur[0] == "meter" and port_key(cur[1]) == port_key(self.active.port)))
        self.disconnect_button.setVisible(active_chosen)
        self.connect_button.setVisible(not active_chosen)

    def _fit_header(self) -> None:
        """Hides the chips that do not fit: Drive and Rec first, then the bootloader chip; the
        firmware chip elides its text (DESIGN_UI 6.12)."""
        hl = self.header.layout()
        m = hl.contentsMargins()
        avail = self.header.width() - m.left() - m.right() - self.title_label.sizeHint().width() \
            - 18
        for w in (self.device_list, self.connect_button, self.disconnect_button):
            if not w.isHidden():
                avail -= w.sizeHint().width() + hl.spacing()
        shown = [c for c in self.chips if self._chip_wanted[c]]
        for c in (self.drive_chip, self.rec_chip, self.boot_chip):
            if sum(x.sizeHint().width() + hl.spacing() for x in shown) <= avail:
                break
            if c in shown:
                shown.remove(c)
        over = sum(x.sizeHint().width() + hl.spacing() for x in shown) - avail
        hint = self.fw_chip.sizeHint().width()
        self.fw_chip.setMaximumWidth(max(110, hint - over) if over > 0 else hint)
        for c in self.chips:
            c.setVisible(c in shown)
        self.bar_status.setVisible(self.active is None)

    @Slot(bool)
    def _recording_changed(self, _on: bool) -> None:
        self.live.show_recording()
        self._refresh_chips()

    def _scheme_changed(self, *_a) -> None:
        if self.settings.get("theme") == "system":
            style.apply(QApplication.instance(), "system")

    # ------------------------------------------------------------------------------------------
    # context offered to the pages
    # ------------------------------------------------------------------------------------------
    def confirm(self, title: str, text: str) -> str | None:
        return self.confirmer(title, text)

    def ask_open(self, title: str, filt: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(self, title, "", filt)
        return path or None

    def ask_save(self, title: str, name: str, filt: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(self, title, name, filt)
        return path or None

    def ask_folder(self, title: str, start: str) -> str | None:
        return QFileDialog.getExistingDirectory(self, title, start) or None

    def note(self, text: str) -> None:
        self.events.add(text)
        if self.eventlog is not None:
            self.eventlog.write(time.time(), text)
        self.status_message.setText(text)

    def publish_state(self, st) -> None:
        self._on_state(st)

    def file_prefix(self) -> str:
        return self.active.device.model if self.active else "FNIRSI"

    def value_header(self) -> str:
        st = self._last_state
        if isinstance(st, c2.State) and not st.dmm.has_si_view:
            return "value as displayed (no prefix)"
        return "value"

    def mounted_meter_volumes(self, vid: int, pid: int) -> list[str] | None:
        """Mount points of the mounted volumes on the USB device vid:pid (the probes of
        link.discover), or None on a system without a probe (the check cannot be made). Runs the
        probe (ioreg / sysfs / Win32): call it from a job thread."""
        if not self._volume_check:
            return None
        return [v.path for v in self.volume_probe({(vid, pid)})
                if v.mounted and (v.vid, v.pid) == (vid, pid)]

    def drives_snapshot(self) -> list:
        """The update drives last listed (callable from any thread)."""
        with self._lock:
            return list(self._drive_list)

    def preferences_changed(self, key: str) -> None:
        if key == "let_2c53t_sleep" and not self.settings.get(key):
            self._restore_dtr()
        if key in ("recordings_folder", "csv_separator"):
            self.log.refresh_files()
        if key == "backups_folder" and self.active is not None:
            self.active.device.backup_folder = Path(self.settings.backups_folder())

    def apply_theme(self) -> None:
        hints = QGuiApplication.styleHints()
        scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(
            self.settings.get("theme"), Qt.ColorScheme.Unknown)
        try:
            hints.setColorScheme(scheme)
        except AttributeError:
            self.note("theme: this Qt cannot set the colour scheme")
        style.apply(QApplication.instance(), self.settings.get("theme"))

    # ------------------------------------------------------------------------------------------
    # watcher
    # ------------------------------------------------------------------------------------------
    @Slot(list)
    def _on_meters(self, lst) -> None:
        with self._lock:
            self._meter_list = list(lst)
        self.meters = list(lst)
        present = {port_key(p) for p in self.meters}
        self.failed = {k: v for k, v in self.failed.items() if k in present}
        c2_now = [p for p in self.meters if p.model == "2C53T"]
        new_c2 = [p for p in c2_now if port_key(p) not in self._c2_present]
        if new_c2:
            serials = {p.serial for p in new_c2}
            self._last_c2 = serials.pop() if len(serials) == 1 else None
            self._last_c2_known = True
        self._c2_present = {port_key(p) for p in c2_now}
        self._refresh_bar()
        self._sync()
        self._recheck_c2_drive_page()

    @Slot(list)
    def _on_drives(self, lst) -> None:
        with self._lock:
            self._drive_list = list(lst)
        self.drives = list(lst)
        self._refresh_bar()
        self._sync_drive_page()

    @Slot(str)
    def _on_scan_failed(self, text: str) -> None:
        self.note("scan: %s" % text)

    def _find_port(self, model: str, serial: str):
        with self._lock:
            for p in self._meter_list:
                if p.model == model and p.serial == serial:
                    return p
        return None

    def _sync(self) -> None:
        """Applies the port list: loss of the active port, reconnect of the wanted one, the
        auto-connect and the switch to a new meter."""
        if self.jobs.busy:
            self._pending_sync = True
            return
        self._pending_sync = False
        present = {port_key(p) for p in self.meters}
        new = [p for p in self.meters if port_key(p) not in self._seen]
        self._seen = present
        conn = self.active
        if conn is not None and port_key(conn.port) not in present:
            if conn.link.is_open:
                conn.link.mark_lost("the port left the system's port list")
            else:
                self._lost(conn, "the port left the system's port list")
            return
        if self.connecting is not None:
            return
        if self.active is None:
            cand = None
            if self.wanted is not None:
                # the meter that left is waited for; another one is opened only by hand
                cand = self._find_port(*self.wanted)
            elif self.autoconnect:
                cand = next((p for p in self.meters if port_key(p) not in self.failed), None)
            if cand is not None:
                self.connect_port(cand)
            return
        if self.autoconnect and self.settings.get("switch_to_new_meter"):
            cand = next((p for p in new if port_key(p) not in self.failed
                         and port_key(p) != port_key(self.active.port)), None)
            if cand is not None:
                self.note("new meter %s (%s): switching to it" % (cand.model, cand.device))
                self.connect_port(cand)

    def _refresh_bar(self) -> None:
        keep = self.device_list.currentData()
        self.device_list.blockSignals(True)
        self.device_list.clear()
        for p in self.meters:
            k = port_key(p)
            text = "%s  serial %s  %s" % (p.model, p.serial or "none", p.device)
            if self.active is not None and port_key(self.active.port) == k:
                text += "  — active"
            elif self.connecting is not None and port_key(self.connecting) == k:
                text += "  — connecting"
            elif k in self.failed:
                text += "  — " + self.failed[k]
            self.device_list.addItem(text, ("meter", p))
        for d in self.drives:
            text = "%s in update mode  %s  (%s)" % (d.model, d.path, d.status)
            if d.status == "unmounted":
                text += "  mount it: " + lc.MOUNT_COMMAND % d.path
            self.device_list.addItem(text, ("drive", d))
        if not self.meters and not self.drives:
            self.device_list.addItem("no meter found", None)
        if keep is not None:
            for i in range(self.device_list.count()):
                if self.device_list.itemData(i) == keep:
                    self.device_list.setCurrentIndex(i)
                    break
        self.device_list.blockSignals(False)
        if self.active is not None:
            d = self.active.device
            # the firmware text of the LC1020E starts with the model ("LC1020E V1.1 OWN")
            fw = d.info.firmware
            self.bar_status.setText("%s, UID %s" % (fw if fw.startswith(d.model)
                                                    else "%s %s" % (d.model, fw), d.info.uid_hex))
        elif self.connecting is not None:
            self.bar_status.setText("connecting to %s %s" % (self.connecting.model,
                                                             self.connecting.device))
        elif self.wanted is not None:
            self.bar_status.setText("waiting for %s serial %s to come back" % self.wanted)
        elif self.drives:
            self.bar_status.setText("%s in update mode" % self.drives[0].model)
        else:
            self.bar_status.setText("no meter connected")
        busy = self.jobs.busy
        self.connect_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(not busy and self.active is not None)
        self.logo.set_lines(*LOGO_LINES.get(self.active.device.model if self.active else "",
                                            ("FN", "—")))
        self._refresh_title()
        self._refresh_chips()

    def _sync_drive_page(self) -> None:
        if self.jobs.busy:
            self._pending_drives = True      # not while a job (its own copy) runs
            return
        self._pending_drives = False
        dp = self.drive_page
        if dp is not None and not any(d.path == dp.drive.path and d.model == dp.drive.model
                                      for d in self.drives):
            self._remove_drive_page()
            dp = None
        if dp is not None:
            cur = next(d for d in self.drives if d.path == dp.drive.path)
            if cur != dp.drive:                      # its status changed
                self._remove_drive_page()
                self.show_drive(cur)
            return
        if self.active is None and self.connecting is None and self.drives:
            self.show_drive(self.drives[0])

    def c2_drive_owner(self) -> tuple:
        """((serial, UID, bootloader version), "") of the 2C53T whose slot bootloader a 2C53T
        update drive is taken to belong to, or (None, why) (module docstring, DESIGN 13.14)."""
        if not self._last_c2_known:
            return None, "this program has not seen a 2C53T in this run"
        s = self._last_c2
        if s is None:
            return None, ("the 2C53T port that appeared last has no USB serial number (or two "
                          "appeared at once), so the meter of this drive is not known")
        if s not in self.slot_meters:
            return None, ("the 2C53T that appeared last (serial %s) was not seen running the slot "
                          "bootloader (INFO bootloader version 1 or above)" % s)
        if any(p.model == "2C53T" and p.serial == s for p in self.meters):
            return None, ("the 2C53T with serial %s is still connected with its application, so "
                          "this drive is not its" % s)
        uid, version = self.slot_meters[s]
        return (s, uid, version), ""

    def _recheck_c2_drive_page(self) -> None:
        dp = self.drive_page
        if dp is None or dp.drive.model != "2C53T" or self.jobs.busy:
            return
        if self.c2_drive_owner()[0] != dp.slot_meter:
            self.show_drive(dp.drive)

    def show_drive(self, drive) -> None:
        self._remove_drive_page()
        owner, why = self.c2_drive_owner() if drive.model == "2C53T" else (None, "")
        self.drive_page = DrivePage(self, drive, owner, why)
        self.drive_page.set_busy(self.jobs.busy)
        self._add_view("update").add(self.drive_page)
        self._rebuild_nav()
        self.select_view("update")

    def _remove_drive_page(self) -> None:
        dp, self.drive_page = self.drive_page, None
        if dp is not None:
            dp.retire()
            v = self.views.get("update")
            if v is not None:
                v.remove(dp)
            dp.deleteLater()
            self._drop_view("update")
            self._rebuild_nav()

    # ------------------------------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------------------------------
    def connect_selected(self) -> None:
        item = self.device_list.currentData()
        if item is None:
            return
        kind, obj = item
        if kind == "drive":
            self.show_drive(obj)
            return
        self.failed.pop(port_key(obj), None)
        if self.active is not None and port_key(self.active.port) == port_key(obj):
            return
        self.connect_port(obj)

    def disconnect_active(self) -> None:
        if self.jobs.busy or self.active is None:
            return
        self.wanted = None
        conn = self.active
        self.failed[port_key(conn.port)] = "disconnected by you (Connect opens it again)"
        self._closed_by_user = port_key(conn.port)    # not shown as a failed connection
        self._teardown(conn)
        self.note("disconnected %s" % conn.device.model)

    def _make_link(self, port) -> SerialLink:
        """Opens a port in the GUI thread (the link lives there)."""
        link = SerialLink(port.device, serial_factory=self.serial_factory)
        link.open()
        link.lost.connect(self._on_link_lost)
        return link

    def connect_port(self, port) -> None:
        if self.jobs.busy:
            self.note("%s waits: a job holds the port" % port.device)
            self._pending_sync = True
            return
        if self.active is not None:
            self._teardown(self.active)
        self._expect_uid = self.wanted_uid if self.wanted is not None and \
            (port.model, port.serial) == self.wanted else None
        self.wanted = None
        self.wanted_uid = None
        self._remove_drive_page()
        try:
            link = self._make_link(port)
        except PortLost as e:
            self.failed[port_key(port)] = open_error_text(port, e)
            self.note(self.failed[port_key(port)])
            self._refresh_bar()
            self._sync()                     # the next meter, if there is one
            return
        self.connecting = port
        self._refresh_bar()
        try:
            self.jobs.submit(lambda p: identify(link, port.model), what="identify %s" % port.device,
                             exclusive=True,
                             on_result=lambda info: self._identified(port, link, info),
                             on_error=lambda e: self._identify_failed(port, link, e))
        except JobRefused as e:
            self.connecting = None
            link.close()
            self.note(str(e))

    def _identify_failed(self, port, link, exc) -> None:
        self.connecting = None
        if isinstance(exc, IdentifyError):
            text = "meter answers as %s (%s)" % (exc.answers_as, exc.detail)
        else:
            text = "no identification: %s" % describe_error(exc)
        self.failed[port_key(port)] = text
        self.note("%s: %s" % (port.device, text))
        self.jobs.submit(lambda p: link.close(), what="close %s" % port.device)
        self._refresh_bar()
        self._sync()                         # the next meter, if there is one
        self._sync_drive_page()

    def _identified(self, port, link, info) -> None:
        self.connecting = None
        if self._expect_uid is not None and info.uid_hex != self._expect_uid:
            self.note("serial %s came back with UID %s, not %s: treated as another meter"
                      % (port.serial, info.uid_hex, self._expect_uid))
        elif self._expect_uid is not None:
            self.note("serial %s is back with the same UID" % port.serial)
        self._expect_uid = None
        cls = lc.LC1020E if port.model == "LC1020E" else c2.C2C53T
        dev = cls(link, info, backup_folder=Path(self.settings.backups_folder()),
                  reconnect=self._reconnect_function(port.model))
        self._serial_of[info.uid_hex] = port.serial
        if port.model == "2C53T":
            self._note_c2(port.serial, info)
        conn = Conn(port, link, dev)
        self.active = conn
        dev.reading.connect(self._on_reading)
        dev.state.connect(self._on_state)
        dev.meter_event.connect(self._on_meter_event)
        conn.pages = self._build_pages(conn)
        self._show_pages(conn)
        self.note("connected %s %s, UID %s, on %s (serial %s)" % (
            dev.model, info.firmware, info.uid_hex, port.device, port.serial or "none"))
        self._refresh_bar()
        self._start(conn, first=True)
        if self.settings.release_check_due(True):
            conn.meter_page.check_releases(True)

    def _note_c2(self, serial, info) -> None:
        """A 2C53T identified (or back after a restart): it is the one seen last; with the slot
        bootloader (INFO bootloader version >= 1 and a known running slot) its serial is kept."""
        self._last_c2 = serial
        self._last_c2_known = True
        if not serial:
            return
        if info.slot_version >= 1 and info.running_slot in c2.SLOT_NAME \
                and info.running_slot != 0xFF:
            self.slot_meters[serial] = (info.uid_hex, info.slot_version)
        else:
            self.slot_meters.pop(serial, None)

    def _build_pages(self, conn: Conn) -> list:
        dev, ctx = conn.device, self
        meter = MeterPage(ctx, dev, conn.port)
        conn.meter_page = meter
        if dev.model == "LC1020E":
            lcr, cmp, sett = LcrPage(ctx, dev), ComparePage(ctx, dev), LcSettingsPage(ctx, dev)
            cal, bak, keys = CalibrationPage(ctx, dev), BackupPage(ctx, dev), LcKeysPage(ctx, dev)
            shot, diag = ScreenshotPage(ctx, dev, lc.SCREENSHOT), LcDiagnosticsPage(ctx, dev)
            pages = [lcr, cmp, sett, cal, bak, keys, meter, shot, diag]
            conn.views = {"compare": [cmp], "msettings": [sett, cal, bak], "meter": [meter],
                          "screen": [shot]}
            conn.live = LiveModel(lc_lcr.LCD_WORDS, lcr.lcd_flags, True, lcr, keys.live_keys)
        else:
            mm, scope, gen = MultimeterPage(ctx, dev), ScopePage(ctx, dev), GeneratorPage(ctx, dev)
            files, sett, keys = FilesPage(ctx, dev), C2SettingsPage(ctx, dev), C2KeysPage(ctx, dev)
            shot, diag = ScreenshotPage(ctx, dev, c2.SCREENSHOT), C2DiagnosticsPage(ctx, dev)
            pages = [mm, scope, gen, files, sett, keys, meter, shot, diag]
            conn.views = {"scope": [scope], "generator": [gen], "files": [files],
                          "msettings": [sett], "meter": [meter], "screen": [shot]}
            conn.live = LiveModel(c2_panels.LCD_WORDS, mm.lcd_flags, False, mm, keys.live_keys)
        conn.diag = [diag, keys]
        for p in pages:
            p.set_busy(self.jobs.busy)
        return pages

    def _show_pages(self, conn: Conn) -> None:
        """Puts a connected meter's pages into their views, the Diagnostics view and the Live
        page, and shows its sidebar items."""
        for key, pages in conn.views.items():
            v = self._add_view(key)
            for p in pages:
                v.add(p)
        diag = self.views["diagnostics"]
        for i, p in enumerate(conn.diag):
            diag.add(p, i)
        self.live.attach(conn.live)
        self._rebuild_nav()

    def _start(self, conn: Conn, first: bool) -> None:
        """Live stream (and the LC1020E's STATE stream), the 2C53T's first STATE and, at its first
        identification in this run, its calibration backup (DESIGN 13.1)."""
        dev = conn.device
        folder = Path(self.settings.backups_folder())
        do_backup = dev.model == "2C53T" and dev.info.uid_hex not in self.backed_up \
            and dev.has(c2.CAL_READ)

        def job(_p):
            out = {}
            if dev.model == "LC1020E":
                if dev.has(lc.MEASURE_STREAM):
                    dev.start_live()
                if dev.has(lc.STREAM):
                    dev.stream(LC_STATE_PERIOD_MS)
                if dev.has(lc.STATE):
                    out["state"] = dev.read_state()
            else:
                if do_backup:
                    try:
                        out["backup"] = dev.backup(folder)
                    except Exception as e:     # reported; STATE and the stream still start
                        out["backup_error"] = e
                if dev.has(c2.STATE):
                    out["state"] = dev.read_state()
                if dev.has(c2.STREAM):
                    dev.start_live()
            return out

        def done(out):
            if "backup" in out:
                self.backed_up.add(dev.info.uid_hex)
                self.note("calibration backup of the 2C53T: %s" % out["backup"])
            if "backup_error" in out:
                self.note("calibration backup of the 2C53T FAILED (tried again at the next "
                          "connection; every save makes its own first): %s"
                          % describe_error(out["backup_error"]))
            if "state" in out and self.active is conn:
                self._on_state(out["state"])

        def failed(e):
            self.note("start of %s: %s" % (dev.model, describe_error(e)))

        try:
            self.jobs.submit(job, what="start %s" % dev.model, exclusive=True, on_result=done,
                             on_error=failed)
        except JobRefused as e:
            self.note(str(e))

    def _teardown(self, conn: Conn) -> None:
        """Removes the meter's pages at once; stopping its streams and closing the port run in a
        job (they wait for the meter)."""
        dev, link = conn.device, conn.device.link
        for sig, slot in ((dev.reading, self._on_reading), (dev.state, self._on_state),
                          (dev.meter_event, self._on_meter_event)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.live.meter_gone()                  # takes the model's block off the Live page
        current = self.current_view()
        for p in conn.pages:
            p.retire()
            for v in self.views.values():
                v.remove(p)
            p.setParent(None)
            for w in p.extras:
                w.setParent(None)
                w.deleteLater()
            p.deleteLater()
        for key in conn.views:
            self._drop_view(key)
        conn.pages = []
        if self.active is conn:
            self.active = None
        self._last_state = None
        self._last_reading = None
        self._dtr_dropped = False
        self._rebuild_nav()
        if current not in self.views:
            self.select_view("live")

        def job(_p):
            if link.is_open:
                try:
                    if dev.model == "LC1020E" and dev.has(lc.STREAM):
                        dev.stream(0)
                    dev.stop_live()
                except Exception:        # the port may be gone already: closing is what counts
                    pass
            link.close()

        self.jobs.submit(job, what="close %s" % dev.model, on_result=lambda r: dev.close(),
                         on_error=lambda e: dev.close())
        self._refresh_bar()

    @Slot(str)
    def _on_link_lost(self, reason: str) -> None:
        conn = self.active
        if conn is None or conn.device.link.is_open:
            return                       # an old link, or not the active meter's
        if self.jobs.busy:
            self._pending_sync = True    # the job follows the restart or fails; checked after it
            return
        self._lost(conn, reason)

    def _lost(self, conn: Conn, reason: str) -> None:
        self.wanted = (conn.port.model, conn.port.serial) if conn.port.serial else None
        self.wanted_uid = conn.device.info.uid_hex if self.wanted else None
        self._teardown(conn)
        self.note("%s lost: %s%s" % (conn.device.model, reason,
                                     "; waiting for serial %s" % conn.port.serial
                                     if conn.port.serial else
                                     "; the port has no serial number, choose it again"))
        self._refresh_bar()
        self._sync()
        self._sync_drive_page()

    # ------------------------------------------------------------------------------------------
    # reconnect inside a job (called by the device modules in the job thread)
    # ------------------------------------------------------------------------------------------
    def _reconnect_function(self, model: str):
        def reconnect(uid_hex: str, timeout: float):
            serial = self._serial_of.get(uid_hex)
            if not serial:
                raise DeviceError("the meter's port has no USB serial number: it cannot be found "
                                  "again (ports are followed by serial number only)")
            deadline = time.monotonic() + timeout
            gone = time.monotonic() + self.reconnect_gone_wait
            while time.monotonic() < gone and self._find_port(model, serial) is not None:
                time.sleep(0.1)          # the old port leaves the list first
            last = None
            while time.monotonic() < deadline:
                port = self._find_port(model, serial)
                if port is None:
                    time.sleep(0.2)
                    continue
                try:
                    link = self._gui(lambda: self._make_link(port))
                except PortLost as e:
                    last = e
                    time.sleep(0.5)
                    continue
                try:
                    info = identify(link, model)
                except Exception as e:   # not answering yet: closed, tried again
                    last = e
                    link.close()
                    time.sleep(0.5)
                    continue
                if info.uid_hex != uid_hex:
                    link.close()
                    raise DeviceError("the port of serial %s now has a meter with UID %s"
                                      % (serial, info.uid_hex))
                self._gui(lambda: self._adopt_port(port))
                return link
            raise DeviceError("the meter (serial %s) did not come back within %.0f s%s"
                              % (serial, timeout, ": %s" % last if last else ""))
        return reconnect

    def _adopt_port(self, port) -> None:
        if self.active is not None:
            self.active.port = port

    # ------------------------------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------------------------------
    @Slot(bool)
    def _busy_changed(self, busy: bool) -> None:
        for p in self._all_pages():
            p.set_busy(busy)
        self._refresh_bar()
        job = self.jobs.exclusive_job
        self.cancel_button.setEnabled(busy and job is not None and job.cancel.allowed)
        if not busy:
            self._after_job()
            if self._close_after_job:
                self._close_after_job = False
                QTimer.singleShot(0, self.close)

    @Slot(object)
    def _job_started(self, job) -> None:
        if job is self.jobs.exclusive_job:          # also a job the ending one handed the port to
            self.cancel_button.setEnabled(job.cancel.allowed)

    @Slot(object, bool)
    def _cancellable_changed(self, job, allowed: bool) -> None:
        if job is self.jobs.exclusive_job:
            self.cancel_button.setEnabled(allowed)

    def cancel_job(self) -> bool:
        """The Cancel button: asks the exclusive job to stop at its next safe point."""
        job = self.jobs.exclusive_job
        if job is None:
            return False
        if self.jobs.cancel():
            self.cancel_button.setEnabled(False)
            self.note("cancel of %s requested: it stops before its next request" % job.what)
            return True
        self.note("%s cannot be cancelled now: %s" % (job.what, job.cancel.why_not))
        return False

    def _all_pages(self) -> list:
        out = list(self.common)
        if self.active is not None:
            out += self.active.pages
        if self.drive_page is not None:
            out.append(self.drive_page)
        return out

    def _after_job(self) -> None:
        conn = self.active
        if conn is not None:
            dev = conn.device
            if dev.link is not conn.link:            # the device followed a restart
                conn.link = dev.link
                if conn.meter_page is not None:
                    conn.meter_page.port = conn.port
                    conn.meter_page.show_info(dev.info)
                if dev.model == "2C53T":
                    self._note_c2(conn.port.serial, dev.info)
                self.note("%s is back after its restart (UID %s)" % (dev.model, dev.info.uid_hex))
                self._start(conn, first=False)
                return
            if not conn.link.is_open:
                self._lost(conn, "the port was lost during the job")
                return
        if self._pending_sync:
            self._sync()
        if self._pending_drives:
            self._sync_drive_page()

    # ------------------------------------------------------------------------------------------
    # device feeds
    # ------------------------------------------------------------------------------------------
    @Slot(object)
    def _on_reading(self, r) -> None:
        first = self._last_reading is None or \
            time.monotonic() - self._last_reading >= LIVE_TIMEOUT
        self._last_reading = time.monotonic()
        if first:
            self._refresh_chips()            # the Live chip lights at once
        verdict = self.live.add(r)
        self.trend.add(r)
        self.log.add(r, verdict)

    @Slot(object)
    def _on_state(self, st) -> None:
        conn = self.active
        if conn is None:
            return
        self._last_state = st
        for p in conn.pages:
            p.on_state(st)
        self.live.on_state(st)                # the LCD's annunciators follow the record

    @Slot(object)
    def _on_meter_event(self, ev) -> None:
        conn = self.active
        fields = getattr(ev, "fields", None)
        if fields is None:
            fields = {"value": getattr(ev, "value", None)}
        self.note("event %s %s" % (ev.name, ", ".join("%s=%s" % kv for kv in fields.items())))
        if conn is not None:
            for p in conn.pages:
                p.on_event(ev)

    # ------------------------------------------------------------------------------------------
    # status line, DTR, releases
    # ------------------------------------------------------------------------------------------
    def _show_stats(self) -> None:
        conn = self.active
        self._refresh_chips()                 # the Live chip goes off 3 s after the last reading
        if conn is None:
            self.stats_label.setText("no link")
            self.dtr_label.setText("")
            return
        s = conn.device.link.stats
        text = "frames in %d, CRC errors %d, skipped %d, timeouts %d, stale %d" % (
            s["frames_in"], s["crc_errors"], s["skipped"], s["timeouts"], s["stale"])
        st = self._last_state
        if isinstance(st, lc.State):
            text += ", dropped by the meter %d" % st.dropped
        self.stats_label.setText(text)
        if conn.device.model == "2C53T":
            self.dtr_label.setText("DTR set: holds the 2C53T's auto power-off"
                                   if conn.device.link.dtr else
                                   "DTR clear: the 2C53T may switch itself off when idle")
        else:
            self.dtr_label.setText("")

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel):
            self.user_action()
        elif ev.type() == QEvent.Resize and obj is self.header:
            self._fit_header()
        return False

    def user_action(self) -> None:
        self._last_action = time.monotonic()
        if self._dtr_dropped:
            self._restore_dtr()

    def _restore_dtr(self) -> None:
        conn = self.active
        if conn is None or not self._dtr_dropped:
            return
        self._dtr_dropped = False
        link = conn.device.link
        self.jobs.submit(lambda p: link.set_dtr(True), what="DTR on",
                         on_result=lambda r: self.note("DTR set again: the 2C53T stays awake"))

    def _idle_check(self) -> None:
        conn = self.active
        if conn is None or conn.device.model != "2C53T" or self._dtr_dropped \
                or not self.settings.get("let_2c53t_sleep") or self.jobs.busy:
            return
        if time.monotonic() - self._last_action < self.idle_limit:
            return
        dev = conn.device
        folder = Path(self.settings.backups_folder())

        def job(_p):
            path = dev.backup(folder)        # 13.1: the switch-off may save; backup first
            dev.link.set_dtr(False)
            return path

        def dropped(path):
            self._dtr_dropped = True
            self.note("no user action for %d min: settings page backed up (%s), DTR dropped, "
                      "the 2C53T may switch itself off" % (self.idle_limit // 60, path))

        def failed(e):
            self.note("DTR stays set: the backup before it failed: %s" % describe_error(e))

        self.jobs.submit(job, what="DTR off", exclusive=True, on_error=failed, on_result=dropped)

    def _daily_release_check(self) -> None:
        conn = self.active
        if conn is not None and conn.meter_page is not None and \
                self.settings.release_check_due(False):
            conn.meter_page.check_releases(True)

    # ------------------------------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stops the timers, the recording, the watcher and the link (window close)."""
        for t in (self.stats_timer, self.idle_timer, self.release_timer):
            t.stop()
        for p in self._all_pages():
            p.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        try:
            self.watcher.stop()
        except RuntimeError:
            pass
        conn = self.active
        if conn is not None:
            conn.device.close()
            conn.device.link.close()
        self.jobs.wait_all(5000)

    def _ask_yes_no(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def closeEvent(self, ev) -> None:
        job = self.jobs.exclusive_job
        if job is not None:
            ev.ignore()                      # never cut the port under an update / save
            now = " (now: %s)" % job.last_text if job.last_text else ""
            if job.cancel.allowed and self.asker(
                    "Close the window",
                    "%s holds the meter's port%s. It can stop safely before its next request. "
                    "Cancel it and close the window when it has stopped?" % (job.what, now)):
                if self.jobs.cancel():
                    self._close_after_job = True
                    self.note("cancel of %s requested: the window closes when it has stopped"
                              % job.what)
                    return
            self.note("the window stays open: %s holds the meter's port%s and %s; close it when "
                      "the job has ended" % (job.what, now,
                                             "was not cancelled" if job.cancel.allowed else
                                             "cannot be cancelled now (%s)" % job.cancel.why_not))
            return
        self.shutdown()
        super().closeEvent(ev)
