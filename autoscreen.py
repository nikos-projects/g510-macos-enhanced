"""Switch the LCD to whatever the Mac is actually doing.

The display shows one screen at a time, and which one is worth showing
depends on what is happening: a game wants the status screen, music wants
the track, a machine left alone wants the clock, a working one wants to know
what it is working on. This reads those signals and picks a screen out of
the config.

The whole policy is config - which rules exist, in what order, and what each
one looks for:

    "autoscreen": {
      "enabled": true,
      "check_seconds": 2,     how often the signals are re-read
      "idle_seconds": 60,     how long with no input counts as idle
      "hold_seconds": 3,      a rule must win this long before it shows
      "manual_seconds": 600,  hands off for a while after you pick one yourself
      "fallback": "",         what to show when nothing matches; "" keeps
                              whatever is on screen
      "games": [],            app names or bundle ids that count as games
      "not_games": [],        ...and that do not, whatever they look like
      "rules": [
        {"name": "game",  "screen": "status", "when": {"game": true}},
        {"name": "music", "screen": "media",  "when": {"playing": true}},
        {"name": "afk",   "screen": "clock",  "when": {"idle": true,
                                                       "playing": false}},
        {"name": "busy",  "screen": "app",    "when": {"active": true}}
      ]
    }

Rules are tried in order and the first whose conditions all hold wins, so
order is priority. A rule's `when` is an AND; there is no OR inside one
rule, because two rules pointing at the same screen say that just as well.
A rule's screen may be `off`, which blanks the display.

Nothing here writes to the keyboard. The daemon asks `AutoScreen.update()`
for a screen and does whatever it says; every switch is a change to
`lcd.screen` in the config, which is also how a switch made by hand - the
CLI, the GUI, a soft key - gets noticed, and gets the agent off that screen
for `manual_seconds`.
"""
import fnmatch
import os
import plistlib
import subprocess

import refresh
import screens

IDLE_SECONDS = 60.0
CHECK_SECONDS = 2.0
HOLD_SECONDS = 3.0
MANUAL_SECONDS = 600.0

# Answers are kept for at most this many app bundles. A session sees a
# handful of apps in front; if a machine churns through more than that, the
# cache starts again rather than growing without bound.
MAX_APPS = 64

# Launchers and emulators, matched against the frontmost app's name and
# bundle id. Their windows are on screen for as long as a game is, and unlike
# a game's own bundle id - chosen by whoever wrote the game, and published
# nowhere - these are stable and known. Games themselves are named in
# `games` in the config, because guessing at them from here is exactly the
# guesswork this exists to avoid.
GAME_APPS = (
    "com.valvesoftware.steam", "steam",
    "com.epicgames.launcher", "epic games launcher",
    "com.gog.galaxy", "gog galaxy",
    "com.blizzard.battle-net", "battle.net",
    "org.dolphin.dolphin", "dolphin",
    "net.openemu.openemu", "openemu",
    "com.retroarch.retroarch", "retroarch",
    "com.mojang.*", "minecraft*",
)

# What each condition in a rule's `when` looks for. The CLI prints this, so
# `g510 autoscreen add` never has to repeat itself.
CONDITION_HELP = {
    "game": "true/false - the frontmost app counts (or not) as a game",
    "playing": "true/false - something is playing right now",
    "idle": "true/false, or a number of seconds - no input for that long",
    "active": "true/false, or a number of seconds - input within that long",
    "app": "the frontmost app: a name, a bundle id, or a glob",
    "not_app": "the frontmost app is none of these",
    "running": "one of these is running, frontmost or not",
    "not_running": "none of these is running",
    "fullscreen": "true/false - the frontmost app covers a whole display",
    "command": "a shell command; true when it exits 0",
}

# What an app declares about itself does not change while it runs, so the
# category is read off disk once per app bundle and kept.
_category_cache = {}


# -- signals ---------------------------------------------------------------

