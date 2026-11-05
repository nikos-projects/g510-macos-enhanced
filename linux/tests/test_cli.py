"""What the CLI says when there is nothing here to work with.

The window reader and the banner store are the two things in the CLI that
reach for notify's sources. On Linux notify exists only so the shared
daemon and the shared CLI can import it (linux/notify.py: AVAILABLE, and
nothing behind it), which is what makes the daemon start at all - see
test_daemon.py for its half, where no watcher is ever started.

The other two commands here answer a different absence: a machine with no
keyboard plugged into it and a session with no display to open a window
in. Both answer the same way a real obstacle would - no node, no display -
and both used to report the obstacle they were built for instead, which
sends the reader off to fix something that is not broken.

These cover the shell's half: the commands that advertise themselves in the
Linux USAGE must say what cannot be read here and where chat does arrive
instead, rather than traceback or promise a read that cannot happen.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import cli
import notify


class DiscordWithoutASource(unittest.TestCase):
    def _run(self, *args):
        """One discord command with chat reading on, as its output."""
        settings = {"notifications": {"enabled": True}}
        out = io.StringIO()
        with mock.patch.object(cli.config, "load", return_value=settings), \
                contextlib.redirect_stdout(out):
            cli.cmd_discord(list(args))
        return out.getvalue()

    def test_the_module_says_there_is_no_source(self):
        """Everything above asks this one question before it reads."""
        self.assertFalse(notify.AVAILABLE)

    def test_reading_chat_points_at_the_plugin_that_reads_it(self):
        text = self._run()
        self.assertIn("window", text)
        self.assertIn("g510 discord channels", text)
        # The macOS advice would send the user to a permission that does
        # not exist here, so it must not be offered.
        self.assertNotIn("g510 permissions", text)

    def test_banners_say_there_is_no_store_to_read(self):
        text = self._run()
        self.assertIn("banners", text)
        self.assertIn("no notification store", text)

    def test_the_app_list_is_not_looked_for(self):
        """The list belongs to the window reader, and there is no window."""
        text = self._run("apps")
        self.assertIn("macOS-only", text)
        self.assertNotIn("running:", text)


class PermissionsWithoutAKeyboard(unittest.TestCase):
    """`g510 permissions` on a board that is not plugged in.

    can_read_input() is "is there a node, and may we open it", so absent
    hardware and locked-down hardware answer alike - and only one of them
    is a permissions problem.
    """

    def _run(self, node, reading, posting=True):
        out = io.StringIO()
        with mock.patch.object(cli.actions, "input_node",
                               return_value=node), \
                mock.patch.object(cli.actions, "can_read_input",
                                  return_value=reading), \
                mock.patch.object(cli.actions, "can_post_events",
                                  return_value=posting), \
                mock.patch.object(cli.actions, "request_post_access"), \
                contextlib.redirect_stdout(out):
            cli.cmd_permissions([])
        return out.getvalue()

    def test_no_keyboard_is_said_to_be_no_keyboard(self):
        text = self._run(node=None, reading=False)
        self.assertIn("no keyboard node", text)
        self.assertIn("not plugged in", text)
        # The udev advice is for a node we may not open, not one that is
        # not there: sending the user to the README for a missing board
        # would be sending them nowhere.
        self.assertNotIn("udev rule grants it", text)

    def test_a_node_we_may_not_open_is_still_the_permissions_answer(self):
        text = self._run(node="/dev/input/event7", reading=False)
        self.assertIn("NOT readable", text)
        self.assertIn("udev rule grants it", text)


class GuiWithoutADisplay(unittest.TestCase):
    def test_it_says_so_before_qt_is_imported(self):
        """Qt's own answer to a headless box is a page of plugin
        diagnostics ending in "Aborted", and it costs loading Qt to get
        it. The question is only whether there is a desktop session, so it
        is asked first - an ssh login never pays for the import."""
        with mock.patch.dict(os.environ, {"DISPLAY": "",
                                          "WAYLAND_DISPLAY": ""}):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_gui([])
        self.assertIn("no display", str(caught.exception))
        self.assertNotIn("gui", sys.modules)


if __name__ == "__main__":
    unittest.main()
