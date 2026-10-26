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
# The version comes from the changelog, so there is one place to bump it.
# Parsed by hand in the fallback, because dpkg-parsechangelog is part of the
# toolchain we may not have.
version="$(sed -n '1s/.*(\(.*\)).*/\1/p' "$HERE/debian/changelog")"

if [ -n "$missing" ]; then
    echo "    missing:$missing"
    echo "    falling back to dpkg-deb, which needs none of them"
    echo
    echo "==> staging"
    # Everything debhelper would have done, done plainly. The package is a
    # directory tree plus a control file; there is nothing to compile, so the
    # only thing lost by not using debhelper is the generated ${python3:Depends}
    # substitution, and the dependencies are spelled out below instead.
    stage="$(mktemp -d)"
    trap 'rm -rf "$stage"' EXIT
    # mktemp -d gives 0700, which would become the mode of the package root.
    chmod 755 "$stage"
    share="$stage/usr/share/g510-linux"

    install -d "$share" "$share/linux" "$stage/usr/bin" \
              "$stage/lib/udev/rules.d" "$stage/usr/lib/systemd/user" \
              "$stage/usr/share/doc/g510-linux" "$stage/DEBIAN"

    for module in cli.py config.py control.py daemon.py ipc.py printer.py \
                  refresh.py; do
        install -m 644 "$TOP/$module" "$share"
    done
    # Top-level *.py only, so linux/tests and __pycache__ stay out.
    install -m 644 "$TOP"/linux/*.py "$share/linux"
    install -m 755 "$HERE/g510" "$stage/usr/bin/g510"
    install -m 644 "$HERE/udev/99-g510.rules" "$stage/lib/udev/rules.d/"
    install -m 644 "$HERE/systemd/g510.service" "$stage/usr/lib/systemd/user/"
    install -d "$stage/usr/share/applications"
    install -m 644 "$HERE/g510.desktop" "$stage/usr/share/applications/"
    install -m 644 "$TOP/linux/README.md" "$stage/usr/share/doc/g510-linux/"
    install -m 644 "$HERE/debian/copyright" "$stage/usr/share/doc/g510-linux/"
    # Debian policy wants the changelog installed and compressed.
    gzip -9nc "$HERE/debian/changelog" \
        > "$stage/usr/share/doc/g510-linux/changelog.Debian.gz"
    chmod 644 "$stage/usr/share/doc/g510-linux/changelog.Debian.gz"

    for script in postinst prerm postrm; do
        install -m 755 "$HERE/debian/$script" "$stage/DEBIAN/$script"
    done

    # debian/control's Depends carries ${misc:Depends} and ${python3:Depends},
    # which only debhelper knows how to expand, so the fields are written out
    # here instead of transformed. The Description is long and is the one part
    # worth keeping in a single place, so that is copied across verbatim.
    {
        echo "Package: g510-linux"
        echo "Version: $version"
        echo "Section: utils"
        echo "Priority: optional"
        echo "Architecture: all"
        echo "Maintainer: Paul <paulloth2208@gmail.com>"
        echo "Depends: python3, python3-evdev, python3-pil"
        echo "Recommends: playerctl, wtype, python3-pyside6.qtwidgets, python3-pyside6.qtsvg"
        sed -n '/^Description:/,$p' "$HERE/debian/control"
    } > "$stage/DEBIAN/control"

    (cd "$stage" && find usr lib -type f -exec md5sum {} + > DEBIAN/md5sums)

    echo "==> dpkg-deb"
    deb="$TOP/g510-linux_${version}_all.deb"
    dpkg-deb --build --root-owner-group "$stage" "$deb" >/dev/null
    echo
    echo "Built $deb ($(du -h "$deb" | cut -f1))"
    echo "  sudo apt install $deb"
    echo "  (apt, not dpkg -i, so the dependencies come with it)"
    exit 0
fi
echo "    ok"

echo "==> dpkg-buildpackage"
(cd "$HERE" && dpkg-buildpackage -us -uc -b)

deb="$TOP/g510-linux_${version}_all.deb"

echo
if [ -f "$deb" ]; then
    echo "Built $deb ($(du -h "$deb" | cut -f1))"
else
    echo "Built, but not where this expected. Look in $TOP for *.deb."
fi
echo "  sudo apt install $deb"
echo "  (apt, not dpkg -i, so the dependencies come with it)"
