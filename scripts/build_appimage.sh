#!/usr/bin/env bash
# Kept as the stable entry point; the AppImage is built from the PyInstaller
# Linux binary so it uses the host Tk 8.6 stack and real font scaling.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/scripts/build_appimage_pyinstaller.sh" "$@"
