"""The rules behind the automatic screen switching.

Only the pure parts: what each condition means, which rule wins, when a
switch is allowed to happen, and how an app gets called a game. Reading the
system itself is up to macOS - the reader takes hooks so a test can stand in
for it.
"""
import os
import plistlib
import tempfile
import unittest
from unittest import mock

import autoscreen
import config
import daemon


def signals(**fields):
    """A full set of signals to run conditions against."""
    base = {"app": "Safari", "bundle": "com.apple.Safari", "path": "",
            "idle": 0.0, "playing": False, "track": None,
            "running": ["Safari", "com.apple.Safari"],
            "fullscreen": False, "game": False, "game_reason": "",
            "commands": {}}
    base.update(fields)
    return base


BLOCK = {"idle_seconds": 60, "hold_seconds": 3.0, "check_seconds": 2.0,
         "manual_seconds": 600, "enabled": True, "fallback": ""}


def holds(when, **fields):
    return autoscreen.matches(when, signals(**fields), BLOCK)


class ConditionTests(unittest.TestCase):
    """Each key a rule's `when` can be written with."""

    def test_an_empty_when_matches_everything(self):
        self.assertTrue(autoscreen.matches(None, signals(), BLOCK))
        self.assertTrue(autoscreen.matches({}, signals(), BLOCK))

    def test_a_condition_this_build_does_not_know_never_matches(self):
        # A hand-edited config with a typo must not become a rule that
        # applies to everything.
        self.assertFalse(autoscreen.matches({"gamee": True}, signals(), BLOCK))

    def test_a_value_of_the_wrong_shape_never_matches(self):
        self.assertFalse(autoscreen.matches({"idle": "soon"}, signals(), BLOCK))
        self.assertFalse(autoscreen.matches({"idle": None}, signals(), BLOCK))

    def test_game(self):
        self.assertTrue(holds({"game": True}, game=True))
        self.assertFalse(holds({"game": True}, game=False))
        self.assertTrue(holds({"game": False}, game=False))
        self.assertFalse(holds({"game": False}, game=True))
        self.assertTrue(holds({"game": "true"}, game=True))

    def test_playing(self):
        self.assertTrue(holds({"playing": True}, playing=True))
        self.assertFalse(holds({"playing": True}, playing=False))
        self.assertTrue(holds({"playing": False}, playing=False))

    def test_idle_true_uses_the_configured_window(self):
        self.assertTrue(holds({"idle": True}, idle=60))
        self.assertTrue(holds({"idle": True}, idle=600))
        self.assertFalse(holds({"idle": True}, idle=59.9))

    def test_idle_false_is_the_other_side_of_that_window(self):
        self.assertTrue(holds({"idle": False}, idle=10))
        self.assertFalse(holds({"idle": False}, idle=61))

    def test_idle_takes_a_window_of_your_own(self):
        self.assertTrue(holds({"idle": 30}, idle=31))
        self.assertFalse(holds({"idle": 30}, idle=29))

    def test_active_is_idle_turned_around(self):
        self.assertTrue(holds({"active": True}, idle=5))
        self.assertFalse(holds({"active": True}, idle=61))
        self.assertTrue(holds({"active": False}, idle=61))

    def test_active_takes_a_window_of_your_own(self):
        self.assertTrue(holds({"active": 30}, idle=29))
        self.assertFalse(holds({"active": 30}, idle=31))

    def test_idle_can_take_a_window_other_than_the_configured_one(self):
        block = dict(BLOCK, idle_seconds=60)
        self.assertTrue(autoscreen.matches({"idle": 30}, signals(idle=45),
                                           block))

    def test_app_matches_the_name(self):
        self.assertTrue(holds({"app": "Safari"}))
        self.assertFalse(holds({"app": "Terminal"}))

    def test_app_matches_the_bundle_id(self):
        self.assertTrue(holds({"app": "com.apple.Safari"}))
        self.assertFalse(holds({"app": "com.apple.Terminal"}))

    def test_app_patterns_are_case_insensitive_globs(self):
        self.assertTrue(holds({"app": "safari"}))
        self.assertTrue(holds({"app": "com.apple.*"}))
        self.assertTrue(holds({"app": "Saf*"}))
        self.assertTrue(holds({"app": ["Terminal", "Safari*"]}))

    def test_app_with_nothing_in_front_matches_nothing(self):
        self.assertFalse(holds({"app": "*"}, app=None, bundle=None))

    def test_not_app_is_the_negation(self):
        self.assertTrue(holds({"not_app": "Terminal"}))
        self.assertFalse(holds({"not_app": "Safari"}))
        self.assertTrue(holds({"not_app": ["Terminal", "Firefox"]}))
        self.assertFalse(holds({"not_app": ["Terminal", "Saf*"]}))

    def test_running_looks_at_every_app_not_just_the_frontmost(self):
        self.assertTrue(holds({"running": "Music"},
                              running=["Music", "com.apple.Music"]))
        self.assertFalse(holds({"running": "Music"}, running=["Safari"]))

    def test_not_running(self):
        self.assertTrue(holds({"not_running": "Music"}, running=["Safari"]))
        self.assertFalse(holds({"not_running": "Music"}, running=["Music"]))

    def test_fullscreen(self):
        self.assertTrue(holds({"fullscreen": True}, fullscreen=True))
        self.assertFalse(holds({"fullscreen": True}, fullscreen=False))
        self.assertTrue(holds({"fullscreen": False}, fullscreen=False))

    def test_command_reads_the_answer_the_reader_cached(self):
        wanted = {"command": "pgrep -x Music"}
        self.assertTrue(holds(wanted,
                              commands={"pgrep -x Music": True}))
        self.assertFalse(holds(wanted, commands={"pgrep -x Music": False}))
        # Never run at all: no answer, no match.
        self.assertFalse(holds(wanted, commands={}))

    def test_conditions_in_one_rule_all_have_to_hold(self):
        self.assertTrue(holds({"playing": True, "idle": True},
                              playing=True, idle=120))
        self.assertFalse(holds({"playing": True, "idle": True},
                               playing=True, idle=5))


