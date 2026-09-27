#!/bin/sh
# Install these keyd configs. Run with sudo.
#
# To undo:
#   rm /etc/keyd/g510-de.conf /etc/keyd/thinkpad-gb.conf
#   rm /usr/share/keyd/layouts/de-fixed
#   systemctl restart keyd
#
# The ids in g510-de.conf and thinkpad-gb.conf are this machine's. Regenerate
# them for another with keyd-device-ids.py, or with `keyd.rvaiya monitor`.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> corrected de layout"
# Beside the shipped layouts because keyd only documents resolving includes
# from there. A new file, so it overwrites nothing the keyd package owns.
install -d /usr/share/keyd/layouts
install -m 644 "$HERE/layouts/de-fixed" /usr/share/keyd/layouts/de-fixed

echo "==> configs"
install -d /etc/keyd
install -m 644 "$HERE/g510-de.conf"     /etc/keyd/g510-de.conf
install -m 644 "$HERE/thinkpad-gb.conf" /etc/keyd/thinkpad-gb.conf

echo "==> restarting keyd"
systemctl restart keyd
sleep 2
systemctl is-active keyd

echo
echo "==> which devices keyd took"
journalctl -u keyd --since "30 seconds ago" --no-pager \
    | grep -iE 'g510|gaming keys|AT Translated|ignoring|added' || true
echo
echo "The Gaming Keys node must NOT be listed as taken, or the G-keys stop."
