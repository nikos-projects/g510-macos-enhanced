#!/bin/sh
# Build one self-contained executable: dist/g510
#
#   ./make-exec.sh        -> dist/g510
#
# The same bargain as make-dmg.sh, without the bundle: no Homebrew, no
# virtualenv, no .app - just a file. No arguments runs the menu bar app,
# `--cli ...` is the command line, `--daemon` is the agent:
#
#   ./dist/g510 --cli --help
#   ./dist/g510 --daemon
#
# It wants to be universal - one file for Intel and Apple Silicon - and
# that is only possible while every piece that goes inside it is itself
# universal2 (the interpreter, hidapi, pyobjc) and this Mac can run both
# slices. It asks for it, then looks at what it got and says which it is.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD_VENV="$HERE/build-venv"
OUT="$HERE/dist/g510"

echo "==> build environment"
if [ ! -x "$BUILD_VENV/bin/python" ]; then
    python3 -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/pip" -q install -r "$HERE/requirements.txt" pyinstaller

echo "==> executable"
rm -rf "$HERE/build/pyi" "$OUT"
cd "$HERE"
"$BUILD_VENV/bin/pyinstaller" --noconfirm --clean --onefile \
    --name g510 \
    --paths "$HERE" \
    --target-arch universal2 \
    --hidden-import hid --hidden-import websocket \
    --hidden-import objc --hidden-import Foundation --hidden-import AppKit \
    --hidden-import Quartz --hidden-import CoreFoundation \
    --hidden-import CoreAudio --hidden-import CoreText \
    main.py

# Ad-hoc signature, the same as the app gets. Enough for a Mac the user
# controls; distributing this would want a Developer ID.
codesign --force -s - "$OUT"

echo
if file "$OUT" | grep -q "universal binary"; then
    echo "Built $OUT - universal (Intel and Apple Silicon)"
else
    echo "Built $OUT - $(uname -m) only"
    echo
    echo "Every dependency here is universal2, so a universal file is one"
    echo "slice away: run this script on an Apple Silicon Mac too, then"
    echo
    echo "  lipo -create dist/g510 dist/g510-arm64 -output dist/g510-universal"
    echo
    echo "or push - .github/workflows/build.yml does both and joins them."
fi
echo "  $(du -h "$OUT" | cut -f1)   ./dist/g510 --cli --help"
