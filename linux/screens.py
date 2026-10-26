"""The things the LCD can show.

Each screen is a function taking (canvas, state) and drawing one frame.
State is a small dict the daemon keeps between refreshes.

The Linux port keeps every screen the macOS build has; only the places the
numbers come from change - /proc and /sys instead of vm_stat and pmset, MPRIS
instead of macOS's now-playing service.
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import time

import config
import refresh
from device import LCD_WIDTH
from lcd import Canvas

_CPU_COUNT = os.cpu_count() or 1
_mem_cache = {"when": 0.0, "value": 0.0}
_batt_cache = {"when": 0.0, "value": None}

# Claude Code keeps its cache in ~/.claude.json, next to a ~/.claude directory
# for everything else. CLAUDE_CONFIG_DIR moves both, which is a common thing
# to do on Linux to keep $HOME tidy.
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR")
                                or "~/.claude")
CLAUDE_CONFIG = (os.path.join(CLAUDE_DIR, ".claude.json")
                 if os.environ.get("CLAUDE_CONFIG_DIR")
                 else os.path.expanduser("~/.claude.json"))

# Claude Code's own cache goes stale, so a statusline script's dump is often
# fresher. Which script a person runs varies, so look for any of the known
# shapes rather than hard-coding one, and let the config name another.
CLAUDE_STATUSLINE_CANDIDATES = (
    os.path.join(CLAUDE_DIR, "runcat-usage.json"),
    os.path.join(CLAUDE_DIR, "usage.json"),
    os.path.join(CLAUDE_DIR, "statusline.json"),
)


def claude_statusline_path():
    """The statusline dump to read, if the user has one at all."""
    configured = (config.load().get("claude") or {}).get("statusline")
    candidates = ([configured] if configured else []) + \
        list(CLAUDE_STATUSLINE_CANDIDATES)
    for path in candidates:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    return None
# Keyed on the source files' mtimes rather than a timer, so the screen follows
# whichever source last changed instead of lagging behind both.
_util_cache = {"key": None, "value": None}
MEDIA_REFRESH = 3.0

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


def load_fraction():
    """1-minute load average as a fraction of available cores."""
    try:
        with open("/proc/loadavg") as handle:
            one_minute = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return min(1.0, one_minute / _CPU_COUNT)


def memory_fraction():
    """Share of physical memory in use, refreshed at most once a second.

    MemAvailable is the kernel's own estimate of what a new process could get
    without swapping, so it already discounts reclaimable cache - far closer
    to what a person means by "used" than MemTotal minus MemFree.
    """
    now = time.time()
    if now - _mem_cache["when"] < 1.0:
        return _mem_cache["value"]
    try:
        fields = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                fields[name] = float(rest.split()[0])
        total = fields.get("MemTotal", 0.0)
        available = fields.get("MemAvailable", fields.get("MemFree", 0.0))
        value = max(0.0, (total - available) / total) if total else 0.0
    except (OSError, ValueError, IndexError):
        value = 0.0
    _mem_cache.update(when=now, value=value)
    return value


def battery():
    """(percent, charging) or None on a desktop, refreshed every 30s."""
    now = time.time()
    if now - _batt_cache["when"] < 30.0:
        return _batt_cache["value"]
    value = None
    for path in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        percent = _read_sysfs(os.path.join(path, "capacity"))
        if not percent.isdigit():
            continue
        # "Not charging" means plugged in but holding below a charge limit,
        # which is still mains power: only Discharging is running off cells.
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


_media_source = refresh.Background(
    lambda: (_from_playerctl(), None), MEDIA_REFRESH)


def now_playing():
    """What is playing, from whichever source can see it.

    MPRIS covers every player including browsers. Asking costs ~100ms, so it
    is fetched off-thread and the elapsed time advanced locally in between -
    which also makes the progress bar move smoothly rather than in
    three-second steps.
    """
    reading = _media_source.get()
    return _advance(reading, time.time() - _media_source.fetched)


def _advance(reading, seconds):
    """Move a cached reading's elapsed time forward without re-polling."""
    if not reading or not reading.get("playing") or not reading.get("duration"):
        return reading
    moved = dict(reading)
    moved["elapsed"] = min(reading["duration"],
                           (reading.get("elapsed") or 0) + seconds)
    return moved


