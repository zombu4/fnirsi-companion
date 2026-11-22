# Changelog

## 0.2.0 - unreleased

- New look after the 121GW Companion: sidebar with painted icons instead of tabs, a header with firmware, bootloader,
  port and status chips, cards instead of group boxes, a painted seven-segment LCD on the Live page with the meter's
  annunciators and keys, segmented controls, dark and light themes from one set of colours (the "system" theme follows
  the computer), the same Fusion style on macOS, Windows and Linux. The pages' commands, confirmations, backups and
  jobs are unchanged.
- LC1020E: supports the INFO capability bit 21 "direct set" of the new firmware. With it set, SCREEN, LCR_SET, HOLD,
  SETTINGS_SET and COMPARE_SET wait 4 s for the answer (23 s / 63 s on firmware without the bit), and SETTINGS_SET /
  COMPARE_SET no longer switch the meter to the settings screen.
- LC1020E: supports the INFO capability bits 22 to 24 of the new firmware. Bit 22: the frequency list has the
  sixth entry 60 Hz (the selector shows the list as the meter reports it). Bit 23: TOLERANCE_SET (vendor, limits in
  % or pF, IEC 60062 letter, secondary limit D / Q / ESR) and the 50-byte COMPARE record; the Compare page shows the
  judgement (IN, HI, LO, ---, OUT), the signed deviation, the mode and the limits in force, and has a TOLERANCE_SET
  form. Bit 24: the MEASURE marker byte (a main or secondary value outside the manual's accuracy table is shown
  as "unspecified") and the save result 9 "unchanged". Firmware without the bits keeps its 64-byte MEASURE and
  29-byte COMPARE records.
- LC1020E: supports the own calibration area of the new firmware (INFO capability bit 26, commands 0x30 to 0x37,
  event 0x98 OCAL_CHANGED, MEASURE byte 65). New page "Gain calibration" (Meter settings): the reference resistors
  with their typed values and uncertainties, a run per resistor through the lock (autoranged cell and neighbours
  per frequency and level, with a progress bar), the short and open steps, the solver (weighted least squares of
  log magnitude and phase per frequency, anchors referred back through the fitted factors, identifiability bits,
  residual checks per frequency and level, all-or-nothing per frequency) with its report, and Write GAIN with the
  typed OK; cards "Constants" (CLIP from lock sweeps, SETTLE from settle traces, both editable) and "Level monitor"
  (the DMM's AC volts per frequency give kV; the I check line). The LCR page shows Vac / Iac from MONITOR
  ("uncalibrated" without a calibration) and the "gain cal" mark of the MEASURE flags with a warning for a
  frequency whose cell is not calibrated. The Backup page lists the .ocal files (both area slots, bound to the
  meter's UID), restores one record (another UID in the name or the record is refused), shows OCAL_INFO and
  rolls back. Every write of the area reads both slots into a .ocal file first and needs the typed OK.
- LC1020E: supports the INFO capability bit 25, the calibration sweep of the new firmware. CALIBRATE waits 366 s
  (126 s on firmware without the bit), reads the 8-byte answer and shows the point (n/15) and the reason when the
  sweep ends without a result; the progress line shows the point and the windows held (the step of the CALIBRATION
  event counts 3 per point) instead of the vendor's seconds.

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
