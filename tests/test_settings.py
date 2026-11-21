"""Tests of core/settings.py, core/paths.py and core/eventlog.py (files in temporary folders only)."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fnirsi_companion.core import eventlog, paths, settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'cfg', 'settings.json')

    def test_defaults(self):
        s = settings.Settings(self.path)
        self.assertIsNone(s.problem)
        self.assertEqual(s.as_dict(), settings.DEFAULTS)
        self.assertEqual((s.get('update_check'), s.get('theme'), s.get('csv_separator')), ('connect', 'system', ','))
        self.assertIs(s.get('let_2c53t_sleep'), False)
        self.assertFalse(os.path.exists(self.path))

    def test_set_saves_and_reloads(self):
        s = settings.Settings(self.path)
        folder = os.path.join(self.tmp.name, 'rec')
        s.set('recordings_folder', folder)
        s.set('theme', 'dark')
        s.set('csv_separator', ';')
        s.set('let_2c53t_sleep', True)
        s.set('update_check', 'daily')
        s.set('switch_to_new_meter', False)
        t = settings.Settings(self.path)
        self.assertEqual((t.recordings_folder(), t.get('theme'), t.csv_format(), t.get('let_2c53t_sleep'),
                          t.get('update_check'), t.get('switch_to_new_meter')), (folder, 'dark', (';', ','), True,
                                                                               'daily', False))
        self.assertFalse(os.path.exists(self.path + '.part'))

    def test_refuses_bad_values(self):
        s = settings.Settings(self.path)
        for k, v in (('theme', 'blue'), ('csv_separator', '\t'), ('update_check', 'hourly'), ('let_2c53t_sleep', 1),
                     ('switch_to_new_meter', 'yes'), ('recordings_folder', ''), ('recordings_folder', 5),
                     ('release_checked_at', -1), ('release_checked_at', True), ('installed_releases', {'a': 1}),
                     ('nope', 1)):
            with self.assertRaises(ValueError, msg=(k, v)):
                s.set(k, v)
        self.assertFalse(os.path.exists(self.path))
        with self.assertRaises(KeyError):
            s.get('nope')

    def test_bad_value_in_file_reads_as_default_and_unknown_key_kept(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump({'theme': 'blue', 'csv_separator': ';', 'future_key': [1, 2]}, f)
        s = settings.Settings(self.path)
        self.assertEqual((s.get('theme'), s.get('csv_separator')), ('system', ';'))
        s.set('theme', 'light')
        with open(self.path) as f:
            self.assertEqual(json.load(f), {'theme': 'light', 'csv_separator': ';', 'future_key': [1, 2]})

    def test_corrupt_file_kept_as_bad(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ('{not json', '[1, 2]'):
            with open(self.path, 'w') as f:
                f.write(content)
            s = settings.Settings(self.path)
            self.assertIn('defaults used', s.problem)
            self.assertEqual(s.as_dict(), settings.DEFAULTS)
            self.assertFalse(os.path.exists(self.path))
            with open(self.path + '.bad') as f:
                self.assertEqual(f.read(), content)

    def test_reset_keeps_unknown(self):
        s = settings.Settings(self.path)
        s.set('theme', 'dark')
        s._raw['future_key'] = 1
        s.reset()
        with open(self.path) as f:
            self.assertEqual(json.load(f), {'future_key': 1})

    def test_default_folders(self):
        s = settings.Settings(self.path)
        with mock.patch.object(paths, 'documents_dir', return_value='/docs/FNIRSI Companion'):
            self.assertEqual(s.recordings_folder(), os.path.join('/docs/FNIRSI Companion', 'Recordings'))
            self.assertEqual(s.backups_folder(), os.path.join('/docs/FNIRSI Companion', 'Backups'))

    def test_default_mutables_not_shared(self):
        s = settings.Settings(self.path)
        s.get('installed_releases')['x:y'] = 'v1.0.0'
        self.assertEqual(settings.DEFAULTS['installed_releases'], {})
        self.assertEqual(s.get('installed_releases'), {})

    def test_release_check_due(self):
        s = settings.Settings(self.path)
        self.assertTrue(s.release_check_due(True))
        self.assertTrue(s.release_check_due(False) is False)
        s.set('update_check', 'never')
        self.assertFalse(s.release_check_due(True))
        s.set('update_check', 'daily')
        self.assertTrue(s.release_check_due(False, now=1000.0))
        s.mark_release_checked(now=1000.0)
        self.assertFalse(s.release_check_due(True, now=1000.0 + 86399))
        self.assertTrue(s.release_check_due(False, now=1000.0 + 86400))

    def test_installed_release(self):
        s = settings.Settings(self.path)
        self.assertIsNone(s.installed_release('LC1020E', 'ABC'))
        s.record_install('LC1020E', 'ABC', 'v1.0.0')
        self.assertEqual(settings.Settings(self.path).installed_release('LC1020E', 'ABC'), 'v1.0.0')
        self.assertIsNone(s.installed_release('2C53T', 'ABC'))

    def test_config_path_is_in_config_dir(self):
        with mock.patch.object(paths, 'config_dir', return_value='/x/FNIRSI Companion'):
            self.assertEqual(settings.config_path(), os.path.join('/x/FNIRSI Companion', 'settings.json'))
            self.assertEqual(settings.event_log_path(), os.path.join('/x/FNIRSI Companion', 'events.log'))


class PathsTest(unittest.TestCase):
    def test_expected_values_follow_the_host_path_rules(self):
        # the code joins with os.path; on Windows that is ntpath (review_r1 finding 1): the expected
        # values are built the same way, so the tests hold on every system
        import ntpath
        import posixpath
        n = 'FNIRSI Companion'
        for mod in (ntpath, posixpath):
            with mock.patch.object(paths.os, 'path', mod), mock.patch.object(paths, 'home', return_value='/h'), \
                    mock.patch.object(paths.sys, 'platform', 'darwin'):
                self.assertEqual(paths.config_dir(), mod.join('/h', 'Library', 'Application Support', n))
        self.assertEqual(ntpath.join('/h', 'Library'), '/h\\Library')


    def test_per_os(self):
        n = 'FNIRSI Companion'
        with mock.patch.object(paths, 'home', return_value='/h'):
            with mock.patch.object(paths.sys, 'platform', 'darwin'):
                self.assertEqual(paths.config_dir(), os.path.join('/h', 'Library', 'Application Support', n))
                self.assertEqual(paths.cache_dir(), os.path.join('/h', 'Library', 'Caches', n))
            with mock.patch.object(paths.sys, 'platform', 'linux'), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(paths.config_dir(), os.path.join('/h', '.config', n))
                self.assertEqual(paths.cache_dir(), os.path.join('/h', '.cache', n))
            with mock.patch.object(paths.sys, 'platform', 'linux'), \
                    mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': '/xc', 'XDG_CACHE_HOME': '/xk'}):
                self.assertEqual((paths.config_dir(), paths.cache_dir()),
                                 (os.path.join('/xc', n), os.path.join('/xk', n)))
            with mock.patch.object(paths.sys, 'platform', 'win32'), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(paths.config_dir(), os.path.join('/h', 'AppData', 'Roaming', n))
                self.assertEqual(paths.cache_dir(), os.path.join('/h', 'AppData', 'Local', n))
            with mock.patch.object(paths.sys, 'platform', 'win32'), \
                    mock.patch.dict(os.environ, {'APPDATA': 'A', 'LOCALAPPDATA': 'L'}):
                self.assertEqual((paths.config_dir(), paths.cache_dir()), (os.path.join('A', n), os.path.join('L', n)))

    def test_documents(self):
        with tempfile.TemporaryDirectory() as h, mock.patch.object(paths, 'home', return_value=h):
            self.assertEqual(paths.documents_dir(), os.path.join(h, paths.APP_NAME))
            os.mkdir(os.path.join(h, 'Documents'))
            self.assertEqual(paths.documents_dir(), os.path.join(h, 'Documents', paths.APP_NAME))

    def test_home_is_a_folder(self):
        if sys.platform != 'win32':
            self.assertTrue(os.path.isdir(paths.home()))


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'sub', 'events.log')

    def test_marker_lines_and_previous(self):
        a = eventlog.EventLog(self.path, '0.1.0')
        a.write(1_700_000_000.5, 'first\nsecond')
        b = eventlog.EventLog(self.path, '0.1.0')
        b.write(1_700_000_001.0, 'later')
        with open(self.path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith(eventlog.MARKER) and ' program 0.1.0 ---' in lines[0])
        self.assertTrue(lines[1].endswith('.500  first second'))
        self.assertEqual(b.previous(10), lines[:2][-10:])
        self.assertEqual(b.previous(1), [lines[1]])

    def test_rotation(self):
        with mock.patch.object(eventlog, 'MAX_BYTES', 200):
            e = eventlog.EventLog(self.path, '0.1.0')
            for i in range(10):
                e.write(time.time(), 'x' * 50 + str(i))
        self.assertTrue(os.path.exists(self.path + '.1'))
        self.assertLess(os.path.getsize(self.path), 400)

    def test_failed_write_noted_once(self):
        notes = []
        blocker = os.path.join(self.tmp.name, 'file')
        open(blocker, 'w').close()
        e = eventlog.EventLog(os.path.join(blocker, 'events.log'), '0.1.0', on_error=notes.append)
        e.write(0.0, 'a')
        self.assertEqual(len(notes), 1)
        self.assertIn('event log file', notes[0])


if __name__ == '__main__':
    unittest.main()
