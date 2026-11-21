"""Tests of link/discover.py with fake port lists and fake drive probes for each system.
Nothing here opens a port or queries a real device."""

import os
import plistlib
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402

from fnirsi_companion.link import discover  # noqa: E402
from fnirsi_companion.link.discover import (UPDATE_DRIVES, DriveInfo, PortInfo,  # noqa: E402
                                            Volume, Watcher, drive_status, list_meters,
                                            list_update_drives, probe_linux, probe_macos,
                                            probe_windows)

CP2102 = (0x10C4, 0xEA60)
LC = (0x19F5, 0x1020)
C2 = (0x2E3C, 0x2C53)
LC_DRIVE = (0x19F5, 0x5740)
C2_DRIVE = (0x2E3C, 0x5720)


def port(device, ids, serial=None):
    vid, pid = ids if ids else (None, None)
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial)


class Meters(unittest.TestCase):
    def test_macos_cu_only_two_meters_no_cp2102(self):
        ports = [
            port("/dev/cu.usbserial-0001", CP2102, "0001"),
            port("/dev/tty.usbserial-0001", CP2102, "0001"),
            port("/dev/cu.debug-console", None),
            port("/dev/cu.usbmodem2101", LC, "AAAA0000BBBB1111CCCC2222"),
            port("/dev/tty.usbmodem2101", LC, "AAAA0000BBBB1111CCCC2222"),
            port("/dev/tty.usbmodem3101", C2, "0123456789AB"),
            port("/dev/cu.usbmodem3101", C2, "0123456789AB"),
        ]
        got = list_meters(lambda: ports, system="darwin")
        self.assertEqual(got, [
            PortInfo("2C53T", "/dev/cu.usbmodem3101", "0123456789AB", *C2),
            PortInfo("LC1020E", "/dev/cu.usbmodem2101", "AAAA0000BBBB1111CCCC2222", *LC),
        ])

    def test_linux_and_windows_names(self):
        ports = [port("/dev/ttyUSB0", CP2102), port("/dev/ttyACM0", LC, "S1")]
        self.assertEqual(list_meters(lambda: ports, system="linux"),
                         [PortInfo("LC1020E", "/dev/ttyACM0", "S1", *LC)])
        ports = [port("COM3", CP2102), port("COM7", C2, ""), port("COM7", C2, "")]
        self.assertEqual(list_meters(lambda: ports, system="win32"),
                         [PortInfo("2C53T", "COM7", None, *C2)])

    def test_two_meters_of_one_model_sorted_by_serial(self):
        ports = [port("/dev/ttyACM1", LC, "S2"), port("/dev/ttyACM0", LC, "S9"),
                 port("/dev/ttyACM2", LC, "S1")]
        self.assertEqual([p.serial for p in list_meters(lambda: ports, system="linux")],
                         ["S1", "S2", "S9"])

    def test_update_drive_ids_are_not_meters(self):
        ports = [port("/dev/cu.x", LC_DRIVE), port("/dev/cu.y", C2_DRIVE)]
        self.assertEqual(list_meters(lambda: ports, system="darwin"), [])


class Drives(unittest.TestCase):
    def test_rules(self):
        seen = []

        def probe(wanted):
            seen.append(wanted)
            return [
                Volume("/Volumes/IAP", "IAP", *C2_DRIVE, True),
                Volume("/Volumes/LC1020E-IAP", "lc1020e-iap", *LC_DRIVE, True),
                Volume("/Volumes/STICK", "IAP", 0x0781, 0x5567, True),         # other device
                Volume("/Volumes/IAP 1", "OTHER", *C2_DRIVE, True),            # wrong label
                Volume("/Volumes/NOREADY", "IAP", *C2_DRIVE, True),            # no Ready file
                Volume("/Volumes/LC1020E-IAP", "LC1020E-IAP", *C2_DRIVE, True),  # label of LC
                Volume("/dev/sdc", None, *LC_DRIVE, False),                    # not mounted
                Volume("/media/u/X", None, *LC_DRIVE, True),                   # no label known
            ]
        ready = {"/Volumes/IAP", "/Volumes/LC1020E-IAP", "/Volumes/IAP 1", "/Volumes/STICK",
                 "/media/u/X"}
        got = list_update_drives(probe, status=lambda p, m: "ready" if p in ready else None)
        self.assertEqual(seen, [set(UPDATE_DRIVES)])
        self.assertEqual(got, [
            DriveInfo("2C53T", "/Volumes/IAP", *C2_DRIVE, "ready"),
            DriveInfo("LC1020E", "/Volumes/LC1020E-IAP", *LC_DRIVE, "ready"),
            DriveInfo("LC1020E", "/dev/sdc", *LC_DRIVE, "unmounted"),
            DriveInfo("LC1020E", "/media/u/X", *LC_DRIVE, "ready"),
        ])

    def test_status_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(drive_status(d, "LC1020E"))
            open(os.path.join(d, "LC1020E.UF2"), "wb").close()
            self.assertIsNone(drive_status(d, "LC1020E"))
            open(os.path.join(d, "READY.TXT"), "wb").close()
            self.assertEqual(drive_status(d, "LC1020E"), "ready")
            self.assertEqual(drive_status(d, "2C53T"), "ready")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "Failed.TXT"), "wb").close()
            self.assertEqual(drive_status(d, "2C53T"), "failed")   # the retry is offered
            self.assertIsNone(drive_status(d, "LC1020E"))          # not an LC1020E state file
        self.assertIsNone(drive_status(os.path.join(d, "gone"), "2C53T"))


