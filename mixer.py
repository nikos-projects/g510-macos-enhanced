"""The audio mixer on the panel - P7.

Holding L4 brings it up; esc lets go of it. What it is for is the thing
macOS will not put anywhere else: which application is making the sound
you can hear, and how loud each of them is, on a panel that is already
under your fingers.

Three lines, because that is what the panel has after a header, so the
list scrolls rather than growing:

    MIXER                            12:04
  > SYSTEM                        ██████ 57%
    Music                         ██ 65%
    Brave                         ---- --

The bar is what the app is doing *now*, sampled from a process tap per
app (see `audio.Levels`). The number in the right-hand column is the
thing the left and right keys act on: the system volume on the system
row, that app's own volume on the two apps that keep one of their own,
and the level again on every other app - because macOS has no per-app
output volume at all, and a number that promised one would be a lie.

Nothing here talks to CoreAudio. The keys ask for things (`press` hands
back actions) and the agent runs them, exactly as the browser does, so
this stays testable on a machine with no keyboard, no display and no
sound.
"""
import time

SYSTEM = "__system__"          # the id of the row that is not an app
VISIBLE = 3                    # rows the panel has room for
KEYS = ("up", "down", "left", "right", "enter", "esc")
STEP = 5                       # volume per press, in percent
LEVEL_FALL = 0.8               # what a bar falls to per sweep with no sound


