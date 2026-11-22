"""The host procedures that collect the data for the gain calibration, the constants and the level
monitor of the LC1020E (OWN_CAL_DESIGN.md "OCD" 3.3, 6.3), as functions of a connected LC1020E.

Every function works through the lock (OCAL_LOCK), the raw record (OCAL_RAW) and the settle trace
(OCAL_TRACE) of devices.lc1020e.LC1020E and waits for windows with a deadline (ocal_wait_window); none
writes to the meter's flash. The caller holds the port (LC1020E.ocal_session) and releases the lock
with the `finally` of each function.

  collect_resistor   OCD 3.3 step 2: per frequency and level the autoranged cell and its neighbours
  collect_shortopen  OCD 3.3 step 2a: short bar or open terminals, 8 windows each at one cell
  sweep_clip         OCD 6.3: every gain code locked, max / min of ADC1 and ADC2 per code
  measure_trace      OCD 6.3: one settle trace after a change of a kind, and its proposed settle time
"""

from __future__ import annotations

from typing import Callable

from ..devices import lc1020e as lc
from . import ocal_solve as solve

Progress = Callable[[float, str], None]

# LCR_SET setting numbers are the LCR_TABLES list ids (SL 5.3)
LIST_FREQUENCY, LIST_LEVEL, LIST_BIAS, LIST_SPEED = 2, 3, 4, 6
KIND_LIST = {1: LIST_FREQUENCY, 2: LIST_LEVEL, 3: LIST_BIAS, 4: LIST_SPEED}   # settle kind -> list
# neighbouring cells of the autoranged one (OCD 3.3 step 2): one code at a time, (dc5, dc6, dc8)
NEIGHBOURS = [(0, 0, 1), (0, 0, -1), (1, 0, 0), (-1, 0, 0), (2, 0, 0), (-2, 0, 0),
              (0, 1, 0), (0, -1, 0), (0, 2, 0), (0, -2, 0)]
SETTLE_WINDOWS = 2               # the window closed after a change is the second one
SHORTOPEN_WINDOWS = 8            # OCD 3.3 step 2a


def _say(progress: Progress | None, frac: float, text: str) -> None:
    if progress is not None:
        progress(frac, text)


def meter_points(dev) -> tuple:
    """(frequency index of each fi in OCAL_FREQ that the meter's frequency list offers,
    [(level in volts, list index)]) from LCR_TABLES lists 2 and 3."""
    freqs = {int(v): i for i, (v, _t) in enumerate(dev.lcr_tables(LIST_FREQUENCY).entries)}
    by_fi = {fi: freqs[hz] for fi, hz in enumerate(lc.OCAL_FREQ) if hz in freqs}
    levels = [(float(v), i) for i, (v, _t) in enumerate(dev.lcr_tables(LIST_LEVEL).entries)]
    return by_fi, levels


def settle_point(dev, freq_index: int, level_index: int):
    """OCD 3.3 step 2: release the lock, LCR_SET frequency and level, wait for the settle (the
    second window after the change), then MEASURE with autoranging. Returns the OCAL_RAW record of
    the autoranged cell (an unlocked window)."""
    dev.ocal_release()
    dev.lcr_set(LIST_FREQUENCY, freq_index)
    dev.lcr_set(LIST_LEVEL, level_index)
    w0 = dev.ocal_raw().window
    dev.ocal_wait_window(w0, SETTLE_WINDOWS)
    dev.measure()
    raw = dev.ocal_wait_window(dev.ocal_raw().window, 1)
    if raw.locked:
        raise lc.DeviceError("the meter reports a lock after the release")
    return raw


def lock_cell(dev, cell: tuple):
    """Locks a cell and returns the window closed after the change (the second one)."""
    first = dev.ocal_lock(*cell)
    return dev.ocal_wait_window(first.window, SETTLE_WINDOWS)


def neighbour_cells(cell: tuple) -> list:
    """The cells next to `cell` (NEIGHBOURS) that exist: c5 0..7, c6 0..7, c8 0..3."""
    out = []
    for d5, d6, d8 in NEIGHBOURS:
        c = (cell[0] + d5, cell[1] + d6, cell[2] + d8)
        if 0 <= c[0] <= 7 and 0 <= c[1] <= 7 and 0 <= c[2] <= 3:
            out.append(c)
    return out


