"""The on-screen menu: navigation, editing, and where each value lands.

Everything here runs against a fake host - a plain dict for the config and
a recorder for saves and actions - so the menu is exercised without a
display, an event tap or an agent.
"""
import copy
import os
import tempfile
import time
import unittest
from unittest import mock

import autoscreen
import cli
import config
import daemon
import menu


def fresh():
    """The factory config, with banks laid out the way load() leaves them."""
    return config._migrate(copy.deepcopy(config.DEFAULTS))


class FakeHost(menu.Host):
    """A host that remembers what it was asked to do.

    Like the agent, it applies a change to the settings it holds, so the
    tests can go on asserting on the values themselves.
    """

    def __init__(self, settings=None):
        self.saves = 0
        self.actions = []
        super().__init__(settings if settings is not None else fresh(),
                         save=self._save, act=self._act)

    def _save(self, apply=None):
        self.saves += 1
        if callable(apply):
            apply(self.settings)
        return True

    def _act(self, name, arg=None):
        self.actions.append((name, arg))
        return f"{name} done"


def find(m, label):
    for index, entry in enumerate(m._entries()):
        if entry.get("label") == label:
            return index
    raise AssertionError(
        f"no entry {label!r} on this page: "
        f"{[entry.get('label') for entry in m._entries()]}")


def goto(m, *labels):
    """Walk into submenus by name, as the arrow keys would."""
    for label in labels:
        m.sel = find(m, label)
        m.press("enter")
    return m


def rows(view):
    """Every line the LCD would draw, for assertions about width."""
    return [view["title"]] + list(view["main"]) + ([view["sub"]]
                                                   if view["sub"] else [])


