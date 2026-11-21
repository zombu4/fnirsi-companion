"""Write THIRD_PARTY_LICENSES.txt: the licence texts of the Python packages the build bundles, of Qt and of Python.

    python -I packaging/third_party.py OUT.txt

The packages are the roots PySide6-Essentials, shiboken6, pyqtgraph, numpy, pyserial and certifi and the packages they
require, found with importlib.metadata in the build environment.  Licence files come from the installed metadata.
Exceptions, kept in packaging/licenses/ because the installed distributions carry no licence file of their own:
  * PySide6-Essentials, shiboken6 and Qt: the wheels declare "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only" and ship no
    licence text.  The program uses them under the LGPL v3: its text (and the GPL v3 text it refers to) is written out
    with the places the Qt and PySide6 sources are published and the notice about replacing the Qt libraries.
  * pyserial: the wheel ships no licence file; the BSD-3-Clause text of the pyserial 3.5 source tree is used.
A package that ships no licence file and has no entry above stops the build with its name, so nothing is shipped
without its notice.
"""
import importlib.metadata as md
import os
import re
import sys

ROOTS = ['PySide6-Essentials', 'shiboken6', 'pyqtgraph', 'numpy', 'pyserial', 'certifi']
HERE = os.path.dirname(os.path.abspath(__file__))
LICENCES = os.path.join(HERE, 'licenses')


def norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirements(name, seen):
    key = norm(name)
    if key in seen:
        return
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return                                  # a dependency for another platform (marker not met)
    seen[key] = dist
    for req in dist.requires or []:
        if 'extra ==' in req:
            continue
        marker_ok = True
        if ';' in req:
            from packaging.markers import Marker  # packaging is a dependency of PyInstaller, present in the build
            marker_ok = Marker(req.split(';', 1)[1]).evaluate()
        if marker_ok:
            requirements(re.split(r'[ <>=!~;\[(]', req.strip(), maxsplit=1)[0], seen)


def read(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read().strip()


def licence_text(dist):
    files = [f for f in (dist.files or []) if re.search(r'(LICEN[CS]E|COPYING|NOTICE)', f.name, re.I)]
    texts = []
    for f in files:
        p = f.locate()
        if os.path.isfile(p):
            texts.append(read(p))
    return '\n\n'.join(texts)


def qt_text():
    """Licence text shared by PySide6-Essentials, shiboken6 and the Qt libraries inside them."""
    import PySide6
    from PySide6.QtCore import qVersion
    return '\n'.join([
        'PySide6 %s, shiboken6 %s and Qt %s are used under the GNU Lesser General Public License version 3 (LGPLv3).' % (
            PySide6.__version__, md.version('shiboken6'), qVersion()),
        '',
        'The program is distributed as a one-folder application. The Qt and PySide6 libraries are separate files inside',
        'the application folder (not linked into the program executable) and can be replaced there with a build of the',
        'same Qt / PySide6 version made from the sources named below.',
        '',
        'Where the sources are published:',
        '  Qt %s:      https://download.qt.io/official_releases/qt/ (and https://code.qt.io/cgit/qt/)' % qVersion(),
        '  PySide6 / shiboken6:  https://code.qt.io/cgit/pyside/pyside-setup.git/ (tag v%s)' % PySide6.__version__,
        'Qt\'s own list of the third-party components inside Qt and their licences:',
        '  https://doc.qt.io/qt-6/licenses-used-in-qt.html',
        '',
        '-' * 78,
        read(os.path.join(LICENCES, 'LGPL-3.0.txt')),
        '',
        '-' * 78,
        'The GNU General Public License version 3, which the LGPLv3 above refers to:',
        '',
        read(os.path.join(LICENCES, 'GPL-3.0.txt'))])


def pyserial_text():
    return read(os.path.join(LICENCES, 'pyserial-LICENSE.txt'))


MIT = '''Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.'''


def declared_mit(dist):
    """Standard MIT text for a package whose metadata declares exactly MIT and ships no licence file, else ''."""
    m = dist.metadata
    lic = (m.get('License-Expression') or m.get('License') or '').strip()
    if lic != 'MIT':
        return ''
    author = m.get('Author') or m.get('Author-email') or m.get('Maintainer') or 'the authors named in the package'
    return ('(The package ships no licence file; its metadata declares the MIT licence. Standard MIT text:)\n\n'
            'Copyright (c) %s\n\n%s' % (author, MIT))


def python_licence():
    for p in (os.path.join(sys.base_prefix, 'LICENSE.txt'), os.path.join(sys.base_prefix, 'LICENSE'),
              os.path.join(sys.base_prefix, 'lib', 'python%d.%d' % sys.version_info[:2], 'LICENSE.txt')):
        if os.path.isfile(p):
            return read(p)
    raise SystemExit('third_party: Python licence file not found under %s' % sys.base_prefix)


def main(argv):
    out = argv[0]
    seen = {}
    for r in ROOTS:
        requirements(r, seen)
    absent = [r for r in ROOTS if norm(r) not in seen]
    if absent:
        raise SystemExit('third_party: not installed: %s' % ', '.join(absent))
    parts = ['Third-party software bundled with the FNIRSI Companion', '',
             '=' * 78, 'Python %s (Python Software Foundation License)' % sys.version.split()[0], '=' * 78, python_licence()]
    missing = []
    for key in sorted(seen):
        dist = seen[key]
        if key == 'pyside6-essentials':
            text = qt_text()
        elif key == 'shiboken6':
            text = 'Same licence terms as PySide6_Essentials: see its entry above (LGPLv3, texts and source locations).'
        elif key == 'pyserial':
            text = pyserial_text()
        else:
            text = licence_text(dist) or declared_mit(dist)
        if not text:
            missing.append(dist.metadata['Name'])
            continue
        lic = dist.metadata.get('License-Expression') or dist.metadata.get('License') or ''
        parts += ['', '=' * 78, '%s %s%s' % (dist.metadata['Name'], dist.version, (' (%s)' % lic.splitlines()[0]) if lic else ''),
                  '=' * 78, text]
    if missing:
        raise SystemExit('third_party: no licence file in the metadata of: %s' % ', '.join(missing))
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts) + '\n')
    print('third_party: %d packages + Python -> %s' % (len(seen), out))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