def app_info():
    """{'name', 'bundle', 'path'} for whatever is in front, or all None.

    NSWorkspace answers this without any permission, and fast enough to ask
    on every pass - unlike the screen readers, which need to be believed
    slowly.
    """
    empty = {"name": None, "bundle": None, "path": None}
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return empty
        url = app.bundleURL()
        return {"name": str(app.localizedName() or "") or None,
                "bundle": str(app.bundleIdentifier() or "") or None,
                "path": str(url.path() or "") if url else None}
    except Exception:
        return empty


def idle_seconds():
    """How long since the last key press or mouse movement.

    This asks the system rather than tapping into events, so it needs no
    permission and costs nothing to call often. When it cannot be asked at
    all the answer is 0 - reading as "busy" keeps the display on whatever a
    busy machine wants, where reading as "idle for ever" would pin it to the
    clock screen for the rest of the session.
    """
    try:
        import Quartz
        state = getattr(Quartz, "kCGEventSourceStateHIDSystemState", None)
        if state is None:
            state = Quartz.kCGEventSourceStateCombinedSessionState
        # The C constant is -1, but pyobjc takes the event type as an
        # unsigned int and refuses the negative, so ask for the same value
        # the way this binding spells it.
        any_event = getattr(Quartz, "kCGAnyInputEventType", 0xFFFFFFFF)
        return max(0.0, float(Quartz.CGEventSourceSecondsSinceLastEventType(
            state, any_event)))
    except Exception:
        return 0.0


def running_apps():
    """Names and bundle ids of everything running, for the `running` rules."""
    try:
        from AppKit import NSWorkspace
        found = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name, bundle = app.localizedName(), app.bundleIdentifier()
            if name:
                found.append(str(name))
            if bundle:
                found.append(str(bundle))
        return found
    except Exception:
        return []


def _covers(bounds, sizes):
    """Whether a window is at least as big as one of the displays.

    Only exactly-fullscreen windows get here: a window merely maximised
    stops below the menu bar, so it is a whole 24 points short and does not
    match. Pure, because the matching is the part worth being sure about -
    which windows exist is up to whatever machine this lands on.
    """
    width, height = bounds.get("Width", 0), bounds.get("Height", 0)
    return any(width >= screen_width and height >= screen_height
               for screen_width, screen_height in sizes)


def display_sizes():
    """(width, height) of every display, in the points windows are measured
    in - NSScreen and CGWindowList count from the same ruler."""
    try:
        from AppKit import NSScreen
        return [(float(screen.frame().size.width),
                 float(screen.frame().size.height))
                for screen in NSScreen.screens()]
    except Exception:
        return []


def fullscreen_apps():
    """Owners of a window that covers a whole display.

    Games and video are what run like this, and it is one of the few things
    about a window macOS still hands out with no Screen Recording
    permission: who owns it and how big it is, though not what is written
    in it. Only asked for when a rule wants it - see `fullscreen` below.
    """
    sizes = display_sizes()
    if not sizes:
        return []
    try:
        from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                            kCGWindowListExcludeDesktopElements,
                            kCGWindowListOptionOnScreenOnly)
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID)
    except Exception:
        return []
    found = []
    for window in windows or ():
        # Layer 0 is an ordinary window; the menu bar, the Dock and every
        # panel above it sit on higher layers and never cover a display.
        if window.get("kCGWindowLayer") != 0:
            continue
        if not _covers(window.get("kCGWindowBounds") or {}, sizes):
            continue
        owner = window.get("kCGWindowOwnerName")
        if owner and str(owner) not in found:
            found.append(str(owner))
    return found


def bundle_category(path):
    """What the app says about itself: LSApplicationCategoryType.

    Only apps that declare it answer, and games in particular often do not,
    so this is one vote rather than the verdict. Read off disk and cached by
    path, because it does not change while the app runs.
    """
    if not path:
        return ""
    cached = _category_cache.get(path)
    if cached is not None:
        return cached
    value = ""
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as handle:
            value = str(plistlib.load(handle).get("LSApplicationCategoryType") or "")
    except Exception:
        value = ""
    if len(_category_cache) >= MAX_APPS:
        _category_cache.clear()
    _category_cache[path] = value
    return value


