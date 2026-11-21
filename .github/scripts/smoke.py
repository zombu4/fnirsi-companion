"""smoke.py - the packaged program starts, stays up and ends when asked.

    python3 -I .github/scripts/smoke.py EXECUTABLE [--seconds 8]

Starts EXECUTABLE with --no-autoconnect (the program does not open any serial port) and QT_QPA_PLATFORM=offscreen
(no display needed), waits SECONDS and checks that the process is still running, then ends it and checks that it ended
within 30 s and that its output holds no Python traceback.  Ending: SIGTERM on macOS / Linux (the exit status must be 0,
-15 or 143); on Windows Process.terminate(), the only way to ask a program without a console window (status 0 or 1).
No meter is needed.  Exits with 0 when every check passes.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time

GRACE = 30.0


def main(argv):
    if not argv:
        sys.exit(__doc__)
    exe = argv[0]
    seconds = float(argv[argv.index('--seconds') + 1]) if '--seconds' in argv else 8.0
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    checks = []
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([exe, '--no-autoconnect'], env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        end = time.time() + seconds
        while time.time() < end and proc.poll() is None:
            time.sleep(0.2)
        up = proc.poll() is None
        checks.append(('still running after %g s' % seconds, up))
        if up:
            if sys.platform == 'win32':
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(timeout=GRACE)
                checks.append(('ended within %d s of the request' % GRACE, True))
                checks.append(('exit status %d accepted' % rc, rc in ((0, 1) if sys.platform == 'win32' else (0, -15, 143))))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                checks.append(('ended within %d s of the request' % GRACE, False))
        else:
            checks.append(('exit status of the early end: %d' % proc.returncode, False))
        log.seek(0)
        out = log.read().decode('utf-8', errors='replace')
    checks.append(('no traceback in the output', 'Traceback (most recent call last)' not in out))
    for name, ok in checks:
        print('smoke: %s %s' % ('PASS' if ok else 'FAIL', name))
    if not all(ok for _, ok in checks):
        print('--- program output ---')
        print(out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
