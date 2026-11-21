"""Finding the meters: serial ports, update drives, and a watcher that rescans every second.

Serial ports (DESIGN.md 5, 13.19, 13.20): a port counts only by its USB VID:PID
(0x2E3C:0x2C53 2C53T, 0x19F5:0x1020 LC1020E). Nothing here opens a port; any other device (for
example a CP2102 bridge, 0x10C4:0xEA60) is never listed. macOS lists a CDC device twice, as
/dev/cu.* and /dev/tty.*; only /dev/cu.* is listed there (the tty node waits for carrier).

Update drives (DESIGN.md 5, 13.3): a volume counts only when its USB parent device has the
VID:PID of a meter's update drive (0x2E3C:0x5720 2C53T, 0x19F5:0x5740 LC1020E), its volume label
is the drive's label (2C53T "IAP": bootloader rodata.s 0x080049B9; LC1020E "LC1020E-IAP":
tools/lc1020e.py DRIVE_LABEL) and its root holds the meter's status file (any case). LC1020E:
Ready.TXT only (bootloader rodata.s "Ready   TXT"). 2C53T: the slot bootloader renames its file by
its state, Ready / Success / Failed / Large / Unkown / Jump (2C53T bootloader iap_04.s
fn_08001d2c), so each of them counts and DriveInfo.status says which; after a failed install the
drive stays listed with status "failed" so the copy can be offered again (DESIGN 13.14). Where the system
gives no label (Linux without udev's by-label links) the VID:PID and the file decide. On Linux a
matching device that is not mounted (no automount, DESIGN 13.29) is listed with its device node
as path (/dev/sdX or /dev/sdXN); the file cannot be checked before it is mounted.

The volume -> USB parent lookup per system:
- macOS: `ioreg -a -r -c IOUSBHostDevice -l -w0` (property list of the USB devices with their
  IOService subtrees; every "BSD Name" below a device is a disk of that device) and
  `diskutil info -plist <mount point>` (DeviceIdentifier, ParentWholeDisk, VolumeName).
- Linux: /sys/block/<dev> resolved and walked up to the USB device directory holding idVendor /
  idProduct; /proc/mounts for the mount points; /dev/disk/by-label for the labels.
- Windows: GetLogicalDriveStringsW, GetDriveTypeW, GetVolumeInformationW;
  IOCTL_STORAGE_GET_DEVICE_NUMBER on the volume and on every disk interface of SetupAPI
  (GUID_DEVINTERFACE_DISK); CM_Get_Parent / CM_Get_Device_IDW up to the "USB\\VID_xxxx&PID_xxxx"
  device.

Every probe is a function probe(wanted) -> list[Volume]; list_update_drives takes another one for
tests, and the per-system probes take their system access (command runner, file-system root,
Win32 calls) as arguments.
"""

from __future__ import annotations

import os
import plistlib
import posixpath
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from PySide6.QtCore import QObject, Signal

MODELS = {(0x2E3C, 0x2C53): "2C53T", (0x19F5, 0x1020): "LC1020E"}
UPDATE_DRIVES = {(0x2E3C, 0x5720): "2C53T", (0x19F5, 0x5740): "LC1020E"}
DRIVE_LABELS = {"2C53T": "IAP", "LC1020E": "LC1020E-IAP"}
STATUS_FILES = {
    "LC1020E": {"ready.txt": "ready"},
    "2C53T": {"ready.txt": "ready", "success.txt": "success", "failed.txt": "failed",
              "large.txt": "large", "unkown.txt": "unknown", "jump.txt": "jump"},
}


@dataclass(frozen=True)
class PortInfo:
    model: str
    device: str
    serial: str | None
    vid: int
    pid: int


@dataclass(frozen=True)
class DriveInfo:
    model: str
    path: str           # mount point / drive root; Linux, not mounted: the device node
    vid: int
    pid: int
    status: str         # "ready", "success", "failed", "large", "unknown", "jump" (2C53T states),
                        # "unmounted" (Linux device node: the file cannot be read yet)


