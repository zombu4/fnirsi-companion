# PyInstaller spec of the FNIRSI Companion: one-folder application with everything it needs (Python, PySide6 / Qt,
# pyqtgraph, numpy, pyserial, certifi).  Build on each system:
#   APP_VERSION=1.2.3 APP_ICON=<icon> pyinstaller --noconfirm packaging/companion.spec
# APP_VERSION defaults to __version__ of fnirsi_companion/__init__.py.  APP_ICON: .icns (macOS), .ico (Windows).
# macOS: "FNIRSI Companion.app".  No usage strings: the program uses no Bluetooth, camera or microphone.
import os
import re
import sys

root = os.path.abspath(os.path.join(SPECPATH, '..'))
version = os.environ.get('APP_VERSION')
if not version:
    with open(os.path.join(root, 'fnirsi_companion', '__init__.py'), encoding='utf-8') as f:
        version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M).group(1)
icon = os.environ.get('APP_ICON') or None

# Qt modules the program does not use (PySide6-Essentials ships them all); an excluded module keeps its libraries and
# plug-ins out of the folder.  If a page ever imports one of these, the packaged program fails at start and the CI
# smoke test (.github/scripts/smoke.py) reports it: remove the name here.
unused_qt = ['QtQml', 'QtQuick', 'QtQuickWidgets', 'QtQuickControls2', 'QtQuick3D', 'QtDesigner', 'QtHelp', 'QtTest',
             'QtSql', 'QtWebChannel', 'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebEngineQuick', 'QtMultimedia',
             'QtMultimediaWidgets', 'Qt3DCore', 'Qt3DRender', 'Qt3DInput', 'Qt3DLogic', 'Qt3DAnimation', 'Qt3DExtras',
             'QtCharts', 'QtDataVisualization', 'QtBluetooth', 'QtNfc', 'QtPositioning', 'QtSensors',
             'QtRemoteObjects', 'QtScxml', 'QtStateMachine', 'QtTextToSpeech', 'QtWebSockets', 'QtPdf', 'QtPdfWidgets',
             'QtUiTools', 'QtConcurrent', 'QtXml']

a = Analysis(
    [os.path.join(root, 'packaging', 'entry.py')],
    pathex=[root],
    datas=[(os.path.join(root, 'LICENSE'), '.')],
    hiddenimports=['fnirsi_companion.__main__'],
    excludes=['tkinter', 'matplotlib', 'PIL', 'IPython', 'scipy', 'pytest'] + ['PySide6.' + m for m in unused_qt],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='fnirsi-companion', icon=icon, console=False)
coll = COLLECT(exe, a.binaries, a.datas, name='fnirsi-companion')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='FNIRSI Companion.app', icon=icon, bundle_identifier='io.github.zombu4.fnirsi-companion',
                 info_plist={'CFBundleName': 'FNIRSI Companion', 'CFBundleDisplayName': 'FNIRSI Companion',
                             'CFBundleShortVersionString': version, 'CFBundleVersion': version,
                             'NSHighResolutionCapable': True})
