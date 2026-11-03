"""Which keys the menu takes on Linux, and which it leaves alone.

`feed` is the whole policy laid out as a plain function, so this runs with no
keyboard grabbed, no uinput device and no compositor - which matters, because
these tests are also what pins the contract the daemon's `wants_menu_key`
policy is written against. The keycodes are the kernel's input-event-codes.h
values: a stable ABI, which is why linux/keytap.py hard-codes them and why
they can be written out here without a keyboard to read them from.

The shape of every case is the macOS suite's (tests/test_keytap.py), with
macOS keycodes swapped for kernel ones. The two files drifting apart would
mean the same policy behaving differently on the two platforms, which is the
one thing this port must not do.
"""
import os
import tempfile
import unittest

import keytap


class Recording:
    """A policy that says yes to a fixed set, and remembers what it saw."""

    def __init__(self, wanted):
        self.wanted = set(wanted)
        self.seen = []

    def __call__(self, name, character):
        self.seen.append((name, character))
        return name in self.wanted


class Feed(unittest.TestCase):
    def setUp(self):
        self.policy = Recording(keytap.NAV_KEYS)
        self.tap = keytap.KeyTap(decide=self.policy, log=self._log)
        self.logged = []

    def _log(self, message):
        self.logged.append(message)

    def test_navigation_keys_are_taken_with_their_keyup(self):
        self.assertTrue(self.tap.feed("down", keytap.UP, None))
        self.assertEqual(self.tap.drain(), [("up", None)])
        self.assertTrue(self.tap.feed("up", keytap.UP, None))
        # A keyUp with no keyDown behind it belongs to something else.
        self.assertFalse(self.tap.feed("up", keytap.UP, None))

    def test_a_character_is_taken_only_when_the_policy_wants_it(self):
        self.policy.wanted = {"a"}
        self.assertTrue(self.tap.feed("down", 30, "a"))        # KEY_A
        self.assertEqual(self.tap.drain(), [("a", "a")])
        self.assertTrue(self.tap.feed("up", 30, None))
        self.policy.wanted = set()
        self.assertFalse(self.tap.feed("down", 48, "b"))       # KEY_B
        self.assertEqual(self.tap.drain(), [])

    def test_a_shortcut_is_never_taken(self):
        self.policy.wanted = {"c"}
        # Super-C is the application's, however much the menu might like it.
        self.assertFalse(self.tap.feed("down", 46, "c", blocked=True))
        self.assertEqual(self.tap.drain(), [])

    def test_a_shifted_key_is_still_the_menus(self):
        """Shift is not a shortcut modifier here, exactly as it is not on
        macOS: the tap watches Ctrl, Alt and Super, so Shift+A reaches the
        menu as the menu's key rather than the application's."""
        self.assertNotIn(42, keytap.MODIFIER_KEYS)       # KEY_LEFTSHIFT
        self.assertNotIn(54, keytap.MODIFIER_KEYS)       # KEY_RIGHTSHIFT
        self.policy.wanted = {"A"}
        self.assertTrue(self.tap.feed("down", 30, "A"))
        self.assertEqual(self.tap.drain(), [("A", "A")])

    def test_keys_the_menu_cannot_mean_pass_straight_through(self):
        self.policy.wanted = set(keytap.NAV_KEYS) | {"a"}
        self.assertFalse(self.tap.feed("down", 15, "\t"))      # Tab
        self.assertFalse(self.tap.feed("down", 59, None))      # F1
        self.assertFalse(self.tap.feed("down", 111, ""))       # Delete
        self.assertEqual(self.tap.drain(), [])

    def test_a_policy_that_throws_lets_the_key_through(self):
        def explode(_name, _character):
            raise RuntimeError("no")

        self.tap = keytap.KeyTap(decide=explode, log=self._log)
        self.assertFalse(self.tap.feed("down", keytap.UP, None))
        self.assertEqual(self.tap.drain(), [])
        self.assertIn("no", "".join(self.logged))

    def test_draining_empties_the_queue(self):
        self.policy.wanted = {"up"}
        self.tap.feed("down", keytap.UP, None)
        self.tap.feed("down", keytap.UP, None)
        self.assertEqual(len(self.tap.drain()), 2)
        self.assertEqual(self.tap.drain(), [])

    def test_a_fresh_tap_starts_empty_and_touches_no_device(self):
        """Constructing is safe anywhere: the grab only happens in start()."""
        tap = keytap.KeyTap(decide=lambda _n, _c: True, log=None)
        self.assertEqual(tap.drain(), [])
        self.assertIsNone(tap.tap)


