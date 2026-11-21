# FNIRSI Companion

A desktop program for the **FNIRSI 2C53T** (oscilloscope / multimeter / generator) and the **FNIRSI LC1020E** (LCR
meter) running the rewritten firmware:

- 2C53T firmware: <https://github.com/zombu4/fnirsi-2c53t-firmware-rewrite>
- LC1020E firmware: <https://github.com/zombu4/fnirsi-lc1020e-firmware-rewrite>

It talks to the meter over the meter's USB serial control channel. It is a native window (Qt for Python / PySide6): no
web server and no browser.

## Both meters, detected by themselves

The program looks at the USB devices, recognises each meter by its USB vendor / product ID and serial number, connects,
and shows the pages of the attached model. A second meter plugged in appears in the device list; unplugging and
re-plugging is followed without restarting the program. Ports are matched by VID:PID and serial number, never by port
name.

| Meter  | USB IDs (VID:PID)                                   |
|--------|------------------------------------------------------|
| 2C53T  | 2E3C:2C53 (application), 2E3C:5720 (update drive)    |
| LC1020E| 19F5:1020 (application), 19F5:5740 (update drive)    |

## Pages

The window has a sidebar with one item per page (Ctrl+1..9 switch them), a header with the meter's firmware,
bootloader, port and status chips, and a status line with the last message and the link counters.

- **Live**: the reading on a seven-segment display as the meter shows it (annunciators, quantity, the LC1020E's
  secondary reading), the meter's keys, the model's measurement controls (LC1020E: the LCR lists, HOLD and the screen;
  2C53T: the multimeter function, hold and relative), statistics (min, max, mean, standard deviation, count), a limit
  test, the recording and the last 10 minutes of the trend.
- **Trend**: the readings over time as a plot, with time window, auto or fixed scale and two cursors.
- **Log**: record to CSV, with triggers (every reading, interval, change, limit).
- **Meter**: model, firmware text, serial, bootloader, last update result, battery; firmware update from a file or from
  the GitHub releases of the firmware repositories; RESET and power off.
- **Screen**: capture the meter's screen and save it as PNG.
- **Diagnostics**: the model's diagnostic commands, the table of all meter keys and the event list.
- **Settings**: folders, CSV format, connection, release check, theme (system, dark, light).
- 2C53T pages: Oscilloscope, Generator, Files, Meter settings.
- LC1020E pages: Compare, Meter settings (settings, calibration, backup and restore).

## Install

Download the file for your system from the [Releases](https://github.com/zombu4/fnirsi-companion/releases) page and
check it against `SHA256SUMS`.

### macOS

Open `FNIRSI-Companion-<version>-macos-arm64.dmg` (Apple silicon) or `...-macos-x86_64.dmg` (Intel) and drag
"FNIRSI Companion" onto Applications. If the disk image says it is not signed, `First start - READ ME.txt` on it
lists the steps macOS asks for the first time.

### Windows

Run `FNIRSI-Companion-<version>-windows-x64.exe`. It installs for all users and asks Windows for administrator rights;
`/CURRENTUSER` on the command line installs for the current user only.

### Linux

- **.deb** (Debian, Ubuntu): `sudo apt install ./FNIRSI-Companion-<version>-linux-amd64.deb`. The package depends on
  `libxcb-cursor0` and installs the udev rule below; plug the meter in again after the first install.
- **AppImage**: `chmod +x FNIRSI-Companion-<version>-linux-x86_64.AppImage` and run it. It carries `libxcb-cursor0`. An
  AppImage cannot install the udev rule: copy it yourself (below).

#### udev rule

Without the rule a normal user may not open the meter's serial port ("permission denied"). The rule
`packaging/linux/60-fnirsi.rules` gives the logged-in user access (`TAG+="uaccess"`) and keeps ModemManager from probing
the meters (`ENV{ID_MM_DEVICE_IGNORE}="1"`) for the four VID:PIDs in the table above:

```sh
sudo cp packaging/linux/60-fnirsi.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty --action=add
```

Then unplug and re-plug the meter. (The AppImage also carries the file under `usr/share/doc/fnirsi-companion/`.)

## Run from source

Python 3.11 or later.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt      # Windows: .venv\Scripts\python
.venv/bin/python -m fnirsi_companion
```

`--no-autoconnect` starts the program without opening any serial port. On Linux Qt's xcb plug-in needs the system
package `libxcb-cursor0`.

## Tests

No meter and no display are needed:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions (`.github/workflows/build.yml`) runs them on macOS, Windows and Linux on every push and pull request. A
tag `vA.B.C` also builds the packages, starts each one (`.github/scripts/smoke.py`) and attaches them to the release.

## Build the packages yourself

```sh
python -m pip install -r requirements.txt pyinstaller==6.22.3
python packaging/make_icons.py build/icons
python packaging/third_party.py build/THIRD_PARTY_LICENSES.txt
APP_ICON=$PWD/build/icons/fnirsi-companion.icns pyinstaller --noconfirm packaging/companion.spec   # .ico on Windows
```

Then `packaging/make_dmg.sh` (macOS), `packaging/windows/fnirsi_companion.iss` with Inno Setup (Windows),
`packaging/linux/build_appimage.sh` or `build_deb.sh` (Linux); the commands are in the workflow.

## Licence

The program is under the non-commercial licence in [LICENSE](LICENSE). Third-party software bundled with the builds
(Python, PySide6 / Qt under the LGPLv3, pyqtgraph, numpy, pyserial, certifi) keeps its own licences; their texts, and
where the Qt and PySide6 sources are, are in `THIRD_PARTY_LICENSES.txt` inside each package.
