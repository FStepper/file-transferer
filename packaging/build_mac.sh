#!/usr/bin/env bash
# Builds dist/<arch>/file-transferer.app (+ ZIP) on a Mac.
#
#   ./packaging/build_mac.sh            # for the native architecture (M2: arm64)
#   ./packaging/build_mac.sh x86_64     # Intel build on an Apple Silicon Mac (via Rosetta)
#
# Requirement: Python >= 3.10 from python.org (includes Tk and is "universal2").
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
    || { echo "Python >= 3.10 required - install it from https://www.python.org/downloads/macos/"; exit 1; }
run "$PY" -c 'import tkinter; tkinter.Tcl()' \
    || { echo "tkinter is missing - use Python from python.org (or: brew install python-tk)."; exit 1; }

echo "==> Build environment ($TARGET_ARCH)"
[[ -d "$VENV" ]] || run "$PY" -m venv "$VENV"
run "$VENV/bin/python" -m pip install -q --upgrade pip
run "$VENV/bin/python" -m pip install -q -e ".[dev]"

echo "==> Tests"
run "$VENV/bin/python" -m pytest -q

echo "==> PyInstaller"
run "$VENV/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "dist/$TARGET_ARCH" --workpath "build/$TARGET_ARCH" packaging/file-transferer.spec

APP="dist/$TARGET_ARCH/file-transferer.app"
echo "==> App self-test"
run "$APP/Contents/MacOS/file-transferer" --version

echo "==> Package (app + install script + instructions)"
PKG="dist/$TARGET_ARCH/file-transferer"
rm -rf "$PKG"
mkdir -p "$PKG"
ditto "$APP" "$PKG/file-transferer.app"
cp "packaging/Install File Transferer.command" "$PKG/"
chmod +x "$PKG/Install File Transferer.command"
cp packaging/README-INSTALL.txt "$PKG/"

ZIP="dist/file-transferer-macOS-$TARGET_ARCH.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$PKG" "$ZIP"
echo
echo "Done:"
echo "  $APP"
echo "  $ZIP  (folder file-transferer with the app, the install script and README-INSTALL.txt)"
echo "Install: unzip, then double-click 'Install File Transferer.command' (see README-INSTALL.txt)."