def nearest_cell(cell: tuple, allowed) -> tuple:
    """The allowed cell nearest to `cell` (sum of the code differences; the first on a tie in
    sorted order); `cell` itself when allowed is empty or contains it."""
    allowed = sorted(allowed)
    if not allowed or cell in allowed:
        return cell
    return min(allowed, key=lambda c: sum(abs(a - b) for a, b in zip(c, cell)))


def collect_resistor(dev, resistor: int, progress: Progress | None = None) -> tuple:
    """OCD 3.3 step 2 for the resistor now in the terminals: per frequency (of OCAL_FREQ the meter
    offers) and level, the autoranged cell (recorded as autoranged) and each neighbouring cell that
    shows no saturated acquisition and both amplitudes of at least 300 counts. Returns
    (windows, notes); the lock is released at the end."""
    by_fi, levels = meter_points(dev)
    windows: list = []
    notes: list = []
    points = [(fi, lv) for fi in sorted(by_fi) for lv in levels]
    try:
        for n, (fi, (level, lindex)) in enumerate(points):
            hz = lc.OCAL_FREQ[fi]
            _say(progress, n / len(points), "%d Hz, %g V: autoranged cell" % (hz, level))
            dev._cancel_point()
            raw = settle_point(dev, by_fi[fi], lindex)
            if solve.is_usable(raw):
                windows.append(solve.window_from_raw(raw, resistor, fi, autoranged=True))
            else:
                notes.append("%d Hz, %g V: the autoranged cell %s is not usable (saturated or "
                             "below 300 counts)" % (hz, level, raw.cell))
            cell = raw.cell
            for k, nb in enumerate(neighbour_cells(cell)):
                _say(progress, (n + (k + 1) / (len(NEIGHBOURS) + 1)) / len(points),
                     "%d Hz, %g V: cell %s" % (hz, level, nb))
                dev._cancel_point()
                r = lock_cell(dev, nb)
                if r.cell == nb and r.locked and solve.is_usable(r):
                    windows.append(solve.window_from_raw(r, resistor, fi))
            dev.ocal_release()
    finally:
        dev.ocal_release()
    _say(progress, 1.0, "%d windows" % len(windows))
    return windows, notes


def collect_shortopen(dev, kind: str, take: int = 1, cells_ok=None,
                      progress: Progress | None = None) -> tuple:
    """OCD 3.3 step 2a: `kind` "short" or "open" in the terminals. Per frequency and level: the
    autoranged cell, then locked at it (or at the nearest cell of `cells_ok`, a set of (c5, c6, c8)
    the resistors' data calibrate, when the autoranged one is not among them) and 8 windows read.
    Saturated windows are dropped; the 300-count rule does not apply. Returns (windows, notes)."""
    if kind not in ("short", "open"):
        raise ValueError('kind "short" or "open"')
    by_fi, levels = meter_points(dev)
    windows: list = []
    notes: list = []
    points = [(fi, lv) for fi in sorted(by_fi) for lv in levels]
    try:
        for n, (fi, (level, lindex)) in enumerate(points):
            hz = lc.OCAL_FREQ[fi]
            _say(progress, n / len(points), "%s, %d Hz, %g V" % (kind, hz, level))
            dev._cancel_point()
            auto = settle_point(dev, by_fi[fi], lindex).cell
            cell = nearest_cell(auto, cells_ok or ())
            if cell != auto:
                notes.append("%d Hz, %g V: autoranged cell %s is not calibrated by the resistors; "
                             "read at %s" % (hz, level, auto, cell))
            raw = lock_cell(dev, cell)
            got = 0
            for _ in range(SHORTOPEN_WINDOWS):
                if not raw.saturated and not raw.overload and raw.locked and raw.cell == cell \
                        and raw.znom != 0:
                    windows.append(solve.shortopen_from_raw(raw, kind, fi, take))
                    got += 1
                dev._cancel_point()
                raw = dev.ocal_wait_window(raw.window, 1)
            if got == 0:
                notes.append("%d Hz, %g V: every window of cell %s was saturated" % (hz, level, cell))
            dev.ocal_release()
    finally:
        dev.ocal_release()
    _say(progress, 1.0, "%d %s windows" % (len(windows), kind))
    return windows, notes


