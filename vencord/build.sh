#!/bin/sh -e
#
# Build the G510 plugin into a Vencord checkout, so Vencord (Vesktop) can
# load it.
#
# The plugin's source lives here, in this tree - this script is what puts
# it where Vencord's build can see it (`src/userplugins`), builds, and
# then points Vesktop at the result by writing `vencordDir` into its
# state file. Vesktop has to be restarted afterwards.
#
#   VENCORD=~/src/Vencord ./vencord/build.sh      default ~/Documents/Vencord
#
# Needs node and pnpm (Vencord's package.json names the pnpm it wants, so
# corepack picks it up). Nothing here touches the g510 config file.

VENCORD="${VENCORD:-$HOME/Documents/Vencord}"
STATE="$HOME/Library/Application Support/Vesktop/state.json"

if [ ! -d "$VENCORD/src" ]; then
    echo "no Vencord checkout at $VENCORD" >&2
    echo "  git clone https://github.com/Vendicated/Vencord $VENCORD" >&2
    exit 1
fi

# Vencord names the pnpm it wants in package.json, so corepack can fetch
# the right one - which is also how it is run when pnpm itself is not on
# PATH yet.
if command -v pnpm >/dev/null 2>&1; then
    PNPM="pnpm"
elif command -v corepack >/dev/null 2>&1; then
    PNPM="corepack pnpm"
else
    echo "no pnpm, and no corepack to run it with - install node first" >&2
    exit 1
fi

echo "building from $(dirname "$0")/g510 into $VENCORD"
rm -rf "$VENCORD/src/userplugins/g510"
mkdir -p "$VENCORD/src/userplugins"
cp -R "$(dirname "$0")/g510" "$VENCORD/src/userplugins/"

cd "$VENCORD"
if [ ! -d node_modules ]; then
    $PNPM install
fi
$PNPM build
for file in vencordDesktopMain.js vencordDesktopRenderer.js \
            vencordDesktopPreload.js; do
    if [ ! -f "dist/$file" ]; then
        echo "build did not produce dist/$file" >&2
        exit 1
    fi
done

# Vesktop reads this from its own state file (State store, not the
# settings the plugin list lives in), and only at startup. It also writes
# that file from memory when it quits, so doing this while it runs would
# be thrown away the moment it closes - hence the check.
if pgrep -x vesktop >/dev/null 2>&1; then
    echo "Vesktop is running: it would overwrite this on the way out." >&2
    echo "Quit it, run this script again, then start it." >&2
    exit 1
fi

python3 - "$STATE" "$VENCORD/dist" <<'PY'
import json, os, sys
path, dist = sys.argv[1], sys.argv[2]
state = {}
if os.path.exists(path):
    with open(path) as handle:
        state = json.load(handle)
state["vencordDir"] = dist
with open(path, "w") as handle:
    json.dump(state, handle, indent=4)
print(f"vencordDir -> {dist}")
PY

echo
echo "now restart Vesktop. Check it worked with:"
echo "  g510 discord channels"
