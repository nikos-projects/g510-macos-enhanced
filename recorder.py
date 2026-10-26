"""Record a sequence of keystrokes to replay from a G-key.

This watches the keyboard while recording, which is the whole point of a macro
recorder, but it is worth being precise about the scope: the tap is listen-only,
it runs only for as long as a recording is explicitly in progress, and the only
thing kept is the chord sequence the user asked to record. Nothing is logged.
"""
import time

import Quartz

from actions import KEY_CODES

# code -> name, preferring the first spelling in KEY_CODES
CODE_NAMES = {}
for _name, _code in KEY_CODES.items():
    CODE_NAMES.setdefault(_code, _name)

ESCAPE = 53
MAX_STEPS = 64

# Order matters: this is how a chord is spelled back to the user.
MODIFIER_ORDER = [
    ("ctrl", Quartz.kCGEventFlagMaskControl),
    ("alt", Quartz.kCGEventFlagMaskAlternate),
    ("shift", Quartz.kCGEventFlagMaskShift),
    ("cmd", Quartz.kCGEventFlagMaskCommand),
]


class RecordingError(Exception):
    pass


def describe_event(keycode, flags):
    """A CGEvent keycode plus flags as a chord string, or None if unmappable."""
    name = CODE_NAMES.get(keycode)
    if name is None:
        return None
    parts = [label for label, mask in MODIFIER_ORDER if flags & mask]
    parts.append(name)
    return "+".join(parts)


class Recorder:
    """Collects chords until Escape is pressed or the limit is reached."""

    def __init__(self, on_step=None):
        self.steps = []
        self.last_time = None
        self.on_step = on_step
        self.tap = None
        self.loop = None

    def _callback(self, _proxy, event_type, event, _refcon):
        if event_type != Quartz.kCGEventKeyDown:
            return event
        keycode = int(Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode))
        if keycode == ESCAPE:
            Quartz.CFRunLoopStop(Quartz.CFRunLoopGetCurrent())
            return event
        flags = Quartz.CGEventGetFlags(event)
        chord = describe_event(keycode, flags)
        if chord:
            now = time.time()
            delay = 0.0 if self.last_time is None else round(now - self.last_time, 2)
            self.last_time = now
            step = {"keys": chord}
            if delay > 0.05:
                step["delay"] = min(delay, 2.0)
            self.steps.append(step)
            if self.on_step:
                self.on_step(chord, len(self.steps))
            if len(self.steps) >= MAX_STEPS:
                Quartz.CFRunLoopStop(Quartz.CFRunLoopGetCurrent())
        return event

    def run(self, timeout=120.0):
        """Block until Escape, the step limit, or the timeout. Returns steps."""
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        # Publish the loop before the tap exists: a stop() arriving in between
        # would otherwise be a silent no-op, leaving the tap listening for the
        # full timeout after the user cancelled.
        self.loop = Quartz.CFRunLoopGetCurrent()
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
        if not self.tap:
            raise RecordingError(
                "Could not watch the keyboard. Accessibility permission is "
                "needed - run: g510 permissions")
        source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        loop = self.loop
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, timeout, False)
        Quartz.CGEventTapEnable(self.tap, False)
        Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopCommonModes)
        return self.steps


    def stop(self):
        """Finish the recording from another thread (the MR key)."""
        if self.loop is not None:
            Quartz.CFRunLoopStop(self.loop)


def record(on_step=None, timeout=120.0):
    return Recorder(on_step=on_step).run(timeout=timeout)


def summarise(steps, limit=4):
    """'cmd+c, cmd+tab, cmd+v' for showing a macro in a list."""
    chords = [step.get("keys", "?") for step in steps]
    if len(chords) <= limit:
        return ", ".join(chords)
    return ", ".join(chords[:limit]) + f", +{len(chords) - limit} more"
