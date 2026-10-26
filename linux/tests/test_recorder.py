"""Spelling recorded key events back as chords, and summarising a macro.

Both are pure. The Recorder's event folding is exercised by handing _handle()
made-up events, so nothing here opens /dev/input, grabs a keyboard or needs
python3-evdev - recorder.py keeps its evdev import optional for that reason.
"""
import collections
import time
import unittest
from unittest import mock

import actions
import recorder
from recorder import MAX_STEPS, describe_event, summarise

CTRL = actions.MOD_CTRL
ALT = actions.MOD_ALT
SHIFT = actions.MOD_SHIFT
SUPER = actions.MOD_SUPER
FN = actions.MOD_FN

# evdev codes: KEY_A, KEY_C, KEY_4, KEY_LEFT, KEY_F1, KEY_ENTER, KEY_ESC.
A, C, K4, LEFT, F1, RETURN, ESCAPE = 30, 46, 5, 105, 59, 28, 1
EV_KEY = 0x01
EV_SYN = 0x00

_Event = collections.namedtuple("_Event", "type code value")


def key(code, value=1, kind=EV_KEY):
    return _Event(kind, code, value)


def setUpModule():
    """Pin the fallback table; see the same note in test_actions."""
    actions._layout_keys = {}


class DescribeEventTests(unittest.TestCase):

    def test_an_unmodified_key(self):
        self.assertEqual(describe_event(A, 0), "a")

    def test_a_modified_key(self):
        self.assertEqual(describe_event(C, SUPER), "cmd+c")

    def test_super_is_spelled_cmd(self):
        """parse_chord takes either, and cmd is what the macOS build wrote."""
        self.assertEqual(describe_event(C, SUPER), "cmd+c")
        self.assertEqual(actions.parse_chord("cmd+c"),
                         actions.parse_chord("super+c"))

    def test_modifiers_are_spelled_in_a_fixed_order(self):
        # ctrl, alt, shift, cmd - the order the chord is shown back in.
        self.assertEqual(describe_event(K4, SUPER | SHIFT), "shift+cmd+4")
        self.assertEqual(describe_event(K4, SHIFT | SUPER), "shift+cmd+4")

    def test_all_four_modifiers(self):
        self.assertEqual(describe_event(A, CTRL | ALT | SHIFT | SUPER),
                         "ctrl+alt+shift+cmd+a")

    def test_each_modifier_on_its_own(self):
        self.assertEqual(describe_event(A, CTRL), "ctrl+a")
        self.assertEqual(describe_event(A, ALT), "alt+a")
        self.assertEqual(describe_event(A, SHIFT), "shift+a")
        self.assertEqual(describe_event(A, SUPER), "cmd+a")

    def test_the_first_spelling_of_a_key_is_the_one_used(self):
        self.assertEqual(describe_event(RETURN, 0), "return")
        self.assertEqual(describe_event(14, 0), "delete")
        self.assertEqual(describe_event(ESCAPE, 0), "escape")

    def test_function_and_arrow_keys(self):
        self.assertEqual(describe_event(F1, 0), "f1")
        self.assertEqual(describe_event(LEFT, 0), "left")

    def test_the_fn_flag_is_left_out_of_the_chord(self):
        """fn is unreplayable through uinput, so carrying it would break the
        macro it appears in."""
        self.assertEqual(describe_event(LEFT, FN), "left")
        self.assertEqual(describe_event(C, SUPER | FN), "cmd+c")

    def test_a_key_with_no_name_is_dropped(self):
        for keycode in (999, -1, 0x290, 190):
            with self.subTest(keycode=keycode):
                self.assertIsNone(describe_event(keycode, 0))

    def test_an_unnamed_key_is_dropped_whatever_the_modifiers(self):
        self.assertIsNone(describe_event(999, SUPER | SHIFT))

    def test_everything_it_produces_can_be_replayed(self):
        # A recorded chord goes straight back through parse_chord on playback,
        # so every spelling this produces has to survive the round trip.
        for keycode in sorted(set(actions.KEY_CODES.values())):
            for flags in (0, SUPER, SHIFT, CTRL | ALT | SHIFT | SUPER):
                with self.subTest(keycode=keycode, flags=flags):
                    chord = describe_event(keycode, flags)
                    self.assertIsNotNone(chord)
                    self.assertEqual(actions.parse_chord(chord),
                                     (keycode, flags))

    def test_every_named_key_has_exactly_one_spelling(self):
        for code, name in recorder.CODE_NAMES.items():
            with self.subTest(code=code):
                self.assertEqual(actions.KEY_CODES[name], code)


