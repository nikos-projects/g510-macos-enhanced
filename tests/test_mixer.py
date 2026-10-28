"""The audio mixer with keys: holding L4, watching what is playing.

P7 is the panel answering the question macOS will not: which application
is making the sound, and how loud each of them is. The tapping itself is
CoreAudio's half and lives in `audio`; this is the other half - the rows
(`mixer.Mixer`), the keys routed into them, the render that shows them,
the taps the agent asks for and lets go of again, and `g510 mixer`.

The machine is kept out of every one of them. `audio.candidates`,
`audio.Levels`, `audio.volume` and the AppleScript volume calls are all
patched, so nothing here needs a keyboard, a display or a sound card -
the same bargain the other agent tests make.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

import audio
import cli
import config
import daemon
import mixer
import screens


# -- fixtures -----------------------------------------------------------------

MUSIC = {"pid": 71, "name": "Music", "bundle": "com.apple.Music",
         "adjustable": True, "volume": 65}
BRAVE = {"pid": 72, "name": "Brave", "bundle": "com.brave.Browser",
         "adjustable": False, "volume": None}


class FakeLevels:
    """What `audio.Levels` would open, without opening anything."""

    def __init__(self, peaks=None, opens=True, log=None):
        self.peaks = dict(peaks or {})
        self.opens = opens
        self.log = log
        self.tapped = []
        self.stopped = False

    def start(self, apps):
        self.tapped = list(apps)
        return self.opens

    def poll(self):
        return dict(self.peaks)

    def stop(self):
        self.stopped = True


class StubCanvas:
    """Measures eight pixels a character and writes down what it drew."""

    def __init__(self, per_char=8):
        self.per_char = per_char
        self.drawn = []
        self.bars = []

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char

    def text(self, string, x, y, size=12.0, font=None):
        self.drawn.append(str(string))

    def bar(self, x, y, width, height, fraction):
        self.bars.append((x, y, width, height, fraction))


def view(apps, **over):
    """The panel's view, from a mixer that found `apps`."""
    panel = mixer.Mixer().open()
    panel.set_system(over.get("volume", 0.57), over.get("muted", False))
    panel.set_apps(apps)
    return panel


# -- the panel's own state machine --------------------------------------------