def classify(app, block):
    """(is this a game, why) for a frontmost app.

    The order is certainty: what the user wrote down first, then what is
    known outright, then what the app declares about itself. Only the user's
    own two lists can overrule the rest - `not_games` beats everything,
    `games` beats the built-in list and the category.
    """
    values = (app.get("name"), app.get("bundle"))
    if _hits((block or {}).get("not_games"), values):
        return False, "not_games"
    if _hits((block or {}).get("games"), values):
        return True, "games"
    if _hits(GAME_APPS, values):
        return True, "built-in"
    if "games" in bundle_category(app.get("path")).lower():
        return True, "category"
    return False, ""


def _hits(wanted, values):
    """Whether any pattern matches any value, case-insensitively.

    Patterns are globs, so "com.mojang.*" is a bundle id and "Minecraft*"
    is an app name, and one list can hold both.
    """
    patterns = _patterns(wanted)
    for pattern in patterns:
        for value in values:
            if value and fnmatch.fnmatch(str(value).lower(), pattern.lower()):
                return True
    return False


def _patterns(wanted):
    """A condition's value as a list of patterns: 'Safari' or ['a', 'b']."""
    if wanted is None:
        return []
    if isinstance(wanted, str):
        return [wanted]
    if isinstance(wanted, (list, tuple)):
        return [str(item) for item in wanted]
    return [str(wanted)]


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _run_predicate(command):
    """A `command` condition is true when it exits 0, the way a shell test
    reads - `pgrep -x Music` says everything there is to say already."""
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


# -- conditions ------------------------------------------------------------

def _when_game(wanted, signals, _block):
    return _as_bool(wanted) == bool(signals.get("game"))


def _when_playing(wanted, signals, _block):
    return _as_bool(wanted) == bool(signals.get("playing"))


def _parse_idle(wanted, block):
    """(seconds, sense) for an idle/active condition.

    `true` means the configured window, `false` the other side of it, and a
    number is a window of your own; the sense says which side of that window
    counts as matching.
    """
    if isinstance(wanted, str) and wanted.strip().lower() in (
            "true", "false", "yes", "no", "on", "off"):
        wanted = _as_bool(wanted)
    if wanted is True:
        return float(block.get("idle_seconds", IDLE_SECONDS)), True
    if wanted is False:
        return float(block.get("idle_seconds", IDLE_SECONDS)), False
    return float(wanted), True


def _when_idle(wanted, signals, block):
    seconds, sense = _parse_idle(wanted, block)
    idle = float(signals.get("idle") or 0.0)
    return (idle >= seconds) if sense else (idle < seconds)


def _when_active(wanted, signals, block):
    seconds, sense = _parse_idle(wanted, block)
    idle = float(signals.get("idle") or 0.0)
    return (idle < seconds) if sense else (idle >= seconds)


def _when_app(wanted, signals, _block):
    return _hits(wanted, (signals.get("app"), signals.get("bundle")))


def _when_not_app(wanted, signals, _block):
    return not _hits(wanted, (signals.get("app"), signals.get("bundle")))


def _when_running(wanted, signals, _block):
    return _hits(wanted, signals.get("running") or ())


def _when_not_running(wanted, signals, _block):
    return not _hits(wanted, signals.get("running") or ())


def _when_fullscreen(wanted, signals, _block):
    return _as_bool(wanted) == bool(signals.get("fullscreen"))


def _when_command(wanted, signals, _block):
    return bool((signals.get("commands") or {}).get(str(wanted)))