class ModifierCodeTests(unittest.TestCase):
    """Both halves of a modifier report the same flag."""

    def test_the_left_and_right_key_of_each_modifier_agree(self):
        for left, right in ((29, 97), (56, 100), (42, 54), (125, 126)):
            with self.subTest(pair=(left, right)):
                self.assertEqual(recorder.MODIFIER_CODES[left],
                                 recorder.MODIFIER_CODES[right])

    def test_altgr_counts_as_alt(self):
        self.assertEqual(recorder.MODIFIER_CODES[100], actions.MOD_ALT)

    def test_the_flags_are_the_ones_actions_parses(self):
        self.assertEqual(set(recorder.MODIFIER_CODES.values()),
                         {CTRL, ALT, SHIFT, SUPER, FN})


class HandleEventTests(unittest.TestCase):
    """Folding one event into the step list. No device is ever opened."""

    def setUp(self):
        self.recorder = recorder.Recorder()
        self.addCleanup(self.recorder._close)

    def test_a_key_press_becomes_a_step(self):
        self.assertFalse(self.recorder._handle(key(C)))
        self.assertEqual(self.recorder.steps, [{"keys": "c"}])

    def test_a_step_is_just_the_chord_when_there_is_no_pause(self):
        self.recorder._handle(key(C))
        self.assertEqual(set(self.recorder.steps[0]), {"keys"})

    def test_releases_and_autorepeat_are_not_steps(self):
        for value in (0, 2):
            with self.subTest(value=value):
                self.recorder._handle(key(C, value))
        self.assertEqual(self.recorder.steps, [])

    def test_events_that_are_not_key_events_are_ignored(self):
        self.assertFalse(self.recorder._handle(key(C, 1, EV_SYN)))
        self.assertEqual(self.recorder.steps, [])

    def test_a_held_modifier_joins_the_next_chord(self):
        self.recorder._handle(key(125))            # KEY_LEFTMETA down
        self.recorder._handle(key(C))
        self.assertEqual(self.recorder.steps, [{"keys": "cmd+c"}])

    def test_releasing_a_modifier_clears_it(self):
        self.recorder._handle(key(125))
        self.recorder._handle(key(125, 0))
        self.recorder._handle(key(C))
        self.assertEqual(self.recorder.steps, [{"keys": "c"}])

    def test_a_modifier_is_never_a_step_of_its_own(self):
        self.recorder._handle(key(42))
        self.recorder._handle(key(42, 0))
        self.assertEqual(self.recorder.steps, [])

    def test_a_key_with_no_chord_spelling_is_dropped(self):
        self.assertFalse(self.recorder._handle(key(0x290)))   # KEY_MACRO1
        self.assertEqual(self.recorder.steps, [])

    def test_escape_finishes_the_recording(self):
        self.assertTrue(self.recorder._handle(key(ESCAPE)))
        self.assertEqual(self.recorder.steps, [])

    def test_escape_on_release_does_not_finish_it(self):
        self.assertFalse(self.recorder._handle(key(ESCAPE, 0)))

    def test_the_callback_sees_each_chord_and_its_number(self):
        seen = []
        rec = recorder.Recorder(on_step=lambda chord, n: seen.append((chord, n)))
        self.addCleanup(rec._close)
        rec._handle(key(C))
        rec._handle(key(A))
        self.assertEqual(seen, [("c", 1), ("a", 2)])

    def test_recording_stops_at_the_step_limit(self):
        for index in range(MAX_STEPS - 1):
            with self.subTest(step=index):
                self.assertFalse(self.recorder._handle(key(A)))
        self.assertTrue(self.recorder._handle(key(A)))
        self.assertEqual(len(self.recorder.steps), MAX_STEPS)

    def test_the_step_limit_is_sane(self):
        self.assertEqual(MAX_STEPS, 64)

    def test_every_recorded_step_replays(self):
        for code in (A, C, K4, LEFT, F1, RETURN):
            self.recorder._handle(key(code))
        for step in self.recorder.steps:
            with self.subTest(step=step):
                self.assertIsInstance(actions.parse_chord(step["keys"]), tuple)


