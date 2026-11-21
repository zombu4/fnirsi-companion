"""Readings of a session: statistics, trend history and the CSV recording.

A reading is a devices.base.Reading; this module reads only its fields (t, wall, quantity, value, unit, text, decimals,
validity, secondary, key), so it needs no import of the device code.  Only readings with a finite numeric value that
is not an overload, a missing reading or a held one enter the statistics (a held value repeated would bias them); the
trend keeps every reading, a reading without a number as a gap (None); the readings the log trigger accepts go into the
CSV file while recording.  The statistics and the trend restart / break when Reading.key changes, because values of
different scales cannot be combined.  Adapted from the 121GW companion's record.py.
"""
import csv
import math
import os
import re
import threading
import time
from collections import deque

TREND_POINTS = 6000                      # about 10 minutes at 10 readings per second
NOT_COUNTED = ('overload', 'no reading', 'held')     # validities whose value does not enter the statistics

LOG_MODES = ('all', 'interval', 'change', 'limit')   # every reading; every N seconds; change by N counts; limit verdict
LOG_INTERVAL_MAX, LOG_CHANGE_MAX = 999, 255          # seconds; counts
LOG_DEFAULT = ('all', 1, 10)                         # mode, interval, change threshold

CSV_HEADER = ['time', 'quantity', 'display', 'value', 'unit', 'validity',
              'secondary quantity', 'secondary display', 'secondary value', 'secondary unit', 'secondary validity']

_NUMBER = re.compile(r'([-+]?)\s*(\d[\d.,]*)')


def numeric(reading):
    """The reading's value when it is a finite number, else None."""
    v = reading.value
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


class Stats:
    """Minimum, maximum, mean, standard deviation and count of the readings since the last key change."""

    def __init__(self):
        self.reset(None)

    def reset(self, key):
        self.key, self.n, self.min, self.max, self._mean, self._m2 = key, 0, None, None, 0.0, 0.0

    def add(self, key, v):
        """Count the number `v` of the readings with `key`; a different key than before restarts everything."""
        if key != self.key:
            self.reset(key)
        self.n += 1
        self.min = v if self.min is None else min(self.min, v)
        self.max = v if self.max is None else max(self.max, v)
        d = v - self._mean                              # Welford: numerically stable mean and variance
        self._mean += d / self.n
        self._m2 += d * (v - self._mean)

    def add_reading(self, reading):
        """Feed one Reading: its key restarts the statistics; its value counts when it is a counted number."""
        if reading.key != self.key:
            self.reset(reading.key)
        v = numeric(reading)
        if v is not None and reading.validity not in NOT_COUNTED:
            self.add(reading.key, v)

    @property
    def mean(self):
        return self._mean if self.n else None

    @property
    def sd(self):
        """Sample standard deviation (n - 1); None below two readings."""
        return math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else None

    def snapshot(self):
        return {'n': self.n, 'min': self.min, 'max': self.max, 'mean': self.mean, 'sd': self.sd}


class Trend:
    """A ring buffer of (t, value, key); None as value marks a reading without a number."""

    def __init__(self, points=TREND_POINTS):
        self._buf = deque(maxlen=points)
        self._key = None

    def add(self, reading):
        v = numeric(reading)
        self._key = reading.key
        self._buf.append((reading.t, v, reading.key))

    def clear(self):
        self._buf.clear()
        self._key = None

    def __len__(self):
        return len(self._buf)

    def points(self, since=None):
        """[(t, value)] for the plot, oldest first, newer than `since` when given.  Points of an earlier key are None
        (a gap), so the trace never joins two scales; a reading without a number is None too."""
        key = self._key
        return [(t, v if k == key else None) for t, v, k in self._buf if since is None or t > since]

    def current(self):
        """[(t, value)] of the run of readings with the newest key only (value may be None)."""
        out = []
        for t, v, k in reversed(self._buf):
            if k != self._key:
                break
            out.append((t, v))
        out.reverse()
        return out

    def export_csv(self, folder, unit='', prefix='FNIRSI'):
        """Write current() as <prefix>-trend-<time>.csv in `folder` (unit in the value header); (path, points)."""
        pts = self.current()
        if not pts:
            raise ValueError('nothing to export yet')
        path = _new_path(folder, prefix + '-trend')
        with open(path, 'w', newline='', encoding='utf-8') as f:
            f.write('time_unix_s,value' + ('_' + unit if unit else '') + '\n')
            f.writelines('%.3f,%s\n' % (t, '' if v is None else repr(v)) for t, v in pts)
        return path, len(pts)


def _new_path(folder, stem):
    """folder/<stem>_<local time>.csv, made new: an existing name gets -2, -3, ..."""
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, '%s_%s' % (stem, time.strftime('%Y-%m-%d_%H-%M-%S')))
    path, n = base + '.csv', 1
    while os.path.exists(path):
        n += 1
        path = '%s-%d.csv' % (base, n)
    return path


