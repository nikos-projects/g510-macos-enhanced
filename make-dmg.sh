#!/bin/sh
# Build G510.app and wrap it in a disk image.
#
#   ./make-dmg.sh        -> dist/G510.dmg
#
# The app is self-contained: the interpreter, the Python modules and the
# compiled extensions all live inside the bundle, so it runs on a Mac with
# neither Homebrew nor a virtualenv.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD_VENV="$HERE/build-venv"
STAGE="$HERE/dist/dmg"
DMG="$HERE/dist/G510.dmg"

echo "==> build environment"
if [ ! -x "$BUILD_VENV/bin/python" ]; then
    python3 -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/pip" -q install -r "$HERE/requirements.txt" py2app

echo "==> app bundle"
rm -rf "$HERE/build" "$HERE/dist/G510.app" "$STAGE" "$DMG"
(cd "$HERE" && "$BUILD_VENV/bin/python" setup.py py2app >/dev/null)

# The media screen shells out to this. Bundling it means the app does not need
# Homebrew on the machine it lands on; without it that screen falls back to a
# browser window title.
if command -v nowplaying-cli >/dev/null 2>&1; then
    cp "$(command -v nowplaying-cli)" "$HERE/dist/G510.app/Contents/MacOS/"
    echo "    included nowplaying-cli"
fi

# Ad-hoc signature. Good enough for a Mac the user controls; a Developer ID
# would be needed to distribute this without Gatekeeper complaining.
codesign --force --deep -s - "$HERE/dist/G510.app"

echo "==> disk image"
mkdir -p "$STAGE"
cp -R "$HERE/dist/G510.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/Read me.txt" <<'NOTE'
G510 — Logitech G510 support for macOS

Drag G510.app to Applications, then open it. It lives in the menu bar; there
is no Dock icon. Choose Configuration from its menu to set things up.

The keyboard needs two macOS permissions, which the app will ask for:

  Input Monitoring  to read the G-keys
  Accessibility     only for bindings that type keystrokes

To start the background agent (which drives the G-keys and the display), open
Configuration, go to Agent, and press Start. It will then run at login.

There is also a command line tool. Install it from Configuration > Agent, or
run the app with --cli, e.g.

  /Applications/G510.app/Contents/MacOS/G510 --cli --help
NOTE

hdiutil create -volname "G510" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "Built $DMG ($(du -h "$DMG" | cut -f1))"
