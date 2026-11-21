"""Checks of the Qt for Python release in requirements.txt that the program depends on.

PySide6_Essentials 6.12.0 on Linux with Python 3.11 takes one reference of True away at every
signal emit, from any thread and with any argument type; after a few hundred emits Python stops
with "bool_dealloc: deallocating True or False" (measured on the CI runner ubuntu-24.04,
2026-11-21: 100 emits, True -100; 6.11.3 and 6.10.3 unchanged; macOS and Windows 6.12.0
unchanged). The link emits a signal for every stream and event frame, so such a release ends the
program within seconds. This test fails for any release with that fault on the platform it runs on.

The emits run in a new Python process: threads left by other tests (links, watchers) change the
counts of True and False while they run, the new process has no other thread. While the Python
thread counts and emits, the main thread waits in a plain lock acquire, which touches neither
(Thread.start and Thread.join return through waits that do). From Python 3.12 on,
True and False are immortal and their count says nothing; the test is skipped.
"""

import os
import subprocess
import sys
import unittest

EMITS = 300

PROBE = r"""
import sys, threading
from PySide6.QtCore import QCoreApplication, QObject, Signal
app = QCoreApplication([])
class E(QObject):
    obj = Signal(object)
    num = Signal(int)
e = E()
def emit():
    for k in range(%d):
        e.obj.emit(("frame", k))
        e.num.emit(k)
def counts():
    return (sys.getrefcount(True), sys.getrefcount(False))
b = counts(); emit(); a = counts()
print("main", b[0], b[1], a[0], a[1])
out = []
go, done = threading.Lock(), threading.Lock()
go.acquire(); done.acquire()
def worker():
    go.acquire()
    b = counts(); emit(); a = counts()
    out.append("thread %%d %%d %%d %%d" %% (b + a))
    done.release()
t = threading.Thread(target=worker); t.start()
go.release(); done.acquire()
t.join()
print(out[0])
""" % EMITS


@unittest.skipIf(sys.version_info >= (3, 12), "True and False are immortal from Python 3.12 on")
class SignalEmitKeepsReferences(unittest.TestCase):
    """The reference counts of True and False are the same before and after EMITS emits, in the
    main thread and in a Python thread."""

    def test_emit_keeps_true_and_false(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        r = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        lines = r.stdout.split("\n")
        for where, line in zip(("main", "thread"), lines[:2]):
            parts = line.split()
            self.assertEqual(parts[0], where, r.stdout)
            t0, f0, t1, f1 = map(int, parts[1:])
            self.assertEqual((t1 - t0, f1 - f0), (0, 0), "%s: %d emits changed True by %+d, "
                             "False by %+d" % (where, 2 * EMITS, t1 - t0, f1 - f0))


if __name__ == "__main__":
    unittest.main()
