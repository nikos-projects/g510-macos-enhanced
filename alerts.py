"""Something that takes the display for a few seconds, then hands it back.

An alarm going off and a message arriving are both interruptions with a shape
the rest of the display has no room for, so each gets the panel to itself for
a while. Nothing underneath is disturbed while that happens: the screen the
agent would have gone on showing keeps being chosen the whole time, so when
the interruption is over that is what comes back - including whatever was
picked by hand in the meantime.
"""
import os
import subprocess
import threading
import time

SOUND_DIR = "/System/Library/Sounds"
AFPLAY = "/usr/bin/afplay"


def sound_path(name):
    """Where a configured sound lives, or None when there is no such one."""
    if not name:
        return None
    text = str(name)
    candidate = text if os.path.isabs(text) else os.path.join(
        SOUND_DIR, text if text.endswith(".aiff") else text + ".aiff")
    return candidate if os.path.exists(candidate) else None


def play_sound(name):
    """Start a sound without waiting for it. Quiet about every failure."""
    path = sound_path(name)
    if not path:
        return False
    try:
        subprocess.Popen([AFPLAY, path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


class Alerts:
    """Interruptions waiting their turn, newest one on the display.

    Only the most recent is shown. The ones under it keep their own clock
    running, so a message landing in the middle of an alarm takes over - and
    the alarm does not come back when it goes, because by then it is over,
    which is what it wanted anyway.
    """

    def __init__(self, play=None, clock=None):
        self._play = play or play_sound
        self._clock = clock or time.time
        self._items = []
        self._lock = threading.Lock()

    def push(self, screen, data=None, seconds=10.0, sound=None, now=None):
        """Put something up for `seconds`, ringing `sound` as it arrives.

        A display that cannot show it - switched off, or busy with a macro
        being recorded - still gets the sound: hearing about it late beats
        not hearing about it at all. Seconds of zero means exactly that.
        """
        moment = self._clock() if now is None else now
        item = {"screen": screen, "data": dict(data or {}),
                "until": moment + max(0.0, float(seconds or 0))}
        with self._lock:
            self._items = [other for other in self._items
                           if other["until"] > moment]
            self._items.append(item)
        if sound:
            self._play(sound)
        return item

    def current(self, now=None):
        """What belongs on the display right now, or None for the usual one."""
        moment = self._clock() if now is None else now
        with self._lock:
            self._items = [other for other in self._items
                           if other["until"] > moment]
            return dict(self._items[-1]) if self._items else None

    def clear(self):
        with self._lock:
            self._items = []