CONDITIONS = {
    "game": _when_game,
    "playing": _when_playing,
    "idle": _when_idle,
    "active": _when_active,
    "app": _when_app,
    "not_app": _when_not_app,
    "running": _when_running,
    "not_running": _when_not_running,
    "fullscreen": _when_fullscreen,
    "command": _when_command,
}


def matches(when, signals, block):
    """Whether every condition in one rule's `when` holds.

    A condition this build does not recognise never matches, rather than
    matching always: the config is hand-edited, and a typo that quietly made
    a rule apply to everything would be the worse surprise.
    """
    if when is None:
        return True
    if not isinstance(when, dict):
        return False
    for key, wanted in when.items():
        handler = CONDITIONS.get(key)
        if handler is None:
            return False
        try:
            if not handler(wanted, signals, block):
                return False
        except (TypeError, ValueError):
            return False        # a value of the wrong shape never matches
    return True


def evaluate(rules, signals, block):
    """The first rule whose conditions hold - order is priority - or None."""
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        if matches(rule.get("when"), signals, block):
            return rule
    return None


def is_screen(name):
    """A screen the display can actually show. `off` blanks it."""
    return name == "off" or name in screens.SCREENS


def describe(rule):
    """A rule as one word for a log line."""
    if not isinstance(rule, dict):
        return "?"
    return str(rule.get("name") or rule.get("screen") or "?")


# -- reading ---------------------------------------------------------------

class Reader:
    """Turns the system into the dict the conditions read.

    The slow parts - media, the window list, the command conditions - are
    asked for rarely or off-thread, so `read` itself is safe to call from
    the agent's polling loop. The hooks exist so a test can stand in for
    macOS.
    """

    def __init__(self, frontmost=app_info, idle=idle_seconds,
                 running=running_apps, fullscreen=None, media=None):
        self.frontmost = frontmost
        self.idle = idle
        self.running = running
        self.fullscreen = fullscreen or fullscreen_apps
        self.media = media
        self._commands = {}

    def read(self, block, rules=None, wait=False):
        """The signals as the conditions see them, right now."""
        block = block or {}
        app = self.frontmost() or {}
        playing, track = self._playing(wait)
        game, why = classify(app, block)
        return {
            "app": app.get("name"),
            "bundle": app.get("bundle"),
            "path": app.get("path"),
            "idle": float(self.idle() or 0.0),
            "playing": playing,
            "track": track,
            "running": list(self.running() or ()),
            "fullscreen": self._fullscreen(app, rules),
            "game": game,
            "game_reason": why,
            "commands": self._commands_for(block, rules),
        }

    def _fullscreen(self, app, rules):
        """Whether the frontmost app covers a whole display.

        The window list is the most expensive thing read here, so it is only
        walked over when a rule asks: a config that never says `fullscreen`
        never pays for looking.
        """
        name = app.get("name")
        if not name:
            return False
        for rule in rules or ():
            when = rule.get("when") if isinstance(rule, dict) else None
            if isinstance(when, dict) and "fullscreen" in when:
                return str(name) in self.fullscreen()
        return False

    def _playing(self, wait):
        if self.media is not None:
            track = self.media(wait) if wait else self.media()
            return bool(track and track.get("playing")), track
        track = screens.now_playing(wait=wait)
        return bool(track and track.get("playing")), track

    def _commands_for(self, block, rules):
        """Run each `command` condition the rules use, off-thread.

        Only the ones that appear: a config with no command rules runs no
        shells at all, and one that does asks at the same pace as everything
        else rather than on every pass.
        """
        wanted = []
        for rule in rules or ():
            when = rule.get("when") if isinstance(rule, dict) else None
            if isinstance(when, dict) and isinstance(when.get("command"), str):
                wanted.append(when["command"])
        answers = {}
        for command in dict.fromkeys(wanted):
            answers[command] = bool(self._command(command, block).get())
        return answers

    def _command(self, command, block):
        source = self._commands.get(command)
        if source is None:
            interval = float(block.get("check_seconds", CHECK_SECONDS))
            source = refresh.Background(
                lambda c=command: (_run_predicate(c), None), interval)
            self._commands[command] = source
        return source


