#!/usr/bin/env bash
# Build a self-contained Linux AppImage from the PyInstaller binary.
# This uses the host Tk 8.6 stack, which supports real font scaling.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/appimage-pyinstaller"
PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1; then
  echo "PyInstaller is required. Install with: $PYTHON -m pip install pyinstaller" >&2
  exit 1
fi

rm -rf "$BUILD"
mkdir -p "$BUILD"

PYTHONPATH="$ROOT/src" "$PYTHON" -m PyInstaller \
  --noconfirm --clean --onefile --windowed --name SAK-Modern \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  --paths "$ROOT/src" \
  --add-data "$ROOT/src/sak_modern/resources:sak_modern/resources" \
  --collect-all cryptography \
  "$ROOT/windows_launcher.py" >/dev/null

APPDIR="$BUILD/AppDir"
mkdir -p "$APPDIR/usr/bin"
cp "$BUILD/dist/SAK-Modern" "$APPDIR/usr/bin/sak-modern"
cp "$ROOT/resources/sak-modern.png" "$APPDIR/sak-modern.png"
cp "$ROOT/resources/sak-modern.png" "$APPDIR/.DirIcon"
cp "$ROOT/resources/sak-modern.desktop" "$APPDIR/sak-modern.desktop"
cp "$ROOT/README.md" "$APPDIR/README.md"
cp "$ROOT/LICENSE" "$APPDIR/LICENSE" 2>/dev/null || true
cp "$ROOT/THIRD_PARTY_NOTICES.md" "$APPDIR/THIRD_PARTY_NOTICES.md" 2>/dev/null || true

cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/sak-modern" "$@"
RUN
chmod +x "$APPDIR/AppRun"

TOOL="$ROOT/build/appimage/appimagetool-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
  mkdir -p "$(dirname "$TOOL")"
  curl -fsSL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/SAK-Modern-x86_64.AppImage"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo "Built $OUT"