class Translation(unittest.TestCase):
    def test_the_navigation_keys_by_keycode(self):
        for code, name in ((103, "up"), (108, "down"), (105, "left"),
                           (106, "right"), (28, "enter"), (96, "enter"),
                           (1, "esc"), (14, "backspace")):
            with self.subTest(code=code):
                self.assertEqual(keytap.key_for(code, None), (name, None))

    def test_a_printable_character_is_its_own_name(self):
        self.assertEqual(keytap.key_for(30, "a"), ("a", "a"))  # KEY_A
        self.assertEqual(keytap.key_for(57, " "), (" ", " "))  # KEY_SPACE

    def test_anything_else_is_not_a_menu_key(self):
        for code, character in ((15, "\t"), (59, None), (30, ""),
                                (111, None), (29, None)):
            with self.subTest(code=code, character=character):
                self.assertIsNone(keytap.key_for(code, character))

    def test_a_long_character_is_ignored(self):
        self.assertIsNone(keytap.key_for(30, "ab"))

    def test_no_modifier_is_a_menu_key_on_its_own(self):
        """Holding Ctrl makes the *next* key the application's; the Ctrl
        press itself has no meaning to the menu and must never be taken."""
        for code in keytap.MODIFIER_KEYS:
            with self.subTest(code=code):
                self.assertIsNone(keytap.key_for(code, None))

    def test_every_name_the_menu_asks_for_can_actually_be_produced(self):
        """The daemon asks for the names in NAV_KEYS, so key_for has to be
        able to yield all of them or a key would be swallowed and then never
        recognised."""
        produced = {keytap.key_for(code, None)[0] for code in keytap.KEY_NAMES}
        self.assertTrue(set(keytap.NAV_KEYS) <= produced,
                        f"NAV_KEYS not producible: {set(keytap.NAV_KEYS) - produced}")


class Layout(unittest.TestCase):
    """Where the keycode-to-character answer comes from."""

    KEYS = ("XKB_DEFAULT_LAYOUT", "XKB_DEFAULT_VARIANT")

    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in self.KEYS}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_the_environment_wins(self):
        os.environ["XKB_DEFAULT_LAYOUT"] = "de"
        os.environ["XKB_DEFAULT_VARIANT"] = "nodeadkeys"
        self.assertEqual(keytap._system_layout(), ("de", "nodeadkeys"))

    def test_a_layout_without_a_variant(self):
        os.environ["XKB_DEFAULT_LAYOUT"] = "us"
        os.environ.pop("XKB_DEFAULT_VARIANT", None)
        self.assertEqual(keytap._system_layout(), ("us", None))

    def test_without_the_environment_it_still_answers(self):
        """Never raises: the layout is a bonus, and a menu that cannot say
        what a key types still navigates."""
        for key in self.KEYS:
            os.environ.pop(key, None)
        layout, variant = keytap._system_layout()
        self.assertTrue(layout is None or isinstance(layout, str))
        self.assertTrue(variant is None or isinstance(variant, str))

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".keyboard", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, handle.name)
        with handle:
            handle.write(text)
        return handle.name

    def test_the_installer_file_is_read(self):
        for key in self.KEYS:
            os.environ.pop(key, None)
        path = self._write('XKBMODEL="pc105"\nXKBBLAYOUT="de"\n'
                           'XKBVARIANT="nodeadkeys"\nXKBOPTIONS=""\n')
        self.assertEqual(keytap._system_layout(path), ("de", "nodeadkeys"))

    def test_a_file_without_a_layout_is_no_answer(self):
        """A variant on its own names half of a pair. The locale is a
        better guess than half, and the variant must never survive into
        the answer."""
        for key in self.KEYS:
            os.environ.pop(key, None)
        path = self._write('XKBVARIANT="nodeadkeys"\n')
        layout, variant = keytap._system_layout(path)
        self.assertIsNone(variant)
        self.assertNotEqual((layout, variant), (None, "nodeadkeys"))

    def test_a_missing_file_is_no_answer(self):
        self.assertEqual(keytap._keyboard_file("/nonexistent/keyboard"),
                         (None, None))


if __name__ == "__main__":
    unittest.main()
