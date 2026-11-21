"""Draw the program icon: OUTDIR/fnirsi-companion.png (256 x 256), .ico (Windows) and .icns (macOS).

    QT_QPA_PLATFORM=offscreen python packaging/make_icons.py OUTDIR

A dark rounded square with the text "FC", drawn with Qt (PySide6, already a dependency), so no imaging package is needed.
The .ico and .icns files are written by Qt's own image writers; the script stops with the writer's message if one fails.
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QImageWriter, QPainter  # noqa: E402

BG, ACC, INK = QColor(18, 24, 33), QColor(0, 170, 220), QColor(236, 240, 244)


def draw(size):
    im = QImage(size, size, QImage.Format_ARGB32)
    im.fill(Qt.transparent)
    p = QPainter(im)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    m = size * 0.06
    box = QRectF(m, m, size - 2 * m, size - 2 * m)
    p.setBrush(BG)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(box, size * 0.18, size * 0.18)
    w = max(2.0, size / 64)
    p.setBrush(Qt.NoBrush)
    p.setPen(QColor(ACC))
    pen = p.pen()
    pen.setWidthF(w)
    p.setPen(pen)
    inner = box.adjusted(size * 0.04, size * 0.04, -size * 0.04, -size * 0.04)
    p.drawRoundedRect(inner, size * 0.14, size * 0.14)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * 0.42))
    p.setFont(font)
    p.setPen(INK)
    p.drawText(box, Qt.AlignCenter, 'FC')
    p.end()
    return im


def save(im, path):
    w = QImageWriter(path)
    if not w.write(im):
        raise SystemExit('make_icons: cannot write %s: %s' % (path, w.errorString()))


def main(argv):
    out = argv[0]
    os.makedirs(out, exist_ok=True)
    app = QGuiApplication([])  # noqa: F841 - fonts need an application object
    save(draw(256), os.path.join(out, 'fnirsi-companion.png'))
    save(draw(256), os.path.join(out, 'fnirsi-companion.ico'))
    save(draw(256), os.path.join(out, 'fnirsi-companion.icns'))
    print('icons written to %s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