class MixerTests(unittest.TestCase):
    """`mixer.Mixer`: rows, selection, and what a key asks for."""

    def test_a_mixer_starts_with_the_system_row_and_the_keys(self):
        panel = mixer.Mixer()
        self.assertFalse(panel.active)
        self.assertFalse(panel.wants("up", None))
        panel.open()
        self.assertTrue(panel.active)
        self.assertEqual([row["name"] for row in panel.view()["rows"]],
                         ["SYSTEM"])
        self.assertTrue(panel.wants("up", None))

    def test_only_the_navigation_keys_belong_to_the_panel(self):
        # Everything else stays with whatever is underneath, so a game
        # can still be played at with the letters while the panel is up.
        panel = mixer.Mixer().open()
        for name in ("up", "down", "left", "right", "enter", "esc"):
            self.assertTrue(panel.wants(name, None), name)
        self.assertFalse(panel.wants("a", "a"))

    def test_detection_adds_a_row_per_app_and_keeps_the_selection(self):
        panel = view([MUSIC])
        panel.press("down", now=1.0)                     # onto Music
        self.assertEqual(panel.view()["rows"][1]["name"], "Music")
        panel.set_apps([BRAVE, MUSIC])                   # the list churns
        self.assertEqual([row["name"] for row in panel.view()["rows"]],
                         ["SYSTEM", "Brave", "Music"])
        self.assertEqual(panel.view()["rows"][2]["selected"], True)
        self.assertEqual(panel.index, 2)

    def test_a_row_that_goes_away_leaves_the_selection_on_a_row(self):
        panel = view([MUSIC])
        panel.press("down", now=1.0)
        panel.set_apps([])                               # Music stopped
        self.assertEqual(panel.index, 0)
        self.assertEqual(panel.view()["rows"][0]["id"], mixer.SYSTEM)

    def test_a_bar_moves_with_the_app_and_falls_when_it_stops(self):
        panel = view([MUSIC])
        panel.press("down", now=1.0)
        self.assertTrue(panel.set_levels({71: 0.5}))     # something moved
        self.assertEqual(panel.view()["rows"][1]["level"], 0.5)
        # Nothing since: the bar falls rather than sitting on its last
        # loud reading.
        self.assertTrue(panel.set_levels({71: 0.0}))
        self.assertLess(panel.view()["rows"][1]["level"], 0.5)
        self.assertGreater(panel.view()["rows"][1]["level"], 0.0)
        self.assertTrue(panel.set_levels({71: 0.0}))     # still falling
        for _ in range(40):
            panel.set_levels({71: 0.0})
        self.assertEqual(panel.view()["rows"][1]["level"], 0.0)

    def test_a_silent_sweep_is_not_a_change_worth_redrawing(self):
        # Levels are read ten times a second and most of them change
        # nothing; the panel must not be redrawn for every one of them.
        panel = view([MUSIC])
        panel.set_levels({71: 0.0})
        self.assertFalse(panel.set_levels({71: 0.0}))
        self.assertFalse(panel.set_levels({71: 0.0}))

    def test_the_left_and_right_keys_ask_for_the_system_volume(self):
        panel = mixer.Mixer().open()
        self.assertEqual(panel.press("right", now=1.0),
                         [("volume", mixer.SYSTEM, mixer.STEP)])
        self.assertEqual(panel.press("left", now=2.0),
                         [("volume", mixer.SYSTEM, -mixer.STEP)])
        self.assertEqual(panel.drain(), [])

    def test_enter_on_the_system_row_mutes(self):
        panel = mixer.Mixer().open()
        panel.press("up", now=1.0)          # still row zero, and that is fine
        self.assertEqual(panel.press("enter", now=2.0) + panel.drain(),
                         [("mute",)])

    def test_the_volume_keys_act_on_the_app_the_row_can_move(self):
        panel = view([BRAVE, MUSIC])
        panel.press("down", now=1.0)                       # Brave: no volume
        self.assertEqual(panel.press("left", now=2.0) + panel.drain(), [])
        panel.press("down", now=3.0)                       # Music: one of its own
        self.assertEqual(panel.press("right", now=4.0) + panel.drain(),
                         [("volume", "com.apple.Music", mixer.STEP)])

    def test_a_volume_that_came_back_is_what_the_panel_shows(self):
        panel = mixer.Mixer().open()
        panel.set_volume(mixer.SYSTEM, 80.0)               # what it became
        self.assertEqual(panel.view()["rows"][0]["value"], "80%")
        self.assertFalse(panel.set_volume("com.apple.Music", 40.0))
        self.assertEqual(panel.view()["rows"][0]["value"], "80%")

    def test_a_mute_says_so_rather_than_showing_a_number(self):
        panel = mixer.Mixer().open()
        panel.set_muted(True)
        self.assertEqual(panel.view()["rows"][0]["value"], "MUTE")

    def test_esc_asks_to_be_left(self):
        panel = mixer.Mixer().open()
        self.assertEqual(panel.press("esc", now=1.0) + panel.drain(),
                         [("close",)])
        panel.close()
        self.assertFalse(panel.wants("up", None))

    def test_the_list_scrolls_rather_than_growing(self):
        # The panel has three rows after a header and no more.
        apps = [{"pid": 100 + index, "name": f"App{index}",
                 "bundle": "", "adjustable": False, "volume": None}
                for index in range(6)]
        panel = view(apps)
        for _ in range(6):
            panel.press("down", now=1.0)
        drawn = panel.view()["rows"]
        self.assertEqual(len(drawn), mixer.VISIBLE)
        self.assertEqual(drawn[-1]["selected"], True)
        self.assertEqual(drawn[-1]["name"], "App5")
        panel.press("up", now=2.0)
        panel.press("up", now=3.0)
        drawn = panel.view()["rows"]
        self.assertEqual(drawn[0]["name"], "App1")         # the window moved

    def test_nothing_found_says_so_rather_than_an_empty_panel(self):
        panel = mixer.Mixer().open()
        self.assertEqual(panel.view()["hint"], "listening...")
        panel.set_apps([])
        self.assertEqual(panel.view()["hint"], "nothing is playing")


