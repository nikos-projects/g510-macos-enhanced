"""The screens, with the numbers they read taken from this platform.

See _shared.py for why this amends rather than forks. The fork this replaces
had drifted badly, and every part of the drift was invisible until something
called a function it did not have:

  * Six screens the daemon switches to by name were missing - `timers`, the
    timer alert, a `message`, the Discord view, the `menu` and a pinned
    `note`. `render()` falls back to `screen_status` for an unknown name, so
    opening the menu drew the status screen and looked like the panel had
    frozen rather than like a screen was missing.
  * `autoscreen` calls `screens.now_playing(wait=True)`; the fork's copy had
    dropped the parameter, so the media-driven screen change raised TypeError
    on every pass.
  * tests/test_screens.py runs against this module and expects `_reconcile`,
    `_media_anchor` and `_parse_position`, none of which the fork had.

The drawing itself never needed forking: a screen takes (canvas, state) and
draws from config and from state the daemon hands it. Only where the *numbers*
come from differs, and that is six small functions plus the media source.

The media source is the one amendment root's own functions have to see.
`now_playing` lives in the root module and looks `_media_source` up in the
root module's globals, and `_media_source` begins with an underscore, which
`adopt` deliberately skips - so it is assigned onto the root module directly,
below.
"""
import glob
import os
import shutil
import subprocess
import time

import _shared
import refresh

_root = _shared.load("screens")
_shared.reexport(_root, globals())

# -- where the numbers come from -------------------------------------------


def load_fraction():
    """1-minute load average as a fraction of available cores.

    os.getloadavg() would do the same thing, but it raises where /proc is not
    mounted - a container, a chroot - and this runs in a loop that treats an
    exception as the keyboard having gone away. Reading the file costs nothing
    and says so plainly instead.
    """
    try:
        with open("/proc/loadavg") as handle:
            one_minute = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return min(1.0, one_minute / _CPU_COUNT)


def memory_fraction():
    """Share of physical memory in in-use state, refreshed at most a second.

    MemAvailable is the kernel's own estimate of what a new process could get
    without swapping, so it already discounts reclaimable cache - far closer
    to what a person means by "used" than MemTotal minus MemFree.
    """
    now = time.time()
    if now - _mem_cache["when"] < 1.0:
        return _mem_cache["value"]
    fields = {}
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                fields[name] = float(rest.split()[0])
    except (OSError, ValueError, IndexError):
        fields = {}
    total = fields.get("MemTotal", 0.0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0.0))
    value = max(0.0, (total - available) / total) if total else 0.0
    _mem_cache.update(when=now, value=value)
    return value


def battery():
    """(percent, charging) or None on a desktop, refreshed every 30s.

    sysfs rather than pmset, which does not exist here. "Not charging" reads
    as plugged in: it means mains power holding below a charge limit, and only
    "Discharging" is actually running off cells.
    """
    now = time.time()
    if now - _batt_cache["when"] < 30.0:
        return _batt_cache["value"]
    value = None
    for path in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        percent = _read_sysfs(os.path.join(path, "capacity"))
        if not percent.isdigit():
            continue
        status = _read_sysfs(os.path.join(path, "status"))
        value = (int(percent), status != "Discharging")
        break
    _batt_cache.update(when=now, value=value)
    return value


def _read_sysfs(path):
    """One short sysfs attribute, or an empty string if it is not there."""
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def frontmost_app():
    """Always None on Linux: nothing tells us what has focus.

    Wayland deliberately gives an unprivileged client no way to read the
    focused window - there is no NSWorkspace equivalent, and no portal for it
    either. The AppKit import in the root copy simply fails here and returns
    None, so overriding it is about the reason being written down where the
    next reader will look, not about changing the answer.
    """
    return None


def screen_app(canvas, _state):
    """Says why the screen is empty rather than drawing a bare "-".

    frontmost_app() can never answer on Wayland, and the root version draws
    whatever it got or a dash, which reads as a bug in the agent rather than
    as a limit of the platform.
    """
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    canvas.text("no app profiles", 2, 18, 11)
    canvas.text("Wayland hides focus", 2, 31, 10)