class EvaluateTests(unittest.TestCase):
    """Which rule wins when several could."""

    RULES = [
        {"name": "game", "screen": "status", "when": {"game": True}},
        {"name": "music", "screen": "media", "when": {"playing": True}},
        {"name": "afk", "screen": "clock", "when": {"idle": True}},
        {"name": "busy", "screen": "app", "when": {"active": True}},
    ]

    def pick(self, **fields):
        return autoscreen.evaluate(self.RULES, signals(**fields), BLOCK)

    def test_the_first_rule_that_holds_wins(self):
        # Idle and playing at once: music is written above afk, so music
        # wins - order is the priority.
        chosen = self.pick(idle=300, playing=True, active=False)
        self.assertEqual(chosen["name"], "music")

    def test_a_later_rule_wins_when_the_first_does_not_hold(self):
        self.assertEqual(self.pick(idle=300, active=False)["name"], "afk")
        self.assertEqual(self.pick(idle=2, active=True)["name"], "busy")
        self.assertEqual(self.pick(game=True)["name"], "game")

    def test_nothing_holds(self):
        # Every rule fails on a machine that is neither busy nor idle...
        self.assertIsNone(autoscreen.evaluate(
            [{"name": "x", "screen": "clock", "when": {"running": "Nope"}}],
            signals(), BLOCK))

    def test_rules_that_are_not_objects_are_skipped(self):
        rules = ["nonsense", None, {"name": "ok", "screen": "clock",
                                    "when": {}}]
        self.assertEqual(autoscreen.evaluate(rules, signals(), BLOCK)["name"],
                         "ok")


class CoverTests(unittest.TestCase):
    """Whether a window fills a display - the fullscreen signal."""

    DISPLAY = [(1600, 900)]

    def test_a_window_the_size_of_the_display(self):
        self.assertTrue(autoscreen._covers({"Width": 1600, "Height": 900},
                                           self.DISPLAY))

    def test_a_window_stopping_below_the_menu_bar_does_not(self):
        # Maximised, not fullscreen: 24 points short, and that difference is
        # the whole point of asking.
        self.assertFalse(autoscreen._covers({"Width": 1600, "Height": 876},
                                            self.DISPLAY))

    def test_a_small_window_does_not(self):
        self.assertFalse(autoscreen._covers({"Width": 800, "Height": 600},
                                            self.DISPLAY))

    def test_a_window_on_a_second_display_counts(self):
        self.assertTrue(autoscreen._covers({"Width": 1920, "Height": 1080},
                                           [(1600, 900), (1920, 1080)]))

    def test_a_window_with_no_bounds_at_all_does_not(self):
        self.assertFalse(autoscreen._covers({}, self.DISPLAY))

    def test_with_no_displays_known_nothing_covers_one(self):
        self.assertFalse(autoscreen._covers({"Width": 1600, "Height": 900},
                                            []))


