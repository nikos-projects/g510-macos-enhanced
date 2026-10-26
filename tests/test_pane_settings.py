"""The Settings pane and the LCD menu are two front-ends over one config.

If the menu can change a path that neither this pane nor one of the other
four tabs can, the window has a hole; if the pane writes a path neither the
menu nor the config knows, it is inventing settings. Both directions are
asserted here, so the two front-ends can only drift apart on purpose.

The pane is also built for real: AppKit answers questions about a table the
moment it is attached, and a FIELDS row that names a card nobody builds or a
kind nobody handles would otherwise only show up when somebody opened the
window and clicked.
"""
import os
import tempfile
import unittest
from unittest import mock

import config
import menu
import pane_settings
import window

MISSING = object()


def menu_paths(entries=None):
    """Every config path a plain entry in the LCD menu writes.

    A page whose children are a function - the rules, the gauges, the G-keys
    - is skipped: those are lists, and the pane edits them as lists.
    """
    found = set()
    for entry in menu.build() if entries is None else entries:
        if entry.get("kind") == "sub":
            children = entry.get("children")
            if callable(children):
                continue
            found |= menu_paths(children)
        elif entry.get("path"):
            found.add(entry["path"])
    return found


def scalar_paths():
    return {path for path, spec in pane_settings.FIELDS.items()
            if spec["kind"] in pane_settings._SCALAR}


class ParityWithTheMenu(unittest.TestCase):
    def test_the_pane_reaches_every_path_the_menu_does(self):
        missing = (menu_paths() - scalar_paths()
                   - pane_settings.OWNED_ELSEWHERE)
        self.assertEqual(missing, set(),
                         "the menu can change these and no tab can: "
                         + ", ".join(sorted(missing)))

    def test_the_pane_shows_nothing_another_tab_already_shows(self):
        extra = scalar_paths() - (menu_paths() - pane_settings.OWNED_ELSEWHERE)
        self.assertEqual(extra, set(),
                         "these are built twice: " + ", ".join(sorted(extra)))

    def test_every_path_is_one_the_config_already_has(self):
        defaults = config._merge({}, config.DEFAULTS)
        for path in sorted(scalar_paths()):
            self.assertIsNot(menu.get(defaults, path, MISSING), MISSING,
                             f"{path} is not in the default config")

    def test_every_row_says_what_it_is_and_where_it_goes(self):
        for path, spec in pane_settings.FIELDS.items():
            self.assertIn(spec["kind"], pane_settings._KINDS, path)
            self.assertIn("card", spec, path)
            if spec["kind"] in pane_settings._SCALAR:
                for key in ("top", "x", "w"):
                    self.assertIn(key, spec, f"{path} has no {key}")
            if "options" in spec:
                self.assertTrue(tuple(spec["options"]()), path)

    def test_the_lists_it_edits_are_the_lists_the_menu_edits(self):
        kinds = {}
        for path, spec in pane_settings.FIELDS.items():
            kinds.setdefault(spec["kind"], set()).add(path)
        self.assertEqual(kinds.get("rows"),
                         {"gauges", "timers/presets", "app_colors"})
        self.assertEqual(kinds.get("rules"),
                         {"autoscreen/rules", "triggers/rules"})

    def test_the_window_offers_the_pane(self):
        self.assertIn("Settings", window.PANES)
        for _block, card, _title in pane_settings.RULE_BLOCKS:
            self.assertIn(card, {spec["card"]
                                 for spec in pane_settings.FIELDS.values()}
                         | {"rules-auto"})


class FakeSender:
    """As much of a control as the dispatcher asks for."""

    def __init__(self, ident, state=0, value="", index=-1):
        self._ident, self._state = ident, state
        self._value, self._index = value, index

    def identifier(self):
        return self._ident

    def state(self):
        return self._state

    def stringValue(self):
        return self._value

    def indexOfSelectedItem(self):
        return self._index

    def selectItemAtIndex_(self, index):
        self._index = index