class Mixer:
    """One mixer session: which row, what the bars say, what is owed.

    The rows are rebuilt from what the agent detected while the
    *selection* is kept by id rather than by position - an app quits,
    another starts playing - for the same reason the browser keeps its
    cursors as ids: the list under the cursor changes while it is being
    read.
    """

    def __init__(self):
        self.active = False
        self.index = 0
        self.touch = 0.0
        self.scanned = False      # whether detection has had its answer
        self.rows = []
        self._outbox = []
        self._reset()

    # -- life ---------------------------------------------------------------

    def _reset(self):
        self.rows = [{
            "id": SYSTEM, "name": "SYSTEM", "kind": "system",
            "level": 0.0, "volume": None, "muted": False,
            "adjust": "system",
        }]
        self.index = 0
        self.scanned = False

    def open(self, now=None):
        """Take the keys.

        The rows start with the system row alone: what is playing arrives
        with the agent's detection, which happens on its own thread
        because opening the taps can take ten seconds.
        """
        self.active = True
        self._reset()
        self.touch = time.time() if now is None else now
        return self

    def close(self):
        """Let the keys go, and the levels with them."""
        self.active = False
        self._reset()
        self._outbox = []
        return self

    @property
    def idle(self):
        """The last key, for the timeout that hands the keyboard back."""
        return self.touch

    # -- what the agent found -----------------------------------------------

    def set_system(self, volume, muted):
        """The system output as it currently is: 0..1 and whether muted.

        Called on every detection as well as on the first, so a volume
        turned up somewhere else - the keyboard's own keys, the app's own
        slider - shows up here within a couple of seconds instead of the
        panel going on claiming the old number.
        """
        if not self.rows or self.rows[0].get("kind") != "system":
            return
        if volume is not None:
            self.rows[0]["volume"] = max(0.0, min(100.0, float(volume) * 100.0))
        if muted is not None:
            self.rows[0]["muted"] = bool(muted)

    def set_apps(self, apps):
        """The apps detection found, with the selection kept where it was.

        The system row is always row zero and never goes away, so the
        volume the keys act on is in sight no matter how the list churns.
        Levels and volumes are carried across for apps that were already
        here: rebuilding the list every couple of seconds must not flash
        every bar to zero on the way through.
        """
        if not self.active:
            return
        previous = {row.get("id"): row for row in self.rows}
        chosen = self.rows[self.index] if self.rows else None
        system = previous.get(SYSTEM) or self.rows[0]
        rows = [system]
        for app in apps or []:
            try:
                pid = int(app.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if not pid:
                continue
            identifier = str(pid)
            was = previous.get(identifier) or {}
            rows.append({
                "id": identifier,
                "name": str(app.get("name") or "?"),
                "kind": "app",
                "pid": pid,
                "bundle": str(app.get("bundle") or ""),
                "level": float(was.get("level") or 0.0),
                "volume": app.get("volume") if app.get("adjustable")
                else was.get("volume"),
                "adjust": "app" if app.get("adjustable") else None,
            })
        if chosen is not None:
            for index, row in enumerate(rows):
                if row.get("id") == chosen.get("id"):
                    self.index = index
                    break
            else:
                self.index = min(self.index, len(rows) - 1)
        self.rows = rows
        self.scanned = True

    def set_levels(self, peaks):
        """One sweep of the tap: what each app did since the last one.

        A bar falls as well as rises. The tap only ever reports the
        loudest moment since it was last asked, so without `LEVEL_FALL`
        a bar would go on sitting at its last loud reading long after the
        sound had stopped - and a bar that lies about what is playing is
        worse than no bar.

        True when a bar moved, which is the only thing that makes a
        redraw worth asking for: the levels are read ten times a second
        and most of those readings change nothing.
        """
        if not isinstance(peaks, dict):
            peaks = {}
        moved = False
        for row in self.rows:
            if row.get("kind") != "app":
                continue
            peak = float(peaks.get(row.get("pid"), 0.0) or 0.0)
            was = row.get("level", 0.0)
            level = max(peak, was * LEVEL_FALL)
            # Under the noise floor is silence. Left alone a bar halves
            # towards zero for ever, and every one of those sweeps would
            # look like a change worth redrawing the panel for.
            if level < 0.001:
                level = 0.0
            if level != was:
                row["level"] = level
                moved = True
        return moved

    def set_volume(self, key, value):
        """What a volume came back as, so the panel shows the number the
        key actually set rather than the one it meant to set."""
        if value is None:
            return False
        for row in self.rows:
            if row.get("id") == SYSTEM and key in (SYSTEM, "system"):
                row["volume"] = max(0.0, min(100.0, float(value)))
                return True
            if row.get("kind") == "app" and row.get("bundle") == key:
                row["volume"] = max(0.0, min(100.0, float(value)))
                return True
        return False

    def set_muted(self, on):
        """The mute switch, as it ended up."""
        if self.rows and self.rows[0].get("kind") == "system":
            self.rows[0]["muted"] = bool(on)

    # -- keys ---------------------------------------------------------------

    def wants(self, name, character=None):
        """Whether one key belongs to the panel while the mixer is up.

        Called on the tap's own thread, so this reads flags and nothing
        else. Only the navigation keys: the letters stay with whatever is
        underneath, exactly as they do behind the browser, because there
        is nothing here to type into.
        """
        return self.active and name in KEYS

    def press(self, name, character=None, now=None):
        """One key, and what it asks the agent to do about it."""
        if not self.active:
            return []
        self.touch = time.time() if now is None else now
        self._outbox.extend(self._actions(name))
        return self.drain()

    def _actions(self, name):
        if name == "esc":
            return [("close",)]
        if name in ("up", "down"):
            if len(self.rows) > 1:
                step = -1 if name == "up" else 1
                self.index = (self.index + step) % len(self.rows)
            return []
        if name in ("left", "right"):
            if not self.rows:
                return []
            row = self.rows[self.index]
            delta = -STEP if name == "left" else STEP
            if row.get("id") == SYSTEM:
                return [("volume", SYSTEM, delta)]
            if row.get("adjust") == "app":
                return [("volume", str(row.get("bundle") or ""), delta)]
            # An app macOS will not let anything set the volume of. There
            # is nothing to do, and the panel says so with `--` rather
            # than leaving the key looking broken.
            return []
        if name == "enter" and self.rows and \
                self.rows[self.index].get("id") == SYSTEM:
            return [("mute",)]
        return []

    def drain(self):
        """Everything the keys asked for since the last call."""
        out, self._outbox = self._outbox, []
        return out

    # -- what the screen draws ----------------------------------------------

    def _window(self):
        """The start of the visible window, scrolled to keep the cursor."""
        if len(self.rows) <= VISIBLE:
            return 0
        start = max(0, self.index - VISIBLE + 1)
        return min(start, len(self.rows) - VISIBLE)

    def _value(self, row):
        """The number in the right-hand column: what the left and right
        keys move.

        A volume where macOS will let one be set and `--` where it will
        not, which is a promise about the *keys* rather than about the
        sound: the bar is already saying how loud the app is, so a
        number here that could not be moved would only look broken.
        """
        if row.get("kind") == "system":
            if row.get("muted"):
                return "MUTE"
            volume = row.get("volume")
            return "--" if volume is None else f"{volume:.0f}%"
        if row.get("adjust") == "app":
            volume = row.get("volume")
            return "--" if volume is None else f"{volume:.0f}%"
        return "--"

    def view(self):
        """What the screen draws: the rows in the window, and a hint."""
        start = self._window()
        rows = []
        for offset, row in enumerate(self.rows[start:start + VISIBLE]):
            drawn = dict(row)
            drawn["selected"] = (start + offset) == self.index
            drawn["value"] = self._value(row)
            rows.append(drawn)
        return {"title": "MIXER", "rows": rows, "hint": self._hint()}

    def _hint(self):
        """What to say where the apps would be, before and after finding
        none - the two look the same from the keys' point of view."""
        if not self.scanned:
            return "listening..."
        apps = [row for row in self.rows if row.get("kind") == "app"]
        if not apps:
            return "nothing is playing"
        return ""
