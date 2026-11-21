"""Tests of core/record.py: statistics, trend, log trigger and CSV recording (no meter, no display)."""
import csv
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fnirsi_companion.core import record


def rd(t=0.0, value=1.0, text='1.000 nF', decimals=3, validity='valid', key=('C', 'nF'), quantity='C', unit='F',
       secondary=None, wall=None):
    """A Reading built through the class of devices.base (imported when first needed)."""
    from fnirsi_companion.devices.base import Reading
    return Reading(t=t, wall=1_700_000_000.25 + t if wall is None else wall, meter_ms=None, quantity=quantity,
                   value=value, unit=unit, text=text, decimals=decimals, validity=validity, secondary=secondary, key=key)


class StatsTest(unittest.TestCase):
    def test_values(self):
        s = record.Stats()
        for i, v in enumerate((1.0, 2.0, 3.0, 4.0, 5.0)):
            s.add_reading(rd(i, v))
        snap = s.snapshot()
        self.assertEqual((snap['n'], snap['min'], snap['max'], snap['mean']), (5, 1.0, 5.0, 3.0))
        self.assertAlmostEqual(snap['sd'], math.sqrt(2.5))

    def test_empty_and_single(self):
        s = record.Stats()
        self.assertEqual(s.snapshot(), {'n': 0, 'min': None, 'max': None, 'mean': None, 'sd': None})
        s.add_reading(rd(0, 7.0))
        self.assertEqual((s.n, s.mean, s.sd), (1, 7.0, None))

    def test_key_change_restarts(self):
        s = record.Stats()
        s.add_reading(rd(0, 1.0))
        s.add_reading(rd(1, 3.0))
        s.add_reading(rd(2, 100.0, key=('R', 'ohm')))
        self.assertEqual((s.n, s.min, s.max, s.key), (1, 100.0, 100.0, ('R', 'ohm')))

    def test_key_change_without_number_restarts_too(self):
        s = record.Stats()
        s.add_reading(rd(0, 1.0))
        s.add_reading(rd(1, None, 'OL', validity='overload', key=('R', 'ohm')))
        self.assertEqual((s.n, s.key), (0, ('R', 'ohm')))

    def test_not_counted(self):
        s = record.Stats()
        s.add_reading(rd(0, 1.0))
        s.add_reading(rd(1, None, 'OL', validity='overload'))
        s.add_reading(rd(2, 9.0, validity='held'))
        s.add_reading(rd(3, None, '', validity='no reading'))
        s.add_reading(rd(4, float('nan')))
        s.add_reading(rd(5, float('inf')))
        s.add_reading(rd(6, 3.0, validity='unknown'))
        self.assertEqual((s.n, s.min, s.max), (2, 1.0, 3.0))

    def test_reset_keeps_key(self):
        s = record.Stats()
        s.add_reading(rd(0, 1.0))
        s.reset(s.key)
        self.assertEqual((s.n, s.key), (0, ('C', 'nF')))


class TrendTest(unittest.TestCase):
    def test_gaps_and_keys(self):
        tr = record.Trend()
        tr.add(rd(0, 1.0))
        tr.add(rd(1, None, 'OL', validity='overload'))
        tr.add(rd(2, 2.0))
        tr.add(rd(3, 50.0, key=('R', 'ohm')))
        tr.add(rd(4, 51.0, key=('R', 'ohm')))
        self.assertEqual(tr.points(), [(0, None), (1, None), (2, None), (3, 50.0), (4, 51.0)])
        self.assertEqual(tr.current(), [(3, 50.0), (4, 51.0)])
        self.assertEqual(tr.points(since=2), [(3, 50.0), (4, 51.0)])

    def test_current_includes_gap(self):
        tr = record.Trend()
        tr.add(rd(0, 1.0))
        tr.add(rd(1, None, 'OL', validity='overload'))
        tr.add(rd(2, 2.0))
        self.assertEqual(tr.current(), [(0, 1.0), (1, None), (2, 2.0)])

    def test_ring(self):
        tr = record.Trend(points=3)
        for i in range(5):
            tr.add(rd(i, float(i)))
        self.assertEqual((len(tr), tr.points()), (3, [(2, 2.0), (3, 3.0), (4, 4.0)]))
        tr.clear()
        self.assertEqual((len(tr), tr.points(), tr.current()), (0, [], []))

    def test_export(self):
        tr = record.Trend()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                tr.export_csv(d)
            tr.add(rd(1.5, 2.0))
            tr.add(rd(2.5, None, 'OL', validity='overload'))
            path, n = tr.export_csv(os.path.join(d, 'sub'), unit='F')
            self.assertEqual(n, 2)
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'time_unix_s,value_F\n1.500,2.0\n2.500,\n')
            self.assertTrue(os.path.basename(path).startswith('FNIRSI-trend_'))


