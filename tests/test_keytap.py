"""The event tap: which keys the menu takes, and which it leaves alone.

`feed` is the whole policy laid out as a plain function, so this runs
without a display, a permission prompt or a keyboard attached.
"""
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
        self.assertTrue(self.tap.feed("down", 0, "a"))
        self.assertEqual(self.tap.drain(), [("a", "a")])
        self.assertTrue(self.tap.feed("up", 0, None))
        self.policy.wanted = set()
        self.assertFalse(self.tap.feed("down", 0, "b"))
        self.assertEqual(self.tap.drain(), [])

    def test_a_shortcut_is_never_taken(self):
        self.policy.wanted = {"a"}
        # Cmd-C is the application's, however much the menu might like it.
        self.assertFalse(self.tap.feed("down", 0, "c", blocked=True))
        self.assertEqual(self.tap.drain(), [])
        self.assertFalse(self.tap.feed("down", 8, "c", blocked=True))

    def test_keys_the_menu_cannot_mean_pass_straight_through(self):
        self.policy.wanted = set(keytap.NAV_KEYS) | {"a"}
        self.assertFalse(self.tap.feed("down", 48, "\t"))     # Tab
        self.assertFalse(self.tap.feed("down", 96, None))     # F key
        self.assertFalse(self.tap.feed("down", 117, ""))      # forward delete
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


class Translation(unittest.TestCase):
    def test_the_navigation_keys_by_keycode(self):
        for code, name in ((126, "up"), (125, "down"), (123, "left"),
                           (124, "right"), (36, "enter"), (76, "enter"),
                           (53, "esc"), (51, "backspace")):
            self.assertEqual(keytap.key_for(code, None), (name, None))

    def test_a_printable_character_is_its_own_name(self):
        self.assertEqual(keytap.key_for(0, "a"), ("a", "a"))
        self.assertEqual(keytap.key_for(49, " "), (" ", " "))

    def test_anything_else_is_not_a_menu_key(self):
        for code, character in ((48, "\t"), (96, None), (0, ""),
                                (117, None), (50, None)):
            self.assertIsNone(keytap.key_for(code, character))

    def test_a_long_character_is_ignored(self):
        self.assertIsNone(keytap.key_for(0, "ab"))


if __name__ == "__main__":
    unittest.main()
