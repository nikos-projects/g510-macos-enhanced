"""The shared agent, with a run loop that sleeps on the keyboard.

See _shared.py for why this amends rather than forks. Only the loop changes.

The macOS loop polls: hidapi gives it no file descriptor to wait on, so it
asks the keyboard for a report every four milliseconds and sleeps in between.
That is 250 wakeups a second whether or not anyone is typing, which on a
laptop is a measurable share of a core - this agent had burned eleven minutes
of CPU in its first four hours of doing nothing in particular.

On Linux the kernel has already turned those reports into an evdev device, so
there is a descriptor to block on. Waiting on it costs nothing while the
keyboard is idle, and a keypress wakes the loop at once instead of up to a
poll interval later, so the latency improves as well. The only reason left to
wake on a timer is the LCD, which redraws about once a second, so the wait is
bounded by whenever the next redraw falls due - and by a running beat pulse,
which paces itself at beat.WRITE_INTERVAL and would otherwise be written one
redraw late, which for a beat is the same as not at all.

Everything else the loop owes is the shared agent's own work (see `_tick`):
this file is only about *when* it runs, not about what any of it means.
"""
import select
import signal
import time

import _shared
import ipc

_root = _shared.load("daemon")
_shared.reexport(_root, globals())

# Never sleep longer than this, whatever the config says. It bounds how long
# a reloaded config or a stopped agent takes to be noticed, and keeps a
# disabled LCD from parking the loop indefinitely.
IDLE_CAP = 1.0


def _next_redraw(self, now):
    """Seconds until the LCD is next due, clamped to something sensible."""
    lcd = self.config.get("lcd", {})
    if not lcd.get("enabled", True):
        return IDLE_CAP
    try:
        every = float(lcd.get("refresh_seconds", 1.0))
    except (TypeError, ValueError):
        every = 1.0
    due = max(0.0, min(self.last_lcd + every - now, IDLE_CAP))
    # A pulse is paced at beat.WRITE_INTERVAL and has to end on time, so
    # it gets its own wakes: without them the climb back would happen in
    # one step at the next redraw, up to a second late, and a beat seen
    # a second late is a beat the music has already finished.
    if self.pulse_end:
        due = min(due, max(0.0, self.pulse_end - now))
        if self.pulse_end > now:
            due = min(due,
                      max(0.0, self.pulse_at + beat.WRITE_INTERVAL - now))
    return due


def _boot(self):
    """Everything switched on once, before the loop starts.

    Kept apart from run() so the wiring can be tested without running a
    daemon: each sync_* is one switch that follows its config block.
    """
    self.server.start()
    self.sync_link()
    self.sync_beat()
    self.sync_api()
    # sync_watcher is deliberately absent: notify.py reads macOS's
    # notification store through Accessibility, and there is no Linux
    # source behind it yet (DBus banners are a separate piece of work).
    # Calling it would start a reader that finds nothing, forever.


def _tick(self, now):
    """One pass over everything the loop owes at `now`.

    The macOS order, minus refresh_app_color: screens.frontmost_app is a
    permanent None here, so it was a no-op - the part of the loop that
    would have used it degrades through autoscreen's own fallbacks.
    """
    self.handle_gkeys()
    self.refresh_gestures(now)
    self.refresh_menu(now)
    self.refresh_alerts(now)
    self.refresh_triggers(now)
    self.refresh_lcd(now)
    self.refresh_beat(now)
    self.refresh_indicators(now)
    self.sweep_screens(now)


def _wait(self, timeout):
    """Sleep until a key moves, a beat buffer lands, or the clock says.

    Waiting on the beat listener's pipe is what makes a pulse land on the
    music rather than on the next LCD redraw: the keyboard fd alone only
    ever wakes for a keypress. With neither descriptor available (no
    evdev, no beat) it falls back to a plain sleep, which is the macOS
    cadence and better than spinning.
    """
    fds = []
    reader = getattr(self.keyboard, "fileno", None)
    if reader is not None:
        try:
            fd = reader()
        except Exception:
            fd = None
        if isinstance(fd, int):
            fds.append(fd)
    listener = self.beat
    if listener is not None:
        reader = getattr(listener, "fileno", None)
        if reader is not None:
            try:
                fd = reader()
            except Exception:
                fd = None
            if isinstance(fd, int):
                fds.append(fd)
    if not fds:
        time.sleep(max(0.0, timeout))
        return
    try:
        select.select(fds, [], [], max(0.0, timeout))
    except (OSError, ValueError):
        time.sleep(max(0.0, timeout))


def run(self):
    signal.signal(signal.SIGTERM, self.stop)
    signal.signal(signal.SIGINT, self.stop)
    log("g510 daemon starting")
    self._boot()
    log(f"control socket at {ipc.SOCKET_PATH}")
    while self.running:
        if self.keyboard is None:
            if not self.connect():
                time.sleep(RECONNECT_DELAY)
                continue
        try:
            now = time.time()
            self._tick(now)
        except (OSError, ValueError) as exc:
            self.drop(exc)
            time.sleep(RECONNECT_DELAY)
            continue
        try:
            self._wait(self._next_redraw(time.time()))
        except (OSError, ValueError) as exc:
            self.drop(exc)
            time.sleep(RECONNECT_DELAY)
            continue
    self.shutdown()


Daemon._next_redraw = _next_redraw
Daemon._boot = _boot
Daemon._tick = _tick
Daemon._wait = _wait
Daemon.run = run

_shared.adopt(_root, globals())
