#!/usr/bin/env bash
# Baut dist/VidTransfer.app (+ ZIP) auf einem Mac.
#
#   ./packaging/build_mac.sh            # für die eigene Architektur (M2: arm64)
#   ./packaging/build_mac.sh x86_64     # Intel-Version auf einem Apple-Silicon-Mac (über Rosetta)
#
# Voraussetzung: Python ≥ 3.10 von python.org (enthält Tk, ist "universal2").
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET_ARCH="${1:-$(uname -m)}"
PY="${PYTHON:-python3}"
VENV=".venv-build-$TARGET_ARCH"

if [[ "$TARGET_ARCH" != "$(uname -m)" ]]; then
    RUN=(arch "-$TARGET_ARCH")
else
    RUN=()
fi
run() { if ((${#RUN[@]})); then "${RUN[@]}" "$@"; else "$@"; fi; }

run "$PY" -c 'import sys; assert sys.version_info >= (3, 10)' \
    || { echo "Python >= 3.10 nötig – bitte von https://www.python.org/downloads/macos/ installieren."; exit 1; }
run "$PY" -c 'import tkinter; tkinter.Tcl()' \
    || { echo "tkinter fehlt – Python von python.org verwenden (oder: brew install python-tk)."; exit 1; }

echo "==> Build-Umgebung ($TARGET_ARCH)"
[[ -d "$VENV" ]] || run "$PY" -m venv "$VENV"
run "$VENV/bin/python" -m pip install -q --upgrade pip
run "$VENV/bin/python" -m pip install -q -e ".[dev]"

echo "==> Tests"
run "$VENV/bin/python" -m pytest -q

echo "==> PyInstaller"
run "$VENV/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "dist/$TARGET_ARCH" --workpath "build/$TARGET_ARCH" packaging/VidTransfer.spec

APP="dist/$TARGET_ARCH/VidTransfer.app"
echo "==> Selbsttest der App"
run "$APP/Contents/MacOS/VidTransfer" --version

ZIP="dist/VidTransfer-macOS-$TARGET_ARCH.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
echo
echo "Fertig:"
echo "  $APP"
echo "  $ZIP"
echo "Installieren: VidTransfer.app nach /Programme ziehen und einmal per Rechtsklick → Öffnen starten."
