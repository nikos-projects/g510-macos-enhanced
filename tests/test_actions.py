"""Chord parsing and binding descriptions.

Nothing here posts an event: parse_chord and describe are pure, which is the
whole reason they are worth pinning down.
"""
import unittest

import Quartz

import actions
from actions import KEY_CODES, MODIFIER_FLAGS, ActionError, describe, parse_chord

CMD = Quartz.kCGEventFlagMaskCommand
SHIFT = Quartz.kCGEventFlagMaskShift
ALT = Quartz.kCGEventFlagMaskAlternate
CTRL = Quartz.kCGEventFlagMaskControl
FN = Quartz.kCGEventFlagMaskSecondaryFn


def setUpModule():
    """Pin the fallback table.

    parse_chord normally resolves a single character against the keyboard
    layout in use, so these would otherwise assert different keycodes on a
    German machine than on a US one. chord parsing is tested against the positional US table; the layout path has its own tests.
    """
    actions._layout_keys = {}


class ParseChordTests(unittest.TestCase):

    def test_a_bare_key_has_no_modifiers(self):
        self.assertEqual(parse_chord("a"), (0, 0))

    def test_a_modified_key(self):
        self.assertEqual(parse_chord("cmd+shift+4"), (KEY_CODES["4"], CMD | SHIFT))

    def test_every_key_name_parses_to_its_code(self):
        for name, code in KEY_CODES.items():
            with self.subTest(key=name):
                self.assertEqual(parse_chord(name), (code, 0))

    def test_every_modifier_name_parses_to_its_flag(self):
        for name, flag in MODIFIER_FLAGS.items():
            with self.subTest(modifier=name):
                self.assertEqual(parse_chord(f"{name}+a"), (0, flag))

    def test_modifier_aliases_agree(self):
        self.assertEqual(parse_chord("cmd+a"), parse_chord("command+a"))
        self.assertEqual(parse_chord("alt+a"), parse_chord("option+a"))
        self.assertEqual(parse_chord("alt+a"), parse_chord("opt+a"))
        self.assertEqual(parse_chord("ctrl+a"), parse_chord("control+a"))

    def test_key_aliases_agree(self):
        self.assertEqual(parse_chord("return"), parse_chord("enter"))
        self.assertEqual(parse_chord("delete"), parse_chord("backspace"))
        self.assertEqual(parse_chord("escape"), parse_chord("esc"))

    def test_case_is_ignored(self):
        self.assertEqual(parse_chord("CMD+Shift+A"), (KEY_CODES["a"], CMD | SHIFT))

    def test_spaces_around_the_parts_are_ignored(self):
        self.assertEqual(parse_chord(" cmd + shift + a "),
                         (KEY_CODES["a"], CMD | SHIFT))

    def test_modifier_order_does_not_matter(self):
        self.assertEqual(parse_chord("shift+cmd+a"), parse_chord("cmd+shift+a"))

    def test_four_modifiers_at_once(self):
        keycode, flags = parse_chord("ctrl+alt+shift+cmd+k")
        self.assertEqual(keycode, KEY_CODES["k"])
        self.assertEqual(flags, CTRL | ALT | SHIFT | CMD)

    def test_a_repeated_modifier_is_harmless(self):
        self.assertEqual(parse_chord("cmd+cmd+a"), (KEY_CODES["a"], CMD))

    def test_the_fn_modifier(self):
        self.assertEqual(parse_chord("fn+f1"), (KEY_CODES["f1"], FN))

    def test_punctuation_keys(self):
        self.assertEqual(parse_chord("cmd+-"), (KEY_CODES["-"], CMD))
        self.assertEqual(parse_chord("cmd+shift+="), (KEY_CODES["="], CMD | SHIFT))

    def test_an_empty_chord_is_refused(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                with self.assertRaises(ActionError) as caught:
                    parse_chord(text)
                self.assertIn("Empty", str(caught.exception))

    def test_plus_names_the_plus_key_rather_than_being_empty(self):
        # "+" is the separator, so it used to parse to nothing at all. It is
        # a main-row key on several layouts, so it now names itself - and is
        # refused here only because the US fallback table has no plus key.
        for text in ("+", "++", " + "):
            with self.subTest(text=text):
                with self.assertRaises(ActionError) as caught:
                    parse_chord(text)
                self.assertIn("+", str(caught.exception))
                self.assertIn("key", str(caught.exception))

    def test_an_unknown_modifier_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("meta+a")
        self.assertIn("meta", str(caught.exception))
        self.assertIn("modifier", str(caught.exception))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+nope")
        self.assertIn("nope", str(caught.exception))
        self.assertIn("cmd+nope", str(caught.exception))

    def test_a_trailing_plus_leaves_a_modifier_where_the_key_should_be(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+")
        self.assertIn("cmd", str(caught.exception))

    def test_a_modifier_alone_is_not_a_key(self):
        for text in ("cmd", "shift", "ctrl+shift"):
            with self.subTest(text=text):
                with self.assertRaises(ActionError):
                    parse_chord(text)

    def test_a_key_in_the_modifier_position_is_refused(self):
        with self.assertRaises(ActionError):
            parse_chord("a+b")

    def test_unknown_keys_and_modifiers_raise_the_same_family(self):
        # Callers catch ActionError; nothing may escape as a bare ValueError.
        for text in ("meta+a", "cmd+nope", "", "cmd+"):
            with self.subTest(text=text):
                with self.assertRaises(actions.ActionError):
                    parse_chord(text)


class DescribeTests(unittest.TestCase):

    def test_key_binding(self):
        self.assertEqual(describe({"type": "keys", "keys": "cmd+shift+4"}),
                         "cmd+shift+4")

    def test_text_binding(self):
        self.assertEqual(describe({"type": "text", "text": "hello"}),
                         'type "hello"')

    def test_text_is_truncated_with_an_ellipsis(self):
        self.assertEqual(describe({"type": "text", "text": "x" * 18}),
                         'type "%s"' % ("x" * 18))
        self.assertEqual(describe({"type": "text", "text": "x" * 19}),
                         'type "%s..."' % ("x" * 18))

    def test_shell_binding(self):
        self.assertEqual(describe({"type": "shell", "command": "say hello"}),
                         "$ say hello")

    def test_shell_is_truncated_with_an_ellipsis(self):
        self.assertEqual(describe({"type": "shell", "command": "y" * 22}),
                         "$ %s" % ("y" * 22))
        self.assertEqual(describe({"type": "shell", "command": "y" * 23}),
                         "$ %s..." % ("y" * 22))

    def test_app_binding(self):
        self.assertEqual(describe({"type": "app", "name": "Safari"}),
                         "open Safari")

    def test_macro_binding(self):
        self.assertEqual(describe({"type": "macro", "name": "G1-m1"}),
                         "macro G1-m1")

    def test_nothing_bound(self):
        self.assertEqual(describe(None), "-")
        self.assertEqual(describe({}), "-")

    def test_a_missing_field_does_not_crash(self):
        self.assertEqual(describe({"type": "keys"}), "?")
        self.assertEqual(describe({"type": "app"}), "open ?")
        self.assertEqual(describe({"type": "macro"}), "macro ?")
        self.assertEqual(describe({"type": "text"}), 'type ""')
        self.assertEqual(describe({"type": "shell"}), "$ ")

    def test_an_unknown_type_is_shown_as_itself(self):
        self.assertEqual(describe({"type": "teleport"}), "teleport")

    def test_a_binding_with_no_type_does_not_crash(self):
        result = describe({"name": "Safari"})
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    def test_every_well_formed_type_survives_a_missing_field(self):
        for kind in ("keys", "text", "shell", "app", "macro", "nonsense"):
            with self.subTest(kind=kind):
                result = describe({"type": kind})
                self.assertIsInstance(result, str)
                self.assertTrue(result)

    def test_a_non_string_name_is_survivable(self):
        self.assertEqual(describe({"type": "app", "name": 7}), "open 7")
        self.assertEqual(describe({"type": "macro", "name": None}), "macro ?")

    def test_a_field_of_the_wrong_type_does_not_crash(self):
        # BUG: describe() trusts the field types a config file happens to
        # carry. A null or numeric "keys"/"text"/"command" - easy to leave
        # behind in a hand edit, and what an unfinished GUI binding looks like
        # - either returns None instead of a string ("keys") or raises
        # TypeError on the slice ("text", "shell"). Reported, not fixed.
        for binding in ({"type": "keys", "keys": None},
                        {"type": "text", "text": None},
                        {"type": "text", "text": 5},
                        {"type": "shell", "command": 5}):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)

    def test_the_default_bindings_all_describe(self):
        import config
        for key, binding in config.DEFAULTS["bindings"].items():
            with self.subTest(key=key):
                self.assertTrue(describe(binding))

    def test_a_binding_that_is_not_a_dict_does_not_crash(self):
        # BUG: describe() calls binding.get() on whatever it is handed, so a
        # hand-edited config with "G1": "cmd+c" raises AttributeError in the
        # menus and listings rather than showing anything. Reported, not fixed.
        for binding in ("cmd+c", ["cmd+c"], 7):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)


