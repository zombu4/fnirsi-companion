#!/bin/sh
# build_deb.sh - Debian / Ubuntu package of the companion from the PyInstaller one-folder output (amd64).
#
#   sh packaging/linux/build_deb.sh dist/fnirsi-companion TAG THIRD_PARTY.txt OUT.deb ICON.png
#
# TAG is the release tag vA.B.C (any other text gives the version 0.0.0~TEXT).  Everything the program needs is inside
# the package except libxcb-cursor0 (Qt's xcb platform plug-in needs it), which the package depends on, and the C library.
# The package installs the udev rule 60-fnirsi.rules and reloads the rules.  Installed by a double click (the software
# centre) or with "sudo apt install ./OUT.deb"; after the first install, plug the meter in again.
set -eu
src=${1:?usage: build_deb.sh DIST_DIR TAG THIRD_PARTY.txt OUT.deb ICON.png}
tag=${2:?}
third=${3:?}
out=${4:?}
icon=${5:?}
root=$(cd "$(dirname "$0")/../.." && pwd)
case "$tag" in
  v[0-9]*) version=${tag#v} ;;
  *) version="0.0.0~$(printf '%s' "$tag" | tr -c 'A-Za-z0-9.+~\n' '.')" ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pkg="$work/pkg"
mkdir -p "$pkg/DEBIAN" "$pkg/opt" "$pkg/usr/bin" "$pkg/usr/share/applications" "$pkg/usr/lib/udev/rules.d" \
         "$pkg/usr/share/icons/hicolor/256x256/apps" "$pkg/usr/share/doc/fnirsi-companion"
cp -R "$src" "$pkg/opt/fnirsi-companion"
ln -s /opt/fnirsi-companion/fnirsi-companion "$pkg/usr/bin/fnirsi-companion"
cp "$icon" "$pkg/usr/share/icons/hicolor/256x256/apps/fnirsi-companion.png"
cp "$root/LICENSE" "$pkg/usr/share/doc/fnirsi-companion/copyright"
cp "$third" "$pkg/usr/share/doc/fnirsi-companion/THIRD_PARTY_LICENSES.txt"
cp "$root/packaging/linux/60-fnirsi.rules" "$pkg/usr/lib/udev/rules.d/60-fnirsi.rules"
cat > "$pkg/usr/share/applications/fnirsi-companion.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=FNIRSI Companion
Comment=Live view, recording and firmware updates for the FNIRSI 2C53T and LC1020E
Exec=/opt/fnirsi-companion/fnirsi-companion
Icon=fnirsi-companion
Terminal=false
Categories=Utility;Electronics;
DESK
cat > "$pkg/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=tty --action=add || true
fi
exit 0
POST
cat > "$pkg/DEBIAN/postrm" <<'POST'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
fi
exit 0
POST
chmod 755 "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/postrm"
size=$(du -sk "$pkg" | cut -f1)
cat > "$pkg/DEBIAN/control" <<CTRL
Package: fnirsi-companion
Version: $version
Architecture: amd64
Maintainer: zombu4 <https://github.com/zombu4/fnirsi-companion>
Installed-Size: $size
Depends: libc6 (>= 2.35), libxcb-cursor0
Section: electronics
Priority: optional
Homepage: https://github.com/zombu4/fnirsi-companion
Description: Companion program for the FNIRSI 2C53T and LC1020E
 Finds the meter on USB and shows its pages: live readings, recording to CSV,
 oscilloscope traces, meter information and firmware updates. Installs the
 udev rule that lets the logged-in user open the meter's serial port.
CTRL
dpkg-deb --root-owner-group --build "$pkg" "$out"
echo "build_deb: $out ($version)"