@dataclass(frozen=True)
class Volume:
    """One volume a probe found on a USB device of a wanted VID:PID."""
    path: str           # mount point or drive root; not mounted (Linux): the device node
    label: str | None   # None: the system gives none
    vid: int
    pid: int
    mounted: bool


# ----------------------------------------------------------------------------------------------
# Serial ports
# ----------------------------------------------------------------------------------------------

def _pyserial_comports():
    from serial.tools import list_ports
    return list_ports.comports()


def list_meters(comports: Callable[[], Iterable] | None = None, *,
                system: str | None = None) -> list[PortInfo]:
    """The serial ports of attached meters, sorted by model, serial number, device.

    comports: function returning port objects with device, vid, pid, serial_number (pyserial's
    list_ports.comports by default). system: sys.platform value (default: this system)."""
    comports = comports or _pyserial_comports
    system = system or sys.platform
    out: list[PortInfo] = []
    seen = set()
    for p in comports():
        vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
        model = MODELS.get((vid, pid))
        if model is None:
            continue
        device = p.device
        if system == "darwin" and not device.startswith("/dev/cu."):
            continue
        if device in seen:
            continue
        seen.add(device)
        out.append(PortInfo(model, device, getattr(p, "serial_number", None) or None, vid, pid))
    out.sort(key=lambda i: (i.model, i.serial or "", i.device))
    return out


# ----------------------------------------------------------------------------------------------
# Update drives
# ----------------------------------------------------------------------------------------------

def drive_status(path: str, model: str) -> str | None:
    """The state named by the model's status file in the folder path (any case), or None when
    the folder holds none of them (or cannot be read)."""
    files = STATUS_FILES[model]
    try:
        for n in sorted(os.listdir(path)):
            if n.lower() in files:
                return files[n.lower()]
    except OSError:
        return None
    return None


def has_probe(system: str | None = None) -> bool:
    """True on the systems that have a volume probe (macOS, Linux, Windows)."""
    system = system or sys.platform
    return system in ("darwin", "win32") or system.startswith("linux")


def default_probe() -> Callable[[set], list[Volume]]:
    """The probe of this system. Systems other than macOS, Linux and Windows have none: their
    probe finds no drives."""
    if sys.platform == "darwin":
        return probe_macos
    if sys.platform.startswith("linux"):
        return probe_linux
    if sys.platform == "win32":
        return probe_windows
    return lambda wanted: []


def list_update_drives(probe: Callable[[set], list[Volume]] | None = None, *,
                       status: Callable[[str, str], str | None] = drive_status) -> list[DriveInfo]:
    """The update drives of attached meters, sorted by model and path (rules: module
    docstring). probe(wanted) -> volumes on USB devices whose (vid, pid) is in wanted."""
    probe = probe or default_probe()
    out: dict[str, DriveInfo] = {}
    for v in probe(set(UPDATE_DRIVES)):
        model = UPDATE_DRIVES.get((v.vid, v.pid))
        if model is None:
            continue
        if v.label is not None and v.label.strip().upper() != DRIVE_LABELS[model].upper():
            continue
        st = status(v.path, model) if v.mounted else "unmounted"
        if st is None:
            continue
        out.setdefault(v.path, DriveInfo(model, v.path, v.vid, v.pid, st))
    return sorted(out.values(), key=lambda d: (d.model, d.path))


# -- macOS -------------------------------------------------------------------------------------

def _run_command(argv: list[str]) -> bytes | None:
    """stdout of argv when it exits with 0, else None."""
    r = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    return r.stdout if r.returncode == 0 else None


def _ioreg_bsd_names(node: dict, out: list[str]) -> None:
    """BSD names in the subtree of node, not descending into another USB device."""
    for child in node.get("IORegistryEntryChildren", ()):
        if child.get("IOObjectClass") == "IOUSBHostDevice":
            continue
        name = child.get("BSD Name")
        if isinstance(name, str):
            out.append(name)
        _ioreg_bsd_names(child, out)


def _ioreg_devices(node: dict, found: list[dict]) -> None:
    if node.get("IOObjectClass") == "IOUSBHostDevice":
        found.append(node)
    for child in node.get("IORegistryEntryChildren", ()):
        _ioreg_devices(child, found)