# -- what the screen draws ----------------------------------------------------

class ScreenTests(unittest.TestCase):
    """`screen_mixer`: a name, a bar and a number per row."""

    def draw(self, panel):
        canvas = StubCanvas()
        screens.screen_mixer(canvas, {"mixer": panel.view()})
        return canvas

    def test_the_header_says_what_this_is_and_what_time_it_is(self):
        canvas = self.draw(view([MUSIC]))
        self.assertEqual(canvas.drawn[0], "MIXER")

    def test_each_row_gets_its_name_and_the_number_the_keys_move(self):
        canvas = self.draw(view([MUSIC]))
        self.assertIn(">SYSTEM", canvas.drawn)       # row zero, and it is picked
        self.assertIn(" Music", canvas.drawn)
        self.assertIn("57%", canvas.drawn)           # the system volume
        self.assertIn("65%", canvas.drawn)           # Music's own volume

    def test_an_app_macos_wont_let_us_set_says_so(self):
        canvas = self.draw(view([BRAVE]))
        self.assertIn("--", canvas.drawn)           # no volume to promise

    def test_the_bar_is_the_level_and_on_the_system_row_the_volume(self):
        canvas = self.draw(view([MUSIC]))
        self.assertEqual(canvas.bars[0][-1], 0.57)  # the system row's bar

    def test_a_selected_row_is_marked(self):
        canvas = self.draw(view([MUSIC]))
        self.assertIn(">SYSTEM", canvas.drawn)


# -- the agent ----------------------------------------------------------------

