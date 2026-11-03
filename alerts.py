"""Something that takes the display for a few seconds, then hands it back.

An alarm going off and a message arriving are both interruptions with a shape
the rest of the display has no room for, so each gets the panel to itself for
a while. Nothing underneath is disturbed while that happens: the screen the
agent would have gone on showing keeps being chosen the whole time, so when
the interruption is over that is what comes back - including whatever was
picked by hand in the meantime.
"""
import os
import shutil
import subprocess
import sys
import threading
import time

# macOS rings through afplay and its own sound set; everywhere else the
# freedesktop sound set is the one that ships, and paplay (PulseAudio or
# PipeWire) or pw-play plays it. Resolved per play rather than cached at
# import: the tools can be installed after the agent starts, and a
# missing one must cost a silent ring, not an exception.
SOUND_DIR = ("/System/Library/Sounds" if sys.platform == "darwin"
             else "/usr/share/sounds/freedesktop/stereo")
SUFFIX = ".aiff" if sys.platform == "darwin" else ".oga"
PLAYERS = (["/usr/bin/afplay"] if sys.platform == "darwin"
           else ["paplay", "pw-play", "aplay"])


def sound_path(name):
    """Where a configured sound lives, or None when there is no such one."""
    if not name:
        return None
    text = str(name)
    if os.path.isabs(text):
        return text if os.path.exists(text) else None
    if sys.platform == "darwin":
        candidate = os.path.join(
            SOUND_DIR, text if text.endswith(".aiff") else text + ".aiff")
        return candidate if os.path.exists(candidate) else None
    # Linux: the configured name is tried as it stands and as the two
    # extensions the freedesktop set uses, so a user who writes
    # "message-new-instant" or a full file name gets their sound. A
    # macOS name like "Glass" resolves to nothing here, which is a
    # silence rather than a wrong noise - and the config defaults are
    # amended on Linux (linux/config.py) so the shipped setting works.
    stem = text[:-4] if text.endswith((".oga", ".wav")) else text
    for extension in (".oga", ".wav", ""):
        candidate = os.path.join(SOUND_DIR, stem + extension)
        if os.path.exists(candidate):
            return candidate
    return None


def available_sounds():
    """The names a `sound` setting may take, sorted, for the CLI's hint.

    Empty when the directory is not there at all: a minimal container or a
    server install has no sound theme, and the caller turns this into a
    "try one of" line in the middle of a config edit, where a missing
    directory would otherwise raise out of it.
    """
    try:
        return sorted(name for name in os.listdir(SOUND_DIR)
                      if not name.startswith("."))
    except OSError:
        return []


def play_sound(name):
    """Start a sound without waiting for it. Quiet about every failure."""
    path = sound_path(name)
    if not path:
        return False
    for player in PLAYERS:
        command = [player] if os.path.isabs(player) else [player]
        if not os.path.isabs(player) and not shutil.which(player):
            continue
        try:
            subprocess.Popen([player, path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            continue
        return True
    return False


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
