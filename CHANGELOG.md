# Changelog

## 0.1.0 - 2026-11-21

Tested with the LC1020E on the meter (all pages). The 2C53T side is tested against the fake meter of the
test suite only; its test on a meter follows.

- First version of the FNIRSI Companion: one desktop program for the FNIRSI 2C53T and the LC1020E running the
  rewritten firmware, a native Qt window (no web server, no browser).
- Finds the meter on USB by VID:PID and serial number, connects by itself and shows the pages of the attached model.
- Packages: macOS disk image (Apple silicon and Intel), Windows installer, Linux AppImage and .deb (with the udev
  rule for the meters' serial ports).
- Updates back up the meter's settings and calibration first (also the LC1020E application and bootloader-slot
  updates); a bootloader slot file that is not in the SHA256SUMS of a release gets the "not ours" warning.
- Long jobs stop between two requests when cancelled (backups, screenshots, the 2C53T transfer before its install,
  the wait for the LC1020E update drive); saves, installs and copies always run to their end.
- "End drive sharing" asks for the eject first and refuses while the meter's drive is still mounted.
- Qt for Python 6.11.3: 6.12.0 on Linux with Python 3.11 loses a reference of True at every signal emit and stops
  the program after a few hundred readings; tests/test_qt_binding.py checks every release for it.