class StepDelayTests(unittest.TestCase):
    """A pause is only recorded when it is worth replaying."""

    def setUp(self):
        self.recorder = recorder.Recorder()
        self.addCleanup(self.recorder._close)

    def handle_at(self, moment, code=A):
        with mock.patch.object(time, "time", return_value=moment):
            self.recorder._handle(key(code))

    def test_the_first_step_has_no_delay(self):
        self.handle_at(1000.0)
        self.assertNotIn("delay", self.recorder.steps[0])

    def test_a_short_gap_is_not_worth_recording(self):
        self.handle_at(1000.0)
        self.handle_at(1000.05)              # exactly the threshold
        self.assertNotIn("delay", self.recorder.steps[1])

    def test_a_real_pause_is_recorded(self):
        self.handle_at(1000.0)
        self.handle_at(1000.5)
        self.assertEqual(self.recorder.steps[1]["delay"], 0.5)

    def test_a_pause_is_rounded_to_hundredths(self):
        self.handle_at(1000.0)
        self.handle_at(1000.123456)
        self.assertEqual(self.recorder.steps[1]["delay"], 0.12)

    def test_a_long_pause_is_capped(self):
        """Nobody wants a macro that sits there for a minute on replay."""
        self.handle_at(1000.0)
        self.handle_at(1060.0)
        self.assertEqual(self.recorder.steps[1]["delay"], 2.0)

    def test_delays_are_measured_between_steps_not_from_the_start(self):
        self.handle_at(1000.0)
        self.handle_at(1000.5)
        self.handle_at(1001.0)
        self.assertEqual([step.get("delay") for step in self.recorder.steps],
                         [None, 0.5, 0.5])

    def test_a_dropped_key_does_not_reset_the_clock(self):
        self.handle_at(1000.0)
        self.handle_at(1000.2, code=0x290)   # unnamed, so not a step
        self.handle_at(1000.5)
        self.assertEqual(len(self.recorder.steps), 2)
        self.assertEqual(self.recorder.steps[1]["delay"], 0.5)

    def test_every_step_is_replayable_by_play_macro(self):
        # play_macro reads exactly these two fields, and nothing else.
        self.handle_at(1000.0)
        self.handle_at(1000.5)
        for step in self.recorder.steps:
            with self.subTest(step=step):
                self.assertLessEqual(set(step), {"keys", "delay"})


class StopTests(unittest.TestCase):
    """stop() has to work before run() as well as during it."""

    def test_stopping_before_the_loop_starts_is_harmless(self):
        rec = recorder.Recorder()
        self.addCleanup(rec._close)
        rec.stop()

    def test_closing_twice_is_harmless(self):
        rec = recorder.Recorder()
        rec._close()
        rec._close()
        rec.stop()


class SummariseTests(unittest.TestCase):

    def test_a_short_macro_is_shown_in_full(self):
        steps = [{"keys": "cmd+c"}, {"keys": "cmd+tab"}, {"keys": "cmd+v"}]
        self.assertEqual(summarise(steps), "cmd+c, cmd+tab, cmd+v")

    def test_an_empty_macro(self):
        self.assertEqual(summarise([]), "")

    def test_one_step(self):
        self.assertEqual(summarise([{"keys": "cmd+c"}]), "cmd+c")

    def test_exactly_the_limit_is_not_abbreviated(self):
        steps = [{"keys": "a"}] * 4
        self.assertEqual(summarise(steps), "a, a, a, a")

    def test_a_long_macro_is_counted_instead(self):
        steps = [{"keys": f"f{n}"} for n in range(1, 8)]
        self.assertEqual(summarise(steps), "f1, f2, f3, f4, +3 more")

    def test_the_limit_is_adjustable(self):
        steps = [{"keys": "a"}, {"keys": "b"}, {"keys": "c"}]
        self.assertEqual(summarise(steps, limit=2), "a, b, +1 more")
        self.assertEqual(summarise(steps, limit=10), "a, b, c")

    def test_a_step_with_no_chord_is_marked_rather_than_dropped(self):
        self.assertEqual(summarise([{"delay": 0.5}, {"keys": "cmd+c"}]),
                         "?, cmd+c")

    def test_delays_are_not_shown(self):
        steps = [{"keys": "cmd+c", "delay": 1.5}, {"keys": "cmd+v", "delay": 2}]
        self.assertEqual(summarise(steps), "cmd+c, cmd+v")

    def test_a_recorded_macro_summarises(self):
        steps = [{"keys": describe_event(C, SUPER)},
                 {"keys": describe_event(K4, SUPER | SHIFT), "delay": 0.4}]
        self.assertEqual(summarise(steps), "cmd+c, shift+cmd+4")


class ModuleTests(unittest.TestCase):

    def test_it_is_the_linux_recorder(self):
        import os
        self.assertTrue(os.path.dirname(
            os.path.abspath(recorder.__file__)).endswith("/linux"))

    def test_it_works_without_the_evdev_bindings(self):
        """Only run() needs evdev; the spellings must not."""
        if recorder.evdev is None:
            self.assertIsNotNone(recorder._EVDEV_ERROR)
            rec = recorder.Recorder()
            self.addCleanup(rec._close)
            with self.assertRaises(recorder.RecordingError) as caught:
                rec.run(timeout=0)
            self.assertIn("python3-evdev", str(caught.exception))
        self.assertEqual(describe_event(C, SUPER), "cmd+c")


if __name__ == "__main__":
    unittest.main()
