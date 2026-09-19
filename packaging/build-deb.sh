#!/bin/sh
# Build the .deb.
#
#   ./build-deb.sh      -> ../g510-linux_0.1.0_all.deb
#
# The debian/ directory lives here rather than at the top of the checkout, so
# this is the source package root and dpkg-buildpackage writes its output one
# level up, into the checkout itself.
#
# Binary only (-b) and unsigned (-us -uc): this is a package for the machine
# in front of you, not something going to an archive.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TOP="$(dirname "$HERE")"

echo "==> build dependencies"
missing=""
for command in dpkg-buildpackage dh; do
    if ! command -v "$command" >/dev/null 2>&1; then
        missing="$missing $command"
    fi
done
# dh-python has no command of its own to look for; the sequence file it drops
# in is what `dh --with python3` actually needs.
if [ ! -e /usr/share/perl5/Debian/Debhelper/Sequence/python3.pm ]; then
    missing="$missing dh-python"
fi
if [ -n "$missing" ]; then
    echo "    missing:$missing"
    echo "    install them with:"
    echo "      sudo apt install build-essential debhelper dh-python python3-all"
    exit 1
fi
echo "    ok"

echo "==> dpkg-buildpackage"
(cd "$HERE" && dpkg-buildpackage -us -uc -b)

# The version comes from the changelog rather than being repeated here, so
# there is one place to bump it.
version="$(cd "$HERE" && dpkg-parsechangelog -S Version)"
deb="$TOP/g510-linux_${version}_all.deb"

echo
if [ -f "$deb" ]; then
    echo "Built $deb ($(du -h "$deb" | cut -f1))"
else
    echo "Built, but not where this expected. Look in $TOP for *.deb."
fi
echo "  sudo apt install $deb"
echo "  (apt, not dpkg -i, so the dependencies come with it)"