class LogTrigger:
    """Which readings become CSV rows while recording; the live display, statistics and trend are not affected.
    The first reading of a recording is always written.
      all       every reading
      interval  a row every `interval` seconds; 0: every reading
      change    a row when the displayed digits (an integer, sign included) differ from those of the last row by at
                least `change` counts; tested once per second.  A change of key, or between a number and an overload /
                a reading without a number, is a change.  With an interval above 0 also a row every interval
      limit     a row when the limit verdict ('PASS', 'HI', 'LO', None) changes; with an interval above 0 also every
                interval while the verdict is not 'PASS' (None, no limit set, counts)
    The interval and the once-per-second test are clocks of their own: a row for another reason does not restart them.
    They run on Reading.t and step by whole intervals, so a late reading does not move the following rows.  With change
    and limit an interval of 0 means "the trigger only".
    """

    def __init__(self, mode='all', interval=1, change=10):
        self.configure(mode, interval, change)

    def configure(self, mode, interval, change):
        """Set the rule and forget the last row (a new recording starts)."""
        if mode not in LOG_MODES:
            raise ValueError('log trigger: one of %s' % ', '.join(LOG_MODES))
        for name, v, lo, hi in (('interval', interval, 0, LOG_INTERVAL_MAX), ('change', change, 1, LOG_CHANGE_MAX)):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError('log trigger %s: a whole number %d..%d' % (name, lo, hi))
        self.mode, self.interval, self.change = mode, interval, change
        self.reset()

    def reset(self):
        self._last = None                   # (key, kind, level) of the last row; None: no row yet
        self._next_tick = None              # reading time of the next interval row
        self._next_second = None            # reading time of the next change test
        self._verdict = None                # verdict of the previous reading

    @staticmethod
    def level(reading):
        """(kind, level): 'num' with the displayed digits as an integer (from the text the meter shows: "1.234 nF" is
        1234; without digits in the text, value * 10**decimals), 'ofl' for an overload, 'blank' for no number."""
        v = numeric(reading)
        if v is None:
            return ('ofl' if reading.validity == 'overload' else 'blank'), 0
        m = _NUMBER.match(reading.text or '')
        if m:
            digits = re.sub(r'\D', '', m.group(2))
            return 'num', -int(digits) if m.group(1) == '-' else int(digits)
        return 'num', int(round(v * 10 ** (reading.decimals or 0)))

    @staticmethod
    def _due(t, nxt, step):
        """(due, next time): `nxt` has come at t; the next one is a whole step later, or a step after t after a gap."""
        if t < nxt:
            return False, nxt
        nxt += step
        return True, nxt if nxt > t else t + step

    def accept(self, reading, verdict=None):
        """True when the reading is written as a row.  `verdict`: the limit verdict of this reading (only mode 'limit')."""
        if self.mode == 'all':
            return True
        t = reading.t
        kind, lev = self.level(reading)
        cur = (reading.key, kind, lev)
        if self._last is None:                          # the first reading of the recording
            take = True
            self._next_tick = t + self.interval if self.interval else None
            self._next_second = t + 1.0
        else:
            tick = False
            if self.interval:
                tick, self._next_tick = self._due(t, self._next_tick, self.interval)
            if self.mode == 'interval':
                take = tick or not self.interval
            elif self.mode == 'change':
                second, self._next_second = self._due(t, self._next_second, 1.0)
                take = tick or (second and self._changed(cur))
            else:
                take = verdict != self._verdict or (tick and verdict != 'PASS')
        self._verdict = verdict
        if take:
            self._last = cur
        return take

    def _changed(self, cur):
        key, kind, lev = self._last
        if (key, kind) != cur[:2]:
            return True
        return kind == 'num' and abs(cur[2] - lev) >= self.change


class Recorder:
    """Writes the readings the trigger accepts as rows of a CSV file.

    Columns: CSV_HEADER.  `value_header` replaces the header of the value columns (for example "value as displayed (no
    prefix)", DESIGN 13.4).  `sep`: the field separator; `dec`: the decimal mark of the numbers (',' with ';').  The
    unit columns carry each row's unit, since the quantity can change during a recording.  Thread safe.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.trigger = LogTrigger()
        self.path = None
        self.rows = 0
        self.started = None
        self._file = None
        self._writer = None
        self._dec = '.'

    @property
    def recording(self):
        return self._writer is not None

    def start(self, folder, prefix='FNIRSI', sep=',', dec='.', trigger=LOG_DEFAULT, value_header='value'):
        """Open a new file in `folder`; returns its path.  A bad trigger starts nothing.  Running already: its path."""
        with self.lock:
            if self._writer is not None:
                return self.path
            self.trigger.configure(*trigger)
            self.path = _new_path(folder, prefix)
            self._file = open(self.path, 'w', newline='', encoding='utf-8')
            self._writer = csv.writer(self._file, delimiter=sep)
            self._dec = dec
            head = list(CSV_HEADER)
            head[3] = value_header
            head[8] = 'secondary ' + value_header
            self._writer.writerow(head)
            self.rows, self.started = 0, time.time()
            return self.path

    def add(self, reading, verdict=None):
        """Write the reading when recording and the trigger accepts it; True when a row was written."""
        with self.lock:
            if self._writer is None or not self.trigger.accept(reading, verdict):
                return False
            d = self._dec
            self._writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reading.wall))
                                   + d + '%03d' % int((reading.wall % 1) * 1000)]
                                  + self._cells(reading, d)
                                  + self._cells(reading.secondary, d))
            self.rows += 1
            if self.rows % 50 == 0:
                self._file.flush()
            return True

    @staticmethod
    def _cells(r, d):
        """quantity, display, value, unit, validity of one reading (empty for a missing secondary)."""
        if r is None:
            return [''] * 5
        v = numeric(r)
        return [r.quantity, (r.text or '').replace('.', d), '' if v is None else repr(v).replace('.', d), r.unit,
                r.validity]

    def stop(self):
        """Close the file; returns its path (None when nothing was recorded)."""
        with self.lock:
            if self._file is not None:
                self._file.close()
            self._file = self._writer = None
            return self.path