class MacOS(unittest.TestCase):
    """Property lists shaped like this machine's `ioreg -a` and `diskutil info -plist`."""

    @staticmethod
    def usb(ids, children=(), name="dev"):
        return {"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryName": name,
                "idVendor": ids[0], "idProduct": ids[1],
                "IORegistryEntryChildren": list(children)}

    @staticmethod
    def media(bsd, children=()):
        return {"IOObjectClass": "IOMedia", "BSD Name": bsd,
                "IORegistryEntryChildren": list(children)}

    def runner(self, ioreg_tree, diskutil):
        calls = []

        def run(argv):
            calls.append(argv)
            if argv[0] == "ioreg":
                return plistlib.dumps(ioreg_tree)
            if argv[:3] == ["diskutil", "info", "-plist"]:
                info = diskutil.get(argv[3])
                return plistlib.dumps(info) if info is not None else None
            raise AssertionError(argv)
        return run, calls

    def test_volume_below_wanted_device(self):
        storage = {"IOObjectClass": "IOUSBMassStorageDriverNub", "IORegistryEntryChildren": [
            self.media("disk4", [self.media("disk4s1")])]}
        lc_disk = {"IOObjectClass": "IOUSBMassStorageDriverNub", "IORegistryEntryChildren": [
            self.media("disk5")]}
        tree = [
            self.usb(CP2102, [{"IOObjectClass": "IOSerialBSDClient",
                               "IOCalloutDevice": "/dev/cu.usbserial-0001"}]),
            # a hub whose subtree holds the 2C53T drive (and a stick of another vendor)
            self.usb((0x05E3, 0x0610), [self.usb(C2_DRIVE, [storage]),
                                        self.usb((0x0781, 0x5567), [self.media("disk6")])]),
            self.usb(LC_DRIVE, [lc_disk]),
        ]
        diskutil = {
            "/Volumes/Macintosh HD": {"DeviceIdentifier": "disk3s1s1", "ParentWholeDisk": "disk3",
                                      "VolumeName": "Macintosh HD", "MountPoint": "/"},
            "/Volumes/IAP": {"DeviceIdentifier": "disk4s1", "ParentWholeDisk": "disk4",
                             "VolumeName": "IAP", "MountPoint": "/Volumes/IAP"},
            "/Volumes/LC1020E-IAP": {"DeviceIdentifier": "disk5", "ParentWholeDisk": "disk5",
                                     "VolumeName": "LC1020E-IAP",
                                     "MountPoint": "/Volumes/LC1020E-IAP"},
            "/Volumes/STICK": {"DeviceIdentifier": "disk6s1", "ParentWholeDisk": "disk6",
                               "VolumeName": "IAP", "MountPoint": "/Volumes/STICK"},
            "/Volumes/Gone": {"Error": True, "ErrorMessage": "Could not find disk"},
        }
        run, calls = self.runner(tree, diskutil)
        got = probe_macos(set(UPDATE_DRIVES), run=run,
                          listdir=lambda d: ["STICK", "Macintosh HD", "LC1020E-IAP", "IAP", "Gone"])
        self.assertEqual(got, [
            Volume("/Volumes/IAP", "IAP", *C2_DRIVE, True),
            Volume("/Volumes/LC1020E-IAP", "LC1020E-IAP", *LC_DRIVE, True),
        ])
        self.assertEqual(calls[0], ["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"])

    def test_no_wanted_device_no_diskutil(self):
        run, calls = self.runner([self.usb(CP2102), self.usb(LC)], {})
        self.assertEqual(probe_macos(set(UPDATE_DRIVES), run=run, listdir=lambda d: ["IAP"]), [])
        self.assertEqual(len(calls), 1)

    def test_mount_paths_are_posix_on_every_host(self):
        # the macOS paths are joined with posixpath, so this test also holds on Windows
        seen = []

        def run(argv):
            if argv[0] == "ioreg":
                return plistlib.dumps([self.usb(C2_DRIVE, [self.media("disk4")])])
            seen.append(argv[3])
            return None
        probe_macos(set(UPDATE_DRIVES), run=run, listdir=lambda d: ["IAP"])
        self.assertEqual(seen, ["/Volumes/IAP"])

    def test_has_probe(self):
        self.assertTrue(all(discover.has_probe(s) for s in ("darwin", "win32", "linux")))
        self.assertFalse(discover.has_probe("freebsd14"))

    def test_ioreg_failure(self):
        self.assertEqual(probe_macos(set(UPDATE_DRIVES), run=lambda argv: None), [])


