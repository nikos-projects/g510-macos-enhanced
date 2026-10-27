"""The things the LCD can show.

Each screen is a function taking (canvas, state) and drawing one frame.
State is a small dict the daemon keeps between refreshes.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

import config
import refresh
import timers
from device import LCD_WIDTH
from lcd import Canvas

_CPU_COUNT = os.cpu_count() or 1
_mem_cache = {"when": 0.0, "value": 0.0}
_batt_cache = {"when": 0.0, "value": None}

CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")

# Claude Code's own cache goes stale, so a statusline script's dump is often
# fresher. Which script a person runs varies, so look for any of the known
# shapes rather than hard-coding one, and let the config name another.
CLAUDE_STATUSLINE_CANDIDATES = (
    "~/.claude/runcat-usage.json",
    "~/.claude/usage.json",
    "~/.claude/statusline.json",
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

# launchd gives the agent a minimal PATH with no Homebrew on it, so the bare
# name is not enough: unresolved, the media screen silently falls back to a
# browser window title, which carries no duration and so draws no progress bar.
NOWPLAYING_CANDIDATES = (
    "/opt/homebrew/bin/nowplaying-cli",
    "/usr/local/bin/nowplaying-cli",
)


def _find_nowplaying():
    """Look inside the app bundle first, then the usual install locations.

    A packaged app cannot rely on PATH at all - launchd gives an agent a
    minimal one, and a Mac that installed the .app may have no Homebrew.
    """
    bundled = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                           "nowplaying-cli")
    if os.path.exists(bundled):
        return bundled
    for path in NOWPLAYING_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("nowplaying-cli")


NOWPLAYING_CLI = _find_nowplaying()

# Ask only players that are already running, so nothing gets launched.
_TRACK_SCRIPT = "\n".join([
    'on trackFor(appName)',
    '  tell application "System Events"',
    '    if (count of (every process whose name is appName)) is 0 then return ""',
    '  end tell',
    '  tell application appName',
    '    if player state is not playing then return ""',
    '    return (get artist of current track) & " - " & (get name of current track)',
    '  end tell',
    'end trackFor',
    'set out to trackFor("Music")',
    'if out is "" then set out to trackFor("Spotify")',
    'return out',
])


def load_fraction():
    """1-minute load average as a fraction of available cores."""
    return min(1.0, os.getloadavg()[0] / _CPU_COUNT)


def memory_fraction():
    """Share of physical memory in use, refreshed at most once a second."""
    now = time.time()
    if now - _mem_cache["when"] < 1.0:
        return _mem_cache["value"]
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=2).stdout
        counts = {}
        for line in out.splitlines():
            match = re.match(r'Pages (.+?):\s+(\d+)', line)
            if match:
                counts[match.group(1)] = int(match.group(2))
        used = counts.get("active", 0) + counts.get("wired down", 0)
        total = used + counts.get("free", 0) + counts.get("inactive", 0)
        value = used / total if total else 0.0
    except (subprocess.SubprocessError, OSError, ValueError):
        value = 0.0
    _mem_cache.update(when=now, value=value)
    return value


def battery():
    """(percent, charging) or None on a desktop, refreshed every 30s."""
    now = time.time()
    if now - _batt_cache["when"] < 30.0:
        return _batt_cache["value"]
    value = None
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=2).stdout
        match = re.search(r'(\d+)%', out)
        if match:
            value = (int(match.group(1)), "AC Power" in out or "charging" in out)
    except (subprocess.SubprocessError, OSError):
        value = None
    _batt_cache.update(when=now, value=value)
    return value


_media_source = refresh.Background(
    lambda: (_from_nowplaying() or _from_players() or _from_window_title(), None),
    MEDIA_REFRESH)


# Where the running count of elapsed time was last known to agree with the
# player: the position, the time that position was true at, which track it
# belongs to, and whether it was playing. See now_playing.
_media_anchor = {"key": None, "elapsed": 0.0, "at": 0.0, "playing": None}


def now_playing(wait=False):
    """What is playing, from whichever source can see it.

    macOS's own now-playing information covers every player including browsers,
    which the per-app AppleScript route cannot. Asking costs ~100ms, so it is
    fetched off-thread and the elapsed time advanced locally in between - which
    also makes the progress bar move smoothly rather than in three-second steps.

    The player's own elapsed time is not a ticking clock, so it cannot simply
    be believed when a poll brings a new one: players push it only when they
    feel like it, and Spotify holds the same value for a minute or more before
    jumping to catch up. Resetting the local count to whatever just arrived is
    what once made the bar race three seconds ahead and snap back again every
    poll. So while a track is playing the local count is the master, and a
    poll moves it only when it has news: forward, because of a seek, or all
    over, because the track or the playback state changed.

    `wait` fetches a fresh reading first, for a one-shot caller - the CLI
    would otherwise report whatever the last poll left behind, which is
    nothing at all on the first call.
    """
    if wait:
        _media_source.get_now()
    reading = _media_source.get()
    return _reconcile(reading, _media_source.fetched, time.time())


def _reconcile(reading, polled_at, now):
    """Merge one poll into the running picture of where the track is.

    `polled_at` is when `reading` was sampled, `now` the time being drawn for.
    A poll that lags the local count is stale and ignored; one that runs ahead
    is a seek and is taken as the new starting point.
    """
    if not reading:
        _media_anchor.update(key=None, elapsed=0.0, at=0.0, playing=None)
        return reading
    anchor = _media_anchor
    duration = float(reading.get("duration") or 0)
    polled = float(reading.get("elapsed") or 0)
    playing = bool(reading.get("playing"))
    key = (reading.get("title"), duration, reading.get("app"))
    if key != anchor["key"] or playing != anchor["playing"]:
        # A new track, or the play/pause flipping over. Both are moments the
        # player has just pushed a fresh position - pausing more or less
        # always does - so this is the one time its number is worth
        # believing outright, and the count starts again from it.
        anchor.update(key=key, elapsed=polled, at=polled_at, playing=playing)
        return reading
    if not playing:
        return reading
    projected = anchor["elapsed"] + max(0.0, now - anchor["at"])
    if polled > projected:
        # The player jumped ahead of us: a seek, or the first news from it
        # after a stretch of silence. Either way it knows better than the
        # guesswork does, so start again from what it says.
        projected = polled
    # A poll behind the count carries no news - the player repeating itself -
    # so the count stands and moves on from there. Carrying it forward also
    # stops a player that fell silent for a long while from looking like a
    # huge backwards seek the moment it speaks again.
    anchor.update(elapsed=projected, at=now)
    moved = dict(reading)
    moved["elapsed"] = min(duration, projected) if duration else projected
    return moved


def _from_nowplaying():
    """macOS now-playing info, covering browsers and every other player."""
    if not NOWPLAYING_CLI:
        return None
    try:
        result = subprocess.run([NOWPLAYING_CLI, "get-raw"],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        info = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    title = info.get("kMRMediaRemoteNowPlayingInfoTitle")
    if not title:
        return None
    bundle = info.get("kMRMediaRemoteNowPlayingInfoClientBundleIdentifier") or ""
    reading = {
        "title": title,
        "artist": info.get("kMRMediaRemoteNowPlayingInfoArtist") or "",
        "elapsed": info.get("kMRMediaRemoteNowPlayingInfoElapsedTime") or 0,
        "duration": info.get("kMRMediaRemoteNowPlayingInfoDuration") or 0,
        "playing": bool(info.get("kMRMediaRemoteNowPlayingInfoPlaybackRate")),
        "app": bundle.rsplit(".", 1)[-1],
    }
    direct = _from_apple_script(DIRECT_POSITION_APPS.get(bundle))
    if direct:
        reading["elapsed"], reading["duration"], reading["playing"] = direct
    return reading


# The bundles whose player answers a direct question about its own position,
# the application name to ask, and what that player counts a track's length
# in - Spotify's AppleScript counts milliseconds where everything else counts
# seconds, though its position is in seconds either way. MediaRemote has to
# be believed for everyone else, and it makes these two up as they go along.
DIRECT_POSITION_APPS = {
    "com.spotify.client": ("Spotify", 1000.0),
    "com.apple.Music": ("Music", 1.0),
}


def _from_apple_script(app):
    """(elapsed, duration, playing) straight from Music or Spotify.

    Only asked when the system route has already named the app, so nothing
    gets launched, and only of the two players known to answer exactly -
    where the system-wide position is only as good as the player's mood.
    """
    if not app:
        return None
    name, duration_scale = app
    script = (f'tell application "{name}"\n'
              '  set pos to get player position\n'
              '  set len to get duration of current track\n'
              '  set hot to player state is playing\n'
              '  return (pos as text) & "|" & (len as text) & "|" & '
              '(hot as text)\n'
              'end tell')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    parsed = _parse_position(result.stdout)
    if not parsed:
        return None
    elapsed, duration, playing = parsed
    return elapsed, duration / duration_scale, playing


def _parse_position(line):
    """(elapsed, duration, playing) out of AppleScript's one-line answer.

    AppleScript writes numbers with whatever decimal separator the session
    uses, so a comma arrives on a German Mac.
    """
    fields = (line or "").strip().split("|")
    if len(fields) != 3 or fields[2] not in ("true", "false"):
        return None
    try:
        return (float(fields[0].replace(",", ".")),
                float(fields[1].replace(",", ".")),
                fields[2] == "true")
    except ValueError:
        return None


def _from_players():
    """Music or Spotify directly, if the system route gave nothing."""
    try:
        result = subprocess.run(["osascript", "-e", _TRACK_SCRIPT],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    line = result.stdout.strip()
    if not line:
        return None
    artist, _, title = line.partition(" - ")
    if not title:
        artist, title = "", line
    return {"title": title.strip(), "artist": artist.strip(), "elapsed": 0,
            "duration": 0, "playing": True, "app": ""}


BROWSERS = ("firefox", "Google Chrome", "Safari", "Brave Browser",
            "Microsoft Edge", "Arc", "Vivaldi", "Opera")

# Sites whose window title names what is playing, and the suffix to trim.
TITLE_SITES = (" - YouTube", " | Twitch", " - Twitch", " - SoundCloud",
               " | Spotify", " - Vimeo")


def _from_window_title():
    """Last resort: a browser's window title names the page that is playing."""
    for browser in BROWSERS:
        script = (f'tell application "System Events" to tell process '
                  f'"{browser}" to return name of front window')
        try:
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True, timeout=2)
        except (subprocess.SubprocessError, OSError):
            continue
        title = result.stdout.strip()
        if not title:
            continue
        for suffix in TITLE_SITES:
            if suffix in title:
                return {"title": title.rsplit(suffix, 1)[0],
                        "artist": suffix.lstrip(" -|"), "elapsed": 0,
                        "duration": 0, "playing": True, "app": browser}
    return None


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
    """Name of the frontmost application, or None."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None
    except Exception:
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
    name = frontmost_app() or "-"
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    canvas.text(name[:22], 2, 20, 12)
    canvas.bar(2, 34, 156, 8, load_fraction())


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


def screen_timers(canvas, _state):
    """The countdowns running now, nearest to finishing first."""
    running = timers.STORE.running()
    canvas.text("TIMERS", 2, 0, 10)
    extra = len(running) - 2
    corner = f"+{extra}" if extra > 0 else time.strftime("%H:%M")
    canvas.text(corner, LCD_RIGHT - canvas.measure(corner, 10), 1, 10)
    if not running:
        canvas.text("nothing running", 2, 16, 12)
        canvas.text("g510 timer start 10m", 2, 31, 9)
        return
    for index, item in enumerate(running[:2]):
        y = 13 + index * 15
        label = item.get("label") or item.get("name") or "timer"
        canvas.text(_fit(canvas, label, 11), 2, y, 11)
        remaining = timers.left(item.get("left") or 0)
        canvas.text(remaining, LCD_RIGHT - canvas.measure(remaining, 11), y, 11)


def screen_timeralert(canvas, state):
    """The panel an alarm gets while it lasts: what it was, and that it is
    over."""
    data = state.get("alert") or {}
    label = str(data.get("label") or data.get("name") or "timer")
    span = timers.left(data.get("seconds") or 0)
    if data.get("phase") == "started":
        canvas.centered("TIMER STARTED", 2, 11)
        canvas.centered(_fit(canvas, label, 14), 16, 14)
        ends = data.get("until")
        detail = f"{span} - ends {time.strftime('%H:%M', time.localtime(ends))}" \
            if ends else span
        canvas.centered(detail, 33, 9)
    else:
        canvas.centered("TIME'S UP", 2, 16)
        canvas.centered(_fit(canvas, label, 12), 21, 12)
        canvas.centered(span, 34, 9)


def screen_message(canvas, state):
    """One message from the chat app, for as long as the alert lasts."""
    data = state.get("alert") or {}
    app = str(data.get("app") or "Discord").upper()
    channel = str(data.get("channel") or "")
    author = str(data.get("author") or "")
    canvas.text(_fit(canvas, app, 9), 2, 0, 9)
    if channel:
        drawn = _fit(canvas, channel, 9)
        canvas.text(drawn, LCD_RIGHT - canvas.measure(drawn, 9), 1, 9)
    if author:
        canvas.text(_fit(canvas, author, 11), 2, 11, 11)
        lines = _wrap(canvas, str(data.get("text") or ""), 10, 2)
        if lines:
            canvas.text(lines[0], 2, 24, 10)
        if len(lines) > 1:
            canvas.text(lines[1], 2, 34, 9)
        return
    # Nothing to attribute it to: a count of what is waiting elsewhere.
    canvas.text(_fit(canvas, str(data.get("text") or ""), 15), 2, 12, 15)
    detail = str(data.get("title") or data.get("time") or "")
    if detail:
        canvas.text(_fit(canvas, detail, 9), 2, 31, 9)


def screen_discord(canvas, state):
    """The conversation, as the Vencord plugin last reported it.

    Three shapes, because there are three things worth showing: the screen
    while its keys are being used to walk it (a channel picked, a message
    answered, a mention written - `browse.Browser` decides what goes in
    here), the message that is on top of the channel you are in, and -
    when nothing is selected, or the plugin has not connected yet - what
    there is to pick from. All three fit the three lines every other
    screen gets.
    """
    view = state.get("browse")
    if view:
        _browse(canvas, view)
        return
    data = state.get("discord") or {}
    corner = time.strftime("%H:%M")
    if not data:
        # No push has ever arrived: the server is up (or off) and Discord
        # has said nothing. Saying which is worth more than a blank panel.
        canvas.text("DISCORD", 2, 0, 11)
        canvas.text(corner, LCD_RIGHT - canvas.measure(corner, 11), 1, 11)
        canvas.text("waiting for Vesktop", 2, 16, 12)
        canvas.text("g510 discord", 2, 32, 9)
        return

    channel = data.get("channel") or {}
    messages = data.get("messages") or []
    if channel and messages:
        last = messages[-1]
        canvas.text(_fit(canvas, str(channel.get("name") or "?"), 10), 2, 0, 10)
        stamp = str(last.get("time") or corner)
        if data.get("has_more"):
            stamp = "<" + stamp
        canvas.text(stamp, LCD_RIGHT - canvas.measure(stamp, 10), 1, 10)
        canvas.text(_fit(canvas, str(last.get("author") or "?"), 11), 2, 13, 11)
        lines = _wrap(canvas, str(last.get("text") or "(empty)"), 10, 2)
        if lines:
            canvas.text(lines[0], 2, 27, 10)
        if len(lines) > 1:
            canvas.text(lines[1], 2, 36, 9)
        return

    canvas.text("DISCORD", 2, 0, 11)
    canvas.text(corner, LCD_RIGHT - canvas.measure(corner, 11), 1, 11)
    rows = [row for row in (data.get("channels") or [])][:3]
    if not rows:
        hint = "connected, nothing open" if channel else "no channels yet"
        canvas.text(_fit(canvas, hint, 12), 2, 17, 12)
        return
    for index, row in enumerate(rows):
        name = str(row.get("name") or row.get("id") or "?")
        mark = ">" if row.get("current") else \
            ("@" if row.get("mentions") else
             ("*" if row.get("unread") else " "))
        canvas.text(_fit(canvas, mark + name, 10), 2, 13 + index * 10, 10)


def _tail(canvas, string, size):
    """The *end* of a line, cut from the left.

    `_fit` keeps the head, which is right for reading a message and wrong
    for writing one: what is being typed sits at the right of the line, so
    the half that has already run off is exactly the half the caret is in.
    An ellipsis marks that something was cut, the same way `_fit` does.
    """
    limit = LCD_WIDTH - 4
    out = str(string)
    cut = False
    while out and canvas.measure("..." + out if cut else out, size) > limit:
        out = out[1:]
        cut = True
    return ("..." + out) if cut else out


def _wrap_tail(canvas, text, size, lines=3):
    """Break text across `lines` of the panel, keeping the *end* of it.

    `_wrap` word-wraps and, when a word is wider than the panel, keeps its
    head with an ellipsis - right for reading a message, wrong for writing
    one. What is being typed sits at the right of the last line, so the half
    that has already run off is exactly the half the caret is in, and a long
    URL or a run of unbroken characters would otherwise hide the caret
    entirely. So the lines are taken from the end backwards and cut wherever
    they overflow, and what was dropped off the front is marked with "...".
    """
    limit = LCD_WIDTH - 4
    out, rest = [], str(text)
    while rest and len(out) < lines:
        if canvas.measure(rest, size) <= limit:
            out.append(rest)
            break
        cut = rest
        while cut and canvas.measure(cut, size) > limit:
            cut = cut[1:]
        if not cut:
            break
        out.append(cut)
        rest = rest[:len(rest) - len(cut)]
    out.reverse()
    if rest and out:
        out[0] = "..." + out[0]
    return out


def _browse(canvas, view):
    """The Discord screen while the keys belong to it.

    A header and three rows, the same budget every other screen has, and
    each pane spends it on one thing: three channels with the one under
    the cursor in the middle; one message at a time with its words wrapped
    underneath; or a draft, with the suggestions it has opened below it
    while an @ is being written. A refusal from the plugin goes over the
    second row for a few seconds, because "nothing happened" without a
    reason is the one answer a panel this small must never give.
    """
    corner = time.strftime("%H:%M")
    mode = str(view.get("mode") or "channels")
    title = _fit(canvas, str(view.get("title") or "DISCORD"), 10)
    right = str(view.get("count") or "")
    draft = str(view.get("draft") or "")
    picks = view.get("picks") or []

    if mode == "compose" and not picks:
        # The header says who this is going to rather than where in
        # anything the user is: an answer, or a fresh message.
        reply = view.get("reply")
        right = _fit(canvas, f"re: {reply}" if reply else "new", 10)
        canvas.text(title, 2, 0, 10)
        canvas.text(right, LCD_RIGHT - canvas.measure(right, 10), 1, 10)
        for index, line in enumerate(_wrap_tail(canvas, draft + "_", 10, 3)):
            canvas.text(line, 2, 13 + index * 10, 10)
    elif mode == "compose":
        # A suggestion is on offer, so the draft takes the header row -
        # the tail of it, because the word being completed is at the end -
        # and the list gets the three rows under it.
        canvas.text(_tail(canvas, draft + "_", 10), 2, 1, 10)
        if right:
            canvas.text(right, LCD_RIGHT - canvas.measure(right, 10), 1, 10)
        for index, row in enumerate(picks):
            mark = ">" if row.get("cursor") else " "
            label = _fit(canvas, mark + " " + str(row.get("label") or "?"),
                         14)
            canvas.text(label, 2, 14 + index * 10, 10)
    else:
        canvas.text(title, 2, 0, 10)
        canvas.text(right or corner,
                    LCD_RIGHT - canvas.measure(right or corner, 10), 1, 10)
        waiting = str(view.get("waiting") or "")
        if waiting:
            canvas.text(_fit(canvas, waiting, 14), 2, 17, 12)
        elif mode == "messages":
            if view.get("empty"):
                canvas.text("no messages", 2, 17, 12)
            else:
                # The `>` is the cursor: it says which message the keys
                # are on, which is what enter would answer.
                #
                # Everything below the header is spent on the message, and
                # the panel has 43 rows to spend it in: a line drawn at
                # `y` puts its ink at `y + 1 .. y + size + 1`, so the two
                # lines of a message have to start at 21 and 32 or the
                # second one loses the bottom of its letters to the edge -
                # which is exactly what it used to do at 27 and 36, where
                # five rows of a nine-row line were all that was left.
                # The author takes the rows the header leaves, at the size
                # that lets both lines have theirs.
                canvas.text(_fit(canvas, "> " + str(view.get("author") or "?"),
                                 11), 2, 11, 9)
                # The whole message, wrapped, and then the two lines of it
                # the panel has room for: `scroll` is how far the browser
                # has already carried the reader down it, so a message
                # longer than the panel goes on being read a piece at a
                # time instead of ending in "..." out of reach.
                lines = _wrap(canvas, str(view.get("text") or "(empty)"),
                              10, None)
                window = _window(canvas, lines, view.get("scroll"))
                if window:
                    canvas.text(window[0], 2, 21, 10)
                if len(window) > 1:
                    canvas.text(window[1], 2, 32, 9)
        else:
            rows = view.get("rows") or []
            if not rows:
                canvas.text("no channels here", 2, 17, 12)
            for index, row in enumerate(rows):
                # A space after the mark, the same as the messages pane
                # puts after its ">", so the names line up down the column.
                marked = str(row.get("mark") or " ") + " " \
                    + str(row.get("name") or "?")
                canvas.text(_fit(canvas, marked, 10), 2, 13 + index * 10, 10)

    error = str(view.get("error") or "")
    if error:
        # Over whatever is underneath: an error is news, and the rows it
        # covers are still there the moment it goes. The messages pane
        # starts its first line two rows above where the other two start
        # theirs, so it is that line the refusal has to sit on.
        canvas.text("!" + _fit(canvas, error, 15), 2,
                    11 if mode == "messages" else 13, 10)


def screen_menu(canvas, state):
    """The menu: where you are, what is on, and what comes next.

    Three rows, the way `menu.view()` hands them over - a breadcrumb, the
    item you are on (which the menu has already marked with ">"), and the
    row under it, which is either the next item or a hint about the key
    you are about to press.
    """
    view = state.get("menu") or {}
    title = str(view.get("title") or "")
    if title:
        canvas.text(_fit(canvas, title, 9), 2, 0, 9)
    rows = list(view.get("main") or [])
    if rows:
        canvas.text(_fit(canvas, str(rows[0]), 12), 2, 12, 12)
    lower = rows[1] if len(rows) > 1 else view.get("sub")
    if lower:
        canvas.text(_fit(canvas, str(lower), 10), 2, 29, 10)


def screen_note(canvas, state):
    """A sticky note: what is being written, or what is pinned.

    Two states on one screen. While a note is being written there is a
    prompt to say so and a caret at the end of the line; pinned, the text
    is the whole panel, wrapped across three rows so a sentence is readable
    at a glance rather than cut off at the right edge.
    """
    if state.get("note_entry"):
        canvas.text(_fit(canvas, "NOTE", 9), 2, 0, 9)
        hint = _fit(canvas, "enter pins  esc cancels", 9)
        canvas.text(hint, LCD_RIGHT - canvas.measure(hint, 9), 1, 9)
        lines = _wrap(canvas, str(state.get("note") or ""), 11, 2)
        if not lines:
            lines = ["_"]
        else:
            lines[-1] = _fit(canvas, lines[-1] + "_", 11)
        for index, line in enumerate(lines):
            canvas.text(line, 2, 13 + index * 14, 11)
        return
    for index, line in enumerate(_wrap(canvas, str(state.get("note") or ""),
                                       11, 3)):
        canvas.text(line, 2, 1 + index * 14, 11)


def _wrap(canvas, text, size, lines=2):
    """Break text across up to `lines` of the panel, cutting what will not
    fit and saying so. `lines=None` wraps all of it instead, which is what
    a caller that means to show a window of its own asks for."""
    limit = LCD_WIDTH - 4
    words = str(text).split()
    out, current, index = [], "", 0
    while index < len(words) and (lines is None or len(out) < lines):
        candidate = words[index] if not current else f"{current} {words[index]}"
        if canvas.measure(candidate, size) <= limit:
            current, index = candidate, index + 1
            continue
        if current:
            out.append(current)
            current = ""
            continue
        out.append(_fit(canvas, words[index], size))     # one word, too wide
        current, index = "", index + 1
    if current and (lines is None or len(out) < lines):
        out.append(current)
    if index < len(words) and out:
        # Something was left over: say so rather than let the panel cut a
        # sentence off as though it had ended there.
        out[-1] = _fit(canvas, out[-1] + " ...", size)
    return out[:lines]


def _window(canvas, lines, scroll):
    """The two lines of `lines` to draw, starting at `scroll`.

    The start is held inside what is actually there, so a scroll that has
    run past the end of a short message simply shows its last two lines -
    the browser counts lines it cannot see, and only the panel knows how
    many there really are. Anything left below the window is said so the
    way `_wrap` says it when it cuts a message short, because a panel that
    went quiet at the bottom of a message would read as the end of it.
    """
    try:
        start = int(scroll or 0)
    except (TypeError, ValueError):
        start = 0
    start = max(0, min(start, max(0, len(lines) - 2)))
    out = list(lines[start:start + 2])
    if out and start + len(out) < len(lines):
        out[-1] = _fit(canvas, out[-1] + " ...", 10)
    return out


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
    "timers": screen_timers,
    # In `SCREENS` so it can be named - by `g510 lcd discord`, by the soft
    # key that asks for it - but deliberately not in the default `cycle`:
    # it is the screen three presses of L3 go and fetch, not one that turns
    # up while you are working.
    "discord": screen_discord,
}

# Not in the cycle: the daemon switches to these itself, while MR is
# recording, an alert is up, the menu is open or a note is pinned, and no
# rule or key can ask for them.
SCREENS_INTERNAL = {"record": screen_record, "timeralert": screen_timeralert,
                    "message": screen_message, "menu": screen_menu,
                    "note": screen_note}


def render(name, state):
    """Draw the named screen and return packed pixel rows."""
    canvas = Canvas()
    drawer = SCREENS.get(name) or SCREENS_INTERNAL.get(name) or screen_status
    drawer(canvas, state)
    return canvas.pixels()