class ClassifyTests(unittest.TestCase):
    """What gets a frontmost app called a game."""

    def app(self, name="Roblox", bundle="com.roblox.RobloxPlayer", path=""):
        return {"name": name, "bundle": bundle, "path": path}

    def with_category(self, category):
        """An app bundle whose Info.plist declares `category`."""
        directory = tempfile.mkdtemp()
        contents = os.path.join(directory, "Contents")
        os.makedirs(contents)
        with open(os.path.join(contents, "Info.plist"), "wb") as handle:
            plistlib.dump({"CFBundleName": "Test",
                           "LSApplicationCategoryType": category}, handle)
        return directory

    def test_a_category_of_games_counts(self):
        path = self.with_category("public.app-category.games")
        game, why = autoscreen.classify(self.app(path=path), {})
        self.assertTrue(game)
        self.assertEqual(why, "category")

    def test_any_other_category_does_not(self):
        path = self.with_category("public.app-category.productivity")
        game, why = autoscreen.classify(self.app(path=path), {})
        self.assertFalse(game)
        self.assertEqual(why, "")

    def test_an_app_that_declares_nothing_is_not_a_game_by_default(self):
        # Which is the honest answer: most games do declare nothing.
        game, why = autoscreen.classify(self.app(), {})
        self.assertFalse(game)
        self.assertEqual(why, "")

    def test_a_built_in_launcher_counts(self):
        game, why = autoscreen.classify(self.app(name="Steam",
                                                 bundle="com.valvesoftware.steam"), {})
        self.assertTrue(game)
        self.assertEqual(why, "built-in")

    def test_a_pattern_the_user_wrote_counts(self):
        block = {"games": ["com.roblox.*"]}
        game, why = autoscreen.classify(self.app(), block)
        self.assertTrue(game)
        self.assertEqual(why, "games")

    def test_a_pattern_by_app_name_counts(self):
        block = {"games": ["osu!"]}
        game, why = autoscreen.classify(self.app(name="osu!",
                                                 bundle="sh.ppy.osu.lazer"), block)
        self.assertTrue(game)

    def test_not_games_beats_everything_else(self):
        # Steam is a launcher and would otherwise count; the user said no.
        steam = self.app(name="Steam", bundle="com.valvesoftware.steam")
        game, why = autoscreen.classify(steam, {"not_games": ["Steam"]})
        self.assertFalse(game)
        self.assertEqual(why, "not_games")

        path = self.with_category("public.app-category.games")
        game, why = autoscreen.classify(self.app(path=path),
                                        {"games": [],
                                         "not_games": ["Roblox"]})
        self.assertFalse(game)

    def test_not_games_beats_a_pattern_that_says_yes(self):
        game, _why = autoscreen.classify(
            self.app(), {"games": ["Roblox"], "not_games": ["Roblox"]})
        self.assertFalse(game)


class FakeReader:
    """A reader that answers from a dict instead of from macOS."""

    def __init__(self, **fields):
        self.value = signals(**fields)
        self.reads = 0

    def read(self, block, rules=None, wait=False):
        self.reads += 1
        return self.value


def settings(**block):
    """A config the way the agent sees it, with the LCD on `status`."""
    whole = dict(BLOCK)
    whole.setdefault("rules", [{"name": "afk", "screen": "clock",
                                "when": {"idle": True}}])
    whole.update(block)
    return {"autoscreen": whole, "lcd": {"screen": "status"}}


