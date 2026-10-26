"""Timers the display counts down, and the keys that start them.

A running timer lives here rather than in the config file: it starts when a
key is pressed and ends when it ends, and neither is worth writing to disk.
What each preset *means* - which duration a key stands for - is config,
because that is the part somebody wants to edit.
"""
import re
import threading
import time

# 25m, 1h30m, 90s, 1h05m30s, and a bare number, which is seconds: "10" is ten
# seconds, and asking for ten minutes is "10m".
_PARTS = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.I)
_WHOLE = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0}


class DurationError(ValueError):
    pass


def parse_duration(value):
    """'25m' -> 1500. A plain number is seconds, because that is what it is."""
    if isinstance(value, bool):
        raise DurationError(f"{value!r} is not a length of time")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            raise DurationError("a timer has to be longer than nothing")
        return seconds
    text = str(value or "").strip().lower()
    if not text:
        raise DurationError("no length of time given")
    if _WHOLE.match(text):
        seconds = float(text.rstrip())
        if seconds <= 0:
            raise DurationError("a timer has to be longer than nothing")
        return seconds
    seconds, used = 0.0, 0
    for number, unit in _PARTS.findall(text):
        seconds += float(number) * _UNITS[unit]
        used += len(number) + 1
    if not used or used != len(re.sub(r"\s+", "", text)):
        raise DurationError(
            f"{value!r} is not a length of time - try 90s, 5m, 1h30m")
    if seconds <= 0:
        raise DurationError("a timer has to be longer than nothing")
    return seconds


def left(seconds):
    """1720 -> '28:40', 7200 -> '1:00:00' - what a countdown shows."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def resolve(value, presets=None):
    """A duration from a preset name, a length of time, or a plain number.

    A preset is tried first so a binding can read `{"preset": "pomodoro"}`
    and `{"seconds": "pomodoro"}` both - the second is what `g510 bind G5
    timer pomodoro` writes, and it should not matter which way round.
    """
    presets = presets or {}
    if isinstance(value, dict):
        return duration_for(value, presets)
    if not isinstance(value, str):
        return parse_duration(value)
    key = value.strip()
    if key in presets:
        return parse_duration(presets[key])
    return parse_duration(key)


def duration_for(binding, presets=None):
    """How long a `{"type": "timer", ...}` binding asks for."""
    presets = presets or {}
    if binding.get("seconds") is not None:
        return resolve(binding.get("seconds"), presets)
    if binding.get("preset") is not None:
        preset = str(binding.get("preset"))
        if preset not in presets:
            known = ", ".join(sorted(presets)) or "none are set up yet"
            raise DurationError(
                f"no timer preset called {preset!r} - known ones: {known}")
        return parse_duration(presets[preset])
    raise DurationError("a timer binding needs a seconds or a preset")


def describe_seconds(seconds):
    """What a binding asked for, in the form it was written."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    return left(seconds)


class Store:
    """The timers that are running, for whoever asks first.

    Every method takes the moment it is being asked about rather than
    reading the clock itself, so a test can run a timer without waiting for
    it. Access is locked: the agent's loop expires them while the control
    socket starts and cancels them.
    """

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self._items = []
        self._next = 1
        self._lock = threading.Lock()

    def start(self, seconds, label="", name="", now=None):
        """Add a timer and return it."""
        seconds = float(seconds)
        if seconds <= 0:
            raise DurationError("a timer has to be longer than nothing")
        moment = self.clock() if now is None else now
        with self._lock:
            item = {"id": self._next, "label": str(label or ""),
                    "name": str(name or ""), "seconds": seconds,
                    "started": moment, "until": moment + seconds}
            self._next += 1
            self._items.append(item)
            return dict(item)

    def running(self, now=None):
        """What is still going, nearest to finishing first."""
        moment = self.clock() if now is None else now
        with self._lock:
            items = [dict(item, left=max(0.0, item["until"] - moment))
                     for item in self._items if item["until"] > moment]
        items.sort(key=lambda item: item["until"])
        return items

    def due(self, now=None):
        """Everything that has run out, taken off the list as it is handed
        over - so a timer can only ever go off once."""
        moment = self.clock() if now is None else now
        with self._lock:
            ended = [item for item in self._items if item["until"] <= moment]
            self._items = [item for item in self._items if item["until"] > moment]
            return ended

    def cancel(self, target):
        """Forget a timer by position, id, label or 'all'. What was dropped.

        Position counts in the same order `running` reports, which is the
        order they are about to end in - the number printed next to a timer
        is the number that takes it away.
        """
        with self._lock:
            if str(target).lower() in ("all", "*"):
                gone, self._items = list(self._items), []
                return gone
            wanted = str(target).strip()
            if not wanted:
                return []
            ordered = sorted(self._items, key=lambda item: item["until"])
            for position, item in enumerate(ordered, 1):
                names = (str(position), str(item["id"]), item["label"],
                         item["name"])
                if wanted in names:
                    self._items = [other for other in self._items
                                   if other is not item]
                    return [item]
            return []

    def clear(self):
        with self._lock:
            self._items = []


# The agent's own timers. A module-level store means the countdown screen can
# read it without the daemon having to hand itself around, and a restart
# starting empty is the right answer anyway: a timer that outlived the agent
# could not have shown anything at the end of it.
STORE = Store()