class LogTriggerTest(unittest.TestCase):
    def test_all(self):
        t = record.LogTrigger('all')
        self.assertTrue(all(t.accept(rd(i * 0.01)) for i in range(5)))

    def test_interval(self):
        t = record.LogTrigger('interval', 1)
        got = [x for x in (0.0, 0.5, 1.0, 1.4, 2.0, 3.2, 4.0, 4.3) if t.accept(rd(x))]
        self.assertEqual(got, [0.0, 1.0, 2.0, 3.2, 4.0])      # the late 3.2 does not move the row at 4.0
        t = record.LogTrigger('interval', 1)
        got = [x for x in (0.0, 5.5, 6.0, 6.5) if t.accept(rd(x))]
        self.assertEqual(got, [0.0, 5.5, 6.5])                # after a gap of several intervals: a step after the late one

    def test_interval_zero_is_every_reading(self):
        t = record.LogTrigger('interval', 0)
        self.assertTrue(all(t.accept(rd(x)) for x in (0.0, 0.1, 0.2)))

    def test_change_counts_from_displayed_digits(self):
        t = record.LogTrigger('change', 0, 10)
        self.assertTrue(t.accept(rd(0.0, 1.0e-9, '1.000 nF')))
        self.assertFalse(t.accept(rd(0.5, 1.02e-9, '1.020 nF')))     # second not due
        self.assertFalse(t.accept(rd(1.0, 1.005e-9, '1.005 nF')))    # 5 counts
        self.assertTrue(t.accept(rd(2.0, 1.015e-9, '1.015 nF')))     # 15 counts from the last row
        self.assertFalse(t.accept(rd(3.0, 1.012e-9, '1.012 nF')))    # 3 counts
        self.assertTrue(t.accept(rd(4.0, 1.002e-9, '1.002 nF')))     # 13 counts

    def test_change_negative_and_zero_crossing(self):
        t = record.LogTrigger('change', 0, 1)
        self.assertTrue(t.accept(rd(0.0, 0.5, '0.500 V')))
        self.assertTrue(t.accept(rd(1.0, -0.5, '-0.500 V')))

    def test_change_key_and_overload(self):
        t = record.LogTrigger('change', 0, 255)
        self.assertTrue(t.accept(rd(0.0)))
        self.assertTrue(t.accept(rd(1.0, key=('R', 'ohm'))))
        self.assertTrue(t.accept(rd(2.0, None, 'OL', validity='overload', key=('R', 'ohm'))))
        self.assertFalse(t.accept(rd(3.0, None, 'OL', validity='overload', key=('R', 'ohm'))))
        self.assertTrue(t.accept(rd(4.0, None, '', validity='no reading', key=('R', 'ohm'))))

    def test_change_with_interval(self):
        t = record.LogTrigger('change', 2, 255)
        self.assertTrue(t.accept(rd(0.0)))
        self.assertFalse(t.accept(rd(1.0)))
        self.assertTrue(t.accept(rd(2.0)))

    def test_level_without_digits_in_text(self):
        self.assertEqual(record.LogTrigger.level(rd(0, 0.0123, 'abc', decimals=3)), ('num', 12))
        self.assertEqual(record.LogTrigger.level(rd(0, 1.0, '1,234 nF')), ('num', 1234))     # decimal comma
        self.assertEqual(record.LogTrigger.level(rd(0, 1.0, '-1.234 nF')), ('num', -1234))

    def test_limit(self):
        t = record.LogTrigger('limit', 0)
        self.assertTrue(t.accept(rd(0.0), 'PASS'))
        self.assertFalse(t.accept(rd(0.1), 'PASS'))
        self.assertTrue(t.accept(rd(0.2), 'HI'))
        self.assertFalse(t.accept(rd(0.3), 'HI'))
        self.assertTrue(t.accept(rd(0.4), 'PASS'))

    def test_limit_with_interval_only_while_not_pass(self):
        t = record.LogTrigger('limit', 1)
        self.assertTrue(t.accept(rd(0.0), 'PASS'))
        self.assertFalse(t.accept(rd(1.0), 'PASS'))
        self.assertTrue(t.accept(rd(1.5), 'HI'))
        self.assertTrue(t.accept(rd(2.0), 'HI'))
        self.assertFalse(t.accept(rd(2.5), 'HI'))

    def test_configure_checks(self):
        for args in (('nope', 1, 10), ('all', -1, 10), ('all', 1000, 10), ('all', 1, 0), ('all', 1, 256),
                     ('all', True, 10), ('all', 1.5, 10)):
            with self.assertRaises(ValueError, msg=args):
                record.LogTrigger(*args)
        record.LogTrigger('all', 999, 255)


