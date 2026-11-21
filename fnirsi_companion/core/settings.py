"""The program's own preferences: a JSON file settings.json in paths.config_dir().

Every key has a default and a check; a value that fails its check (a hand-edited file) reads as the default, and
set() refuses it.  Keys the program does not know are kept in the file when it is saved.  A file that is not valid JSON
(or not an object) is renamed to settings.json.bad at load and the defaults apply; `problem` says so.  Saves write
settings.json.part and rename it, so a crash cannot leave half a file.

    recordings_folder     None (default: <documents>/Recordings) or a folder path
    backups_folder        None (default: <documents>/Backups) or a folder path
    csv_separator         ',' (decimal point) or ';' (decimal comma, for spreadsheets of countries that write one)
    switch_to_new_meter   True: a meter plugged in while another is active becomes the active one (never while a job runs;
                          that is the caller's check)
    update_check          'connect' (at each connection), 'daily', 'never'
    theme                 'system', 'light', 'dark'
    let_2c53t_sleep       True: the program drops DTR after 10 minutes without user action so the 2C53T may switch itself
                          off (DESIGN 13.23); False: DTR stays set and the meter stays awake
    release_checked_at    None or the wall time (seconds) of the last release check, for 'daily'
    installed_releases    {"<model>:<serial>": tag} of the release the program itself last installed on that meter, the
                          only way to tell the LC1020E's release (its INFO text carries none)
"""
import json
import os
import time

from . import paths

CSV_SEPARATORS = (',', ';')
UPDATE_CHECKS = ('connect', 'daily', 'never')
THEMES = ('system', 'light', 'dark')
DAY = 86400.0

DEFAULTS = {
    'recordings_folder': None,
    'backups_folder': None,
    'csv_separator': ',',
    'switch_to_new_meter': True,
    'update_check': 'connect',
    'theme': 'system',
    'let_2c53t_sleep': False,
    'release_checked_at': None,
    'installed_releases': {},
}


def _folder(v):
    return v is None or (isinstance(v, str) and bool(v.strip()))


def _bool(v):
    return isinstance(v, bool)


def _number(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)


def _installed(v):
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(t, str) and k and t for k, t in v.items())


CHECKS = {
    'recordings_folder': _folder,
    'backups_folder': _folder,
    'csv_separator': lambda v: v in CSV_SEPARATORS,
    'switch_to_new_meter': _bool,
    'update_check': lambda v: v in UPDATE_CHECKS,
    'theme': lambda v: v in THEMES,
    'let_2c53t_sleep': _bool,
    'release_checked_at': _number,
    'installed_releases': _installed,
}


def config_path():
    """The settings file."""
    return os.path.join(paths.config_dir(), 'settings.json')


def event_log_path():
    """The event log, next to the settings file."""
    return os.path.join(os.path.dirname(config_path()), 'events.log')


def _default(key):
    v = DEFAULTS[key]
    return dict(v) if isinstance(v, dict) else v


class Settings:
    def __init__(self, path=None):
        """Read the file at `path` (default config_path()).  A missing file is the defaults, silently."""
        self.path = path or config_path()
        self.problem = None
        self._raw = {}
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError('the top level is not an object')
            self._raw = d
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.problem = 'settings file %s unusable (%s); defaults used' % (self.path, e)
            try:
                os.replace(self.path, self.path + '.bad')
                self.problem += ', the file was kept as settings.json.bad'
            except OSError:
                pass

    def get(self, key):
        """The value of a known key: the stored one when it passes its check, else the default."""
        if key not in DEFAULTS:
            raise KeyError('unknown setting %r' % (key,))
        v = self._raw.get(key, DEFAULTS[key])
        if not CHECKS[key](v):
            return _default(key)
        return dict(v) if isinstance(v, dict) else v      # a copy: a caller's change is stored only through set()

    def as_dict(self):
        """Every known key with its effective value."""
        return {k: self.get(k) for k in DEFAULTS}

    def set(self, key, value):
        """Check, store and save one setting; ValueError when the value fails its check."""
        if key not in DEFAULTS:
            raise ValueError('unknown setting %r' % (key,))
        if isinstance(value, str) and key.endswith('_folder'):
            value = os.path.abspath(os.path.expanduser(value.strip())) if value.strip() else value
        if not CHECKS[key](value):
            raise ValueError('%s: %r is not allowed' % (key, value))
        self._raw[key] = value
        self.save()

    def reset(self):
        """Every known key back to its default (unknown keys stay)."""
        for k in DEFAULTS:
            self._raw.pop(k, None)
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + '.part', 'w', encoding='utf-8') as f:
            json.dump(self._raw, f, indent=1)
        os.replace(self.path + '.part', self.path)

    # ---------------------------------------------------------------- resolved values
    def recordings_folder(self):
        return self.get('recordings_folder') or os.path.join(paths.documents_dir(), 'Recordings')

    def backups_folder(self):
        return self.get('backups_folder') or os.path.join(paths.documents_dir(), 'Backups')

    def csv_format(self):
        """(field separator, decimal mark) for the recordings."""
        sep = self.get('csv_separator')
        return sep, ('.' if sep == ',' else ',')

    def release_check_due(self, on_connect, now=None):
        """Whether a release check is due: 'connect' at each connection, 'daily' when 24 h have passed since the last
        one (any moment, a connection or the daily timer), 'never' not at all."""
        mode = self.get('update_check')
        if mode == 'never':
            return False
        if mode == 'connect':
            return on_connect
        last = self.get('release_checked_at')
        return last is None or (time.time() if now is None else now) - last >= DAY

    def mark_release_checked(self, now=None):
        self.set('release_checked_at', time.time() if now is None else now)

    def installed_release(self, model, serial):
        """The tag the program last installed on this meter, or None."""
        return self.get('installed_releases').get('%s:%s' % (model, serial))

    def record_install(self, model, serial, tag):
        d = self.get('installed_releases')
        d['%s:%s' % (model, serial)] = tag
        self.set('installed_releases', d)