if __name__ == "__main__":
    unittest.main()


class LayoutResolutionTests(unittest.TestCase):
    """Chords resolve against the keyboard in use, not a hard-coded US table."""

    def setUp(self):
        self.saved = actions._layout_keys
        # A stand-in for a German layout: y and z swapped against the US
        # positions, and an umlaut where the US board has a bracket.
        actions._layout_keys = {
            "z": (16, False), "y": (6, False), "ü": (33, False),
            "<": (50, False), ">": (50, True), "+": (30, False),
        }

    def tearDown(self):
        actions._layout_keys = self.saved

    def test_a_character_uses_the_layout_not_the_us_position(self):
        self.assertEqual(parse_chord("z")[0], 16)
        self.assertEqual(parse_chord("y")[0], 6)
        self.assertNotEqual(parse_chord("z")[0], KEY_CODES["z"])

    def test_a_key_the_us_table_has_no_name_for(self):
        self.assertEqual(parse_chord("ü")[0], 33)
        self.assertEqual(parse_chord("cmd+ü"), (33, MODIFIER_FLAGS["cmd"]))

    def test_a_shifted_character_adds_the_shift_flag(self):
        code, flags = parse_chord(">")
        self.assertEqual(code, 50)
        self.assertTrue(flags & MODIFIER_FLAGS["shift"])
        self.assertEqual(parse_chord("<"), (50, 0))

    def test_plus_resolves_through_the_layout(self):
        self.assertEqual(parse_chord("+")[0], 30)
        self.assertEqual(parse_chord("cmd++"), (30, MODIFIER_FLAGS["cmd"]))

    def test_named_keys_stay_positional(self):
        for name in ("return", "f5", "left", "space"):
            with self.subTest(name=name):
                self.assertEqual(parse_chord(name)[0], KEY_CODES[name])

    def test_an_unknown_character_is_still_refused(self):
        with self.assertRaises(ActionError):
            parse_chord("§")