class AutoScreenTests(unittest.TestCase):
    """When a rule is allowed to put its screen up."""

    def setUp(self):
        self.reader = FakeReader(idle=300)
        self.switch = autoscreen.AutoScreen(self.reader)

    def test_the_first_look_takes_the_config_as_it_finds_it(self):
        # Nothing to compare against yet: adopting the screen already showing
        # is not a switch, and must not start a hands-off window either.
        self.assertIsNone(self.switch.update(0.0, settings(), "status"))
        self.assertEqual(self.switch.chosen, "status")
        self.assertEqual(self.switch.until, 0.0)

    def test_a_rule_that_holds_shows_after_the_hold(self):
        self.switch.update(0.0, settings(), "status")
        self.assertIsNone(self.switch.update(2.0, settings(), "status"))
        # Three seconds from the rule first winning, one check at a time.
        self.assertIsNone(self.switch.update(4.0, settings(), "status"))
        self.assertEqual(self.switch.update(6.0, settings(), "status"), "clock")
        self.assertEqual(self.switch.rule, "afk")

    def test_a_rule_that_changes_its_mind_starts_the_hold_again(self):
        block = settings()
        self.switch.update(0.0, block, "status")
        self.switch.update(2.0, block, "status")       # pending: clock
        self.reader.value = signals(idle=0)            # nobody is idle now
        self.assertIsNone(self.switch.update(6.0, block, "status"))
        self.reader.value = signals(idle=300)           # idle again
        self.assertIsNone(self.switch.update(8.0, block, "status"))
        self.assertEqual(self.switch.update(12.0, block, "status"), "clock")

    def test_nothing_holds_so_the_screen_stays(self):
        self.reader.value = signals(idle=0)
        self.switch.update(0.0, settings(), "status")
        for moment in (2.0, 4.0, 6.0, 8.0):
            self.assertIsNone(self.switch.update(moment, settings(), "status"))
        self.assertEqual(self.switch.chosen, "status")
        self.assertIsNone(self.switch.rule)

    def test_the_fallback_is_used_when_no_rule_holds(self):
        self.reader.value = signals(idle=0)
        block = settings(fallback="clock")
        self.switch.update(0.0, block, "status")
        self.assertIsNone(self.switch.update(2.0, block, "status"))
        self.assertEqual(self.switch.update(6.0, block, "status"), "clock")
        self.assertEqual(self.switch.rule, "fallback")

    def test_a_rule_can_ask_for_no_hold_at_all(self):
        block = settings(hold_seconds=0.0)
        self.switch.update(0.0, block, "status")
        self.assertEqual(self.switch.update(2.0, block, "status"), "clock")

    def test_one_rule_can_hold_for_its_own_length_of_time(self):
        block = settings(rules=[{"name": "afk", "screen": "clock",
                                 "when": {"idle": True}, "hold_seconds": 10}])
        self.switch.update(0.0, block, "status")
        self.assertIsNone(self.switch.update(4.0, block, "status"))
        self.assertIsNone(self.switch.update(8.0, block, "status"))
        self.assertEqual(self.switch.update(14.0, block, "status"), "clock")

    def test_the_screen_is_only_re_read_at_the_configured_pace(self):
        block = settings(check_seconds=30.0, hold_seconds=0.0)
        self.switch.update(0.0, block, "status")
        self.assertIsNone(self.switch.update(2.0, block, "status"))
        self.assertEqual(self.reader.reads, 0)
        self.assertEqual(self.switch.update(31.0, block, "status"), "clock")

    def test_a_screen_nobody_can_show_is_never_switched_to(self):
        block = settings(rules=[{"name": "typo", "screen": "cllok",
                                 "when": {"idle": True}}])
        self.switch.update(0.0, block, "status")
        self.assertIsNone(self.switch.update(2.0, block, "status"))
        self.assertIsNone(self.switch.update(6.0, block, "status"))
        self.assertEqual(self.switch.chosen, "status")

    def test_off_is_allowed_because_it_blanks_the_display(self):
        block = settings(rules=[{"name": "dark", "screen": "off",
                                 "when": {"idle": True}}])
        self.switch.update(0.0, block, "status")
        self.switch.update(2.0, block, "status")
        self.assertEqual(self.switch.update(6.0, block, "status"), "off")

    def test_a_screen_changed_by_someone_else_puts_itself_out_of_the_way(self):
        block = settings()
        self.switch.update(0.0, block, "status")
        self.switch.update(2.0, block, "status")
        self.assertEqual(self.switch.update(6.0, block, "status"), "clock")
        # The soft keys, the CLI or the GUI picked `media`.
        self.assertIsNone(self.switch.update(7.0, block, "media"))
        self.assertEqual(self.switch.until, 607.0)
        # ...and for the length of that window it stays out of the way,
        # however well the rules match.
        self.assertIsNone(self.switch.update(100.0, block, "media"))
        self.assertIsNone(self.switch.update(606.0, block, "media"))
        # After it, the rules are back in charge.
        self.assertIsNone(self.switch.update(608.0, block, "media"))
        self.assertEqual(self.switch.update(612.0, block, "media"), "clock")

    def test_the_hands_off_window_can_be_turned_off(self):
        block = settings(manual_seconds=0.0)
        self.switch.update(0.0, block, "status")
        self.assertIsNone(self.switch.update(1.0, block, "media"))
        self.assertEqual(self.switch.until, 1.0)
        self.assertIsNone(self.switch.update(4.0, block, "media"))
        self.assertEqual(self.switch.update(8.0, block, "media"), "clock")

    def test_switching_off_stops_it_and_forgets_where_it_was(self):
        block = settings()
        self.switch.update(0.0, block, "status")
        self.switch.update(2.0, block, "status")
        self.assertEqual(self.switch.update(6.0, block, "status"), "clock")
        self.assertEqual(self.switch.chosen, "clock")

        block["autoscreen"]["enabled"] = False
        self.assertIsNone(self.switch.update(7.0, block, "clock"))
        self.assertIsNone(self.switch.chosen)

        # Coming back on it takes the config as it finds it, rather than
        # treating the screen showing as somebody else's choice.
        block["autoscreen"]["enabled"] = True
        self.assertIsNone(self.switch.update(8.0, block, "media"))
        self.assertEqual(self.switch.chosen, "media")
        self.assertEqual(self.switch.until, 0.0)

    def test_a_blank_display_forgets_it_too(self):
        block = settings()
        self.switch.update(0.0, block, "status")
        self.switch.update(2.0, block, "status")
        self.switch.update(6.0, block, "status")
        self.switch.rest()
        self.assertIsNone(self.switch.update(7.0, block, "clock"))
        self.assertEqual(self.switch.chosen, "clock")
        self.assertEqual(self.switch.until, 0.0)

    def test_a_disabled_config_never_reads_the_system(self):
        self.switch.update(0.0, settings(enabled=False), "status")
        self.assertIsNone(self.switch.update(100.0, settings(enabled=False),
                                             "status"))
        self.assertEqual(self.reader.reads, 0)


