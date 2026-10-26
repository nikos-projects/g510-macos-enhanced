"""Causes the user writes themselves: a screen for something that happens.

Autoscreen answers "what is the Mac doing", with four conditions the build
chose. A trigger is the same idea with the conditions left open - the user
says what they care about, and how the display should answer:

    "triggers": {
      "enabled": true,
      "check_seconds": 2,      how often the conditions are re-read
      "rules": [
        {"name": "afk",    "screen": "clock", "when": {"idle": 300}},
        {"name": "build",  "screen": "status", "seconds": 8,
         "when": {"command": "test -f /tmp/build.ok"}},
        {"name": "raid",   "screen": "status", "seconds": 0,
         "when": {"app": "World of Warcraft*"}}
      ]
    }

Two shapes, told apart by `seconds`:

  * **no seconds** - a state rule. It holds its screen for as long as it is
    true, exactly like an autoscreen rule, and so it is one: the rules are
    handed to autoscreen rather than to a second policy engine, which keeps
    the hands-off window, the hold and the fallback all meaning one thing.

  * **seconds** - an event. It fires once, when its condition *becomes* true,
    and puts its screen up for that long before the display goes back to
    whatever was underneath. The first look only arms it, so restarting the
    agent with the condition already true fires nothing. `g510 trigger fire
    <name>` fires one by hand, from a shell script or a cron job, and that
    works whether the rules are watching or not.

Conditions are autoscreen's own - app, playing, idle, fullscreen, running,
command, ... - so both halves of the config read the same way.
"""
import autoscreen

DEFAULT_SECONDS = 10.0        # how long a `fire` on a state rule shows for
CHECK_SECONDS = 2.0


def _rules(block):
    """Every rule in the block that could be obeyed at all.

    Hand-edited config is the input, so a rule naming a screen that does not
    exist, or a `when` that is not a set of conditions, is dropped rather
    than argued with - the rest of the block still works.
    """
    found = []
    for rule in (block or {}).get("rules") or ():
        if not isinstance(rule, dict):
            continue
        if not autoscreen.is_screen(str(rule.get("screen") or "")):
            continue
        when = rule.get("when")
        if when is not None and not isinstance(when, dict):
            continue
        found.append(rule)
    return found


def _is_event(rule):
    try:
        return float(rule.get("seconds") or 0) > 0
    except (TypeError, ValueError):
        return False


def state_rules(settings):
    """The rules that hold a screen, ready to be tried as autoscreen's own.

    Checked before autoscreen's rules, so a cause the user wrote by hand is
    not shadowed by a broad condition next to it.
    """
    block = settings.get("triggers") or {}
    if not block.get("enabled", True):
        return []
    return [rule for rule in _rules(block) if not _is_event(rule)]


class Triggers:
    """Watches the conditions of the event rules, and fires them once each.

    Holds one reader shared with autoscreen, so the frontmost app, the
    now-playing state and every `command` condition are asked for once and
    both halves of the policy see the same answer.
    """

    def __init__(self, reader=None):
        self.reader = reader or autoscreen.Reader()
        self.state = {}          # rule key -> what its condition said last
        self.last_read = 0.0
        self.signals = None

    @staticmethod
    def _key(rule, index):
        """What a rule is remembered by. A nameless rule keeps its place."""
        return str(rule.get("name") or f"#{index}")

    def events(self, settings):
        block = settings.get("triggers") or {}
        if not block.get("enabled", True):
            return []
        return [rule for rule in _rules(block) if _is_event(rule)]

    def update(self, now, settings):
        """Re-read the conditions; return the rules that just rose.

        Rising is the point: a condition that is already true when the agent
        starts is remembered rather than fired, so a flag file that has been
        there for a week does not light the display up on every restart.
        """
        block = settings.get("triggers") or {}
        rules = self.events(settings)
        if not block.get("enabled", True):
            # Watched nothing: the next look arms from scratch.
            self.state = {}
            return []
        if not rules:
            self.state = {}
            return []
        check = float(block.get("check_seconds", CHECK_SECONDS) or 0)
        if now - self.last_read < check:
            return []
        self.last_read = now
        signals = self.reader.read(block, rules)
        self.signals = signals

        fired, seen = [], set()
        for index, rule in enumerate(rules):
            key = self._key(rule, index)
            seen.add(key)
            truth = autoscreen.matches(rule.get("when"), signals, block)
            was = self.state.get(key)          # None: never looked before
            self.state[key] = truth
            if truth and was is False:
                fired.append(self._fire(rule, block))
        for key in list(self.state):
            if key not in seen:
                del self.state[key]            # the rule has been removed
        return fired

    def fire(self, settings, name):
        """The entry for one named rule, whatever it looks like, or KeyError.

        Deliberately not gated on `enabled`: a rule switched off is still a
        rule, and asking for it by name is as explicit as it gets.
        """
        block = settings.get("triggers") or {}
        for rule in _rules(block):
            if str(rule.get("name") or "") == str(name):
                return self._fire(rule, block)
        raise KeyError(str(name))

    @staticmethod
    def _fire(rule, block):
        """One rule as the thing the display is asked to show."""
        try:
            seconds = float(rule.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds <= 0:
            seconds = DEFAULT_SECONDS     # a state rule, fired by hand
        data = dict(rule.get("data") or {})
        data["name"] = str(rule.get("name") or "")
        return {"name": data["name"],
                "screen": str(rule.get("screen") or ""),
                "seconds": seconds,
                "sound": rule.get("sound") or block.get("sound") or "",
                "data": data}
