#!/bin/sh
# Set up a source checkout for development.
#
#   ./install.sh     virtualenv + the `g510` command on your PATH
#   ./make-dmg.sh    the installable app, as a disk image
#
# This deliberately does NOT build an app bundle. make-dmg.sh does that, via
# py2app, and having two things generate bundles is how they drift apart - the
# hand-written launcher this used to produce re-introduced a bug that left the
# menu bar item invisible.
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> hidapi"
if ! brew list hidapi >/dev/null 2>&1; then
    brew install hidapi
fi

echo "==> nowplaying-cli (optional, for the Now Playing screen)"
if ! command -v nowplaying-cli >/dev/null 2>&1; then
    brew install nowplaying-cli || echo "    skipped; that screen will fall back"
fi

echo "==> virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" -q install -r "$APP_DIR/requirements.txt"

echo "==> command line"
"$APP_DIR/venv/bin/python" "$APP_DIR/cli.py" install

echo
echo "Ready."
echo "  g510 --help                          what it can do"
echo "  g510 start                           run the agent, and at login"
echo "  venv/bin/python gui.py               the menu bar app and window"
echo "  ./make-dmg.sh                        build the installable app"
echo
echo "Grant Input Monitoring (to read G-keys) and, for keys/text bindings,"
echo "Accessibility. Check with: g510 permissions"
