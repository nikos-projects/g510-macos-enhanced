"""The causes a user writes themselves: a screen for something they care about.

Two shapes fall out of one config key, and both halves are pinned down here -
the ones that hold a screen (which are autoscreen's rules, by another name)
and the ones that fire once (which the agent watches and shows). What is not
tested is reading the system for real: that is `autoscreen`'s job, and the
reader is stood in for.
"""
import os
import tempfile
import unittest
from unittest import mock

import alerts
import autoscreen
import config
import daemon
import triggers


def signals(**fields):
    """A full set of signals to run conditions against."""
    base = {"app": "Safari", "bundle": "com.apple.Safari", "path": "",
            "idle": 0.0, "playing": False, "track": None,
            "running": ["Safari", "com.apple.Safari"],
            "fullscreen": False, "game": False, "game_reason": "",
            "commands": {}}
    base.update(fields)
    return base


class FakeReader:
    """A reader that answers from a dict instead of from macOS."""

    def __init__(self, **fields):
        self.value = signals(**fields)
        self.reads = 0

    def read(self, block, rules=None, wait=False):
        self.reads += 1
        return self.value


def settings(*rules, **block):
    """A config with these triggers in it, and the usual LCD underneath."""
    whole = {"enabled": True, "check_seconds": 2.0, "rules": list(rules)}
    whole.update(block)
    return {"triggers": whole,
            "autoscreen": {"enabled": True, "hold_seconds": 3.0,
                           "check_seconds": 2.0, "manual_seconds": 600,
                           "rules": [{"name": "afk", "screen": "clock",
                                      "when": {"idle": True}}]},
            "lcd": {"screen": "status"}}


def build(name, screen, when, seconds=None):
    rule = {"name": name, "screen": screen, "when": when}
    if seconds is not None:
        rule["seconds"] = seconds
    return rule


class StateRulesTests(unittest.TestCase):
    """A trigger with no `seconds` holds a screen, so it is a rule."""

    def test_no_seconds_means_it_holds(self):
        rule = build("afk", "clock", {"idle": 300})
        self.assertEqual(triggers.state_rules(settings(rule)), [rule])

    def test_seconds_takes_it_out_of_the_holding_half(self):
        held = triggers.state_rules(settings(build("build", "status",
                                                   {"command": "true"}, 8)))
        self.assertEqual(held, [])

    def test_switched_off_takes_its_rules_with_it(self):
        rule = build("afk", "clock", {"idle": 300})
        block = settings(rule, enabled=False)
        self.assertEqual(triggers.state_rules(block), [])

    def test_a_screen_that_does_not_exist_is_not_offered_to_anybody(self):
        block = settings(build("broken", "underwater", {"idle": 300}),
                         build("ok", "clock", {"idle": 300}))
        self.assertEqual([rule["name"] for rule in
                          triggers.state_rules(block)], ["ok"])

    def test_a_when_that_is_not_conditions_is_dropped(self):
        block = settings({"name": "junk", "screen": "clock", "when": "idle"})
        self.assertEqual(triggers.state_rules(block), [])


