"""The shared config module, with defaults that make sense on Linux.

Everything here comes from the root config.py; see _shared.py for why it is
amended rather than forked. Only the starter values differ - the file format,
the migrations and the colour helpers are the same code on both platforms.
"""
import _shared

_root = _shared.load("config")
_shared.reexport(_root, globals())

# Amended in place, because the root module's own load()/ensure_exists() read
# DEFAULTS out of its globals. Rebinding our copy would change nothing.

# Safari and Terminal do not exist here, and a cmd-chord is a super-chord.
# Shift+Print is Spectacle's region grab on a stock Plasma. Firefox, Konsole
# and Dolphin are what is actually installed on this box.
#
# G4 was "super" until a test caught it: a chord needs a key to go with its
# modifiers, so a bare Super cannot be expressed as a `keys` binding at all
# and would have raised the first time the key was pressed.
DEFAULTS["bindings"] = {
    "G1": {"type": "app", "name": "firefox"},
    "G2": {"type": "app", "name": "konsole"},
    "G3": {"type": "keys", "keys": "shift+print"},
    "G4": {"type": "app", "name": "dolphin"},
}

# The app screen and the per-app colours it feeds are macOS-only: Wayland
# gives an unprivileged client no way to read the focused window, so
# screens.frontmost_app() is a permanent None here. Leaving "app" in the
# cycle would just park a dead screen in the rotation.
DEFAULTS["lcd"]["cycle"] = ["status", "clock", "claude", "media"]

# Kept so a config copied from a Mac still loads, but nothing reads them.
DEFAULTS["app_colors"] = {}
DEFAULTS["app_profiles"] = {}

# hid-lg-g15 gives the kernel a proper keyboard with a real layout, so the
# ANSI/ISO guess macOS has to make does not arise. control.iso_swap says so.
DEFAULTS["iso_keyboard"] = False

# The lock LEDs belong to the kernel and the desktop here, so device.LockLeds
# is a no-op and these indicators have nothing to drive. Left in the schema so
# the setting survives a round trip, but they stay off.
DEFAULTS["indicators"] = {"numlock": "off", "scrolllock": "off"}

# The kernel driver does not surface the game-mode switch, so nothing can
# fire it. An empty mapping keeps cli.py's validation happy.
DEFAULTS["game_switch"] = {}

_shared.adopt(_root, globals())