def sweep_clip(dev, progress: Progress | None = None) -> dict:
    """OCD 6.3 (MFD M8): with the part in the terminals, every gain code locked in turn (c5 = c6 =
    code, the range of the autoranged cell); returns {(channel, code): (minimum, maximum)} of ADC1
    (channel 0, V) and ADC2 (channel 1, I) from the raw record of the window. The lock is released."""
    out: dict = {}
    try:
        dev.ocal_release()
        dev.measure()
        c8 = dev.ocal_wait_window(dev.ocal_raw().window, 1).c8
        for code in range(8):
            _say(progress, code / 8, "gain code %d" % code)
            dev._cancel_point()
            r = lock_cell(dev, (code, code, c8))
            out[(0, code)] = (r.min1, r.max1)
            out[(1, code)] = (r.min2, r.max2)
    finally:
        dev.ocal_release()
    _say(progress, 1.0, "gain codes done")
    return out


def merge_clip(total: dict, part: dict) -> dict:
    """Folds the observations of one part into the lowest minimum / highest maximum so far."""
    for key, (mn, mx) in part.items():
        old = total.get(key)
        total[key] = (mn, mx) if old is None else (min(old[0], mn), max(old[1], mx))
    return total


def measure_trace(dev, kind: int, fi: int, progress: Progress | None = None) -> tuple:
    """One settle trace for the kind of change 1..4 (frequency, level, bias, speed: LCR_SET lists
    2, 3, 4, 6) or 6 (a change of the gain codes by the lock) at frequency index `fi`, and the
    settle time propose_settle makes of it. Procedure: set the first choice of the list (not
    traced), wait the settle, arm, change to the second choice (the traced change), wait until the
    trace is full. Kinds 0 (restart) and 5 (screen) have no trigger here: ValueError. Returns
    (OcalTrace, proposed ms or None); the trace is disarmed and the lock released at the end."""
    if kind not in (1, 2, 3, 4, 6):
        raise ValueError("settle kind %d cannot be triggered from here: enter its time by hand" % kind)
    by_fi, _levels = meter_points(dev)
    if fi not in by_fi:
        raise lc.DeviceError("the meter's frequency list has no %d Hz" % lc.OCAL_FREQ[fi])
    try:
        dev.ocal_release()
        dev.ocal_trace_disarm()
        dev.lcr_set(LIST_FREQUENCY, by_fi[fi])
        if kind == 6:
            a, b = (2, 2, 1), (3, 3, 1)
            _say(progress, 0.1, "first codes")
            lock_cell(dev, a)
            dev.ocal_trace_arm()
            _say(progress, 0.4, "traced change of codes")
            dev.ocal_lock(*b)
        else:
            lid = KIND_LIST[kind]
            lst = dev.lcr_tables(lid)
            if len(lst.entries) < 2:
                raise lc.DeviceError("list %s has fewer than two choices" % lst.name)
            first, second = (0, 1)
            if kind == 1:                      # the traced change is the one onto frequency fi
                first = next(i for i in range(len(lst.entries)) if i != by_fi[fi])
                second = by_fi[fi]
            _say(progress, 0.1, "first choice of %s" % lst.name)
            dev.lcr_set(lid, first)
            w0 = dev.ocal_raw().window
            dev.ocal_wait_window(w0, SETTLE_WINDOWS)
            dev.ocal_trace_arm()
            _say(progress, 0.4, "traced change of %s" % lst.name)
            dev.lcr_set(lid, second)
        trace = dev.ocal_wait_trace()
    finally:
        try:
            dev.ocal_trace_disarm()
        finally:
            dev.ocal_release()
    ms = solve.propose_settle(trace.entries, trace.change_tick)
    _say(progress, 1.0, "trace of %d entries" % len(trace.entries))
    return trace, ms