class Linux(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def mk(self, rel, text=None):
        path = os.path.join(self.root, rel)
        if text is None:
            os.makedirs(path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        return path

    def link(self, rel, target):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)

    def usb_disk(self, port_name, ids, host, disk, parts=()):
        dev = "sys/devices/pci0000:00/0000:00:14.0/usb1/%s" % port_name
        self.mk(dev + "/idVendor", "%04x\n" % ids[0])
        self.mk(dev + "/idProduct", "%04x\n" % ids[1])
        blk = "%s/%s:1.0/%s/target0:0:0/0:0:0:0/block/%s" % (dev, port_name, host, disk)
        self.mk(blk)
        for p in parts:
            self.mk("%s/%s/partition" % (blk, p), "1\n")
        self.link("sys/block/" + disk, os.path.relpath(os.path.join(self.root, blk),
                                                       os.path.join(self.root, "sys/block")))

    @unittest.skipIf(sys.platform == "win32",
                     "the sysfs fake needs what a Windows file system does not give: symbolic "
                     "links (os.symlink raises OSError without the symlink privilege or "
                     "Developer Mode, Python docs), ':' in names (pci0000:00, 1-1:1.0, reserved "
                     "on Windows) and the udev label name 'LC1020E\\x20IAP', whose backslash "
                     "is a path separator there")
    def test_volumes(self):
        self.usb_disk("1-1", C2_DRIVE, "host3", "sdb")                    # FAT without table
        self.usb_disk("1-2", LC_DRIVE, "host4", "sdc", parts=["sdc1"])   # not mounted
        self.usb_disk("1-3", (0x0781, 0x5567), "host5", "sdd", parts=["sdd1"])
        self.usb_disk("1-4", LC_DRIVE, "host6", "sde", parts=["sde1"])   # mounted, escaped
        # an internal disk: no USB device above it
        self.mk("sys/devices/pci0000:00/0000:00:17.0/ata1/host0/block/sda/sda1/partition", "1")
        self.link("sys/block/sda", "../devices/pci0000:00/0000:00:17.0/ata1/host0/block/sda")
        for n in ("sda1", "sdb", "sdc", "sdc1", "sdd1", "sde", "sde1"):
            self.mk("dev/" + n, "")
        self.link("dev/disk/by-label/IAP", "../../sdb")
        self.link("dev/disk/by-label/LC1020E-IAP", "../../sdc1")
        self.link("dev/disk/by-label/LC1020E\\x20IAP", "../../sde1")
        self.link("dev/disk/by-uuid/1234-ABCD", "../../sde1")
        self.mk("proc/mounts",
                "/dev/sda1 / ext4 rw 0 0\n"
                "proc /proc proc rw 0 0\n"
                "/dev/sdb /media/u/IAP vfat rw 0 0\n"
                "/dev/disk/by-uuid/1234-ABCD /media/u/LC1020E\\040IAP vfat rw 0 0\n")
        got = probe_linux(set(UPDATE_DRIVES), root=self.root)
        self.assertEqual(got, [
            Volume(os.path.join(self.root, "media/u/IAP"), "IAP", *C2_DRIVE, True),
            Volume("/dev/sdc1", "LC1020E-IAP", *LC_DRIVE, False),
            Volume(os.path.join(self.root, "media/u/LC1020E IAP"), "LC1020E IAP", *LC_DRIVE,
                   True),
        ])
        # the escaped label is not the drive's label: list_update_drives drops it
        drives = list_update_drives(lambda w: got, status=lambda p, m: "ready")
        self.assertEqual([d.path for d in drives],
                         [os.path.join(self.root, "media/u/IAP"), "/dev/sdc1"])

    def test_no_sysfs(self):
        self.assertEqual(probe_linux(set(UPDATE_DRIVES), root=self.root), [])


class FakeWin32:
    def __init__(self):
        self.drive_list = ["C:\\", "E:\\", "F:\\", "G:\\", "H:\\"]
        self.types = {"C:\\": 3, "E:\\": 2, "F:\\": 3, "G:\\": 5, "H:\\": 2}
        self.numbers = {"C:\\": 0, "E:\\": 1, "F:\\": 2, "G:\\": 3, "H:\\": 4}
        self.ids = {1: LC_DRIVE, 2: (0x0781, 0x5567), 3: C2_DRIVE, 4: C2_DRIVE}
        self.labels = {"C:\\": "Windows", "E:\\": "LC1020E-IAP", "F:\\": "IAP", "G:\\": "IAP",
                       "H:\\": None}
        self.asked_labels = []

    def drives(self):
        return list(self.drive_list)

    def drive_type(self, root):
        return self.types[root]

    def disk_number(self, root):
        return self.numbers[root]

    def disk_usb_ids(self):
        return dict(self.ids)

    def volume_label(self, root):
        self.asked_labels.append(root)
        return self.labels[root]


class Windows(unittest.TestCase):
    def test_volumes(self):
        api = FakeWin32()
        got = probe_windows(set(UPDATE_DRIVES), api=api)
        # G: is a CD-ROM (type 5), H: has no medium, F: is another USB device
        self.assertEqual(got, [Volume("E:\\", "LC1020E-IAP", *LC_DRIVE, True)])
        self.assertEqual(api.asked_labels, ["E:\\", "H:\\"])

    def test_no_wanted_disk(self):
        api = FakeWin32()
        api.ids = {2: (0x0781, 0x5567)}
        self.assertEqual(probe_windows(set(UPDATE_DRIVES), api=api), [])
        self.assertEqual(api.asked_labels, [])

    def test_ioctl_code(self):
        # CTL_CODE(IOCTL_STORAGE_BASE 0x2D, 0x0420, METHOD_BUFFERED 0, FILE_ANY_ACCESS 0)
        self.assertEqual(discover.Win32Drives._IOCTL_STORAGE_GET_DEVICE_NUMBER,
                         (0x2D << 16) | (0 << 14) | (0x0420 << 2) | 0)


class WatcherTest(unittest.TestCase):
    def test_changes_and_failures(self):
        meter_lists = [[], [], ["m1"], ["m1"], ["m1", "m2"]]
        drive_calls = []
        done = threading.Event()

        def meters():
            if len(meter_lists) > 1:
                return meter_lists.pop(0)
            done.set()
            return meter_lists[0]

        def drives():
            drive_calls.append(1)
            if len(drive_calls) == 2:
                raise OSError("ioreg failed")
            return ["d1"]
        w = Watcher(meters, drives, period=0.01)
        got_m, got_d, failed = [], [], []
        w.meters_changed.connect(got_m.append, Qt.DirectConnection)
        w.drives_changed.connect(got_d.append, Qt.DirectConnection)
        w.scan_failed.connect(failed.append, Qt.DirectConnection)
        w.start()
        self.assertTrue(done.wait(5))
        end = time.monotonic() + 5
        while len(drive_calls) < 4 and time.monotonic() < end:
            time.sleep(0.01)
        w.stop()
        self.assertEqual(got_m, [[], ["m1"], ["m1", "m2"]])
        self.assertEqual(got_d, [["d1"]])
        self.assertEqual(failed, ["update drives: OSError: ioreg failed"])
        with self.assertRaises(RuntimeError):
            w.start()


if __name__ == "__main__":
    unittest.main()
