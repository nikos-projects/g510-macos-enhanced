"""The real agent, constructed - the half the loop stubs cannot see.

Every test in test_daemon.py points the loop's functions at a stub, which
is the only way to pin down what each pass owes. What a stub cannot see is
`Daemon.__init__` and `reload_config()`: shared code that the root suite
does construct for real, but only ever with the macOS modules underneath.

Two of the port's bugs lived exactly there and stayed invisible to both
suites - notify's import, which stopped the agent before its loop, and
lcd.set_font, which the Linux lcd never had. Both are now covered here:
this constructs the real thing with the Linux modules in place, which is
what notices the next one.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
import daemon


class Construct(unittest.TestCase):
    def setUp(self):
        # _shared.adopt() hands the root module out under this name with the
        # Linux amendments already on it - one object, whose load() reads its
        # own globals - so this is the ordinary patch and the one that works
        # (see the docstring of _shared.adopt for the trap it removes).
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)

    def test_the_agent_builds_from_the_shared_code(self):
        """__init__ is where both of those bugs were: the import of notify
        at the top of daemon.py, and lcd.set_font on the way down."""
        agent = daemon.Daemon()
        self.assertTrue(agent.running)
        self.assertIsNone(agent.watcher)

    def test_a_reload_reapplies_the_config_without_starting_a_watcher(self):
        """reload_config() reaches sync_watcher() on every config change -
        the path `g510 config set` and `g510 discord on` both take - which
        is why the guard lives there rather than only in the boot that
        skips the call. It sets the display's font too."""
        agent = daemon.Daemon()
        # What a reload would otherwise open for real: the Vencord port,
        # an audio tap and the API server - the same three the root suite
        # shuts before it reloads.
        for target in ("sync_link", "sync_beat", "sync_api"):
            patcher = mock.patch.object(daemon.Daemon, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        agent.reload_config()
        self.assertIsNone(agent.watcher)


if __name__ == "__main__":
    unittest.main()