def read_csv(path, sep=','):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.reader(f, delimiter=sep))


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_rows_header_secondary(self):
        r = record.Recorder()
        self.assertFalse(r.recording)
        self.assertFalse(r.add(rd(0)))
        path = r.start(self.tmp.name)
        self.assertTrue(r.recording)
        sec = rd(0, 0.0123, '0.0123', quantity='D', unit='', validity='valid')
        self.assertTrue(r.add(rd(0, 1.5e-9, '1.500 nF', secondary=sec)))
        self.assertTrue(r.add(rd(1, None, 'OL', validity='overload')))
        self.assertEqual(r.stop(), path)
        self.assertFalse(r.recording)
        self.assertFalse(r.add(rd(2)))
        rows = read_csv(path)
        self.assertEqual(rows[0], record.CSV_HEADER)
        self.assertEqual(rows[0][4], 'unit')
        self.assertEqual(rows[0][5], 'validity')
        self.assertEqual(rows[1][1:], ['C', '1.500 nF', repr(1.5e-9), 'F', 'valid',
                                       'D', '0.0123', '0.0123', '', 'valid'])
        self.assertEqual(rows[1][0], time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(1_700_000_000.25)) + '.250')
        self.assertEqual(rows[2][1:], ['C', 'OL', '', 'F', 'overload', '', '', '', '', ''])
        self.assertEqual((r.rows, len(rows)), (2, 3))

    def test_decimal_comma_and_value_header(self):
        r = record.Recorder()
        path = r.start(self.tmp.name, sep=';', dec=',', value_header='value as displayed (no prefix)')
        r.add(rd(0, 1.5, '1.500 V', unit='V'))
        r.stop()
        rows = read_csv(path, ';')
        self.assertEqual(rows[0][3], 'value as displayed (no prefix)')
        self.assertEqual(rows[0][8], 'secondary value as displayed (no prefix)')
        self.assertEqual(rows[1][2:4], ['1,500 V', '1,5'])
        self.assertTrue(rows[1][0].endswith(',250'))

    def test_trigger_applies(self):
        r = record.Recorder()
        path = r.start(self.tmp.name, trigger=('interval', 2, 10))
        for i in range(5):
            r.add(rd(float(i)))
        r.stop()
        self.assertEqual(len(read_csv(path)), 1 + 3)           # t = 0, 2, 4

    def test_bad_trigger_starts_nothing(self):
        r = record.Recorder()
        with self.assertRaises(ValueError):
            r.start(self.tmp.name, trigger=('bogus', 1, 1))
        self.assertFalse(r.recording)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_start_twice_and_same_second(self):
        r = record.Recorder()
        p1 = r.start(self.tmp.name)
        self.assertEqual(r.start(self.tmp.name), p1)
        r.stop()
        r2 = record.Recorder()
        p2 = r2.start(self.tmp.name)
        r2.stop()
        self.assertNotEqual(p1, p2)
        self.assertEqual(len(os.listdir(self.tmp.name)), 2)

    def test_flush_every_fifty(self):
        r = record.Recorder()
        path = r.start(self.tmp.name)
        for i in range(50):
            r.add(rd(float(i)))
        self.assertEqual(len(read_csv(path)), 51)               # readable before stop()
        r.stop()


if __name__ == '__main__':
    unittest.main()