class Built(unittest.TestCase):
    """The pane as the window builds it, on a config of its own."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        reloads = mock.patch("cli.reload_agent")
        reloads.start()
        self.addCleanup(reloads.stop)
        self.win = window.G510Window.alloc().initWithMenu_(None)
        # A refused change opens an alert and would wait for a click.
        self.win.complain = mock.Mock()

    def test_every_control_is_built_and_reads_itself_back(self):
        # 31 paths: the ones the menu owns a row for and no other tab shows.
        self.assertEqual(len(self.win.s_ctrls),
                         len(scalar_paths()))
        self.win.refresh()
        self.assertEqual(self.win.rule_table["autoscreen"].numberOfRows(),
                         len(config.load()["autoscreen"]["rules"]))
        self.assertEqual(self.win.rule_table["triggers"].numberOfRows(), 0)

    def test_the_lists_are_built_from_the_config(self):
        self.win.refresh()
        # Four default presets, three controls each: name, length, delete.
        self.assertEqual(len(self.win.rows_view["presets"].subviews()), 12)
        self.assertEqual(len(self.win.rows_view["gauges"].subviews()), 0)

    def test_a_toggle_reaches_the_config_file(self):
        pane_settings.changed(self.win, FakeSender("bool:autoscreen/enabled"))
        self.assertFalse(config.load()["autoscreen"]["enabled"])
        self.assertFalse(self.win.config["autoscreen"]["enabled"])

    def test_a_bad_number_is_refused_and_says_why(self):
        pane_settings.changed(
            self.win, FakeSender("num:autoscreen/idle_seconds", value="soon"))
        self.win.complain.assert_called_once()
        self.assertIn("soon", str(self.win.complain.call_args[0][0]))
        self.assertEqual(config.load()["autoscreen"]["idle_seconds"], 60)

    def test_a_number_keeps_its_shape(self):
        pane_settings.changed(
            self.win, FakeSender("num:autoscreen/idle_seconds", value="120"))
        pane_settings.changed(
            self.win, FakeSender("num:autoscreen/hold_seconds", value="1.5"))
        fresh = config.load()["autoscreen"]
        self.assertEqual(fresh["idle_seconds"], 120)
        self.assertIsInstance(fresh["idle_seconds"], int)
        self.assertEqual(fresh["hold_seconds"], 1.5)

    def test_a_comma_list_becomes_a_list(self):
        pane_settings.changed(
            self.win, FakeSender("csv:autoscreen/games", value="Minecraft*, "
                                                      "com.mojang.*"))
        self.assertEqual(config.load()["autoscreen"]["games"],
                         ["Minecraft*", "com.mojang.*"])

    def test_a_rule_can_be_added_and_is_selected_for_editing(self):
        pane_settings.changed(self.win, FakeSender("rule-add:autoscreen"))
        rules = config.load()["autoscreen"]["rules"]
        self.assertEqual(len(rules), 5)
        self.assertEqual(self.win.rule_selected["autoscreen"], 4)
        self.win.refresh()
        self.assertEqual(
            self.win.rule_table["autoscreen"].numberOfRows(), 5)

    def test_a_condition_is_added_to_the_rule_being_edited(self):
        # Row 0 of the default rules is `game`; adding `playing` there puts
        # it in the config and leaves the rest of the rule alone.
        pane_settings.changed(self.win, FakeSender("cond-add:autoscreen",
                                                   index=1))
        when = config.load()["autoscreen"]["rules"][0]["when"]
        self.assertIn("playing", when)
        self.assertIs(when["playing"], False)
        self.assertIn("game", when)

    def test_an_unknown_screen_is_refused(self):
        pane_settings.changed(
            self.win, FakeSender("screens:lcd/cycle", value="status, nope"))
        self.win.complain.assert_called_once()
        self.assertEqual(config.load()["lcd"]["cycle"],
                         ["status", "clock", "claude", "media", "app",
                          "timers"])

    def test_a_gauge_needs_a_label_and_a_command(self):
        refs = self.win.add_refs["gauges"]
        refs["first"].setStringValue_("Disk")
        pane_settings.changed(self.win, FakeSender("gauge-add:"))
        self.win.complain.assert_called_once()
        self.assertEqual(config.load()["gauges"], [])

        refs["second"].setStringValue_("df -h /")
        pane_settings.changed(self.win, FakeSender("gauge-add:"))
        self.assertEqual(config.load()["gauges"],
                         [{"label": "Disk", "command": "df -h /"}])
        self.assertEqual(refs["first"].stringValue(), "")


if __name__ == "__main__":
    unittest.main()
