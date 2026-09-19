#!/bin/sh
# Set up a source checkout for development, on Linux.
#
#   linux/install.sh          the udev rule, the user unit, and the deps
#   packaging/build-deb.sh    the installable .deb
#
# This is the counterpart of the macOS install.sh at the root of the repo. It
# does not build anything and does not create a virtualenv: the two Python
# libraries this needs (evdev and Pillow) are packaged by Debian, and a
# virtualenv on top of them would only add a second copy to keep current.
#
# Everything here is safe to run twice. It compares before it writes, so a
# second run that changes nothing asks for no password.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TOP="$(dirname "$HERE")"

RULE_SRC="$TOP/packaging/udev/99-g510.rules"
RULE_DST="/etc/udev/rules.d/99-g510.rules"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/g510.service"
ENTRY="$TOP/packaging/g510"

# ---------------------------------------------------------------- packages
#
# python3-evdev reads the G-keys and synthesises key presses; python3-pil
# draws the LCD. playerctl feeds the now-playing screen over MPRIS and wtype
# types text on Wayland - both optional, and the features that use them are
# skipped when they are missing.
echo "==> packages"
PACKAGES=""
for pair in python3-evdev:evdev python3-pil:PIL; do
    package="${pair%%:*}"
    module="${pair##*:}"
    if ! python3 -c "import $module" >/dev/null 2>&1; then
        PACKAGES="$PACKAGES $package"
    fi
done
for command in playerctl wtype; do
    if ! command -v "$command" >/dev/null 2>&1; then
        PACKAGES="$PACKAGES $command"
    fi
done

if [ -n "$PACKAGES" ]; then
    echo "    these are missing:$PACKAGES"
    echo
    echo "      sudo apt install$PACKAGES"
    echo
    # Asked rather than run: installing packages is the kind of thing that
    # should not happen because someone ran a setup script for something else.
    printf "    run that now? [y/N] "
    read -r answer
    case "$answer" in
    y|Y|yes|YES)
        sudo apt install $PACKAGES
        ;;
    *)
        echo "    skipped; run it yourself before starting the agent"
        ;;
    esac
else
    echo "    all present"
fi

# ------------------------------------------------------------------- udev
#
# /etc/udev/rules.d here, not /lib: this is a checkout being installed by
# hand, and /etc is where a local rule belongs. The .deb ships the same file
# under /lib instead, where dpkg can own it.
echo "==> udev rule"
if [ -f "$RULE_DST" ] && cmp -s "$RULE_SRC" "$RULE_DST"; then
    echo "    already current: $RULE_DST"
else
    sudo install -m 644 "$RULE_SRC" "$RULE_DST"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=leds
    echo "    installed $RULE_DST"
fi

if ! id -nG | tr ' ' '\n' | grep -qx plugdev; then
    echo "    note: $USER is not in the plugdev group. The display and the"
    echo "    backlight work without it through logind, but bindings that type"
    echo "    keys or text need /dev/uinput:"
    echo "      sudo usermod -aG plugdev $USER      (then log out and back in)"
fi

# ---------------------------------------------------------------- the unit
#
# Written into the user's own unit directory and pointed at this checkout, so
# the running agent is the code being edited. The .deb installs a unit under
# /usr/lib/systemd/user pointing at /usr/bin/g510 instead; a user unit
# shadows the system one, which is the right way round for development.
echo "==> systemd user unit"
chmod +x "$ENTRY"
mkdir -p "$UNIT_DIR"
TMP_UNIT="$(mktemp)"
sed "s|^ExecStart=.*|ExecStart=$ENTRY run|" \
    "$TOP/packaging/systemd/g510.service" > "$TMP_UNIT"
if [ -f "$UNIT_DST" ] && cmp -s "$TMP_UNIT" "$UNIT_DST"; then
    echo "    already current: $UNIT_DST"
    rm -f "$TMP_UNIT"
else
    install -m 644 "$TMP_UNIT" "$UNIT_DST"
    rm -f "$TMP_UNIT"
    systemctl --user daemon-reload
    echo "    installed $UNIT_DST -> $ENTRY"
fi

echo
echo "Ready."
echo "  $ENTRY --help                what it can do"
echo "  systemctl --user enable --now g510.service   run the agent, and at login"
echo "  journalctl --user -u g510 -f                 watch it"
echo "  packaging/build-deb.sh                       build the installable .deb"
echo
echo "Put the entry point on your PATH if you want it there:"
echo "  ln -sf $ENTRY ~/.local/bin/g510"
echo
echo "Per-application profiles are not available on Linux - Wayland gives an"
echo "unprivileged client no way to ask what window has focus. See linux/README.md."