def _from_playerctl():
    """The active MPRIS player, as playerctl reports it.

    MPRIS is the one thing every Linux player agrees on, and playerctl already
    does the bus walking and the "which player is actually the active one"
    part. Talking to D-Bus here directly would mean a bus library the daemon
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


def clock(seconds):
    """612 -> '10:12'."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def claude_utilization():
    """Percentage of the Claude usage limits consumed.

    Two sources carry these numbers and they go stale at very different rates:
    Claude Code's own cache in ~/.claude.json only refreshes now and then,
    while the statusline dump is rewritten on every render. Neither is
    authoritative, so take whichever was written most recently.
    """
    key = (_mtime(CLAUDE_CONFIG), _mtime(claude_statusline_path() or ""))
    if key == _util_cache["key"] and _util_cache["value"]:
        return _mark_stale(_util_cache["value"])

    candidates = [reading for reading in (_from_claude_config(),
                                          _from_statusline()) if reading]
    if candidates:
        best = max(candidates, key=lambda reading: reading["fetched"])
    else:
        best = {"five_hour": None, "seven_day": None, "resets_at": None,
                "context": None, "stale": True, "source": None, "fetched": 0.0}
    _util_cache.update(key=key, value=best)
    return _mark_stale(best)


CLAUDE_ROWS = {
    "five_hour": "5h",
    "seven_day": "7d",
    "context": "ctx",
}


def _mark_stale(reading):
    """Age the reading now, not when it was cached.

    If neither source file changes again the cached entry is returned for
    ever, so staleness has to be judged on the way out or it can never become
    true.
    """
    block = config.load().get("claude") or {}
    limit = float(block.get("stale_after_seconds") or 600)
    reading["stale"] = (time.time() - (reading.get("fetched") or 0)) > limit
    return reading


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _from_claude_config():
    """Claude Code's own cached utilization, with the time it was fetched."""
    try:
        with open(CLAUDE_CONFIG) as handle:
            cached = json.load(handle).get("cachedUsageUtilization") or {}
    except (OSError, ValueError):
        return None
    buckets = cached.get("utilization") or {}
    five = buckets.get("five_hour") or {}
    seven = buckets.get("seven_day") or {}
    if five.get("utilization") is None:
        return None
    return {
        "five_hour": int(five["utilization"]),
        "seven_day": (int(seven["utilization"])
                      if seven.get("utilization") is not None else None),
        "resets_at": five.get("resets_at"),
        "context": None,
        "fetched": (cached.get("fetchedAtMs") or 0) / 1000.0,
        "source": "config",
        "stale": False,
    }


def _from_statusline():
    """The statusline dump, which is rewritten on every render."""
    path = claude_statusline_path()
    if not path:
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    reading = {"five_hour": None, "seven_day": None, "resets_at": None,
               "context": None, "source": "statusline", "stale": False,
               "fetched": _mtime(path)}
    for metric in data.get("metrics") or []:
        value = metric.get("normalizedValue")
        if value is None:
            continue
        title = metric.get("title")
        if title == "5h":
            reading["five_hour"] = int(round(value * 100))
        elif title == "7d":
            reading["seven_day"] = int(round(value * 100))
        elif title == "Context":
            reading["context"] = int(round(value * 100))
    if reading["five_hour"] is None:
        return None
    return reading