class AgentTests(unittest.TestCase):
    """The agent with a temporary config, a keyboard that only takes frames
    and the event tap stubbed out - it would need Input Monitoring."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        for target in ("log",):
            patcher = mock.patch.object(daemon, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("sync_watcher", "ensure_tap"):
            patcher = mock.patch.object(daemon.Daemon, target,
                                        return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()
        # Finding what is playing and opening its taps both belong to a
        # worker thread in the real agent; here they are written down as
        # calls, so every test can see what was asked for.
        self.scans = []
        patcher = mock.patch.object(
            self.agent, "_scan_async",
            side_effect=lambda generation: (
                self.scans.append(generation),
                self.agent._mixer_scan(generation))[-1])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spawned = []
        patcher = mock.patch.object(
            self.agent, "_spawn_mixer",
            side_effect=lambda generation, apps:
            self.spawned.append((generation, list(apps))))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.taps = []
        patcher = mock.patch.object(audio, "Levels",
                                    side_effect=self._levels)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _levels(self, log=None):
        made = FakeLevels(opens=self.opens, log=log)
        self.taps.append(made)
        return made

    opens = True

    # -- the hold that brings it up

    def hold(self, name, start, held_for):
        """A key pressed at `start` and *kept* down, the loop ticking under
        it, then let go."""
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) | {name}
        self.agent.note_gesture(name, now=start)
        moment = start
        while moment < start + held_for:
            moment += 0.05
            self.agent.refresh_gestures(moment)
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) - {name}
        return moment

    def open_mixer(self, moment=None):
        """The mixer up and found, the way holding L4 leaves it.

        The moment it is given is the *agent's* clock, not a made up
        one: a hold fires when the real seconds say so, and every wait
        after it - the rescan, the timeout - is measured against that.
        """
        moment = time.time() if moment is None else moment
        with mock.patch.object(audio, "candidates",
                               return_value=[MUSIC, BRAVE]), \
                mock.patch.object(audio, "volume", return_value=0.57), \
                mock.patch.object(audio, "muted", return_value=False):
            self.hold("L4", moment, daemon.HOLD_SECONDS + 0.1)
            self.agent.refresh_mixer(moment + 1.0)
        return moment + 1.0

    def draw(self, moment):
        """One redraw, and the screen it drew."""
        drawn = []

        def render(name, _state):
            drawn.append(name)
            return b"\x00" * 4096

        with mock.patch.object(screens, "render", render), \
                mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = moment - 100.0
            self.agent.refresh_lcd(moment)
        return drawn[-1] if drawn else None

    # -- the gesture

    def test_holding_L4_brings_the_mixer_up_and_takes_the_arrows(self):
        moment = self.open_mixer()
        self.assertTrue(self.agent.mixer.active)
        self.assertTrue(self.agent.mixer.wants("up", None))
        self.assertEqual(self.scans, [self.agent.mixer_generation])

    def test_a_hold_of_L3_is_still_the_browser(self):
        moment = self.hold("L3", 100.0, daemon.HOLD_SECONDS + 0.1)
        self.assertFalse(self.agent.mixer.active)
        self.assertTrue(self.agent.browse.active)

    def test_letting_go_with_esc_hands_the_keys_back(self):
        moment = self.open_mixer()
        self.agent.feed_mixer("esc", None)
        self.assertFalse(self.agent.mixer.active)
        self.assertFalse(self.agent.mixer.wants("up", None))

    def test_a_second_hold_does_not_open_a_second_mixer(self):
        # esc is how this one is left, so a hold while it is up is just
        # a hand resting on a key.
        moment = self.open_mixer()
        self.agent.start_mixer()
        self.assertTrue(self.agent.mixer.active)
        self.assertEqual(self.agent.mixer.index, 0)

    def test_a_mixer_nobody_touches_gives_the_keys_back(self):
        moment = self.open_mixer()
        self.agent.mixer.touch = moment
        self.agent.refresh_menu(moment + daemon.MIXER_IDLE + 0.1)
        self.assertFalse(self.agent.mixer.active)

    # -- which panel has the keys

    def test_the_menu_lets_go_when_the_mixer_comes_up(self):
        self.agent.open_menu()
        self.assertFalse(self.agent.menu.closed)
        self.open_mixer()
        self.assertTrue(self.agent.menu.closed)

    def test_a_note_takes_the_panel_from_the_mixer(self):
        # The note needs every key to be written with, so a row nobody
        # could pick would be a row nobody could see.
        self.open_mixer()
        self.agent.toggle_note()
        self.assertFalse(self.agent.mixer.active)

    def test_the_browser_takes_the_panel_from_the_mixer(self):
        self.open_mixer()
        self.assertTrue(self.agent.start_browse())
        self.assertFalse(self.agent.mixer.active)

    def test_keys_the_tap_took_are_routed_to_the_mixer(self):
        self.open_mixer()
        self.agent.keytap = mock.Mock()
        self.agent.keytap.drain.return_value = [
            ("up", None), ("down", None), ("esc", None)]
        self.agent.refresh_menu(200.0)
        self.assertFalse(self.agent.mixer.active)       # esc was the last
        self.assertEqual(self.agent.mixer.index, 0)     # up then down

    # -- what the rescan asks for

    def test_the_taps_are_opened_for_what_is_playing(self):
        moment = self.open_mixer()
        self.assertEqual(len(self.spawned), 1)
        generation, apps = self.spawned[0]
        self.assertEqual([app["pid"] for app in apps], [71, 72])
        self.assertEqual(self.agent.mixer_tapped, (71, 72))

    def test_the_same_apps_again_do_not_rebuild_the_taps(self):
        moment = self.open_mixer()
        self.assertEqual(len(self.spawned), 1)
        with mock.patch.object(audio, "candidates", return_value=[MUSIC, BRAVE]), \
                mock.patch.object(audio, "volume", return_value=0.57), \
                mock.patch.object(audio, "muted", return_value=False):
            for when in (moment + 2.5, moment + 5.0, moment + 7.5):
                self.agent.refresh_mixer(when)
        self.assertEqual(len(self.spawned), 1)          # nothing changed

    def test_an_app_starting_is_added_to_the_taps(self):
        moment = self.open_mixer()
        self.agent.mixer_levels = FakeLevels()
        with mock.patch.object(audio, "candidates",
                               return_value=[MUSIC, BRAVE, {
                                   "pid": 73, "name": "Spotify",
                                   "bundle": "com.spotify.client",
                                   "adjustable": True, "volume": 30}]), \
                mock.patch.object(audio, "volume", return_value=0.57), \
                mock.patch.object(audio, "muted", return_value=False):
            self.agent.refresh_mixer(moment + 2.5)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.agent.mixer_tapped, (71, 72, 73))

    def test_the_levels_are_read_into_the_bars_often_but_not_always(self):
        moment = self.open_mixer()
        self.agent.mixer_levels = FakeLevels(peaks={71: 0.5})
        self.agent.last_lcd = 50.0
        self.agent.refresh_mixer(moment + daemon.MIXER_POLL + 0.01)
        rows = self.agent.mixer.view()["rows"]
        self.assertEqual([row["level"] for row in rows if row["kind"] == "app"],
                         [0.5, 0.0])
        self.assertEqual(self.agent.last_lcd, 0.0)         # a bar moved
        # Too soon to look again: the levels are read ten times a second
        # and the loop ticks faster than that.
        self.agent.mixer_poll = moment + daemon.MIXER_POLL + 0.01
        self.agent.mixer_levels = FakeLevels(peaks={71: 0.9})
        self.agent.last_lcd = 50.0
        self.agent.refresh_mixer(moment + daemon.MIXER_POLL + 0.02)
        self.assertEqual(self.agent.last_lcd, 50.0)        # not looked at

    def test_a_silent_panel_does_not_redraw_itself(self):
        moment = self.open_mixer()
        self.agent.mixer_levels = FakeLevels()
        self.agent.last_lcd = 50.0
        self.agent.refresh_mixer(moment + daemon.MIXER_POLL + 0.01)
        self.assertEqual(self.agent.last_lcd, 50.0)        # nothing moved

    def test_a_worker_that_finishes_after_esc_drops_what_it_made(self):
        moment = self.open_mixer()
        self.agent.feed_mixer("esc", None)                # generation bumps
        self.agent._mixer_job(self.agent.mixer_generation - 1, [MUSIC])
        self.assertTrue(self.taps[-1].stopped)            # torn down again
        self.assertIsNone(self.agent.mixer_levels)        # and not installed

    def test_taps_that_will_not_open_leave_a_panel_without_bars(self):
        moment = self.open_mixer()
        self.opens = False
        made = self.agent._mixer_job(self.agent.mixer_generation, [MUSIC])
        self.assertFalse(made)
        self.assertIsNone(self.agent.mixer_levels)
        # And it does not spin: the pids it tried are remembered, so the
        # next scan does not ask for the same taps again.
        self.assertEqual(self.agent.mixer_tapped, (71,))

    # -- the keys that move a volume

    def test_the_right_key_on_the_system_row_moves_the_system_volume(self):
        moment = self.open_mixer()
        with mock.patch.object(audio, "volume", return_value=0.5), \
                mock.patch.object(audio, "set_volume",
                                  return_value=0.55) as set_it:
            self.agent.feed_mixer("right", None)
        set_it.assert_called_once()
        self.assertEqual(set_it.call_args.args[0], 0.55)
        self.assertEqual(self.agent.mixer.view()["rows"][0]["value"], "55%")

    def test_enter_mutes_and_the_panel_says_so(self):
        moment = self.open_mixer()
        with mock.patch.object(audio, "muted", return_value=False), \
                mock.patch.object(audio, "set_muted", return_value=True):
            self.agent.feed_mixer("enter", None)
        self.assertEqual(self.agent.mixer.view()["rows"][0]["value"], "MUTE")

    def test_an_app_volume_is_set_even_when_it_takes_a_round_trip(self):
        # Music's own volume is an AppleScript, so it is done on the
        # mixer's worker rather than where the key was pressed - which
        # is exactly what the patch below stands in for.
        moment = self.open_mixer()
        self.agent.mixer.set_apps([MUSIC])
        self.agent.mixer.index = 1
        with mock.patch.object(audio, "change_app_volume",
                               return_value=70) as moved, \
                mock.patch.object(self.agent, "_mixer_async",
                                  lambda job: job()):
            self.agent.feed_mixer("right", None)
        moved.assert_called_once_with("com.apple.Music", mixer.STEP)
        self.assertEqual(self.agent.mixer.view()["rows"][1]["value"], "70%")

    # -- what the panel shows

    def test_the_mixer_is_what_the_display_draws_while_it_is_up(self):
        moment = self.open_mixer()
        self.assertEqual(self.draw(moment), "mixer")
        self.assertFalse(self.agent.state.get("mixer"))   # popped after

    def test_letting_go_puts_the_screen_underneath_back(self):
        moment = self.open_mixer()
        self.assertEqual(self.draw(moment), "mixer")
        self.agent.feed_mixer("esc", None)
        self.assertNotEqual(self.draw(moment + 1.0), "mixer")

    def test_the_display_may_be_switched_off_and_the_bars_go_on(self):
        # Bars nobody can see are bars that may as well not be measured.
        moment = self.open_mixer()
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(
            enabled=False))
        self.assertEqual(self.draw(moment), "mixer")

    def test_a_message_waits_for_the_mixer_rather_than_covering_it(self):
        moment = self.open_mixer()
        self.agent.alerts.push("message", {"text": "hello"}, 10.0, now=moment)
        with mock.patch.object(daemon, "log") as said:
            self.assertEqual(self.draw(moment), "mixer")
            self.assertEqual(self.draw(moment + 1.0), "mixer")
        self.assertFalse(self.agent.alerting)
        held = [call.args[0] for call in said.call_args_list]
        self.assertEqual(
            held.count("banner held back - the mixer has the keys"), 1)

    def test_the_message_shows_the_moment_the_mixer_lets_go(self):
        moment = self.open_mixer()
        self.agent.alerts.push("message", {"text": "hello"}, 10.0, now=moment)
        self.assertEqual(self.draw(moment), "mixer")
        self.agent.feed_mixer("esc", None)
        self.assertEqual(self.draw(moment + 1.0), "message")

    def test_the_mixer_does_not_touch_the_screen_underneath(self):
        # It borrows the display the way an alert does, so whatever the
        # display was showing is still what it is showing underneath.
        moment = self.open_mixer()
        before = self.screen()
        self.draw(moment)
        self.assertEqual(self.screen(), before)

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")

    # -- the command

    def test_the_command_opens_and_closes_it(self):
        moment = self.open_mixer()
        reply = self.agent.mixer_command({"action": "off"})
        self.assertEqual(reply, {"ok": True, "active": False})
        reply = self.agent.mixer_command({"action": "on"})
        self.assertEqual(reply, {"ok": True, "active": True})
        self.assertTrue(self.agent.mixer.active)

    def test_the_command_needs_the_agent_running_to_be_of_any_use(self):
        reply = mock.patch.object(cli, "agent_reply", return_value=None)
        with reply, self.assertRaises(SystemExit):
            cli.cmd_mixer([])

    def test_the_cli_says_what_the_keys_do(self):
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True, "active": True}) as call:
            cli.cmd_mixer(["on"])
        call.assert_called_once_with("mixer", action="on")

    def test_shutting_down_lets_the_taps_go(self):
        moment = self.open_mixer()
        made = FakeLevels()
        self.agent.mixer_levels = made
        self.agent.shutdown()
        self.assertTrue(made.stopped)
        self.assertIsNone(self.agent.mixer_levels)


if __name__ == "__main__":
    unittest.main()
