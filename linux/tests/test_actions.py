"""Chord parsing, binding descriptions and dispatch routing on Linux.

Nothing here creates a uinput device or spawns anything: parse_chord and
describe are pure, and the dispatch tests replace the four things that would
actually touch the system. The whole file also has to work with python3-evdev
absent, which is why evdev is never imported at module scope - actions.py
hard-codes its keycode table for exactly that reason, and
EvdevIndependenceTests pins the claim down.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

import actions
from actions import (KEY_CODES, MODIFIER_FLAGS, ActionError, PermissionError_,
                     describe, parse_chord)

CTRL = actions.MOD_CTRL
ALT = actions.MOD_ALT
SHIFT = actions.MOD_SHIFT
SUPER = actions.MOD_SUPER
FN = actions.MOD_FN

A, C, K4 = 30, 46, 5          # evdev KEY_A, KEY_C, KEY_4


def setUpModule():
    """Pin the fallback table.

    layout_keys() is empty on Linux by design, but parse_chord consults it
    first for single characters, so a future resolver must not silently
    change what these assert.
    """
    actions._layout_keys = {}


class ParseChordTests(unittest.TestCase):

    def test_a_bare_key_has_no_modifiers(self):
        self.assertEqual(parse_chord("a"), (A, 0))

    def test_a_modified_key(self):
        self.assertEqual(parse_chord("cmd+shift+4"), (K4, SUPER | SHIFT))

    def test_every_key_name_parses_to_its_code(self):
        for name, code in KEY_CODES.items():
            with self.subTest(key=name):
                self.assertEqual(parse_chord(name), (code, 0))

    def test_every_modifier_name_parses_to_its_flag(self):
        for name, flag in MODIFIER_FLAGS.items():
            with self.subTest(modifier=name):
                self.assertEqual(parse_chord(f"{name}+a"), (A, flag))

    def test_cmd_and_meta_both_mean_super(self):
        """macOS configs are full of cmd+c; on Linux that is the Super key."""
        self.assertEqual(MODIFIER_FLAGS["cmd"], SUPER)
        self.assertEqual(MODIFIER_FLAGS["meta"], SUPER)
        self.assertEqual(parse_chord("cmd+c"), parse_chord("super+c"))
        self.assertEqual(parse_chord("meta+c"), parse_chord("super+c"))
        self.assertEqual(parse_chord("command+c"), parse_chord("super+c"))

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
        self.assertEqual(parse_chord("CMD+Shift+A"), (A, SUPER | SHIFT))

    def test_spaces_around_the_parts_are_ignored(self):
        self.assertEqual(parse_chord(" cmd + shift + a "), (A, SUPER | SHIFT))

    def test_modifier_order_does_not_matter(self):
        self.assertEqual(parse_chord("shift+cmd+a"), parse_chord("cmd+shift+a"))

    def test_four_modifiers_at_once(self):
        keycode, flags = parse_chord("ctrl+alt+shift+cmd+k")
        self.assertEqual(keycode, KEY_CODES["k"])
        self.assertEqual(flags, CTRL | ALT | SHIFT | SUPER)

    def test_a_repeated_modifier_is_harmless(self):
        self.assertEqual(parse_chord("cmd+cmd+a"), (A, SUPER))

    def test_the_fn_modifier(self):
        self.assertEqual(parse_chord("fn+f1"), (KEY_CODES["f1"], FN))

    def test_punctuation_keys(self):
        self.assertEqual(parse_chord("cmd+-"), (KEY_CODES["-"], SUPER))
        self.assertEqual(parse_chord("cmd+shift+="), (KEY_CODES["="], SUPER | SHIFT))

    def test_the_codes_are_the_kernels_own(self):
        """Not the macOS virtual keycodes: KEY_A is 30 here, not 0."""
        self.assertEqual(KEY_CODES["a"], 30)
        self.assertEqual(KEY_CODES["escape"], 1)
        self.assertEqual(KEY_CODES["space"], 57)
        self.assertEqual(KEY_CODES["f1"], 59)
        self.assertEqual(KEY_CODES["left"], 105)

    def test_a_non_string_chord_is_refused(self):
        for chord in (None, 7, ["cmd", "c"]):
            with self.subTest(chord=chord):
                with self.assertRaises(ActionError):
                    parse_chord(chord)

    def test_an_empty_chord_is_refused(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                with self.assertRaises(ActionError) as caught:
                    parse_chord(text)
                self.assertIn("Empty", str(caught.exception))

    def test_plus_names_the_plus_key_rather_than_being_empty(self):
        # "+" is the separator, so it used to parse to nothing at all. It now
        # names itself, and is refused here only because the kernel's table
        # has no plus key - shift+= is what a US board sends.
        for text in ("+", "++", " + "):
            with self.subTest(text=text):
                with self.assertRaises(ActionError) as caught:
                    parse_chord(text)
                self.assertIn("+", str(caught.exception))
                self.assertIn("key", str(caught.exception))

    def test_a_trailing_plus_leaves_a_modifier_where_the_key_should_be(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+")
        self.assertIn("cmd", str(caught.exception))

    def test_an_unknown_modifier_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("hyper+a")
        self.assertIn("hyper", str(caught.exception))
        self.assertIn("modifier", str(caught.exception))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+nope")
        self.assertIn("nope", str(caught.exception))
        self.assertIn("cmd+nope", str(caught.exception))

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
        for text in ("hyper+a", "cmd+nope", "", "cmd+", None):
            with self.subTest(text=text):
                with self.assertRaises(actions.ActionError):
                    parse_chord(text)


class SplitChordTests(unittest.TestCase):
    """The separator is also a key on several layouts, hence the edge cases."""

    def test_a_plain_chord_splits_on_the_plus(self):
        self.assertEqual(actions._split_chord("cmd+shift+a"),
                         ["cmd", "shift", "a"])

    def test_surrounding_space_is_stripped_from_every_part(self):
        self.assertEqual(actions._split_chord(" cmd + a "), ["cmd", "a"])

    def test_plus_on_its_own_is_the_plus_key(self):
        self.assertEqual(actions._split_chord("+"), ["+"])

    def test_a_trailing_plus_is_the_plus_key(self):
        self.assertEqual(actions._split_chord("cmd++"), ["cmd", "+"])

    def test_a_trailing_plus_reads_as_the_plus_key(self):
        # "cmd+" and "cmd++" split the same way: whatever follows the last
        # separator is the key, and here that is the plus key itself.
        self.assertEqual(actions._split_chord("cmd+"), ["cmd", "+"])
        self.assertEqual(actions._split_chord("cmd+"),
                         actions._split_chord("cmd++"))

    def test_an_empty_chord_splits_to_nothing(self):
        self.assertEqual(actions._split_chord(""), [])
        self.assertEqual(actions._split_chord("   "), [])

    def test_multi_character_parts_are_lowercased_and_single_ones_are_not(self):
        # A single character may be a key whose case matters to a layout
        # resolver; a name like "CMD" never is.
        self.assertEqual(actions._split_chord("CMD+A"), ["cmd", "A"])


class LayoutKeysTests(unittest.TestCase):
    """uinput carries scancodes, so there is no layout table to consult."""

    def test_the_layout_table_is_empty_on_linux(self):
        self.assertEqual(actions.layout_keys(), {})

    def test_a_layout_table_still_wins_for_single_characters(self):
        # The hook is kept for a future XKB-aware resolver; if one ever lands
        # it has to take precedence over the positional table.
        saved = actions._layout_keys
        actions._layout_keys = {"z": (16, False), ">": (50, True)}
        try:
            self.assertEqual(parse_chord("z")[0], 16)
            code, flags = parse_chord(">")
            self.assertEqual(code, 50)
            self.assertTrue(flags & SHIFT)
        finally:
            actions._layout_keys = saved


class EvdevIndependenceTests(unittest.TestCase):
    """The module has to import and parse chords with python3-evdev absent.

    That is the whole point of hard-coding KEY_CODES: `g510 bind` must still
    validate a config on a machine that cannot type, and this suite must run
    on one.
    """

    @staticmethod
    def load_privately():
        """A second copy of linux/actions.py, under its own module name."""
        path = os.path.abspath(actions.__file__)
        spec = importlib.util.spec_from_file_location("actions_no_evdev", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_is_the_linux_actions_module(self):
        self.assertTrue(
            os.path.dirname(os.path.abspath(actions.__file__)).endswith("/linux"))

    def test_nothing_in_this_test_module_imported_evdev(self):
        self.assertNotIn("evdev", [name for name in dir(sys.modules[__name__])])

    def test_the_module_imports_with_evdev_blocked(self):
        # sys.modules[name] = None is the standard way to make an import of
        # `name` fail, so this is what a box without python3-evdev looks like.
        with mock.patch.dict(sys.modules, {"evdev": None}):
            module = self.load_privately()
        self.assertIsNone(module.evdev)
        self.assertIsNotNone(module._EVDEV_ERROR)

    def test_chords_still_parse_with_evdev_blocked(self):
        with mock.patch.dict(sys.modules, {"evdev": None}):
            module = self.load_privately()
        self.assertEqual(module.KEY_CODES, KEY_CODES)
        self.assertEqual(module.parse_chord("cmd+c"), (C, SUPER))
        self.assertEqual(module.describe({"type": "keys", "keys": "cmd+c"}),
                         "cmd+c")

    def test_sending_a_chord_without_evdev_says_what_to_install(self):
        with mock.patch.dict(sys.modules, {"evdev": None}):
            module = self.load_privately()
        with self.assertRaises(module.ActionError) as caught:
            module._uinput()
        self.assertIn("python3-evdev", str(caught.exception))

    def test_events_cannot_be_posted_without_evdev(self):
        with mock.patch.dict(sys.modules, {"evdev": None}):
            module = self.load_privately()
        self.assertFalse(module.can_post_events())


class DispatchTests(unittest.TestCase):
    """Which binding type reaches which action. Nothing actually runs."""

    def setUp(self):
        # dispatch() probes uinput access first; say yes, so the routing is
        # what is under test rather than the permission gate.
        patch = mock.patch.object(actions, "can_post_events",
                                  return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = {}
        for name in ("send_chord", "type_text", "run_shell", "launch_app",
                     "play_macro"):
            patch = mock.patch.object(actions, name)
            self.calls[name] = patch.start()
            self.addCleanup(patch.stop)

    def assert_only(self, name, *args):
        self.calls[name].assert_called_once_with(*args)
        for other, stub in self.calls.items():
            if other != name:
                self.assertEqual(stub.call_count, 0, other)

    def test_a_keys_binding_sends_the_chord(self):
        actions.dispatch({"type": "keys", "keys": "cmd+c"})
        self.assert_only("send_chord", "cmd+c")

    def test_a_text_binding_types_the_text(self):
        actions.dispatch({"type": "text", "text": "hello"})
        self.assert_only("type_text", "hello")

    def test_a_shell_binding_runs_the_command(self):
        actions.dispatch({"type": "shell", "command": "echo hi"})
        self.assert_only("run_shell", "echo hi")

    def test_an_app_binding_launches_the_app(self):
        actions.dispatch({"type": "app", "name": "org.kde.konsole"})
        self.assert_only("launch_app", "org.kde.konsole")

    def test_a_macro_binding_plays_the_named_steps(self):
        steps = [{"keys": "cmd+c"}]
        actions.dispatch({"type": "macro", "name": "copy"}, {"copy": steps})
        self.assert_only("play_macro", steps)

    def test_a_macro_that_is_not_recorded_is_refused(self):
        for macros in (None, {}, {"copy": []}, {"other": [{"keys": "a"}]}):
            with self.subTest(macros=macros):
                with self.assertRaises(ActionError) as caught:
                    actions.dispatch({"type": "macro", "name": "copy"}, macros)
                self.assertIn("copy", str(caught.exception))

    def test_nothing_bound_does_nothing(self):
        for binding in (None, {}, ""):
            with self.subTest(binding=binding):
                actions.dispatch(binding)
        for stub in self.calls.values():
            self.assertEqual(stub.call_count, 0)

    def test_a_binding_that_is_not_an_object_is_refused(self):
        for binding in ("cmd+c", ["cmd+c"], 7):
            with self.subTest(binding=binding):
                with self.assertRaises(ActionError) as caught:
                    actions.dispatch(binding)
                self.assertIn("object", str(caught.exception))

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            actions.dispatch({"type": "teleport"})
        self.assertIn("teleport", str(caught.exception))

    def test_every_type_needs_its_own_field(self):
        for binding, field in (({"type": "keys"}, "keys"),
                               ({"type": "text"}, "text"),
                               ({"type": "shell"}, "command"),
                               ({"type": "app"}, "name")):
            with self.subTest(binding=binding):
                with self.assertRaises(ActionError) as caught:
                    actions.dispatch(binding)
                self.assertIn(field, str(caught.exception))

    def test_an_empty_field_counts_as_missing(self):
        with self.assertRaises(ActionError):
            actions.dispatch({"type": "keys", "keys": ""})


class DispatchPermissionTests(unittest.TestCase):
    """Bindings that synthesise keystrokes need /dev/uinput; the rest do not."""

    def setUp(self):
        patch = mock.patch.object(actions, "can_post_events",
                                  return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_types_that_need_uinput(self):
        self.assertEqual(set(actions.NEEDS_ACCESSIBILITY),
                         {"keys", "text", "macro"})

    def test_a_keys_binding_without_access_names_uinput(self):
        with mock.patch.object(actions, "_text_helper", return_value=None):
            with self.assertRaises(PermissionError_) as caught:
                actions.dispatch({"type": "keys", "keys": "cmd+c"})
        self.assertIn(actions.UINPUT_PATH, str(caught.exception))

    def test_a_permission_failure_is_still_an_action_error(self):
        # cli.py and the daemon catch ActionError; this must not slip past.
        self.assertTrue(issubclass(PermissionError_, ActionError))

    def test_a_macro_binding_without_access_is_refused(self):
        with mock.patch.object(actions, "_text_helper", return_value=None):
            with self.assertRaises(PermissionError_):
                actions.dispatch({"type": "macro", "name": "copy"},
                                 {"copy": [{"keys": "a"}]})

    def test_shell_and_app_bindings_need_no_access(self):
        with mock.patch.object(actions, "run_shell") as shell:
            actions.dispatch({"type": "shell", "command": "echo hi"})
        shell.assert_called_once_with("echo hi")
        with mock.patch.object(actions, "launch_app") as app:
            actions.dispatch({"type": "app", "name": "firefox"})
        app.assert_called_once_with("firefox")

    def test_a_text_binding_is_exempt_when_a_helper_can_type_it(self):
        """wtype and xdotool never touch uinput, so refusing them would lie."""
        with mock.patch.object(actions, "_text_helper", return_value="wtype"):
            with mock.patch.object(actions, "type_text") as typed:
                actions.dispatch({"type": "text", "text": "hi"})
        typed.assert_called_once_with("hi")

    def test_a_text_binding_is_refused_when_nothing_can_type_it(self):
        with mock.patch.object(actions, "_text_helper", return_value=None):
            with self.assertRaises(PermissionError_):
                actions.dispatch({"type": "text", "text": "hi"})


class RequiredFieldTests(unittest.TestCase):

    def test_a_present_field_comes_back(self):
        self.assertEqual(actions._required({"type": "keys", "keys": "a"},
                                           "keys"), "a")

    def test_a_missing_field_names_the_type_and_the_field(self):
        with self.assertRaises(ActionError) as caught:
            actions._required({"type": "shell"}, "command")
        self.assertIn("shell", str(caught.exception))
        self.assertIn("command", str(caught.exception))

    def test_none_and_empty_are_both_missing(self):
        for value in (None, ""):
            with self.subTest(value=value):
                with self.assertRaises(ActionError):
                    actions._required({"type": "text", "text": value}, "text")


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
        self.assertEqual(describe({"type": "shell", "command": "notify-send hi"}),
                         "$ notify-send hi")

    def test_shell_is_truncated_with_an_ellipsis(self):
        self.assertEqual(describe({"type": "shell", "command": "y" * 22}),
                         "$ %s" % ("y" * 22))
        self.assertEqual(describe({"type": "shell", "command": "y" * 23}),
                         "$ %s..." % ("y" * 22))

    def test_app_binding(self):
        self.assertEqual(describe({"type": "app", "name": "org.kde.konsole"}),
                         "open org.kde.konsole")

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
        result = describe({"name": "firefox"})
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
        """A hand-edited config is the input here, so nothing may raise."""
        for binding in ({"type": "keys", "keys": None},
                        {"type": "text", "text": None},
                        {"type": "text", "text": 5},
                        {"type": "shell", "command": 5}):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)

    def test_a_binding_that_is_not_a_dict_does_not_crash(self):
        for binding in ("cmd+c", ["cmd+c"], 7):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)

    def test_the_default_bindings_all_describe(self):
        import config
        for key, binding in config.DEFAULTS["bindings"].items():
            with self.subTest(key=key):
                self.assertTrue(describe(binding))

    def test_the_default_key_bindings_all_parse(self):
        """The Linux defaults have to be chords this platform can send.

        This caught two that were not: G3 was "shift+print" when KEY_CODES
        had no "print" at all, and G4 was "super", a modifier with no key
        after it, which no chord can express. Both would have raised
        ActionError the first time the key was pressed on a fresh install.
        KEY_CODES gained print/printscreen/sysrq; G4 became an app binding.
        """
        import config
        for key, binding in config.DEFAULTS["bindings"].items():
            if isinstance(binding, dict) and binding.get("type") == "keys":
                with self.subTest(key=key):
                    self.assertIsInstance(parse_chord(binding["keys"]), tuple)


class TextHelperTests(unittest.TestCase):

    def test_as_text_turns_anything_into_a_string(self):
        self.assertEqual(actions._as_text(None), "")
        self.assertEqual(actions._as_text(""), "")
        self.assertEqual(actions._as_text(5), "5")
        self.assertEqual(actions._as_text("hi"), "hi")
        self.assertEqual(actions._as_text(["a"]), "['a']")

    def test_clip_leaves_short_text_alone(self):
        self.assertEqual(actions._clip("abc", 5), "abc")
        self.assertEqual(actions._clip("abcde", 5), "abcde")

    def test_clip_marks_what_it_cut(self):
        self.assertEqual(actions._clip("abcdef", 5), "abcde...")

    def test_clip_of_nothing(self):
        self.assertEqual(actions._clip("", 5), "")


if __name__ == "__main__":
    unittest.main()
