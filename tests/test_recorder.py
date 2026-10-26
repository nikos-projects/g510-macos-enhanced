"""Spelling recorded key events back as chords, and summarising a macro.

Both are pure: nothing here creates an event tap.
"""
import unittest

import Quartz

import actions
from recorder import describe_event, summarise

CMD = Quartz.kCGEventFlagMaskCommand
SHIFT = Quartz.kCGEventFlagMaskShift
ALT = Quartz.kCGEventFlagMaskAlternate
CTRL = Quartz.kCGEventFlagMaskControl
FN = Quartz.kCGEventFlagMaskSecondaryFn
NUMPAD = Quartz.kCGEventFlagMaskNumericPad
CAPS = Quartz.kCGEventFlagMaskAlphaShift

A, C, K4, LEFT, F1 = 0, 8, 21, 123, 122


def setUpModule():
    """Pin the fallback table.

    parse_chord normally resolves a single character against the keyboard
    layout in use, so these would otherwise assert different keycodes on a
    German machine than on a US one. the round-trip invariant is about the fallback table; the layout path has its own tests.
    """
    actions._layout_keys = {}


class DescribeEventTests(unittest.TestCase):

    def test_an_unmodified_key(self):
        self.assertEqual(describe_event(A, 0), "a")

    def test_a_modified_key(self):
        self.assertEqual(describe_event(C, CMD), "cmd+c")

    def test_modifiers_are_spelled_in_a_fixed_order(self):
        # ctrl, alt, shift, cmd - the order the chord is shown back in.
        self.assertEqual(describe_event(K4, CMD | SHIFT), "shift+cmd+4")
        self.assertEqual(describe_event(K4, SHIFT | CMD), "shift+cmd+4")

    def test_all_four_modifiers(self):
        self.assertEqual(describe_event(A, CTRL | ALT | SHIFT | CMD),
                         "ctrl+alt+shift+cmd+a")

    def test_each_modifier_on_its_own(self):
        self.assertEqual(describe_event(A, CTRL), "ctrl+a")
        self.assertEqual(describe_event(A, ALT), "alt+a")
        self.assertEqual(describe_event(A, SHIFT), "shift+a")
        self.assertEqual(describe_event(A, CMD), "cmd+a")

    def test_the_first_spelling_of_a_key_is_the_one_used(self):
        self.assertEqual(describe_event(36, 0), "return")
        self.assertEqual(describe_event(51, 0), "delete")
        self.assertEqual(describe_event(53, 0), "escape")

    def test_function_and_arrow_keys(self):
        self.assertEqual(describe_event(F1, 0), "f1")
        self.assertEqual(describe_event(LEFT, 0), "left")

    def test_flags_that_are_not_chord_modifiers_are_ignored(self):
        # macOS sets the numeric-pad and secondary-fn bits on the arrow keys;
        # carrying them into the chord would make every arrow unreplayable.
        self.assertEqual(describe_event(LEFT, NUMPAD | FN), "left")
        self.assertEqual(describe_event(A, CAPS), "a")
        self.assertEqual(describe_event(C, CMD | NUMPAD | FN), "cmd+c")

    def test_a_key_with_no_name_is_dropped(self):
        for keycode in (10, 999, -1, 65):
            with self.subTest(keycode=keycode):
                self.assertIsNone(describe_event(keycode, 0))

    def test_an_unnamed_key_is_dropped_whatever_the_modifiers(self):
        self.assertIsNone(describe_event(999, CMD | SHIFT))

    def test_everything_it_produces_can_be_replayed(self):
        # A recorded chord goes straight back through parse_chord on playback,
        # so every spelling this produces has to survive the round trip.
        for keycode in sorted(set(actions.KEY_CODES.values())):
            for flags in (0, CMD, CTRL | ALT | SHIFT | CMD):
                with self.subTest(keycode=keycode, flags=flags):
                    chord = describe_event(keycode, flags)
                    self.assertIsNotNone(chord)
                    self.assertEqual(actions.parse_chord(chord),
                                     (keycode, flags))


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
        steps = [{"keys": describe_event(C, CMD)},
                 {"keys": describe_event(K4, CMD | SHIFT), "delay": 0.4}]
        self.assertEqual(summarise(steps), "cmd+c, shift+cmd+4")


if __name__ == "__main__":
    unittest.main()