def until(stamp):
    """'1h23m' until an ISO timestamp, or None."""
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        # No offset means no way to know what it is relative to. Showing
        # nothing beats showing a countdown that is hours wrong.
        return None
    seconds = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if seconds <= 0:
        return "now"
    hours, minutes = int(seconds // 3600), int((seconds % 3600) // 60)
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def frontmost_app():
    """Always None on Linux: nothing tells us what has focus.

    Wayland deliberately gives an unprivileged client no way to read the
    focused window - there is no NSWorkspace equivalent, and no portal for it
    either. So the per-app colours and per-app binding profiles the macOS
    build layers on top of this simply do not apply here; returning None keeps
    every caller on the base bindings and the base backlight colour.
    """
    return None


# -- screens ---------------------------------------------------------------

def screen_clock(canvas, _state):
    canvas.centered(time.strftime("%H:%M:%S"), 1, 22)
    canvas.centered(time.strftime("%a %d %b %Y"), 28, 12)


def screen_status(canvas, _state):
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    power = battery()
    if power:
        percent, charging = power
        label = f"{percent}%{'+' if charging else ''}"
        canvas.text(label, LCD_RIGHT - canvas.measure(label, 11), 2, 11)
    canvas.text("LOAD", 2, 18, 9)
    canvas.bar(34, 18, 124, 9, load_fraction())
    canvas.text("MEM", 2, 30, 9)
    canvas.bar(34, 30, 124, 9, memory_fraction())


def screen_gkeys(canvas, state):
    canvas.text("G-KEYS", 2, 0, 11)
    canvas.text(time.strftime("%H:%M"), 120, 0, 11)
    last = state.get("last_gkey")
    if last:
        canvas.text(last, 2, 14, 18)
        canvas.text(_fit(canvas, state.get("last_action", "unbound"), 9),
                    2, 32, 9)
    else:
        canvas.text("press a G-key", 2, 20, 11)


def screen_app(canvas, _state):
    """Kept so the screen name still resolves, but there is nothing to show.

    frontmost_app() can never answer on Wayland, so rather than drawing a
    permanent "-" that looks like a bug, say why the screen is empty.
    """
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    canvas.text("no app profiles", 2, 18, 11)
    canvas.text("Wayland hides focus", 2, 31, 10)


def screen_claude(canvas, _state):
    """Percentage of the Claude usage limits used, with time to reset."""
    util = claude_utilization()
    canvas.text("CLAUDE", 2, 0, 11)
    context = util.get("context")
    resets = until(util.get("resets_at"))
    if context is not None:
        header = f"ctx {context}%"
    elif resets:
        header = f"resets {resets}"
    else:
        header = time.strftime("%H:%M")
    if util.get("stale"):
        header = "~ " + header
    canvas.text(header, LCD_WIDTH - canvas.measure(header, 10) - 2, 1, 10)

    block = config.load().get("claude") or {}
    wanted = [row for row in (block.get("rows") or ["five_hour", "seven_day"])
              if row in CLAUDE_ROWS][:2]
    for index, key in enumerate(wanted or ["five_hour", "seven_day"]):
        label = CLAUDE_ROWS[key]
        y = 15 + index * 15
        value = util.get(key)
        canvas.text(label, 2, y + 1, 10)
        if value is None:
            canvas.text("no data", 24, y + 1, 10)
            continue
        canvas.bar(22, y, 104, 11, value / 100.0)
        percent = f"{value}%"
        canvas.text(percent, LCD_WIDTH - canvas.measure(percent, 11) - 2, y, 11)


_gauge_sources = {}


def gauge_values():
    """Run each configured gauge command, off-thread, and cache the numbers.

    Claude Code happens to cache its usage locally; most tools do not, and
    inventing a reader per tool would be guesswork. A command that prints a
    number is something anyone can point at whatever they actually use.
    """
    readings = []
    for entry in (config.load().get("gauges") or [])[:3]:
        label = str(entry.get("label") or "?")[:8]
        command = entry.get("command")
        if not command:
            continue
        source = _gauge_sources.get(command)
        if source is None:
            source = refresh.Background(
                lambda c=command: (_run_gauge(c), None),
                float(entry.get("refresh_seconds") or 15))
            _gauge_sources[command] = source
        readings.append((label, source.get(), entry.get("unit", "%")))
    return readings


def _run_gauge(command):
    """A gauge command prints one number. Anything else reads as no value."""
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    line = (result.stdout or "").strip().split("\n")[0].strip().rstrip("%")
    try:
        return float(line)
    except ValueError:
        return None


def screen_gauge(canvas, _state):
    """Whatever the user pointed it at - a quota, a temperature, a queue."""
    readings = gauge_values()
    canvas.text("GAUGES", 2, 0, 10)
    canvas.text(time.strftime("%H:%M"), LCD_WIDTH - canvas.measure("00:00", 10) - 2,
                1, 10)
    if not readings:
        canvas.text("none configured", 2, 16, 12)
        canvas.text("g510 gauge add ...", 2, 31, 9)
        return
    for index, (label, value, unit) in enumerate(readings):
        y = 14 + index * 15
        canvas.text(label, 2, y + 1, 9)
        if value is None:
            canvas.text("--", 46, y + 1, 9)
            continue
        shown = f"{value:.0f}{unit}"
        canvas.bar(46, y, LCD_WIDTH - 52 - canvas.measure(shown, 10), 11,
                   value / 100.0)
        canvas.text(shown, LCD_WIDTH - canvas.measure(shown, 10) - 2, y, 10)


def screen_printer(canvas, _state):
    """3D printer progress: what it is doing, how far in, and how hot."""
    import printer

    reading = printer.status()
    if not reading:
        canvas.text("PRINTER", 2, 0, 11)
        canvas.text(printer.describe_state(None), 2, 16, 12)
        note = printer.last_error()
        if note:
            canvas.text(_fit(canvas, note, 9), 2, 31, 9)
        return

    state = printer.describe_state(reading).upper()
    canvas.text(f"{reading['model']}  {state}", 2, 0, 10)
    if printer.is_printing(reading) and reading["left"]:
        left = _duration(reading["left"])
        canvas.text(left, LCD_WIDTH - canvas.measure(left, 10) - 2, 1, 10)

    canvas.text(_fit(canvas, reading["file"] or "no file", 11), 2, 11, 11)

    detail = f"N{reading['nozzle']:.0f}  B{reading['bed']:.0f}"
    if reading["layers"]:
        detail += f"  L{reading['layer']}/{reading['layers']}"
    canvas.text(detail, 2, 24, 9)
    percent = f"{reading['progress']}%"
    canvas.text(percent, LCD_WIDTH - canvas.measure(percent, 9) - 2, 24, 9)

    canvas.bar(2, 36, LCD_WIDTH - 4, 6, reading["progress"] / 100.0)


def _duration(seconds):
    """1720 -> '28m', 7200 -> '2h00'."""
    seconds = max(0, int(seconds))
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}m"


