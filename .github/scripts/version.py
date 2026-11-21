"""version.py - the version a build gets, as a line VER=... for $GITHUB_ENV.

    python3 -I .github/scripts/version.py REF_TAG >> "$GITHUB_ENV"

REF_TAG is "dev" (no tag) or a tag vA.B.C.  For "dev" the version is __version__ of fnirsi_companion/__init__.py; for a
tag it is A.B.C, and the script fails when that differs from __version__, so a release never carries the wrong number.
"""
import os
import re
import sys


def main(argv):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    with open(os.path.join(root, 'fnirsi_companion', '__init__.py'), encoding='utf-8') as f:
        code = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M).group(1)
    ref = argv[0]
    if ref == 'dev':
        print('VER=%s' % code)
        return 0
    m = re.fullmatch(r'v(\d+\.\d+\.\d+)', ref)
    if not m:
        sys.stderr.write('version: tag "%s" is not of the form vA.B.C\n' % ref)
        return 1
    if m.group(1) != code:
        sys.stderr.write('version: tag %s but fnirsi_companion.__version__ is %s\n' % (ref, code))
        return 1
    print('VER=%s' % m.group(1))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
