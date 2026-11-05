"""The run loop: what it owes each pass, and what it waits on.

Nothing here runs a real agent. `_boot`, `_tick`, `_wait` and `_next_redraw`
are plain functions over `self`, so they can be pointed at a stub that records
what was called - which is the only way to pin the loop's contents at all.

That matters more than it sounds. Drop one refresh_* out of `_tick` and the
feature it guarded does not fail: it simply stops being updated, silently, on
one platform, while every test in both suites stays green. The macOS loop's
order is the contract, and these tests are what notice a divergence.
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import beat
import daemon

# The loop's functions are underscore-prefixed, so _shared.adopt() leaves
# them on the class rather than copying them onto the module - which is
# where they belong anyway, since they are only ever called on a Daemon.
Daemon = daemon.Daemon


class Loop:
    """Every method the run loop could call, recording the order they ran.

    `config`, `last_lcd`, `pulse_end` and `pulse_at` are the state
    `_next_redraw` reads; the rest are the steps it drives.
    """

    def __init__(self, **attrs):
        self.calls = []
        self.config = attrs.pop("config", {"lcd": {"enabled": True,
                                                   "refresh_seconds": 1.0}})
        self.last_lcd = attrs.pop("last_lcd", 0.0)
        self.pulse_end = attrs.pop("pulse_end", 0.0)
        self.pulse_at = attrs.pop("pulse_at", 0.0)
        self.keyboard = attrs.pop("keyboard", None)
        self.beat = attrs.pop("beat", None)
        self.server = SimpleNamespace(start=self._record("server.start"))
        self._boot = self._record("_boot")
        for key, value in attrs.items():
            setattr(self, key, value)

    def _record(self, name):
        def record(*_args):
            self.calls.append(name)
        return record

    def __getattr__(self, name):
        if name.startswith(("handle_", "refresh_", "sweep_", "sync_")):
            return self._record(name)
        raise AttributeError(name)

    def stop(self, *_args):
        self.running = False

    def _next_redraw(self, now):
        return Daemon._next_redraw(self, now)


class Runner(Loop):
    """A Loop that stops itself after a couple of passes, so `run()` can be
    exercised end to end without a keyboard, a clock or a signal."""

    def __init__(self, **attrs):
        super().__init__(**attrs)
        self.running = True
        self.passes = 0

    def _tick(self, now):
        self.calls.append("_tick")
        self.passes += 1

    def _wait(self, timeout):
        self.calls.append("_wait")
        if self.passes >= 2:
            self.running = False

    def shutdown(self):
        self.calls.append("shutdown")


# The macOS order, minus refresh_app_color: screens.frontmost_app() is a
# permanent None on Wayland, so that step was a no-op here.
MACOS_ORDER = ["handle_gkeys", "refresh_gestures", "refresh_menu",
               "refresh_alerts", "refresh_triggers", "refresh_lcd",
               "refresh_beat", "refresh_indicators", "sweep_screens"]


class Tick(unittest.TestCase):
    def test_every_step_of_the_loop_runs_in_the_macos_order(self):
        """Order matters: refresh_lcd decides what is on the panel and
        refresh_beat may change the colour it just chose."""
        stub = Loop()
        Daemon._tick(stub, 1.0)
        self.assertEqual(stub.calls, MACOS_ORDER)

    def test_the_frontmost_app_step_is_deliberately_absent(self):
        """Pinned so nobody adds it back "for parity": on Wayland
        frontmost_app() can never answer, so the step would burn a call
        per tick to return None."""
        stub = Loop()
        Daemon._tick(stub, 1.0)
        self.assertNotIn("refresh_app_color", stub.calls)

    def test_no_step_is_run_twice(self):
        stub = Loop()
        Daemon._tick(stub, 1.0)
        self.assertEqual(len(stub.calls), len(set(stub.calls)))


class Boot(unittest.TestCase):
    def test_the_switches_that_follow_the_config_are_flipped_on(self):
        # "_boot" itself is not recorded here: it is being called directly,
        # not through the stub's own recorder, which only fires when run()
        # goes through self._boot().
        stub = Loop()
        Daemon._boot(stub)
        self.assertEqual(stub.calls, ["server.start", "sync_link",
                                      "sync_beat", "sync_api"])

    def test_the_macos_notification_watcher_is_not_started(self):
        """notify.py reads macOS's notification store through Accessibility
        and there is no Linux source behind it yet, so calling sync_watcher
        would start a reader that finds nothing, forever - and a reader
        that finds nothing is indistinguishable from one that is broken."""
        stub = Loop()
        Daemon._boot(stub)
        self.assertNotIn("sync_watcher", stub.calls)

    def test_boot_does_not_touch_the_loop_steps(self):
        """Wiring the switches and running the loop are separate: booting
        must not draw a frame or read a key."""
        stub = Loop()
        Daemon._boot(stub)
        for name in MACOS_ORDER:
            self.assertNotIn(name, stub.calls)


class SyncWatcher(unittest.TestCase):
    def test_the_shared_reload_path_starts_no_watcher(self):
        """_boot skips sync_watcher, but reload_config() is shared code
        and calls it on every reload - the path every `g510 config set`
        takes. There is no source on Linux, so the guard has to live inside
        sync_watcher itself: skipping it only at boot would leave that one
        free to start a reader that finds nothing, forever."""
        stub = Loop(config={"notifications": {"enabled": True}})
        with mock.patch.object(daemon.notify, "Watcher", create=True) as made:
            Daemon.sync_watcher(stub)
        made.assert_not_called()
        self.assertFalse(hasattr(stub, "watcher"))


class NextRedraw(unittest.TestCase):
    def test_a_disabled_display_never_parks_the_loop(self):
        """The cap exists so a config that turns the LCD off cannot leave
        the agent asleep with a reloaded config it never notices."""
        stub = Loop(config={"lcd": {"enabled": False}})
        self.assertEqual(Daemon._next_redraw(stub, 100.0), daemon.IDLE_CAP)

    def test_the_wait_ends_when_the_next_redraw_falls_due(self):
        stub = Loop(last_lcd=99.5,
                    config={"lcd": {"enabled": True, "refresh_seconds": 1.0}})
        self.assertAlmostEqual(Daemon._next_redraw(stub, 100.0), 0.5)

    def test_it_is_never_longer_than_the_cap(self):
        stub = Loop(last_lcd=0.0,
                    config={"lcd": {"enabled": True, "refresh_seconds": 900.0}})
        self.assertEqual(Daemon._next_redraw(stub, 0.0), daemon.IDLE_CAP)

    def test_a_running_pulse_wakes_the_loop_to_pace_itself(self):
        """The pulse is written at beat.WRITE_INTERVAL; waiting for the
        next redraw would write its climb back in one step, a beat late."""
        stub = Loop(last_lcd=100.0, pulse_at=100.0, pulse_end=100.5,
                    config={"lcd": {"enabled": True, "refresh_seconds": 1.0}})
        due = Daemon._next_redraw(stub, 100.0)
        self.assertGreater(due, 0.0)
        self.assertLessEqual(due, beat.WRITE_INTERVAL)

    def test_a_pulse_that_just_ended_wakes_the_loop_to_restore_the_colour(self):
        """refresh_beat clears pulse_end and writes the colour back on the
        next pass, so the wait has to end now rather than in a second - the
        panel would otherwise keep the dipped colour until then."""
        stub = Loop(last_lcd=100.0, pulse_at=90.0, pulse_end=95.0,
                    config={"lcd": {"enabled": True, "refresh_seconds": 1.0}})
        self.assertEqual(Daemon._next_redraw(stub, 100.0), 0.0)


class Wait(unittest.TestCase):
    def test_it_sleeps_when_there_is_nothing_to_wait_on(self):
        stub = Loop()
        with mock.patch.object(daemon.time, "sleep") as sleep:
            Daemon._wait(stub, 0.5)
        sleep.assert_called_once_with(0.5)

    def test_it_waits_on_the_beats_descriptor(self):
        read, write = os.pipe()
        try:
            stub = Loop(beat=SimpleNamespace(fileno=lambda: read))
            with mock.patch.object(daemon.select, "select") as choose:
                Daemon._wait(stub, 0.25)
            self.assertEqual(choose.call_args[0][0], [read])
        finally:
            os.close(read)
            os.close(write)

    def test_it_waits_on_the_keyboards_descriptor_too(self):
        stub = Loop(keyboard=SimpleNamespace(fileno=lambda: 7))
        with mock.patch.object(daemon.select, "select") as choose:
            Daemon._wait(stub, 0.25)
        self.assertEqual(choose.call_args[0][0], [7])

    def test_a_keyboard_that_cannot_describe_itself_is_skipped(self):
        """The device can vanish under the loop; that must cost a plain
        sleep rather than an exception out of the wait."""
        def gone():
            raise OSError("unplugged")
        stub = Loop(keyboard=SimpleNamespace(fileno=gone))
        with mock.patch.object(daemon.time, "sleep") as sleep:
            Daemon._wait(stub, 0.1)
        sleep.assert_called_once_with(0.1)

    def test_a_descriptor_that_is_not_a_number_is_ignored(self):
        stub = Loop(keyboard=SimpleNamespace(fileno=lambda: None))
        with mock.patch.object(daemon.time, "sleep") as sleep:
            Daemon._wait(stub, 0.1)
        sleep.assert_called_once_with(0.1)


class Run(unittest.TestCase):
    def test_the_loop_boots_once_ticks_repeatedly_and_shuts_down(self):
        stub = Runner(keyboard=object())
        with mock.patch.dict(Daemon.run.__globals__,
                             {"log": lambda *_a: None}), \
                mock.patch.object(daemon.signal, "signal"):
            daemon.run(stub)
        self.assertEqual(stub.calls.count("_boot"), 1)
        self.assertGreaterEqual(stub.calls.count("_tick"), 2)
        self.assertEqual(stub.calls.count("_wait"), stub.calls.count("_tick"))
        self.assertEqual(stub.calls[-1], "shutdown")


if __name__ == "__main__":
    unittest.main()
