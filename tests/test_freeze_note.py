"""The gestures on the display keys: freeze, hold, and pin a note.

E1 is a soft key pressed twice - autoscreen stops touching the screen until
the same key is pressed twice again. E2 is a key *held* down: L2 held for
half a second opens the menu, and holding it again shuts it. E3 is three
presses of L5 - a note is written on the panel and stays there until the
same three presses take it down. All three are runtime state in the agent:
the config file knows nothing about any of them, which is the point, so the
agent here is redirected at a temporary config and the real one is never
touched.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

import alerts
import autoscreen
import cli
import config
import daemon
import device
import screens


class IdleReader:
    """A machine nobody has touched for a while, so the ordinary rules
    pick a screen without asking macOS anything."""

    def read(self, block, rules=None, wait=False):
        return {"idle": 400.0, "playing": False, "game": False,
                "fullscreen": False, "app": "Finder",
                "bundle": "com.apple.finder", "running": (),
                "commands": {}}


class FakeTap:
    """The keys the event tap took, handed over when the loop asks."""

    def __init__(self, keys=()):
        self.keys = list(keys)

    def drain(self):
        out, self.keys = self.keys, []
        return out


class QueueKeyboard:
    """Reports waiting to be read, one read at a time.

    The agent's loop asks for whatever is queued; this hands over the lot
    and then nothing, which is what a non-blocking device does.
    """

    def __init__(self, reports=()):
        self.reports = list(reports)

    def read_raw(self, size=64):
        return self.reports.pop(0) if self.reports else None


class StubCanvas:
    """Measures eight pixels a character and writes down what it drew."""

    def __init__(self, per_char=8):
        self.per_char = per_char
        self.drawn = []

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char

    def text(self, string, x, y, size=12.0, font=None):
        self.drawn.append(str(string))

    def centered(self, string, y, size=12.0, font=None):
        self.drawn.append(str(string))


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
            patcher = mock.patch.object(daemon.Daemon, target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()
        self.agent.autoscreen = autoscreen.AutoScreen(IdleReader())

    def press(self, name, now, times=1):
        """Press a display key `times` times, close together."""
        for index in range(times):
            self.agent.note_gesture(name, now=now + index * 0.1)

    def tap(self, name, now):
        """One press of a display key: down, and up again straight away."""
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) | {name}
        self.agent.note_gesture(name, now=now)
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) - {name}

    def hold(self, name, start, held_for):
        """A key pressed at `start` and *kept* down, the loop ticking under
        it the whole time - which is the only way a hold is ever seen."""
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) | {name}
        self.agent.note_gesture(name, now=start)
        moment = start
        while moment < start + held_for:
            moment += 0.05
            self.agent.refresh_gestures(moment)
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) - {name}
        return moment

    def draw(self, moment):
        """One redraw, and the name of the screen it drew."""
        drawn = []

        def render(name, _state):
            drawn.append(name)
            return b"\x00" * 4096

        with mock.patch.object(screens, "render", render), \
                mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = moment - 100.0
            self.agent.refresh_lcd(moment)
        return drawn[-1] if drawn else None

    # -- E1: a double press freezes the screen

    def test_a_single_press_waits_for_the_second_one_before_doing_anything(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L3", 100.0)
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD - 0.01)
        self.assertEqual(fired, [])              # still deciding what it is
        self.assertFalse(self.agent.autoscreen.frozen)
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD)
        self.assertEqual(fired, ["L3"])          # it was only a press

    def test_a_double_press_of_a_soft_key_freezes_the_screen(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L3", 100.0, times=2)
        self.agent.refresh_gestures(100.1 + daemon.GESTURE_HOLD)
        self.assertTrue(self.agent.autoscreen.frozen)
        self.assertEqual(fired, [])              # it did not also switch

    def test_the_second_double_press_lets_go(self):
        self.press("L3", 100.0, times=2)
        self.agent.refresh_gestures(100.1 + daemon.GESTURE_HOLD)
        self.press("L3", 200.0, times=2)
        self.agent.refresh_gestures(200.1 + daemon.GESTURE_HOLD)
        self.assertFalse(self.agent.autoscreen.frozen)

    def test_the_menu_key_is_not_one_of_the_four_so_it_does_not_freeze(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L1", 100.0, times=2)
        self.agent.refresh_gestures(100.1 + daemon.GESTURE_HOLD)
        self.assertFalse(self.agent.autoscreen.frozen)
        self.assertEqual(fired, ["L1"])

    def test_presses_far_apart_are_two_presses_not_a_double(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L3", 100.0)
        self.press("L3", 100.0 + daemon.GESTURE_SPAN + 0.1)
        self.agent.refresh_gestures(101.0 + daemon.GESTURE_HOLD)
        self.assertFalse(self.agent.autoscreen.frozen)
        self.assertEqual(fired, ["L3"])

    def test_a_single_press_still_switches_screens(self):
        self.press("L3", 100.0)
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD)
        self.assertEqual(self.agent.config["lcd"]["screen"], "clock")

    def test_the_rules_hold_their_fire_while_the_screen_is_frozen(self):
        self.agent.mutate(lambda s: s.setdefault("autoscreen", {}).update(
            hold_seconds=0, check_seconds=0))
        self.agent.autoscreen.freeze()
        self.agent.refresh_autoscreen(1000.0)
        self.assertEqual(self.agent.config["lcd"]["screen"], "status")
        self.agent.autoscreen.freeze(False)
        self.agent.refresh_autoscreen(1000.0)
        self.assertEqual(self.agent.config["lcd"]["screen"], "clock")

    def test_freezing_is_not_something_the_config_file_knows(self):
        self.press("L3", 100.0, times=2)
        self.agent.refresh_gestures(100.1 + daemon.GESTURE_HOLD)
        self.assertTrue(self.agent.autoscreen.frozen)
        self.assertNotIn("frozen", config.load()["autoscreen"])

    # -- the two windows: the action runs at the hold, the count lives longer

    def test_three_presses_of_the_menu_key_are_presses_and_not_a_menu(self):
        # The gap between presses is wider than the hold, so each lone press
        # has already acted by the time the next one lands - and the count
        # is still standing. L1 has no triple any more: E2 moved to holding
        # L2, so this is three of the key's own job and no menu at all.
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        moment = 100.0
        for _ in range(3):
            self.agent.note_gesture("L1", now=moment)
            self.agent.refresh_gestures(moment + daemon.GESTURE_HOLD)
            moment += 0.6                  # within the span, past the hold
        self.assertTrue(self.agent.menu.closed)
        self.assertEqual(fired, ["L1", "L1"])     # the first two ran alone

    # -- E2: a key held down is the menu

    def test_holding_the_key_opens_the_menu_and_no_screen_change_gets_past(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.hold("L2", 100.0, daemon.HOLD_SECONDS + 0.1)
        self.assertFalse(self.agent.menu.closed)
        self.assertEqual(fired, [])            # the tap it was did not run

    def test_holding_the_key_again_shuts_the_menu(self):
        self.hold("L2", 100.0, daemon.HOLD_SECONDS + 0.1)
        self.hold("L2", 200.0, daemon.HOLD_SECONDS + 0.1)
        self.assertTrue(self.agent.menu.closed)

    def test_a_hold_fires_once_however_long_the_finger_stays_down(self):
        # Or the menu would open and shut again under one finger.
        self.hold("L2", 100.0, daemon.HOLD_SECONDS + 2.0)
        self.assertFalse(self.agent.menu.closed)

    def test_a_key_let_go_before_the_hold_is_still_only_a_tap(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.agent.lcd_pressed = {"L2"}
        self.agent.note_gesture("L2", now=100.0)
        self.agent.refresh_gestures(100.0 + daemon.HOLD_SECONDS - 0.05)
        self.assertEqual(fired, [])            # still down, so still waiting
        self.assertTrue(self.agent.menu.closed)
        self.agent.lcd_pressed = set()         # let go just short of it
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD)
        self.assertEqual(fired, ["L2"])        # a tap after all
        self.assertTrue(self.agent.menu.closed)

    def test_a_double_press_is_two_taps_rather_than_a_hold(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.tap("L2", 100.0)
        self.tap("L2", 100.3)
        self.agent.refresh_gestures(100.3 + daemon.GESTURE_HOLD)
        self.assertTrue(self.agent.autoscreen.frozen)
        self.assertTrue(self.agent.menu.closed)
        self.assertEqual(fired, [])            # and no screen change either

    def test_the_count_is_forgotten_once_the_span_has_passed(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.agent.note_gesture("L1", now=100.0)
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD)
        self.assertEqual(fired, ["L1"])           # the lone press ran
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_SPAN + 0.01)
        self.assertNotIn("L1", self.agent._gestures)
        self.agent.note_gesture("L1", now=200.0)
        self.assertEqual(self.agent._gestures["L1"][0], 1)   # one, not two

    def test_a_burst_of_reports_is_all_read_in_one_pass(self):
        # One report per pass let a busy keyboard build a backlog, and the
        # OS drops from the front of a queue that fills - which is how a tap
        # goes missing and a triple is never a triple.
        self.agent.keyboard = QueueKeyboard([
            bytes((0x03, 0, 0, 0, 0x01)),        # L1 down
            bytes((0x03, 0, 0, 0, 0x00)),        # L1 up
            bytes((0x03, 0, 0, 0, 0x01)),        # L1 down again
        ])
        self.agent.handle_gkeys()
        self.assertEqual(self.agent._gestures["L1"][0], 2)
        self.assertEqual(self.agent.keyboard.reports, [])   # queue drained

    # -- E3: three presses of L5 write a note

    def test_three_presses_of_the_note_key_start_writing_one(self):
        self.press("L5", 100.0, times=3)
        self.assertEqual(self.agent.note_draft, "")
        self.assertTrue(self.agent.note_active)
        # It takes the keyboard while it is being written...
        self.assertTrue(self.agent.wants_menu_key("a", "a"))
        self.assertTrue(self.agent.wants_menu_key("enter", None))
        # ...and only then: nothing else holds the keys.
        self.agent.note_draft = None
        self.assertFalse(self.agent.wants_menu_key("a", "a"))

    def test_three_presses_of_the_note_key_while_one_is_up_take_it_down(self):
        self.press("L5", 100.0, times=3)
        self.agent.feed_note("h", "h")
        self.agent.feed_note("enter", None)
        self.assertEqual(self.agent.note, "h")
        self.press("L5", 200.0, times=3)
        self.assertIsNone(self.agent.note)
        self.assertFalse(self.agent.note_active)

    def test_the_note_key_does_not_freeze_on_the_way_to_the_triple(self):
        self.press("L5", 100.0, times=3)
        self.assertFalse(self.agent.autoscreen.frozen)

    def test_the_note_is_pinned_by_enter_and_dropped_by_esc(self):
        self.press("L5", 100.0, times=3)
        for name, character in (("b", "b"), ("u", "u"), ("y", "y"),
                                ("backspace", None), (" ", " "), ("m", "m")):
            self.agent.feed_note(name, character)
        self.assertEqual(self.agent.note_draft, "bu m")
        self.agent.feed_note("esc", None)
        self.assertIsNone(self.agent.note)
        self.assertIsNone(self.agent.note_draft)

    def test_the_keys_the_tap_took_reach_the_note(self):
        self.agent.keytap = FakeTap([("h", "h"), ("i", "i"), ("enter", None)])
        self.press("L5", 100.0, times=3)
        self.agent.refresh_menu(daemon.time.time())
        self.assertEqual(self.agent.note, "hi")

    def test_the_panel_shows_the_note_until_it_is_cleared(self):
        self.press("L5", 100.0, times=3)
        for name, character in (("n", "n"), ("o", "o"), ("t", "t"),
                                ("e", "e")):
            self.agent.feed_note(name, character)
        self.agent.feed_note("enter", None)
        self.assertEqual(self.draw(1000.0), "note")
        self.press("L5", 200.0, times=3)
        self.assertEqual(self.draw(1000.0), "status")

    def test_an_alert_borrows_the_panel_and_the_note_comes_back(self):
        self.agent.alerts = alerts.Alerts(play=lambda _sound: None,
                                          clock=lambda: 1000.0)
        self.press("L5", 100.0, times=3)
        self.agent.feed_note("e", "e")
        self.agent.feed_note("enter", None)
        self.agent.alerts.push("timeralert", {"phase": "done"}, 10.0, None,
                               now=1000.0)
        self.assertEqual(self.draw(1000.0), "timeralert")
        self.assertEqual(self.draw(1011.0), "note")

    def test_a_blank_display_still_shows_the_note(self):
        self.press("L5", 100.0, times=3)
        self.agent.feed_note("e", "e")
        self.agent.feed_note("enter", None)
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(
            enabled=False))
        self.assertEqual(self.draw(1000.0), "note")
        # And without one it stays blank, as it always did.
        self.press("L5", 200.0, times=3)
        self.assertIsNone(self.draw(1000.0))

    def test_a_note_stands_autoscreen_down_like_the_menu_does(self):
        self.agent.last_lcd = 999.0
        self.agent.refresh_lcd(999.0)            # the rules pick something
        self.assertIsNotNone(self.agent.autoscreen.chosen)
        self.press("L5", 100.0, times=3)
        self.agent.feed_note("e", "e")
        self.agent.feed_note("enter", None)
        self.agent.refresh_lcd(1000.0)
        self.assertIsNone(self.agent.autoscreen.chosen)

class NoteScreenTests(unittest.TestCase):
    """The note as it is drawn, not as the agent holds it."""

    def test_it_draws_at_the_panel_size_whatever_is_on_it(self):
        for state in ({"note": "buy milk", "note_entry": False},
                      {"note": "", "note_entry": False},
                      {"note": "buy mi", "note_entry": True},
                      {"note": "", "note_entry": True}):
            with self.subTest(state=state):
                rows = screens.render("note", state)
                self.assertEqual(len(rows), device.LCD_HEIGHT)
                self.assertTrue(all(len(row) == device.LCD_WIDTH
                                    for row in rows))

    def test_a_note_being_written_says_so_and_shows_the_caret(self):
        canvas = StubCanvas()
        screens.screen_note(canvas, {"note": "buy mil", "note_entry": True})
        self.assertIn("NOTE", canvas.drawn)
        self.assertTrue(any(line.endswith("_") for line in canvas.drawn))

    def test_a_pinned_note_is_wrapped_rather_than_cut_off(self):
        canvas = StubCanvas()
        screens.screen_note(
            canvas, {"note": "remember to water the plants before friday",
                     "note_entry": False})
        self.assertIn("remember to water", " ".join(canvas.drawn))

    def test_it_is_not_one_of_the_four_screens_a_rule_can_ask_for(self):
        self.assertNotIn("note", screens.SCREENS)
        self.assertIn("note", screens.SCREENS_INTERNAL)


class CommandTests(unittest.TestCase):
    """What `g510 autoscreen freeze|unfreeze` and `g510 note` ask for."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        self.out = io.StringIO()
        self.out_patch = mock.patch("sys.stdout", new=self.out)
        self.out_patch.start()
        self.addCleanup(self.out_patch.stop)

    def test_status_says_whether_the_screen_is_frozen(self):
        signals = {"idle": 1.0, "app": "Finder", "playing": False}
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True, "frozen": True}), \
                mock.patch.object(cli.autoscreen, "preview",
                                  return_value=(signals, None, None)):
            cli.cmd_autoscreen([])
        self.assertIn("frozen     yes", self.out.getvalue())
        self.assertIn("unfreeze", self.out.getvalue())

    def test_status_without_an_agent_says_it_cannot_know(self):
        signals = {"idle": 1.0, "app": "Finder", "playing": False}
        with mock.patch.object(cli, "agent_reply", return_value=None), \
                mock.patch.object(cli.autoscreen, "preview",
                                  return_value=(signals, None, None)):
            cli.cmd_autoscreen(["status"])
        self.assertIn("?", self.out.getvalue())

    def test_unfreeze_asks_the_agent_to_let_go(self):
        reply = {"ok": True, "frozen": False, "enabled": True}
        with mock.patch.object(cli, "agent_reply", return_value=reply) as ask:
            cli.cmd_autoscreen(["unfreeze"])
        ask.assert_called_once_with("autoscreen", action="unfreeze")
        self.assertIn("unfrozen", self.out.getvalue())

    def test_a_freeze_with_no_agent_explains_where_it_lives(self):
        with mock.patch.object(cli, "agent_reply", return_value=None):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_autoscreen(["freeze"])
        self.assertIn("agent", str(caught.exception))

    def test_a_note_can_be_pinned_from_the_shell(self):
        reply = {"ok": True, "note": "buy milk"}
        with mock.patch.object(cli, "agent_reply", return_value=reply) as ask:
            cli.cmd_note(["buy", "milk"])
        ask.assert_called_once_with("note", action="set", text="buy milk")
        self.assertIn("buy milk", self.out.getvalue())

    def test_a_note_can_be_shown_and_taken_down(self):
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True, "note": "buy milk"}):
            cli.cmd_note([])
        self.assertIn("buy milk", self.out.getvalue())
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True, "note": None}) as ask:
            cli.cmd_note(["clear"])
        ask.assert_called_once_with("note", action="clear")

    def test_a_note_with_no_agent_says_so_instead_of_writing_anywhere(self):
        with mock.patch.object(cli, "agent_reply", return_value=None):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_note(["buy milk"])
        self.assertIn("agent", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