def screen_media(canvas, _state):
    track = now_playing()
    if not track:
        canvas.text("NOW PLAYING", 2, 0, 11)
        canvas.text(time.strftime("%H:%M"), 122, 0, 11)
        canvas.text("nothing playing", 2, 18, 12)
        return

    header = "NOW PLAYING" if track.get("playing") else "PAUSED"
    canvas.text(header, 2, 0, 10)
    duration = track.get("duration") or 0
    if duration:
        stamp = f"{clock(track.get('elapsed') or 0)} / {clock(duration)}"
    else:
        stamp = time.strftime("%H:%M")
    canvas.text(stamp, LCD_WIDTH - canvas.measure(stamp, 10) - 2, 1, 10)

    canvas.text(_fit(canvas, track["title"], 13), 2, 12, 13)
    artist = track.get("artist") or track.get("app") or ""
    if artist:
        canvas.text(_fit(canvas, artist, 10), 2, 26, 10)
    if duration:
        canvas.bar(2, 38, LCD_WIDTH - 4, 5,
                   (track.get("elapsed") or 0) / duration)


def _fit(canvas, string, size):
    """Trim a string to the panel width, with an ellipsis if it had to go."""
    if canvas.measure(string, size) <= LCD_WIDTH - 4:
        return string
    while string and canvas.measure(string + "...", size) > LCD_WIDTH - 4:
        string = string[:-1]
    return string + "..."


def screen_record(canvas, state):
    """Shown while MR recording is armed or running."""
    stage = state.get("record_state")
    if stage == "await_key":
        canvas.text("RECORD", 2, 0, 11)
        canvas.text("press a G-key", 2, 15, 13)
        canvas.text("MR again to cancel", 2, 31, 10)
    else:
        target = state.get("record_target", "?")
        canvas.text(f"RECORDING {target}", 2, 0, 11)
        canvas.text(f"{state.get('record_steps', 0)} keys", 2, 15, 14)
        canvas.text("MR or Esc to finish", 2, 31, 10)


LCD_RIGHT = 158

SCREENS = {
    "status": screen_status,
    "clock": screen_clock,
    "claude": screen_claude,
    "media": screen_media,
    "printer": screen_printer,
    "gauge": screen_gauge,
    "gkeys": screen_gkeys,
    "app": screen_app,
}

# Not in the cycle: the daemon switches to it while MR recording is active.
SCREENS_INTERNAL = {"record": screen_record}


def render(name, state):
    """Draw the named screen and return packed pixel rows."""
    canvas = Canvas()
    drawer = SCREENS.get(name) or SCREENS_INTERNAL.get(name) or screen_status
    drawer(canvas, state)
    return canvas.pixels()
