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
bounded by whenever the next redraw falls due.
"""
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
    return max(0.0, min(self.last_lcd + every - now, IDLE_CAP))


def run(self):
    signal.signal(signal.SIGTERM, self.stop)
    signal.signal(signal.SIGINT, self.stop)
    log("g510 daemon starting")
    self.server.start()
    log(f"control socket at {ipc.SOCKET_PATH}")
    while self.running:
        if self.keyboard is None:
            if not self.connect():
                time.sleep(RECONNECT_DELAY)
                continue
        try:
            now = time.time()
            self.handle_gkeys()
            self.refresh_lcd(now)
            self.refresh_indicators(now)
        except (OSError, ValueError) as exc:
            self.drop(exc)
            time.sleep(RECONNECT_DELAY)
            continue
        # refresh_app_color is not called at all: screens.frontmost_app is a
        # permanent None here, so it was a no-op running 250 times a second.
        try:
            self.keyboard.wait(self._next_redraw(time.time()))
        except (OSError, ValueError) as exc:
            self.drop(exc)
            time.sleep(RECONNECT_DELAY)
    self.shutdown()


Daemon._next_redraw = _next_redraw
Daemon.run = run

_shared.adopt(_root, globals())
