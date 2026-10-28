"""Config file handling for the G510 tools.

Lives at ~/.config/g510/config.json so it survives reinstalls of the app
directory, and is plain JSON so it can be edited by hand.
"""
import copy
import json
import os
import tempfile

CONFIG_DIR = os.path.expanduser("~/.config/g510")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "backlight": "#ffffff",
    "brightness": 100,
    "restore_backlight_on_start": True,
    # The backlight dipping to the beat of the music - see beat.py. Off
    # by default: it opens a system-wide audio tap, which is a thing to
    # opt into rather than to find already running. `amount` is how far
    # down it dips as a percentage, `decay` how long it takes to climb
    # back to full, and `sensitivity` how eager the detector is - 50 is
    # the middle of its range, 100 catches everything.
    "beat": {"enabled": False, "amount": 60, "decay": 0.35,
             "sensitivity": 50},
    # The API any program on this Mac can hook into - see api.py. On by
    # default because it is bound to 127.0.0.1 and costs nothing until
    # something asks it a question, and because an interface nobody can
    # find is not one anybody will write against.
    "api": {"enabled": True, "port": 7420},
    "lcd": {
        "enabled": True,
        "screen": "status",
        "refresh_seconds": 1.0,
        # Any font family or PostScript name macOS knows: Cyrillic, Greek,
        # CJK and emoji all draw as themselves, and anything this font has no
        # glyph for falls back to one that does.
        "font": "Menlo-Bold",
        "cycle": ["status", "clock", "claude", "media", "app", "timers"],
    },
    # Switching the display by what the Mac is doing - see autoscreen.py.
    # Rules are tried in the order they are written; the first whose `when`
    # all holds wins, so the list is the priority. A screen may be "off",
    # which blanks the display.
    "autoscreen": {
        "enabled": True,
        "check_seconds": 2.0,
        "idle_seconds": 60,
        "hold_seconds": 3.0,
        "manual_seconds": 600,
        "fallback": "",
        # App names or bundle ids that count as games - globs are fine, so
        # "com.mojang.*" and "Minecraft*" both work. `not_games` wins over
        # everything else, including these.
        "games": [],
        "not_games": [],
        "rules": [
            {"name": "game", "screen": "status", "when": {"game": True}},
            {"name": "music", "screen": "media", "when": {"playing": True}},
            {"name": "afk", "screen": "clock",
             "when": {"idle": True, "playing": False}},
            {"name": "working", "screen": "app", "when": {"active": True}},
        ],
    },
    # Countdowns the display can show and a key can start. A preset is a
    # length of time a binding can name instead of spelling out, as in
    # {"type": "timer", "preset": "pomodoro"} on a G-key.
    "timers": {
        "enabled": True,
        "presets": {"quick": "5m", "short": "10m", "pomodoro": "25m",
                    "hour": "1h"},
        # How long each one holds the display when it ends, or when it
        # starts; 0 shows nothing and just rings.
        "show_seconds": 10.0,
        "ack_seconds": 2.5,
        "sound": "Glass",
    },
    # Chat from apps that never hand anything to Notification Center - see
    # notify.py. Only the channel left open in the app can be read, which is
    # why the app has to be running at all.
    "notifications": {
        "enabled": True,
        # Bundle ids or app names. Discord and Vesktop to start with.
        "apps": ["dev.vencord.vesktop", "com.hnc.Discord", "com.hnc.DiscordPTB",
                 "com.hnc.DiscordCanary", "Discord", "Vesktop"],
        "poll_seconds": 2.0,
        "show_seconds": 10.0,
        "sound": "Glass",
        # What every *other* app says through macOS, read back off the store
        # macOS keeps. It commits in a batch a few seconds after the most
        # recent notification, so these arrive late but never in pieces.
        "banners": True,
        "banners_seconds": 10.0,
    },
    # The Discord screen (P6): a Vencord plugin inside Vesktop talks to the
    # agent over localhost, and this is where that server listens. The port
    # has to equal HOST in `vencord/g510/index.ts` - the plugin has no way
    # to read this file.
    "vencord": {
        "enabled": True,
        "port": 47832,
    },
    # Causes the user writes themselves - see triggers.py. A rule with no
    # `seconds` holds its screen while its conditions hold (it is tried
    # before the autoscreen rules above); one with `seconds` fires once,
    # when its conditions come true, and shows for that long. Conditions
    # are autoscreen's own: app, playing, idle, fullscreen, running,
    # command, ... `g510 trigger fire <name>` fires one from a shell.
    "triggers": {
        "enabled": True,
        "check_seconds": 2.0,
        "rules": [],
    },
    # The keys around the display. "next"/"prev" walk the cycle above,
    # "screen:<name>" jumps straight to one, "off" blanks the display.
    "lcd_keys": {
        "L1": "next",
        "L2": "screen:status",
        "L3": "screen:clock",
        "L4": "screen:claude",
        "L5": "screen:media",
    },
    # A Creality printer on the LAN. Its WebSocket's first frame is a full
    # status snapshot, so nothing is held open.
    "printer": {
        "enabled": False,
        "host": "",
        "port": 9999,
        "refresh_seconds": 5.0,
        # auto tries Creality's WebSocket, then Moonraker, then OctoPrint.
        "kind": "auto",
        "api_key": "",
    },
    # Which usage windows the Claude screen shows, top to bottom. Any two of
    # five_hour, seven_day, context.
    "claude": {
        "rows": ["five_hour", "seven_day"],
        "stale_after_seconds": 600,
    },
    # Each runs a command that prints one number, shown as a labelled bar.
    # For anything this tool does not know how to read: another assistant's
    # quota, a GPU temperature, a build queue.
    "gauges": [],
    "app_colors": {},
    "app_profiles": {},
    "macros": {},
    "active_bank": "1",
    # macOS assumes this keyboard is ANSI because it declares no locale, which
    # swaps ^/° with <>| on an ISO board. See control.iso_swap.
    "iso_keyboard": False,
    # macOS drives Caps Lock and leaves the other two lock LEDs dark, so they
    # are free. Each can be: off, printing, claude, recording.
    "indicators": {"numlock": "off", "scrolllock": "off"},
    # The joystick switch on the top left is Logitech's game-mode switch. It
    # reports over USB, so it can drive anything: "bank:N", "screen:<name>",
    # "color:<colour>", or a binding object.
    "game_switch": {"on": "bank:3", "off": "bank:1"},
    "bindings": {
        "G1": {"type": "app", "name": "Safari"},
        "G2": {"type": "app", "name": "Terminal"},
        "G3": {"type": "keys", "keys": "cmd+shift+4"},
        "G4": {"type": "keys", "keys": "cmd+space"},
    },
}