class EventTests(unittest.TestCase):
    """A trigger with `seconds` fires once, on the way in."""

    def setUp(self):
        self.reader = FakeReader(idle=0)
        self.watch = triggers.Triggers(self.reader)
        self.rule = build("build", "status", {"idle": 300}, 8)
        self.block = settings(self.rule)

    def test_the_first_look_arms_it_rather_than_firing(self):
        # A condition already true when the agent starts has been true for
        # who knows how long: lighting the display up for that would be the
        # restart firing, not the cause.
        self.assertEqual(self.watch.update(2.0, self.block), [])
        self.assertEqual(self.watch.state["build"], False)

    def test_it_fires_the_moment_it_becomes_true(self):
        self.assertEqual(self.watch.update(2.0, self.block), [])
        self.reader.value = signals(idle=400)
        fired = self.watch.update(4.0, self.block)
        self.assertEqual([event["name"] for event in fired], ["build"])
        self.assertEqual(fired[0]["screen"], "status")
        self.assertEqual(fired[0]["seconds"], 8.0)
        self.assertEqual(fired[0]["data"], {"name": "build"})

    def test_it_does_not_fire_again_while_it_stays_true(self):
        self.watch.update(2.0, self.block)
        self.reader.value = signals(idle=400)
        self.assertEqual(len(self.watch.update(4.0, self.block)), 1)
        self.assertEqual(self.watch.update(6.0, self.block), [])
        self.assertEqual(self.watch.update(8.0, self.block), [])

    def test_it_fires_again_after_letting_go(self):
        self.watch.update(2.0, self.block)
        self.reader.value = signals(idle=400)
        self.assertEqual(len(self.watch.update(4.0, self.block)), 1)
        self.reader.value = signals(idle=0)
        self.assertEqual(self.watch.update(6.0, self.block), [])
        self.reader.value = signals(idle=400)
        self.assertEqual(len(self.watch.update(8.0, self.block)), 1)

    def test_the_conditions_are_read_at_its_own_pace(self):
        # Two seconds between reads, and nothing asked of the system in
        # between - the same cadence autoscreen uses, not every poll.
        self.watch.update(2.0, self.block)
        self.watch.update(3.0, self.block)
        self.assertEqual(self.reader.reads, 1)
        self.watch.update(4.0, self.block)
        self.assertEqual(self.reader.reads, 2)

    def test_switched_off_watches_nothing_and_forgets(self):
        self.watch.update(2.0, self.block)
        self.reader.value = signals(idle=400)
        off = settings(self.rule, enabled=False)
        self.assertEqual(self.watch.update(4.0, off), [])
        self.assertEqual(self.watch.state, {})

    def test_a_rule_taken_out_of_the_config_is_forgotten(self):
        self.watch.update(2.0, self.block)
        self.reader.value = signals(idle=400)
        self.watch.update(4.0, self.block)
        self.watch.update(6.0, settings())
        self.assertEqual(self.watch.state, {})

    def test_what_it_fires_carries_the_screen_and_how_long(self):
        self.watch.update(2.0, self.block)
        self.reader.value = signals(idle=400)
        event = self.watch.update(4.0, self.block)[0]
        self.assertEqual(event["screen"], "status")
        self.assertNotEqual(event["seconds"], 0)


class FireTests(unittest.TestCase):
    """`g510 trigger fire <name>` - by hand, from anywhere."""

    def setUp(self):
        self.watch = triggers.Triggers(FakeReader())
        self.block = settings(build("build", "status", {"command": "true"}, 8),
                              build("afk", "clock", {"idle": 300}))

    def test_it_fires_whether_or_not_anything_is_being_watched(self):
        event = self.watch.fire(self.block, "build")
        self.assertEqual(event["screen"], "status")
        self.assertEqual(event["seconds"], 8.0)

    def test_a_rule_that_holds_fires_for_the_default_length(self):
        event = self.watch.fire(self.block, "afk")
        self.assertEqual(event["screen"], "clock")
        self.assertEqual(event["seconds"], triggers.DEFAULT_SECONDS)

    def test_an_unknown_name_says_so(self):
        with self.assertRaises(KeyError):
            self.watch.fire(self.block, "raid")

    def test_a_sound_of_the_rule_beats_the_sound_of_the_block(self):
        loud = dict(build("build", "status", {"command": "true"}, 8),
                    sound="Sosumi")
        plain = build("build", "status", {"command": "true"}, 8)
        self.assertEqual(self.watch.fire(settings(loud, sound="Glass"),
                                         "build")["sound"], "Sosumi")
        self.assertEqual(self.watch.fire(settings(plain, sound="Glass"),
                                         "build")["sound"], "Glass")
        self.assertEqual(self.watch.fire(settings(plain), "build")["sound"],
                         "")

    def test_the_screen_can_be_switched_off_too(self):
        block = settings(build("blank", "off", {"idle": 300}, 5))
        self.assertEqual(self.watch.fire(block, "blank")["screen"], "off")