# -- Claude Code's own paths ------------------------------------------------

# CLAUDE_CONFIG_DIR moves both the cache and the directory beside it, which is
# a common thing to do on Linux to keep $HOME tidy; the root copy hard-codes
# ~/.claude.json and would then read nothing at all.
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR")
                                or "~/.claude")
CLAUDE_CONFIG = (os.path.join(CLAUDE_DIR, ".claude.json")
                 if os.environ.get("CLAUDE_CONFIG_DIR")
                 else os.path.expanduser("~/.claude.json"))
CLAUDE_STATUSLINE_CANDIDATES = (
    os.path.join(CLAUDE_DIR, "runcat-usage.json"),
    os.path.join(CLAUDE_DIR, "usage.json"),
    os.path.join(CLAUDE_DIR, "statusline.json"),
)

# -- the media source -------------------------------------------------------

# A systemd user unit inherits a minimal PATH with no ~/.local/bin on it, so
# the bare name is not enough: unresolved, the media screen would show nothing
# playing even while a player sits on the bus.
PLAYERCTL_CANDIDATES = (
    "/usr/bin/playerctl",
    "/usr/local/bin/playerctl",
    os.path.expanduser("~/.local/bin/playerctl"),
)


def _find_playerctl():
    """The usual install locations first, then whatever PATH offers."""
    for path in PLAYERCTL_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("playerctl")


PLAYERCTL = _find_playerctl()

# One line, unit-separator delimited: a title with a newline in it would
# otherwise shift every field after it. MPRIS counts time in microseconds.
UNIT_SEPARATOR = "\x1f"
PLAYERCTL_FORMAT = UNIT_SEPARATOR.join((
    "{{status}}", "{{title}}", "{{artist}}", "{{position}}",
    "{{mpris:length}}", "{{playerName}}",
))


def _from_playerctl():
    """The active MPRIS player, as playerctl reports it.

    MPRIS is the one thing every Linux player agrees on, and playerctl already
    does the bus walking and the "which player is actually the active one"
    part. Talking to D-Bus directly here would mean a bus library the daemon
    does not otherwise need, for no more information than this.
    """
    if not PLAYERCTL:
        return None
    try:
        result = subprocess.run(
            [PLAYERCTL, "metadata", "--format", PLAYERCTL_FORMAT],
            capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        # Nothing on the bus: playerctl exits non-zero and says so on stderr.
        return None
    fields = (result.stdout or "").strip().split(UNIT_SEPARATOR)
    fields += [""] * (6 - len(fields))
    status, title, artist, position, length, player = \
        [field.strip() for field in fields[:6]]
    if not title:
        return None
    return {
        "title": title,
        "artist": artist,
        "elapsed": _micros(position),
        "duration": _micros(length),
        "playing": status.lower() == "playing",
        "app": player,
    }


def _micros(value):
    """Microseconds as seconds. An unreported field reads as zero."""
    try:
        return float(value) / 1000000.0
    except (TypeError, ValueError):
        return 0


# Root's now_playing() reads this out of the root module's globals, and
# adopt() skips names beginning with an underscore, so the assignment has to
# be made on the root module itself. Doing it here rather than in adopt() is
# deliberate: this is the only underscore-prefixed amendment in the port, and
# it belongs next to the thing it changes.
_media_source = refresh.Background(
    lambda: (_from_playerctl(), None), MEDIA_REFRESH)
_root._media_source = _media_source

# Root's dict, with the app screen swapped for the one that says why it is
# empty. Rebuilt rather than mutated because SCREENS holds function *objects*:
# rebinding screen_app alone would leave the dict still pointing at the old
# one, and the swap would silently do nothing.
SCREENS = dict(_root.SCREENS, app=screen_app)

_shared.adopt(_root, globals())