def _merge(base, override):
    """Recursive dict merge, so a partial config file still gets all defaults.

    The base is deep-copied: callers mutate nested dicts like `lcd` and
    `macros` in place, and sharing them with DEFAULTS would quietly stop the
    defaults being the defaults for the rest of the process.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


BANKS = ("1", "2", "3")


def _migrate(settings, raw=None):
    """Move a flat `bindings` set into bank 1, the first time banks are used.

    The M1/M2/M3 keys pick between three independent sets of G-key bindings,
    the way Logitech's own software did. `bindings` stays as a live view of the
    active bank so older config files and callers keep working.
    """
    banks = settings.get("banks")
    if not isinstance(banks, dict):
        banks = {}
    for name in BANKS:
        if not isinstance(banks.get(name), dict):
            banks[name] = {}
    # Only a file that predates banks gets its flat `bindings` migrated. After
    # _merge has run, DEFAULTS' bindings are indistinguishable from the user's
    # own - so the decision is made on the raw file contents, or this quietly
    # restores the factory bindings every time bank 1 is emptied.
    legacy = raw is None or ("bindings" in raw and "banks" not in raw)
    if legacy and settings.get("bindings") and not banks["1"]:
        banks["1"] = dict(settings["bindings"])
    settings["banks"] = banks
    active = str(settings.get("active_bank", "1"))
    settings["active_bank"] = active if active in BANKS else "1"
    settings["bindings"] = banks[settings["active_bank"]]
    return settings


# Set when the config on disk could not be parsed, so callers can say so
# instead of pretending the defaults are what the user asked for.
last_error = None
_last_good = None


def load():
    """Read the config, surviving a broken file.

    The config is meant to be hand-edited, and a half-saved file should not
    take down a long-running agent or the GUI. A parse failure falls back to
    the last good config, or the defaults, and is recorded in `last_error`.
    """
    global last_error, _last_good
    if not os.path.exists(CONFIG_PATH):
        last_error = None
        return _migrate(copy.deepcopy(DEFAULTS))
    try:
        with open(CONFIG_PATH) as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("the config must be a JSON object")
        settings = _migrate(_merge(DEFAULTS, raw), raw)
    except (json.JSONDecodeError, ValueError) as exc:
        last_error = f"{CONFIG_PATH} is not valid JSON ({exc})"
        return (copy.deepcopy(_last_good) if _last_good
                else _migrate(copy.deepcopy(DEFAULTS)))
    except OSError as exc:
        last_error = f"Could not read {CONFIG_PATH}: {exc}"
        return (copy.deepcopy(_last_good) if _last_good
                else _migrate(copy.deepcopy(DEFAULTS)))
    last_error = None
    _last_good = copy.deepcopy(settings)
    return settings


def set_bank(settings, name):
    """Switch the active bank, pointing `bindings` at it."""
    name = str(name)
    if name not in BANKS:
        raise ValueError(f"Bank must be one of {', '.join(BANKS)}")
    settings["active_bank"] = name
    settings["bindings"] = settings.setdefault("banks", {}).setdefault(name, {})
    return settings


def save(config, force=False):
    """Write the config out.

    Refuses while the file on disk is unparseable unless forced, so a
    half-finished hand edit is not silently clobbered by a background write.
    """
    if last_error and not force:
        return False
    # `bindings` is a live view of the active bank, rebuilt on every load.
    # Writing it as well would leave two copies to drift apart, so the bank
    # is the only thing persisted.
    payload = dict(config)
    banks = payload.get("banks")
    if isinstance(banks, dict):
        banks = dict(banks)
        banks[str(payload.get("active_bank", "1"))] = payload.get("bindings", {})
        payload["banks"] = banks
    payload.pop("bindings", None)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # A unique temp file per write: the agent, the CLI and the GUI all save
    # here, and a shared name would let two writers interleave into the same
    # file and defeat the atomic rename.
    handle_fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-")
    try:
        with os.fdopen(handle_fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def ensure_exists():
    """Write out the defaults the first time, so there is something to edit.

    Migrated first: save() persists banks and drops the derived `bindings`
    view, so writing raw DEFAULTS produces a starter file with no G-key
    section at all.
    """
    if not os.path.exists(CONFIG_PATH):
        save(_migrate(copy.deepcopy(DEFAULTS)))
        return True
    return False


# -- colour helpers --------------------------------------------------------

NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "yellow": (255, 255, 0),
    "orange": (255, 90, 0), "purple": (140, 0, 255), "pink": (255, 60, 140),
    "white": (255, 255, 255), "off": (0, 0, 0), "black": (0, 0, 0),
}


class ColorError(Exception):
    pass


def parse_color(text):
    """'#rrggbb' or a colour name -> (r, g, b)."""
    if text is None:
        raise ColorError("No colour given")
    key = str(text).strip().lower()
    if key in NAMED_COLORS:
        return NAMED_COLORS[key]
    hexpart = key.lstrip("#")
    if len(hexpart) == 3:
        hexpart = "".join(ch * 2 for ch in hexpart)
    if len(hexpart) != 6:
        raise ColorError(
            f"Bad colour {text!r}. Use #rrggbb or: {', '.join(sorted(NAMED_COLORS))}")
    try:
        return tuple(int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ColorError(f"Bad colour {text!r}. Use #rrggbb or a colour name")


def format_color(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def apply_brightness(rgb, brightness):
    """Scale a colour by a 0-100 brightness.

    The G510 has no separate brightness register - the backlight is just the
    RGB value - so brightness is a scale factor on the chosen colour.
    """
    try:
        level = int(brightness)
    except (TypeError, ValueError):
        level = 100
    factor = max(0, min(100, level)) / 100.0
    return tuple(int(round(channel * factor)) for channel in rgb)


def effective_color(settings):
    """The colour actually sent to the keyboard, brightness included."""
    rgb = parse_color(settings.get("backlight", "#ffffff"))
    return apply_brightness(rgb, settings.get("brightness", 100))
