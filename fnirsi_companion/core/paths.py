"""Folders of the user running the program: home, settings, cache, documents.

On macOS and Linux the home folder comes from the account database (pwd) of the user the program runs as, not from the
HOME variable, which a launcher can pass on from another user (for example sudo -u without -H); on Windows from the
user-profile variables Windows sets for every program.  The folder name in each place is APP_NAME.
"""
import os
import sys

APP_NAME = 'FNIRSI Companion'


def home():
    """The home folder of the user the program runs as."""
    if sys.platform != 'win32':
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    return os.environ.get('USERPROFILE') or os.path.expanduser('~')


def config_dir():
    """Where settings.json and events.log live."""
    if sys.platform == 'darwin':
        return os.path.join(home(), 'Library', 'Application Support', APP_NAME)
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('APPDATA') or os.path.join(home(), 'AppData', 'Roaming'), APP_NAME)
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(home(), '.config'), APP_NAME)


def cache_dir():
    """Where downloaded release files are kept."""
    if sys.platform == 'darwin':
        return os.path.join(home(), 'Library', 'Caches', APP_NAME)
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('LOCALAPPDATA') or os.path.join(home(), 'AppData', 'Local'), APP_NAME)
    return os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(home(), '.cache'), APP_NAME)


def documents_dir():
    """The program's folder for what the user keeps (recordings, backups): <Documents>/APP_NAME, or <home>/APP_NAME
    when the user has no Documents folder."""
    docs = os.path.join(home(), 'Documents')
    return os.path.join(docs if os.path.isdir(docs) else home(), APP_NAME)
