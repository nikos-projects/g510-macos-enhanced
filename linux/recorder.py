"""Record a sequence of keystrokes to replay from a G-key.

This watches the keyboard while recording, which is the whole point of a macro
recorder, but it is worth being precise about the scope: it reads one device -
the G510's own keyboard node - only for as long as a recording is explicitly
in progress, and the only thing kept is the chord sequence the user asked to
record. Nothing is logged, printed or sent anywhere; the captured chords leave
this module through the returned step list and the on_step callback, and by no
other route.

The macOS build used a listen-only CGEventTap. The equivalent here is reading
the evdev node directly, with one deliberate difference: the device is grabbed
for the duration (EVIOCGRAB), so the keys being recorded do not also land in
whatever window happens to be focused.
"""
import os
import select
import time

import actions
from actions import KEY_CODES

try:
    import evdev
    _EVDEV_ERROR = None
except Exception as _exc:
    evdev = None
    _EVDEV_ERROR = f"{type(_exc).__name__}: {_exc}"

# code -> name, preferring the first spelling in KEY_CODES
CODE_NAMES = {}
for _name, _code in KEY_CODES.items():
    CODE_NAMES.setdefault(_code, _name)

ESCAPE = KEY_CODES["escape"]
MAX_STEPS = 64

# Both halves of each modifier report the same flag: a chord says "shift",
# not "the right-hand shift". KEY_RIGHTALT is AltGr on most layouts, which
# is close enough to alt for a macro to replay.
MODIFIER_CODES = {
    29: actions.MOD_CTRL, 97: actions.MOD_CTRL,
    56: actions.MOD_ALT, 100: actions.MOD_ALT,
    42: actions.MOD_SHIFT, 54: actions.MOD_SHIFT,
    125: actions.MOD_SUPER, 126: actions.MOD_SUPER,
    464: actions.MOD_FN,
}

# Order matters: this is how a chord is spelled back to the user. fn is left
# out on purpose - carrying it into a chord would make keys that report it
# unreplayable, and actions.parse_chord spells super as "cmd".
MODIFIER_ORDER = [
    ("ctrl", actions.MOD_CTRL),
    ("alt", actions.MOD_ALT),
    ("shift", actions.MOD_SHIFT),
    ("cmd", actions.MOD_SUPER),
]

EV_KEY = 0x01
POLL = 0.25


class RecordingError(Exception):
    pass


def describe_event(keycode, flags):
    """An evdev key code plus flags as a chord string, or None if unmappable.

    Every string this produces has to parse back through
    actions.parse_chord() to the same (keycode, flags) pair, because that is
    literally what playback does with it.
    """
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
        self.device = None
        self.flags = 0
        # The wake pipe exists before run() does any work, so a stop()
        # arriving in between is not a silent no-op that leaves the device
        # grabbed for the full timeout after the user cancelled.
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)

    def run(self, timeout=120.0):
        """Block until Escape, the step limit, or the timeout. Returns steps."""
        if evdev is None:
            raise RecordingError(
                "python3-evdev is not installed, so the keyboard cannot be "
                f"read ({_EVDEV_ERROR}) - install it with: "
                "sudo apt install python3-evdev")
        node = actions.input_node()
        if node is None:
            raise RecordingError(
                f"No {actions.KEYBOARD_NAME!r} input device found - is the "
                "keyboard plugged in?")
        try:
            self.device = evdev.InputDevice(node)
        except OSError as exc:
            raise RecordingError(
                f"Could not read {node} ({exc.strerror or exc}) - "
                "run: g510 permissions")
        try:
            # Grabbed so the recorded keys do not also reach the focused app.
            self.device.grab()
        except OSError as exc:
            self._close()
            raise RecordingError(
                f"Could not take the keyboard ({exc.strerror or exc}) - "
                "another recorder may already be running")
        try:
            self._loop(timeout)
        finally:
            self._close()
        return self.steps

    def _loop(self, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                ready, _, _ = select.select(
                    [self.device.fileno(), self._wake_r], [], [],
                    min(remaining, POLL))
            except (OSError, ValueError):
                return                      # device or pipe went away
            if self._wake_r in ready:
                return                      # stop() from another thread
            if not ready:
                continue
            try:
                events = list(self.device.read())
            except BlockingIOError:
                continue
            except OSError:
                return                      # keyboard unplugged mid-recording
            for event in events:
                if self._handle(event):
                    return

    def _handle(self, event):
        """Fold one event in. True means the recording is finished."""
        if event.type != EV_KEY:
            return False
        if event.code in MODIFIER_CODES:
            if event.value == 1:
                self.flags |= MODIFIER_CODES[event.code]
            elif event.value == 0:
                self.flags &= ~MODIFIER_CODES[event.code]
            return False
        if event.value != 1:                # ignore release and auto-repeat
            return False
        if event.code == ESCAPE:
            return True
        chord = describe_event(event.code, self.flags)
        if not chord:
            return False
        now = time.time()
        delay = 0.0 if self.last_time is None else round(now - self.last_time, 2)
        self.last_time = now
        step = {"keys": chord}
        if delay > 0.05:
            step["delay"] = min(delay, 2.0)
        self.steps.append(step)
        if self.on_step:
            self.on_step(chord, len(self.steps))
        return len(self.steps) >= MAX_STEPS

    def _close(self):
        """Ungrab, close the node, and drop the wake pipe. Idempotent."""
        device, self.device = self.device, None
        if device is not None:
            try:
                device.ungrab()
            except (OSError, RuntimeError):
                pass
            try:
                device.close()
            except Exception:
                pass
        for name in ("_wake_r", "_wake_w"):
            handle = getattr(self, name, None)
            if handle is not None:
                setattr(self, name, None)
                try:
                    os.close(handle)
                except OSError:
                    pass

    def stop(self):
        """Finish the recording from another thread (the MR key)."""
        handle = self._wake_w
        if handle is None:
            return
        try:
            os.write(handle, b"x")
        except OSError:
            pass                            # already finished


def record(on_step=None, timeout=120.0):
    return Recorder(on_step=on_step).run(timeout=timeout)


def summarise(steps, limit=4):
    """'cmd+c, cmd+tab, cmd+v' for showing a macro in a list."""
    chords = [step.get("keys", "?") for step in steps]
    if len(chords) <= limit:
        return ", ".join(chords)
    return ", ".join(chords[:limit]) + f", +{len(chords) - limit} more"