class Navigation(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.m = menu.Menu(self.host)
        self.m.open()

    def test_opens_at_the_root_and_esc_closes_it(self):
        view = self.m.view()
        self.assertFalse(view["closed"])
        self.assertEqual(view["title"], "Menu")
        self.assertIn("Screen", view["main"][0])
        self.assertEqual(self.m.press("esc"), "closed")
        self.assertTrue(self.m.view()["closed"])
        self.assertEqual(self.m.press("up"), "ignored")

    def test_selection_walks_and_wraps(self):
        last = len(self.m._entries()) - 1
        self.m.sel = last
        self.m.press("down")
        self.assertEqual(self.m.sel, 0)
        self.m.press("up")
        self.assertEqual(self.m.sel, last)

    def test_left_walks_out_but_does_not_close(self):
        goto(self.m, "Settings")
        self.assertEqual(self.m.view()["title"], "Settings")
        self.assertEqual(self.m.press("left"), "redraw")
        self.assertEqual(self.m.view()["title"], "Menu")
        self.assertFalse(self.m.view()["closed"])
        self.assertEqual(self.m.press("left"), "ignored")

    def test_going_back_keeps_where_you_were(self):
        self.m.sel = find(self.m, "Settings")
        self.m.press("enter")
        self.m.press("esc")
        self.assertEqual(self.m.sel, find(self.m, "Settings"))

    def test_the_frame_is_at_most_three_rows(self):
        for path in (("Settings", "Display"), ("Timers", "Presets"),
                     ("Triggers", "Rules"), ("Bindings", "G-keys"),
                     ("Screen", "Gauges"), ("Settings", "Backlight")):
            m = menu.Menu(FakeHost())
            m.open()
            goto(m, *path)
            for step in range(len(m._entries())):
                m.sel = step
                view = m.view()
                self.assertLessEqual(len(rows(view)), 3,
                                     f"{path} row {step}: {rows(view)}")

    def test_next_row_is_the_next_item_or_a_hint(self):
        view = self.m.view()
        self.assertIn("Timers >", view["main"][1])
        self.m.sel = len(self.m._entries()) - 1   # "Leave", the last row
        view = self.m.view()
        self.assertEqual(len(view["main"]), 1)
        self.assertTrue(view["sub"])


class Editing(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.m = menu.Menu(self.host)
        self.m.open()

    def test_a_bool_toggles_in_place_and_saves_once(self):
        before = self.host.settings["lcd"]["enabled"]
        goto(self.m, "Screen")
        self.m.sel = find(self.m, "Display")
        self.assertEqual(self.m.press("enter"), "redraw")
        self.assertEqual(self.host.settings["lcd"]["enabled"], not before)
        self.assertEqual(self.host.saves, 1)
        view = self.m.view()
        self.assertIn("on" if not before else "off", view["main"][0])
        # The row says what it became, so the line underneath is left for
        # the next item rather than repeating it.
        self.assertIn("Refresh", view["main"][1])

    def test_a_choice_is_edited_then_committed(self):
        goto(self.m, "Screen")
        self.m.sel = find(self.m, "Screen")
        self.m.press("enter")
        self.assertEqual(self.m.view()["editing"]["kind"], "choice")
        original = self.host.settings["lcd"]["screen"]
        self.m.press("esc")                    # cancel changes nothing
        self.assertEqual(self.host.settings["lcd"]["screen"], original)
        self.m.press("enter")                  # edit for real this time
        self.m.press("down")
        self.m.press("enter")
        self.assertNotEqual(self.host.settings["lcd"]["screen"], original)
        self.assertEqual(self.host.saves, 1)

    def test_a_choice_shows_a_colour_the_menu_has_no_name_for(self):
        self.host.settings["backlight"] = "#123456"
        goto(self.m, "Settings", "Backlight")
        self.m.sel = find(self.m, "Colour")
        self.m.press("enter")
        self.assertEqual(self.m.view()["editing"]["value"], "#123456")
        self.m.press("esc")

    def test_text_takes_typed_characters_and_backspace(self):
        self.host.settings["lcd"]["font"] = ""
        goto(self.m, "Settings", "Display")
        self.m.sel = find(self.m, "Font")
        self.m.press("enter")
        for char in "Mono":
            self.m.press(char)
        self.assertEqual(self.m.view()["editing"]["value"], "Mono")
        self.m.press("backspace")
        self.m.press("enter")
        self.assertEqual(self.host.settings["lcd"]["font"], "Mon")
        self.assertEqual(self.host.saves, 1)

    def test_a_bad_list_keeps_the_old_value_and_says_so(self):
        goto(self.m, "Screen")
        self.m.sel = find(self.m, "Cycle")
        self.m.press("enter")
        for char in "bogus":
            self.m.press(char)
        self.m.press("enter")
        self.assertEqual(self.m.view()["editing"]["kind"], "text")
        self.assertIn("not one of", self.m.view()["sub"])
        self.assertEqual(self.host.settings["lcd"]["cycle"],
                         config.DEFAULTS["lcd"]["cycle"])
        self.assertEqual(self.host.saves, 0)

    def test_numbers_step_and_stop_at_the_edges(self):
        goto(self.m, "Settings", "Backlight")
        self.m.sel = find(self.m, "Brightness")
        self.m.press("enter")
        for _ in range(40):
            self.m.press("up")
        self.assertEqual(self.m.view()["editing"]["value"], 100)
        for _ in range(60):
            self.m.press("down")
        self.assertEqual(self.m.view()["editing"]["value"], 0)
        self.m.press("enter")
        self.assertEqual(self.host.settings["brightness"], 0)
        self.assertIsInstance(self.host.settings["brightness"], int)

    def test_actions_report_back_on_the_row_underneath(self):
        goto(self.m, "Screen")
        self.m.sel = find(self.m, "Next screen")
        self.m.press("enter")
        self.assertEqual(self.host.actions, [("next", None)])
        self.assertEqual(self.m.view()["sub"], "next done")

    def test_leaving_closes_the_menu(self):
        self.m.sel = find(self.m, "Leave")
        self.assertEqual(self.m.press("enter"), "closed")
        self.assertTrue(self.m.view()["closed"])
        self.assertEqual(self.host.actions, [("leave", None)])


class ConfigPaths(unittest.TestCase):
    def test_put_get_and_drop_round_trip(self):
        settings = {}
        menu.put(settings, "app_colors/com.apple.Terminal", "red")
        self.assertEqual(menu.get(settings,
                                  "app_colors/com.apple.Terminal"), "red")
        menu.put(settings, "autoscreen/rules/0/name", "game")
        self.assertEqual(menu.get(settings, "autoscreen/rules/0/name"), "game")
        self.assertTrue(menu.drop(settings, "app_colors/com.apple.Terminal"))
        self.assertIsNone(menu.get(settings, "app_colors/com.apple.Terminal"))
        self.assertFalse(menu.drop(settings, "app_colors/com.apple.Terminal"))

    def test_get_reads_through_a_list_index(self):
        settings = {"autoscreen": {"rules": [{"name": "music"}]}}
        self.assertEqual(menu.get(settings, "autoscreen/rules/0/name"),
                         "music")
        self.assertIsNone(menu.get(settings, "autoscreen/rules/7/name"))


class DynamicPages(unittest.TestCase):
    def test_the_fire_page_lists_the_triggers_the_config_holds(self):
        host = FakeHost()
        host.settings["triggers"]["rules"] = [
            {"name": "coffee", "screen": "status", "when": {"game": True}},
            {"name": "afk", "screen": "clock", "seconds": 6}]
        m = menu.Menu(host)
        m.open()
        goto(m, "Triggers", "Fire now")
        labels = [entry.get("label") for entry in m._entries()]
        self.assertEqual(labels, ["coffee", "afk"])
        m.sel = 1
        m.press("enter")
        self.assertEqual(host.actions, [("trigger_fire", "afk")])

    def test_a_rule_page_offers_length_only_for_a_one_shot(self):
        host = FakeHost()
        host.settings["triggers"]["rules"] = [{"name": "afk",
                                               "when": {"idle": True}}]
        m = menu.Menu(host)
        m.open()
        goto(m, "Triggers", "Rules", "Rule 1")
        labels = [entry.get("label") for entry in m._entries()]
        self.assertIn("Give it a length (10s)", labels)
        self.assertNotIn("State rule (no length)", labels)
        m.sel = find(m, "Give it a length (10s)")
        m.press("enter")
        self.assertEqual(host.actions,
                         [("field_add", "triggers/rules/0/seconds")])
        menu.add_field(host.settings, "triggers/rules/0/seconds")
        self.assertEqual(host.settings["triggers"]["rules"][0]["seconds"], 10)

    def test_conditions_can_be_added_and_taken_away(self):
        host = FakeHost()
        host.settings["autoscreen"]["rules"][0]["when"] = {"game": True}
        m = menu.Menu(host)
        m.open()
        path = "autoscreen/rules/0/when"
        children = menu._when_page(path)(host, {})
        labels = [entry.get("label") for entry in children]
        self.assertEqual(labels[0], "game")
        self.assertIn("Add a condition", labels)
        menu.add_when(host.settings, path + "/playing")
        self.assertIs(host.settings["autoscreen"]["rules"][0]["when"]
                      ["playing"], False)
        menu.add_when(host.settings, path + "/app")
        self.assertEqual(
            host.settings["autoscreen"]["rules"][0]["when"]["app"], "")
        self.assertTrue(menu.del_when(host.settings, path + "/game"))
        self.assertNotIn("game",
                         host.settings["autoscreen"]["rules"][0]["when"])

    def test_a_rule_can_be_deleted_and_another_added(self):
        host = FakeHost()
        host.settings["autoscreen"]["rules"].append(
            {"name": "spare", "screen": "clock", "when": {}})
        count = len(host.settings["autoscreen"]["rules"])
        m = menu.Menu(host)
        m.open()
        goto(m, "Settings", "Auto switch", "Rules")
        labels = [entry.get("label") for entry in m._entries()]
        self.assertEqual(labels,
                         [f"Rule {index + 1}" for index in range(count)]
                         + ["Add a rule"])
        m.sel = find(m, "Rule 2")
        m.press("enter")
        m.sel = find(m, "Delete rule")
        m.press("enter")
        self.assertEqual(host.actions, [("rule_del",
                                         "autoscreen/rules/1")])

    def test_a_gauge_page_shows_what_is_configured(self):
        host = FakeHost()
        host.settings["gauges"] = [{"label": "Disk", "command": "df"}]
        m = menu.Menu(host)
        m.open()
        goto(m, "Screen", "Gauges")
        labels = [entry.get("label") for entry in m._entries()]
        self.assertEqual(labels, ["Disk: df", "Clear every gauge",
                                  "Add a gauge"])


class Bindings(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.m = menu.Menu(self.host)
        self.m.open()

    def test_the_value_field_follows_the_type(self):
        goto(self.m, "Bindings", "G-keys", "G5")
        self.assertEqual([entry.get("label")
                          for entry in self.m._entries()],
                         ["Action", "not bound to anything"])
        self.m.sel = 0
        self.m.press("enter")                  # choose a type
        self.m.press("down")                   # "app"
        self.m.press("enter")
        self.assertEqual(self.host.settings["bindings"]["G5"],
                         {"type": "app"})
        rows = [entry.get("label") for entry in self.m._entries()]
        self.assertEqual(rows, ["Action", "App"])
        self.m.sel = 1
        self.m.press("enter")
        for char in "Safari":
            self.m.press(char)
        self.m.press("enter")
        self.assertEqual(self.host.settings["bindings"]["G5"],
                         {"type": "app", "name": "Safari"})

    def test_choosing_none_takes_the_binding_away(self):
        self.assertIn("G1", self.host.settings["bindings"])
        goto(self.m, "Bindings", "G-keys", "G1")
        self.m.sel = 0
        self.m.press("enter")                  # edit the type
        self.m.press("up")                     # back to "none", the first
        self.m.press("enter")
        self.assertNotIn("G1", self.host.settings["bindings"])

    def test_switching_bank_acts_instead_of_writing_the_flag(self):
        goto(self.m, "Bindings")
        self.m.sel = find(self.m, "Bank")
        self.m.press("enter")
        self.m.press("down")                   # bank 2
        self.m.press("enter")
        self.assertEqual(self.host.actions, [("bank", "2")])
        self.assertEqual(self.host.settings["active_bank"], "1")
        self.assertEqual(self.host.saves, 0)


class FakeKeyboard:
    """Enough of a keyboard to draw on, without a keyboard attached."""

    def __init__(self):
        self.frames = []
        self.mkeys = []
        self.colour = None
        self.power_on = None

    def send_lcd(self, pixels):
        self.frames.append(pixels)

    def set_backlight(self, rgb):
        self.colour = rgb

    def set_power_on_backlight(self, rgb):
        self.power_on = rgb

    def set_mkeys(self, names):
        self.mkeys = list(names)


class IdleReader:
    """A reader that answers with a machine nobody has touched for a while,
    so the ordinary rules pick a screen without asking macOS anything."""

    def __init__(self):
        self.reads = 0

    def read(self, block, rules=None, wait=False):
        self.reads += 1
        return {"idle": 400.0, "playing": False, "game": False,
                "fullscreen": False, "app": "Finder",
                "bundle": "com.apple.finder", "running": (),
                "commands": {}}


class AgentMenu(unittest.TestCase):
    """The agent's side of the menu: opening it, handing it keys, and where
    a change made from it lands.

    Redirected at a temporary config, so the real one is never touched, and
    with the event tap and the chat reader stubbed out - one needs Input
    Monitoring, the other would leave a thread polling Discord behind every
    test.
    """

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        for target in ("log",):
            patcher = mock.patch.object(daemon, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("sync_watcher", "ensure_tap"):
            patcher = mock.patch.object(daemon.Daemon, target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = FakeKeyboard()
        # The real reader would ask macOS what is in front; these tests
        # only care about what the menu does with the answer.
        self.agent.autoscreen = autoscreen.AutoScreen(IdleReader())

    def keys(self, *names):
        for name in names:
            self.agent.press_menu(name)

    def hold(self, name, held_for=None):
        """The display key pressed and *kept* down while the loop ticks -
        the only shape a hold arrives in."""
        start = time.time()
        held_for = daemon.HOLD_SECONDS + 0.1 if held_for is None else held_for
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) | {name}
        self.agent.note_gesture(name, now=start)
        moment = start
        while moment < start + held_for:
            moment += 0.05
            self.agent.refresh_gestures(moment)
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) - {name}

    # -- opening and closing

    def test_the_menu_opens_and_says_where_it_is(self):
        reply = self.agent.menu_command({"action": "open"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["open"])
        self.assertEqual(reply["view"]["title"], "Menu")
        self.assertIn("Screen", reply["view"]["main"][0])
        self.assertEqual(self.agent.menu_command({"action": "show"})["view"]
                         ["title"], "Menu")

    def test_a_key_walks_it_and_the_frame_follows(self):
        self.agent.menu_command({"action": "open"})
        reply = self.agent.menu_command({"action": "key", "name": "down"})
        self.assertEqual(reply["view"]["main"][0][0], ">")
        self.assertIn("Timers", reply["view"]["main"][0])
        reply = self.agent.menu_command({"action": "key", "name": "enter"})
        self.assertEqual(reply["view"]["title"], "Timers")
        self.agent.menu_command({"action": "close"})
        closed = self.agent.menu_command({"action": "show"})
        self.assertFalse(closed["open"])
        self.assertNotIn("view", closed)

    def test_a_menu_nobody_touches_closes_itself(self):
        self.agent.menu_command({"action": "open"})
        self.agent.refresh_menu(self.agent.menu_touch + daemon.MENU_IDLE)
        self.assertTrue(self.agent.menu.closed)

    # -- what the tap is allowed to take

    def test_only_keys_the_menu_can_use_are_wanted(self):
        self.assertFalse(self.agent.wants_menu_key("up", None))   # closed
        self.agent.menu_command({"action": "open"})
        self.assertTrue(self.agent.wants_menu_key("up", None))
        self.assertTrue(self.agent.wants_menu_key("esc", None))
        # A letter at a list is the app's, not the menu's.
        self.assertFalse(self.agent.wants_menu_key("a", "a"))
        # Settings is four rows down, then Display, then Font.
        self.keys("down", "down", "down", "down", "enter", "enter", "enter")
        editing = self.agent.menu.view()["editing"]
        self.assertEqual(editing["label"], "Font")
        self.assertTrue(self.agent.menu_typing)
        self.assertTrue(self.agent.wants_menu_key("a", "a"))
        self.keys("esc")
        self.assertFalse(self.agent.menu_typing)
        self.assertFalse(self.agent.wants_menu_key("a", "a"))

    # -- the display

    def test_the_menu_wins_the_display_from_an_alert(self):
        self.agent.alerts.push("message", {"text": "hello"}, 30.0, now=1000.0)
        self.agent.last_lcd = 999.0
        self.agent.refresh_lcd(999.0)
        interrupted = self.agent.keyboard.frames[-1]
        self.assertTrue(self.agent.alerting)
        self.agent.menu_command({"action": "open"})
        self.agent.refresh_lcd(1000.0)
        self.assertNotEqual(self.agent.keyboard.frames[-1], interrupted)
        self.assertFalse(self.agent.alerting)
        # The alert waits underneath and takes the panel straight back.
        self.agent.menu_command({"action": "close"})
        self.agent.refresh_lcd(1001.0)
        self.assertTrue(self.agent.alerting)
        self.assertEqual(self.agent.keyboard.frames[-1], interrupted)

    def test_the_autoscreen_rules_stand_down_while_the_menu_is_up(self):
        self.agent.last_lcd = 999.0
        self.agent.refresh_lcd(999.0)          # the rules pick something
        self.assertIsNotNone(self.agent.autoscreen.chosen)
        self.agent.menu_command({"action": "open"})
        self.agent.refresh_lcd(1000.0)
        self.assertIsNone(self.agent.autoscreen.chosen)

    def test_a_blank_display_still_shows_the_menu(self):
        # Otherwise there would be no way to turn it back on.
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
        self.agent.menu_command({"action": "open"})
        self.agent.refresh_lcd(1000.0)
        self.assertTrue(self.agent.keyboard.frames)

    # -- the hold: L2 down for half a second opens the menu

    def test_holding_the_second_key_opens_and_then_shuts_the_menu(self):
        self.assertIn("L2", config.DEFAULTS["lcd_keys"])
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.hold("L2")
        self.assertFalse(self.agent.menu.closed)
        self.hold("L2")
        self.assertTrue(self.agent.menu.closed)
        self.assertEqual(fired, [])             # nothing else ran on the way

    def test_a_press_that_is_not_a_hold_still_does_its_job(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        for _ in range(2):                     # two taps, neither a hold
            self.agent.note_gesture("L1")
            self.agent.refresh_gestures(time.time() + daemon.GESTURE_HOLD)
        self.assertEqual(fired, ["L1", "L1"])
        self.assertTrue(self.agent.menu.closed)

    def test_leave_shuts_the_menu_once_and_not_twice(self):
        # The Leave row tells the host it is leaving *and* closes itself, so
        # the host must not close it again - otherwise one press of enter
        # writes two "menu closed" lines and reads as two gestures.
        self.agent.menu_command({"action": "open"})
        self.keys("up", "enter")                # up wraps onto the last row
        self.assertTrue(self.agent.menu.closed)
        closed = [call.args[0] for call in daemon.log.call_args_list
                  if call.args and call.args[0] == "menu closed"]
        self.assertEqual(closed, ["menu closed"])

    def test_a_key_under_the_menu_changes_nothing(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.agent.menu_command({"action": "open"})
        self.agent.note_gesture("L2")
        self.agent.refresh_gestures(time.time() + daemon.GESTURE_HOLD)
        self.assertEqual(fired, [])             # nothing underneath to switch
        self.assertFalse(self.agent.menu.closed)  # and it did not shut either

    # -- where a change lands

    def test_a_setting_changed_from_the_menu_reaches_the_config_file(self):
        self.agent.menu_command({"action": "open"})
        self.keys("enter", "down", "enter")     # Screen > Display: toggle
        self.assertFalse(self.agent.config["lcd"]["enabled"])
        self.assertTrue(os.path.exists(config.CONFIG_PATH))
        self.assertFalse(config.load()["lcd"]["enabled"])
        self.assertIsNone(config.last_error)

    def test_a_rule_can_be_added_and_taken_away(self):
        self.agent.menu_action("rule_add", "triggers/rules")
        rules = self.agent.config["triggers"]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["screen"], "status")
        self.assertEqual(rules[0]["when"], {})
        self.agent.menu_action("rule_del", "triggers/rules/0")
        self.assertEqual(self.agent.config["triggers"]["rules"], [])

    def test_a_condition_comes_in_with_a_value_that_means_not_yet(self):
        self.agent.menu_action("when_add", "autoscreen/rules/0/when/playing")
        when = self.agent.config["autoscreen"]["rules"][0]["when"]
        self.assertIn("playing", when)
        self.assertIs(when["playing"], False)
        self.assertTrue(self.agent.menu_action("when_del",
                                               "autoscreen/rules/0/when/playing"))
        self.assertNotIn("playing",
                         self.agent.config["autoscreen"]["rules"][0]["when"])

    def test_a_gauge_needs_a_label_and_a_command(self):
        self.assertIn("use:", self.agent.menu_action("gauge_add", "Disk"))
        self.agent.menu_action("gauge_add", "Disk|df -h /")
        self.assertEqual(self.agent.config["gauges"],
                         [{"label": "Disk", "command": "df -h /"}])
        self.agent.menu_action("gauge_clear")
        self.assertEqual(self.agent.config["gauges"], [])

    def test_an_app_colour_needs_a_colour_the_tool_knows(self):
        self.assertIn("use:", self.agent.menu_action("app_color_add", "Safari"))
        message = self.agent.menu_action("app_color_add", "Safari|red")
        self.assertIn("red", message)
        self.assertEqual(self.agent.config["app_colors"], {"Safari": "red"})

    def test_a_timer_started_from_the_menu_says_so_and_stops_again(self):
        self.addCleanup(self.agent.timers.cancel, "all")
        message = self.agent.menu_action("timer_start", "pomodoro")
        self.assertIn("25", message)
        self.assertTrue(self.agent.timers.running())
        self.assertIn("cancelled", self.agent.menu_action("timer_cancel", "all"))
        self.assertFalse(self.agent.timers.running())

    def test_firing_a_trigger_from_the_menu_puts_it_up(self):
        self.agent.config["triggers"] = {
            "enabled": True, "check_seconds": 2.0,
            "rules": [{"name": "coffee", "screen": "status",
                       "when": {"game": True}, "seconds": 6}]}
        message = self.agent.menu_action("trigger_fire", "coffee")
        self.assertIn("coffee", message)
        self.assertIsNotNone(self.agent.alerts.current(time.time()))
        self.assertIn("no trigger",
                      self.agent.menu_action("trigger_fire", "nope"))


class DriftGuards(unittest.TestCase):
    """The menu keeps its own copies of two lists so it can be imported
    without dragging in the CLI and the autoscreen rules; these fail loudly
    if the originals move."""

    def test_conditions_are_autoscreen_conditions(self):
        self.assertEqual(set(menu.CONDITIONS), set(autoscreen.CONDITIONS))

    def test_indicators_are_the_cli_indicators(self):
        self.assertEqual(menu.INDICATORS, cli.INDICATORS)

    def test_backends_are_the_printer_backends(self):
        import printer
        self.assertEqual(menu._backends(), printer.BACKENDS)

    def test_every_factory_screen_is_reachable(self):
        for name in screens_names():
            self.assertIn(name, menu._screens())

    def test_every_display_key_is_offered(self):
        import screens
        options = menu._lcd_keys()
        self.assertEqual(options[:3], ("next", "prev", "off"))
        for name in screens.SCREENS:
            self.assertIn(f"screen:{name}", options)
        for value in config.DEFAULTS["lcd_keys"].values():
            self.assertIn(value, options)


def screens_names():
    import screens
    return tuple(screens.SCREENS)


if __name__ == "__main__":
    unittest.main()