def probe_macos(wanted: set, *, run: Callable[[list[str]], bytes | None] = _run_command,
                volumes_dir: str = "/Volumes",
                listdir: Callable[[str], list[str]] = os.listdir) -> list[Volume]:
    """Volumes under /Volumes whose disk lies below a USB device of a wanted VID:PID.

    Checked on macOS 26 (Darwin 25.6) with the USB devices attached here (an LC1020E, a CP2102
    bridge, a security key): `ioreg -a` gives a list of dictionaries with IOObjectClass, idVendor,
    idProduct (integers) and IORegistryEntryChildren. That a mass-storage device's IOMedia
    objects (key "BSD Name", seen on the internal disk's IOMedia) lie in that subtree is IOKit's
    IOService structure; no USB drive was attached to confirm it on this machine."""
    raw = run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"])
    if not raw:
        return []
    tree = plistlib.loads(raw)
    roots = tree if isinstance(tree, list) else [tree]
    devices: list[dict] = []
    for r in roots:
        _ioreg_devices(r, devices)
    disks: dict[str, tuple[int, int]] = {}
    for d in devices:
        ids = (d.get("idVendor"), d.get("idProduct"))
        if ids not in wanted:
            continue
        names: list[str] = []
        _ioreg_bsd_names(d, names)
        for n in names:
            disks[n] = ids
    if not disks:
        return []
    out = []
    try:
        entries = sorted(listdir(volumes_dir))
    except OSError:
        return []
    for entry in entries:
        mount = posixpath.join(volumes_dir, entry)      # a macOS path on every host (tests)
        raw = run(["diskutil", "info", "-plist", mount])
        if not raw:
            continue
        info = plistlib.loads(raw)
        if info.get("Error"):
            continue
        ids = disks.get(info.get("DeviceIdentifier")) or disks.get(info.get("ParentWholeDisk"))
        if ids is None:
            continue
        out.append(Volume(info.get("MountPoint") or mount, info.get("VolumeName"),
                          ids[0], ids[1], True))
    return out


# -- Linux -------------------------------------------------------------------------------------

