#!/bin/sh
# build_appimage.sh - AppImage of the companion from the PyInstaller one-folder output (Linux x86_64).
#
#   sh packaging/linux/build_appimage.sh dist/fnirsi-companion THIRD_PARTY.txt OUT.AppImage ICON.png APPIMAGETOOL
#
# Qt's xcb platform plug-in needs libxcb-cursor.so.0 (package libxcb-cursor0), which many desktops do not have
# installed: it is copied into the program folder, with the libxcb-* libraries it needs that are not already there.  The
# build machine must have libxcb-cursor0 installed (apt install libxcb-cursor0).  The udev rule is placed in the
# documentation folder; the AppImage cannot install it (see README.md).
set -eu
src=${1:?usage: build_appimage.sh DIST_DIR THIRD_PARTY.txt OUT.AppImage ICON.png APPIMAGETOOL}
third=${2:?}
out=${3:?}
icon=${4:?}
tool=${5:?}
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dir="$work/FNIRSI-Companion.AppDir"
app="$dir/usr/lib/fnirsi-companion"
doc="$dir/usr/share/doc/fnirsi-companion"
mkdir -p "$dir/usr/lib" "$doc"
cp -R "$src" "$app"
cp "$root/LICENSE" "$doc/LICENSE"
cp "$third" "$doc/THIRD_PARTY_LICENSES.txt"
cp "$root/packaging/linux/60-fnirsi.rules" "$doc/60-fnirsi.rules"
cp "$icon" "$dir/fnirsi-companion.png"

# libxcb-cursor and its libxcb-* dependencies, next to the program's own libraries (PyInstaller puts that folder on the
# library search path of the program)
cursor=$(ldconfig -p | awk '/libxcb-cursor\.so\.0 .*x86-64/ {print $NF; exit}')
[ -n "$cursor" ] && [ -f "$cursor" ] || { echo "build_appimage: libxcb-cursor.so.0 not installed on this machine" >&2; exit 1; }
libdir="$app"
[ -d "$app/_internal" ] && libdir="$app/_internal"
for lib in "$cursor" $(ldd "$cursor" | awk '/libxcb-[^ ]* => \// {print $3}'); do
    [ -e "$libdir/$(basename "$lib")" ] || cp -L "$lib" "$libdir/"
done
[ -f "$libdir/libxcb-cursor.so.0" ] || { echo "build_appimage: libxcb-cursor.so.0 not in $libdir" >&2; exit 1; }

cat > "$dir/fnirsi-companion.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=FNIRSI Companion
Comment=Live view, recording and firmware updates for the FNIRSI 2C53T and LC1020E
Exec=fnirsi-companion
Icon=fnirsi-companion
Terminal=false
Categories=Utility;Electronics;
DESK
cat > "$dir/AppRun" <<'RUN'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/lib/fnirsi-companion/fnirsi-companion" "$@"
RUN
chmod +x "$dir/AppRun"
ARCH=x86_64 "$tool" --appimage-extract-and-run "$dir" "$out"
chmod +x "$out"
echo "build_appimage: $out"
