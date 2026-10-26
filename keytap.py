"""Arrow keys for the menu: a session event tap, on its own thread.

The panel has no arrow keys of its own, so the menu borrows the keyboard's.
A `kCGSessionEventTap` sees every keyDown and keyUp before any application
does, which is what makes it possible to swallow a key: the menu is modal
while it is open, and a key the menu takes never reaches the app underneath.

Three rules keep that from becoming a nuisance:

  * A key is only ever swallowed when `decide` says so. The daemon decides
    by asking two questions - is the menu open, is a text field being typed
    - so a letter typed while the menu is merely showing a list goes to the
    app as usual, and Cmd/Ctrl/Option chords are never touched at all.
  * Swallowing is decided on the keyDown and honoured on the matching
    keyUp, or an application sees half of every key.
  * macOS disables a tap that takes too long, and says so by delivering an
    event of its own. The callback re-enables itself on both, so a slow
    frame never leaves the keyboard dark.

Nothing here knows what a menu is: `feed` is a plain function over
(key, keycode, character, blocked) and is where the tests live.
"""
import collections
import threading

import Quartz
from CoreFoundation import (CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
                            CFRunLoopGetCurrent, CFRunLoopRun,
                            CFRunLoopStop, kCFRunLoopCommonModes)

# The keys that mean something to the menu, by macOS keycode.
UP, DOWN, LEFT, RIGHT = 126, 125, 123, 124
RETURN, PAD_ENTER, ESCAPE, BACKSPACE = 36, 76, 53, 51

KEY_NAMES = {UP: "up", DOWN: "down", LEFT: "left", RIGHT: "right",
             RETURN: "enter", PAD_ENTER: "enter", ESCAPE: "esc",
             BACKSPACE: "backspace"}

# What the menu takes while it is open: these always mean something to it.
NAV_KEYS = ("up", "down", "left", "right", "enter", "esc", "backspace")

# Holding any of these makes a key part of a shortcut (Cmd-C, Option-Tab),
# and shortcuts belong to the app, never to the menu.
MODIFIER_FLAGS = (Quartz.kCGEventFlagMaskCommand
                  | Quartz.kCGEventFlagMaskControl
                  | Quartz.kCGEventFlagMaskAlternate)

MASK = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))

_MAX_CHARACTERS = 4


def key_for(code, character):
    """(name, character) for a keycode and what it typed, or None.

    The name is one of NAV_KEYS for a key the menu navigates with, and the
    character itself for anything printable. A key that is neither - Tab,
    a function key, half a composed accent - is None, and passes through.
    """
    name = KEY_NAMES.get(code)
    if name:
        return (name, None)
    if character and len(character) == 1 and character.isprintable():
        return (character, character)
    return None


class KeyTap:
    """A tap that runs while `decide` wants keys, queueing what it takes.

    `decide(name, character)` is called from the tap's own thread and must
    be quick and safe to call there - the daemon keeps it to reading two
    flags. What it takes lands in `queue`, which the daemon drains from its
    main loop, so the menu itself is only ever touched by one thread.
    """

    def __init__(self, decide, log=None):
        self.decide = decide
        self.log = log or (lambda _message: None)
        self.queue = collections.deque()
        self.error = None
        self.tap = None
        self._swallowed = set()      # keycodes whose keyUp still has to go
        self._thread = None
        self._loop = None
        self._ready = threading.Event()

    # -- life cycle -----------------------------------------------------

    def start(self, timeout=5.0):
        """Start the tap. False (with `error`) when macOS will not allow it."""
        if self._thread is not None:
            return self.tap is not None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="g510-keytap")
        self._thread.start()
        self._ready.wait(timeout)
        if self.tap is None and not self.error:
            self.error = "the event tap did not come up"
        return self.tap is not None

    def stop(self):
        thread, self._thread = self._thread, None
        if thread is None:
            return
        loop = self._loop
        if loop is not None:
            CFRunLoopStop(loop)
        thread.join(timeout=2.0)
        self.tap = None
        self._loop = None

    def _run(self):
        try:
            tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap,
                                          Quartz.kCGHeadInsertEventTap,
                                          Quartz.kCGEventTapOptionDefault,
                                          MASK, self._callback, None)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        if not tap:
            # No tap at all: the agent has not been given Input Monitoring.
            self.error = ("no event tap - the agent needs Input Monitoring "
                          "(g510 permissions)")
            self._ready.set()
            return
        self.tap = tap
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source,
                           kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._loop = CFRunLoopGetCurrent()
        self._ready.set()
        CFRunLoopRun()                  # returns once stop() stops it
        Quartz.CGEventTapEnable(tap, False)

    # -- the tap --------------------------------------------------------

    def _callback(self, _proxy, kind, event, _refcon):
        if kind in (Quartz.kCGEventTapDisabledByTimeout,
                    Quartz.kCGEventTapDisabledByUserInput):
            # Whatever the tap was doing took too long, or the screen locked.
            # Back on without a word: a keyboard that stopped working because
            # one frame ran late would be far worse than a lost key.
            if self.tap:
                Quartz.CGEventTapEnable(self.tap, True)
            return event
        code = int(Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode))
        if kind == Quartz.kCGEventKeyUp:
            if self.feed("up", code, None):
                return None
            return event
        if kind != Quartz.kCGEventKeyDown:
            return event
        blocked = bool(Quartz.CGEventGetFlags(event) & MODIFIER_FLAGS)
        _actual, text = Quartz.CGEventKeyboardGetUnicodeString(
            event, _MAX_CHARACTERS, None, None)
        if self.feed("down", code, text, blocked):
            return None
        return event

    # -- the part that can be tested without a keyboard -----------------

    def feed(self, kind, code, character, blocked=False):
        """One key. True means the event is swallowed and never delivered.

        `blocked` is the shortcut case: with Cmd or Option down, whatever
        the key is, it belongs to the application.
        """
        if kind == "up":
            if code in self._swallowed:
                self._swallowed.discard(code)
                return True
            return False
        if blocked:
            return False
        found = key_for(code, character)
        if found is None:
            return False
        name, text = found
        try:
            wanted = bool(self.decide(name, text))
        except Exception as exc:                 # a policy that throws is
            self.log(f"menu key filter failed: {type(exc).__name__}: {exc}")
            wanted = False
        if not wanted:
            return False
        self._swallowed.add(code)
        self.queue.append((name, text))
        return True

    def drain(self):
        """Everything the menu has been handed since the last call."""
        out = list(self.queue)
        self.queue.clear()
        return out
