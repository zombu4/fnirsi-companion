"""Start of the program: python -m fnirsi_companion [--no-autoconnect] [--config-dir DIR].

--no-autoconnect   no meter is opened by itself; the device bar's Connect opens the chosen one
                   (a meter the user opened is still followed across its restarts).
--config-dir DIR   settings.json, events.log and the release cache in DIR instead of the user's
                   folders (core.paths), for tests and portable use.
"""

from __future__ import annotations

import argparse
import os
import sys


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="fnirsi_companion",
                                 description="FNIRSI Companion for the 2C53T and the LC1020E")
    ap.add_argument("--no-autoconnect", action="store_true",
                    help="do not open a meter by itself")
    ap.add_argument("--config-dir", metavar="DIR",
                    help="folder of settings.json, events.log and the release cache")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    from PySide6.QtWidgets import QApplication

    from . import __version__
    from .core import eventlog, settings as settings_mod
    from .link.discover import Watcher
    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("FNIRSI Companion")
    if args.config_dir:
        cfg = os.path.abspath(os.path.expanduser(args.config_dir))
        settings_path = os.path.join(cfg, "settings.json")
        log_path = os.path.join(cfg, "events.log")
        cache = os.path.join(cfg, "cache")
    else:
        settings_path = settings_mod.config_path()
        log_path = settings_mod.event_log_path()
        cache = None
    settings = settings_mod.Settings(settings_path)
    notes = []
    log = eventlog.EventLog(log_path, __version__, on_error=notes.append)
    watcher = Watcher()
    win = MainWindow(settings, watcher=watcher, eventlog=log, autoconnect=not args.no_autoconnect,
                     cache_dir=cache, version=__version__)
    for n in notes:
        win.note(n)
    log.on_error = win.note
    if settings.problem:
        win.note(settings.problem)
    win.resize(1200, 860)
    win.show()
    watcher.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