class AgentTests(unittest.TestCase):
    """What the agent does with them: hand them on, and show them."""

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
        self.reader = FakeReader(idle=300)
        self.now = 1000.0
        self.agent = daemon.Daemon()
        self.agent.autoscreen = autoscreen.AutoScreen(self.reader)
        self.agent.triggers = triggers.Triggers(self.reader)
        self.agent.alerts = alerts.Alerts(clock=lambda: self.now)

    def hold(self, **when):
        """A holding trigger, put in the agent's own config."""
        rule = dict(build("mine", "media", when or {"app": "Safari"}))
        self.agent.config["triggers"] = {
            "enabled": True, "check_seconds": 2.0, "rules": [rule]}

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")

    def test_the_holding_trigger_is_tried_before_the_ordinary_rules(self):
        # The built-in rule would put the clock up for an idle machine; the
        # user asked for `media` when Safari is in front, and asked last.
        self.hold(app="Safari")
        self.agent.refresh_autoscreen(0.0)
        self.agent.refresh_autoscreen(2.0)
        self.agent.refresh_autoscreen(6.0)
        self.assertEqual(self.screen(), "media")
        self.assertEqual(self.agent.autoscreen.rule, "mine")

    def test_switched_off_the_ordinary_rules_are_untouched(self):
        self.hold(app="Safari")
        self.agent.config["triggers"]["enabled"] = False
        self.assertIs(self.agent._policy(), self.agent.config)
        self.agent.refresh_autoscreen(0.0)
        self.agent.refresh_autoscreen(2.0)
        self.agent.refresh_autoscreen(6.0)
        self.assertEqual(self.screen(), "clock")

    def test_a_trigger_that_fired_takes_the_display_and_then_gives_it_back(self):
        self.agent.config["triggers"] = {
            "enabled": True, "check_seconds": 2.0,
            "rules": [build("build", "status", {"idle": 300}, 8)]}
        self.reader.value = signals(idle=0)
        self.assertEqual(self.agent.refresh_triggers(self.now), None)
        self.reader.value = signals(idle=400)
        self.now += 2.0
        self.agent.refresh_triggers(self.now)
        borrowed = self.agent.alerts.current(self.now)
        self.assertIsNotNone(borrowed)
        self.assertEqual(borrowed["screen"], "status")
        # Eight seconds later the interruption is over and the ordinary
        # display is what belongs there again.
        self.assertIsNone(self.agent.alerts.current(self.now + 8.0))

    def test_firing_one_by_hand_borrows_the_display(self):
        self.agent.config["triggers"] = {
            "enabled": False, "check_seconds": 2.0,
            "rules": [build("raid", "status", {"app": "WoW"}, 5)]}
        reply = self.agent.trigger_command({"action": "fire", "name": "raid"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["fired"], "raid")
        borrowed = self.agent.alerts.current(self.now)
        self.assertEqual(borrowed["screen"], "status")
        self.assertEqual(borrowed["data"]["name"], "raid")

    def test_firing_one_that_does_not_exist_says_so(self):
        reply = self.agent.trigger_command({"action": "fire", "name": "nope"})
        self.assertFalse(reply["ok"])
        self.assertIn("nope", reply["error"])

    def test_the_agent_can_be_asked_what_it_is_watching(self):
        self.agent.config["triggers"] = {
            "enabled": True, "check_seconds": 2.0,
            "rules": [build("raid", "status", {"app": "WoW"}, 5),
                      build("afk", "clock", {"idle": 300})]}
        reply = self.agent.trigger_command({"action": "list"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["enabled"])
        self.assertEqual([rule["name"] for rule in reply["rules"]],
                         ["raid", "afk"])


if __name__ == "__main__":
    unittest.main()