class ReaderTests(unittest.TestCase):
    """Asking the system for the signals, with macOS stood in for."""

    def reader(self, **hooks):
        hooks.setdefault("frontmost", lambda: {"name": "osu!",
                                               "bundle": "sh.ppy.osu.lazer",
                                               "path": ""})
        hooks.setdefault("idle", lambda: 5.0)
        hooks.setdefault("running", lambda: ["osu!"])
        hooks.setdefault("media", lambda wait=False: None)
        return autoscreen.Reader(**hooks)

    def test_the_shape_the_conditions_read(self):
        got = self.reader().read(BLOCK, [])
        self.assertEqual(got["app"], "osu!")
        self.assertEqual(got["bundle"], "sh.ppy.osu.lazer")
        self.assertEqual(got["idle"], 5.0)
        self.assertFalse(got["playing"])
        self.assertIn("osu!", got["running"])
        self.assertEqual(got["commands"], {})
        self.assertFalse(got["game"])

    def test_nothing_in_front_is_not_an_error(self):
        got = self.reader(frontmost=lambda: None).read(BLOCK, [])
        self.assertIsNone(got["app"])
        self.assertFalse(got["game"])

    def test_media_playback_comes_through(self):
        track = {"title": "The Box", "artist": "Labrinth", "playing": True}
        got = self.reader(media=lambda wait=False: track).read(BLOCK, [])
        self.assertTrue(got["playing"])
        self.assertEqual(got["track"], track)

    def test_the_window_list_is_only_walked_when_a_rule_asks(self):
        asked = []

        def fullscreen():
            asked.append("looked")
            return ["osu!"]

        reader = self.reader(fullscreen=fullscreen)
        reader.read(BLOCK, [{"when": {"idle": True}}])
        self.assertEqual(asked, [])
        reader.read(BLOCK, [{"when": {"fullscreen": True}}])
        self.assertEqual(asked, ["looked"])

    def test_fullscreen_is_about_the_frontmost_app_only(self):
        reader = self.reader(fullscreen=lambda: ["Finder", "Music"])
        got = reader.read(BLOCK, [{"when": {"fullscreen": True}}])
        self.assertFalse(got["fullscreen"])

        reader = self.reader(fullscreen=lambda: ["osu!"])
        got = reader.read(BLOCK, [{"when": {"fullscreen": True}}])
        self.assertTrue(got["fullscreen"])

    def test_command_conditions_are_the_ones_the_rules_use(self):
        rules = [{"when": {"command": "true"}}, {"when": {"command": "false"}}]
        got = self.reader().read(BLOCK, rules)
        self.assertEqual(sorted(got["commands"]), ["false", "true"])

    def test_a_command_that_has_not_answered_yet_does_not_hold(self):
        # The shell is asked off-thread; until it speaks, the answer is no -
        # a shell assumed true before it ran would match everything at once.
        rules = [{"when": {"command": "sleep 2"}}]
        got = self.reader().read(BLOCK, rules)
        self.assertFalse(got["commands"]["sleep 2"])