def _unescape_mounts(s: str) -> str:
    """/proc/mounts writes space, tab, newline and backslash as octal escapes (\\040 ...)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _unescape_udev(s: str) -> str:
    """udev writes unsafe characters of /dev/disk/by-label names as \\xNN."""
    return re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)


def _linux_mounts(root: str) -> dict[str, str]:
    """Device name (sdb1) -> first mount point of it, from /proc/mounts."""
    out: dict[str, str] = {}
    try:
        with open(os.path.join(root, "proc/mounts"), encoding="utf-8",
                  errors="surrogateescape") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/dev/"):
            continue
        src = os.path.realpath(os.path.join(root, _unescape_mounts(fields[0])[1:]))
        out.setdefault(os.path.basename(src), _unescape_mounts(fields[1]))
    return out


def _linux_labels(root: str) -> dict[str, str]:
    """Device name -> volume label, from the /dev/disk/by-label links."""
    folder = os.path.join(root, "dev/disk/by-label")
    out: dict[str, str] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        target = os.path.realpath(os.path.join(folder, n))
        out[os.path.basename(target)] = _unescape_udev(n)
    return out


def _read_hex(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def _linux_usb_ids(dev_dir: str, top: str) -> tuple[int, int] | None:
    """(idVendor, idProduct) of the nearest ancestor directory of dev_dir (inside top) that has
    both files: the USB device of a block device (sysfs-bus-usb ABI)."""
    d = os.path.dirname(dev_dir)
    while d.startswith(top + os.sep):
        vid, pid = _read_hex(os.path.join(d, "idVendor")), _read_hex(os.path.join(d, "idProduct"))
        if vid is not None and pid is not None:
            return vid, pid
        d = os.path.dirname(d)
    return None


def probe_linux(wanted: set, *, root: str = "/") -> list[Volume]:
    """Block devices below a USB device of a wanted VID:PID: their partitions (or the whole disk
    when it has none), mounted ones with their mount point, the others with their device node.
    root: file-system root (tests use a folder tree). Not run on Linux; written from the sysfs
    layout (/sys/block/<dev> -> ../devices/.../<usb device>/<interface>/host.../block/<dev>)
    and the proc(5) mounts format."""
    sysblock = os.path.join(root, "sys/block")
    top = os.path.realpath(os.path.join(root, "sys/devices"))
    try:
        names = sorted(os.listdir(sysblock))
    except OSError:
        return []
    mounts = labels = None
    out = []
    for name in names:
        dev_dir = os.path.realpath(os.path.join(sysblock, name))
        ids = _linux_usb_ids(dev_dir, top)
        if ids not in wanted:
            continue
        if mounts is None:
            mounts, labels = _linux_mounts(root), _linux_labels(root)
        try:
            parts = sorted(p for p in os.listdir(dev_dir)
                           if os.path.isfile(os.path.join(dev_dir, p, "partition")))
        except OSError:
            parts = []
        for node in [name] + parts:
            mount = mounts.get(node)
            if mount is not None:
                out.append(Volume(os.path.join(root, mount.lstrip("/")), labels.get(node),
                                  ids[0], ids[1], True))
            elif node != name or not parts:
                out.append(Volume("/dev/" + node, labels.get(node), ids[0], ids[1], False))
    return out


# -- Windows -----------------------------------------------------------------------------------

DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3


class Win32Drives:
    """The Win32 calls of the Windows probe, through ctypes. Not run on Windows: written from
    the documented Win32 API (kernel32, setupapi, cfgmgr32); structure layouts as in
    setupapi.h (packed to 8 on 64-bit, to 1 on 32-bit Windows)."""

    _GUID_DEVINTERFACE_DISK = (0x53F56307, 0xB6BF, 0x11D0,
                               (0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B))
    _IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
    _DIGCF_PRESENT, _DIGCF_DEVICEINTERFACE = 0x02, 0x10
    _ERROR_NO_MORE_ITEMS, _ERROR_INSUFFICIENT_BUFFER = 259, 122
    _SEM_FAILCRITICALERRORS = 0x0001
    _OPEN_EXISTING, _FILE_SHARE_READ_WRITE = 3, 0x3
    _USB_ID = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.IGNORECASE)

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w
        self._ct, self._w = ctypes, w

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        pack = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 1

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _pack_ = pack
            _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID), ("Flags", w.DWORD),
                        ("Reserved", ctypes.c_size_t)]

        class SP_DEVINFO_DATA(ctypes.Structure):
            _pack_ = pack
            _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD),
                        ("Reserved", ctypes.c_size_t)]

        class STORAGE_DEVICE_NUMBER(ctypes.Structure):
            _fields_ = [("DeviceType", w.DWORD), ("DeviceNumber", w.DWORD),
                        ("PartitionNumber", w.DWORD)]

        self._GUID, self._IFDATA, self._DEVINFO = GUID, SP_DEVICE_INTERFACE_DATA, SP_DEVINFO_DATA
        self._SDN = STORAGE_DEVICE_NUMBER
        # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: DWORD + one WCHAR, padded: 8 / 6
        self._detail_cb = 8 if pack == 8 else 6

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sapi = ctypes.WinDLL("setupapi", use_last_error=True)
        cfg = ctypes.WinDLL("cfgmgr32")
        H = w.HANDLE
        k32.GetLogicalDriveStringsW.argtypes = [w.DWORD, w.LPWSTR]
        k32.GetLogicalDriveStringsW.restype = w.DWORD
        k32.GetDriveTypeW.argtypes = [w.LPCWSTR]
        k32.GetDriveTypeW.restype = w.UINT
        k32.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, w.LPDWORD,
                                              w.LPDWORD, w.LPDWORD, w.LPWSTR, w.DWORD]
        k32.GetVolumeInformationW.restype = w.BOOL
        k32.SetThreadErrorMode.argtypes = [w.DWORD, w.LPDWORD]
        k32.SetThreadErrorMode.restype = w.BOOL
        k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, H]
        k32.CreateFileW.restype = H
        k32.DeviceIoControl.argtypes = [H, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
                                        w.LPDWORD, w.LPVOID]
        k32.DeviceIoControl.restype = w.BOOL
        k32.CloseHandle.argtypes = [H]
        k32.CloseHandle.restype = w.BOOL
        sapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
        sapi.SetupDiGetClassDevsW.restype = H
        sapi.SetupDiEnumDeviceInterfaces.argtypes = [H, ctypes.POINTER(SP_DEVINFO_DATA),
                                                     ctypes.POINTER(GUID), w.DWORD,
                                                     ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
        sapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
        sapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            H, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), w.LPVOID, w.DWORD, w.LPDWORD,
            ctypes.POINTER(SP_DEVINFO_DATA)]
        sapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
        sapi.SetupDiDestroyDeviceInfoList.argtypes = [H]
        sapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
        cfg.CM_Get_Parent.argtypes = [w.LPDWORD, w.DWORD, w.ULONG]
        cfg.CM_Get_Parent.restype = w.ULONG
        cfg.CM_Get_Device_IDW.argtypes = [w.DWORD, w.LPWSTR, w.ULONG, w.ULONG]
        cfg.CM_Get_Device_IDW.restype = w.ULONG
        self._k32, self._sapi, self._cfg = k32, sapi, cfg
        self._invalid = ctypes.c_void_p(-1).value

    def drives(self) -> list[str]:
        """Drive roots ("E:\\")."""
        buf = self._ct.create_unicode_buffer(1024)
        n = self._k32.GetLogicalDriveStringsW(len(buf), buf)
        if n == 0 or n > len(buf):
            return []
        return [d for d in self._ct.wstring_at(buf, n).split("\0") if d]

    def drive_type(self, root: str) -> int:
        return self._k32.GetDriveTypeW(root)

    def volume_label(self, root: str) -> str | None:
        """The volume label, or None when the drive has no readable volume. Critical-error
        dialogs ("insert a disk") are suppressed for the call."""
        w = self._w
        old = w.DWORD()
        self._k32.SetThreadErrorMode(self._SEM_FAILCRITICALERRORS, self._ct.byref(old))
        try:
            label = self._ct.create_unicode_buffer(261)
            ok = self._k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
        finally:
            self._k32.SetThreadErrorMode(old.value, None)
        return label.value if ok else None

    def _device_number(self, path: str) -> int | None:
        h = self._k32.CreateFileW(path, 0, self._FILE_SHARE_READ_WRITE, None,
                                  self._OPEN_EXISTING, 0, None)
        if h is None or h == self._invalid:
            return None
        try:
            sdn = self._SDN()
            got = self._w.DWORD()
            ok = self._k32.DeviceIoControl(h, self._IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                                           self._ct.byref(sdn), self._ct.sizeof(sdn),
                                           self._ct.byref(got), None)
            return sdn.DeviceNumber if ok else None
        finally:
            self._k32.CloseHandle(h)

    def disk_number(self, root: str) -> int | None:
        """Number of the physical disk that holds the volume of root ("E:\\")."""
        return self._device_number("\\\\.\\" + root.rstrip("\\"))

    def _usb_parent(self, devinst: int) -> tuple[int, int] | None:
        ct, w = self._ct, self._w
        buf = ct.create_unicode_buffer(201)          # MAX_DEVICE_ID_LEN 200 + 0
        node = devinst
        for _ in range(32):
            if self._cfg.CM_Get_Device_IDW(node, buf, len(buf), 0) == 0:
                m = self._USB_ID.match(buf.value)
                if m:
                    return int(m.group(1), 16), int(m.group(2), 16)
            parent = w.DWORD()
            if self._cfg.CM_Get_Parent(ct.byref(parent), node, 0) != 0:
                return None
            node = parent.value
        return None

    def disk_usb_ids(self) -> dict[int, tuple[int, int]]:
        """Disk number -> (vid, pid) of the USB device above it, for every present disk that
        has one."""
        ct = self._ct
        g = self._GUID_DEVINTERFACE_DISK
        guid = self._GUID(g[0], g[1], g[2], (ct.c_ubyte * 8)(*g[3]))
        hdev = self._sapi.SetupDiGetClassDevsW(ct.byref(guid), None, None,
                                               self._DIGCF_PRESENT | self._DIGCF_DEVICEINTERFACE)
        if hdev is None or hdev == self._invalid:
            return {}
        out: dict[int, tuple[int, int]] = {}
        try:
            index = 0
            while True:
                ifd = self._IFDATA()
                ifd.cbSize = ct.sizeof(ifd)
                if not self._sapi.SetupDiEnumDeviceInterfaces(hdev, None, ct.byref(guid), index,
                                                              ct.byref(ifd)):
                    break           # ERROR_NO_MORE_ITEMS ends the list; any other error too
                index += 1
                need = self._w.DWORD()
                self._sapi.SetupDiGetDeviceInterfaceDetailW(hdev, ct.byref(ifd), None, 0,
                                                            ct.byref(need), None)
                if need.value < self._detail_cb + 2:
                    continue
                detail = ct.create_string_buffer(need.value)
                ct.c_uint32.from_buffer(detail).value = self._detail_cb
                dinfo = self._DEVINFO()
                dinfo.cbSize = ct.sizeof(dinfo)
                if not self._sapi.SetupDiGetDeviceInterfaceDetailW(
                        hdev, ct.byref(ifd), ct.byref(detail), need.value, None,
                        ct.byref(dinfo)):
                    continue
                path = ct.wstring_at(ct.addressof(detail) + 4)
                number = self._device_number(path)
                ids = self._usb_parent(dinfo.DevInst)
                if number is not None and ids is not None:
                    out[number] = ids
        finally:
            self._sapi.SetupDiDestroyDeviceInfoList(hdev)
        return out


def probe_windows(wanted: set, *, api=None) -> list[Volume]:
    """Removable and fixed drives whose disk lies below a USB device of a wanted VID:PID.
    api: object with drives(), drive_type(root), volume_label(root), disk_number(root),
    disk_usb_ids() (Win32Drives by default). Not run on Windows."""
    api = api or Win32Drives()
    disks = {n: ids for n, ids in api.disk_usb_ids().items() if ids in wanted}
    if not disks:
        return []
    out = []
    for root in api.drives():
        if api.drive_type(root) not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue
        ids = disks.get(api.disk_number(root))
        if ids is None:
            continue
        label = api.volume_label(root)
        if label is None:
            continue                # no medium / no readable volume
        out.append(Volume(root, label, ids[0], ids[1], True))
    return out


# ----------------------------------------------------------------------------------------------
# Watcher
# ----------------------------------------------------------------------------------------------

class Watcher(QObject):
    """Rescans the meters and the update drives every period seconds, each in its own thread
    (a slow drive scan does not delay the port scan). meters_changed / drives_changed are
    emitted with the new list after the first scan and whenever the list changes;
    scan_failed(text) when a scan raises (the last list stays). Signals come from the scan
    threads."""

    meters_changed = Signal(list)
    drives_changed = Signal(list)
    scan_failed = Signal(str)

    def __init__(self, list_meters: Callable[[], list] = list_meters,
                 list_drives: Callable[[], list] = list_update_drives, period: float = 1.0):
        super().__init__()
        self._scans = ((list_meters, self.meters_changed, "meters"),
                       (list_drives, self.drives_changed, "update drives"))
        self._period = period
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("Watcher.start() may be called once")
        for scan, signal, what in self._scans:
            t = threading.Thread(target=self._run, args=(scan, signal, what),
                                 name="Watcher %s" % what, daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        """Ends the scans and waits for the threads."""
        self._stop.set()
        for t in self._threads:
            if t.ident != threading.get_ident():
                t.join()

    def _run(self, scan, signal, what) -> None:
        last = None
        while not self._stop.is_set():
            try:
                now = list(scan())
            except Exception as e:     # any failure of a scan is reported, the watcher goes on
                self.scan_failed.emit("%s: %s: %s" % (what, type(e).__name__, e))
            else:
                if now != last:
                    last = now
                    signal.emit(now)
            self._stop.wait(self._period)