# -- the switch ------------------------------------------------------------

class AutoScreen:
    """Decides when the screen should change, and to what.

    It holds three pieces of state, and all three are about not fighting
    whoever else is driving the display:

    `chosen`   the screen this put up last. Anything else in `lcd.screen`
               was written by someone else - a soft key, the CLI, the GUI -
               and starts the hands-off window.
    `pending`  a screen that has won but has not been on long enough yet, so
               a signal that flickers cannot make the display flicker with it.
    `until`    when the hands-off window ends.
    `frozen`   somebody asked for the screen to stay where it is (double
               press of a soft key, or `g510 autoscreen freeze`). Unlike the
               hands-off window it has no clock: it ends when it is asked
               for again, and it is runtime state - the agent forgetting it
               on a restart is the intended behaviour, not a leak.
    """

    def __init__(self, reader=None):
        self.reader = reader or Reader()
        self.chosen = None
        self.pending = None
        self.until = 0.0
        self.last_read = 0.0
        self.signals = None
        self.rule = None
        self.frozen = False

    def rest(self):
        """Forget where we were - the display went blank, or autoscreen
        stopped, so the next look takes the config as it finds it.

        A freeze is not forgotten: it is a decision somebody made about
        where the screen should sit, not a position being tracked.
        """
        self.chosen = None
        self.pending = None

    def freeze(self, on=True):
        """Hold the screen where it is, or let the rules have it back."""
        self.frozen = bool(on)
        return self.frozen

    def toggle_freeze(self):
        """Double press: the opposite of whatever it is doing now."""
        return self.freeze(not self.frozen)

    def update(self, now, settings, current):
        """The screen to switch to, or None to leave it alone.

        `current` is what `lcd.screen` says; `settings` the whole config.
        """
        block = settings.get("autoscreen") or {}
        if not block.get("enabled", True):
            self.rest()
            return None
        if self.frozen:
            # Held where it is. The screen somebody else picks is still
            # tracked, so letting go carries on from there rather than
            # jumping somewhere the rules chose while nobody was looking.
            self.chosen = current
            self.pending = None
            return None
        if self.chosen is None:
            self.chosen = current
            return None
        if now < self.until:
            self.chosen = current
            self.pending = None
            return None
        if current != self.chosen:
            # Someone picked a screen by hand. Out of their way for a while.
            self.until = now + float(block.get("manual_seconds", MANUAL_SECONDS))
            self.chosen = current
            self.pending = None
            return None
        check = float(block.get("check_seconds", CHECK_SECONDS))
        if now - self.last_read < check:
            return None
        self.last_read = now

        rules = block.get("rules") or []
        self.signals = self.reader.read(block, rules)
        rule = evaluate(rules, self.signals, block)
        wanted = (rule or {}).get("screen") or block.get("fallback") or None
        self.rule = describe(rule) if rule else ("fallback" if wanted else None)
        if not wanted or not is_screen(wanted) or wanted == current:
            self.pending = None
            return None

        hold = float((rule or {}).get("hold_seconds",
                                      block.get("hold_seconds", HOLD_SECONDS)))
        if self.pending is None or self.pending[0] != wanted:
            self.pending = (wanted, now)
            if hold > 0:
                return None
        elif now - self.pending[1] < hold:
            return None
        self.pending = None
        self.chosen = wanted
        return wanted


def preview(settings):
    """(signals, rule, screen) for what the rules would pick right now.

    One-shot, for the CLI: no hold, no hands-off window, just the answer.
    """
    block = settings.get("autoscreen") or {}
    rules = block.get("rules") or []
    signals = Reader().read(block, rules, wait=True)
    rule = evaluate(rules, signals, block)
    wanted = (rule or {}).get("screen") or block.get("fallback") or None
    if wanted and not is_screen(wanted):
        wanted = None
    return signals, rule, wanted