class PredicateTests(unittest.TestCase):
    """What a `command` condition counts as true."""

    def test_exit_zero_is_true(self):
        self.assertTrue(autoscreen._run_predicate("true"))
        self.assertTrue(autoscreen._run_predicate("test -d /"))

    def test_a_nonzero_exit_is_false(self):
        self.assertFalse(autoscreen._run_predicate("false"))
        self.assertFalse(autoscreen._run_predicate("exit 3"))

    def test_a_command_that_cannot_be_run_is_false(self):
        self.assertFalse(autoscreen._run_predicate(
            "definitely-not-a-command-xyzzy"))


class DaemonSwitchTests(unittest.TestCase):
    """What the agent does with the screen autoscreen asks for.

    The agent saves every switch into `lcd.screen`, which is what makes it
    possible to tell an automatic switch from one somebody else made - and
    what a soft key, the CLI or the GUI writing that same field does to it.
    Redirected at a temporary config, so the real one is never touched.
    """

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        quiet = mock.patch.object(daemon, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.agent = daemon.Daemon()
        self.agent.autoscreen = autoscreen.AutoScreen(FakeReader(idle=300))
        self.agent.last_lcd = 999.0

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")

    def test_the_winning_screen_is_saved_and_drawn_again(self):
        # The default rules: idle with nothing playing is `afk` -> clock.
        self.assertIsNone(self.agent.refresh_autoscreen(0.0))
        self.assertIsNone(self.agent.refresh_autoscreen(2.0))
        self.agent.refresh_autoscreen(6.0)
        self.assertEqual(self.screen(), "clock")
        self.assertEqual(self.agent.autoscreen.chosen, "clock")
        self.assertEqual(self.agent.last_lcd, 0.0)     # redraw it now

    def test_a_screen_picked_by_hand_gets_the_hands_off_window(self):
        self.agent.refresh_autoscreen(0.0)
        self.agent.refresh_autoscreen(6.0)
        # The CLI, the GUI or a soft key wrote `media` over the top of it.
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(screen="media"))
        self.assertIsNone(self.agent.refresh_autoscreen(7.0))
        self.assertEqual(self.agent.autoscreen.until, 7.0 + 600)
        for moment in (100.0, 600.0):
            self.agent.refresh_autoscreen(moment)
            self.assertEqual(self.screen(), "media")

    def test_a_blank_display_is_left_alone(self):
        # The keyboard is not connected here, so this returning quietly is
        # also the proof that nothing was drawn while the display is off.
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
        self.agent.refresh_lcd(1000.0)
        self.assertIsNone(self.agent.autoscreen.chosen)

    def test_switching_is_off_by_default_in_a_config_that_says_so(self):
        self.agent.mutate(lambda s: s.setdefault("autoscreen", {}).update(
            enabled=False))
        for moment in (0.0, 2.0, 6.0, 10.0):
            self.assertIsNone(self.agent.refresh_autoscreen(moment))
        self.assertEqual(self.screen(), "status")


if __name__ == "__main__":
    unittest.main()
