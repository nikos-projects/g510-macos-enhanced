"""Config parsing, merging, banks and saving.

Every test that touches the filesystem redirects the module at a temporary
directory first, so the real ~/.config/g510/config.json is never read or
written by the suite.
"""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from config import (ColorError, apply_brightness, effective_color,
                    format_color, parse_color)


class ColorParsingTests(unittest.TestCase):

    def test_named_colours(self):
        self.assertEqual(parse_color("red"), (255, 0, 0))
        self.assertEqual(parse_color("cyan"), (0, 255, 255))
        self.assertEqual(parse_color("off"), (0, 0, 0))
        self.assertEqual(parse_color("black"), (0, 0, 0))

    def test_names_are_case_and_space_insensitive(self):
        self.assertEqual(parse_color("  RED  "), (255, 0, 0))
        self.assertEqual(parse_color("Green"), (0, 255, 0))

    def test_every_named_colour_is_a_valid_rgb_triple(self):
        for name in config.NAMED_COLORS:
            with self.subTest(colour=name):
                rgb = parse_color(name)
                self.assertEqual(len(rgb), 3)
                self.assertTrue(all(0 <= channel <= 255 for channel in rgb))

    def test_six_digit_hex_with_and_without_hash(self):
        self.assertEqual(parse_color("#00ffff"), (0, 255, 255))
        self.assertEqual(parse_color("00ffff"), (0, 255, 255))

    def test_hex_is_case_insensitive(self):
        self.assertEqual(parse_color("#FF8000"), (255, 128, 0))

    def test_three_digit_hex_is_expanded(self):
        self.assertEqual(parse_color("#0ff"), (0, 255, 255))
        self.assertEqual(parse_color("#abc"), (170, 187, 204))

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(parse_color("  #010203\n"), (1, 2, 3))

    def test_missing_colour(self):
        with self.assertRaises(ColorError):
            parse_color(None)

    def test_empty_and_blank_strings(self):
        for text in ("", "   ", "#"):
            with self.subTest(text=text):
                with self.assertRaises(ColorError):
                    parse_color(text)

    def test_unknown_name(self):
        with self.assertRaises(ColorError):
            parse_color("puce")

    def test_wrong_length_hex(self):
        for text in ("#12345", "#1234567", "#ff", "#12345678"):
            with self.subTest(text=text):
                with self.assertRaises(ColorError):
                    parse_color(text)

    def test_non_hex_digits(self):
        with self.assertRaises(ColorError):
            parse_color("#GGGGGG")
        with self.assertRaises(ColorError):
            parse_color("#ggg")

    def test_error_message_lists_the_alternatives(self):
        with self.assertRaises(ColorError) as caught:
            parse_color("puce")
        self.assertIn("puce", str(caught.exception))
        self.assertIn("red", str(caught.exception))


class ColorFormattingTests(unittest.TestCase):

    def test_formats_lowercase_and_padded(self):
        self.assertEqual(format_color((0, 0, 0)), "#000000")
        self.assertEqual(format_color((255, 255, 255)), "#ffffff")
        self.assertEqual(format_color((1, 2, 3)), "#010203")
        self.assertEqual(format_color((255, 128, 0)), "#ff8000")

    def test_round_trips_through_parse(self):
        for rgb in ((0, 0, 0), (255, 255, 255), (18, 52, 86), (255, 90, 0)):
            with self.subTest(rgb=rgb):
                self.assertEqual(parse_color(format_color(rgb)), rgb)

    def test_every_named_colour_round_trips(self):
        for name, rgb in config.NAMED_COLORS.items():
            with self.subTest(colour=name):
                self.assertEqual(parse_color(format_color(rgb)), rgb)


class BrightnessTests(unittest.TestCase):

    def test_full_brightness_is_the_colour_itself(self):
        self.assertEqual(apply_brightness((255, 90, 0), 100), (255, 90, 0))

    def test_zero_brightness_is_black(self):
        self.assertEqual(apply_brightness((255, 90, 0), 0), (0, 0, 0))

    def test_half_brightness_scales_every_channel(self):
        self.assertEqual(apply_brightness((200, 100, 0), 50), (100, 50, 0))

    def test_result_is_whole_numbers(self):
        result = apply_brightness((255, 255, 255), 33)
        self.assertTrue(all(isinstance(channel, int) for channel in result))
        self.assertEqual(result, (84, 84, 84))

    def test_brightness_above_one_hundred_is_clamped(self):
        self.assertEqual(apply_brightness((10, 20, 30), 400), (10, 20, 30))

    def test_negative_brightness_is_clamped_to_off(self):
        self.assertEqual(apply_brightness((10, 20, 30), -50), (0, 0, 0))

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(apply_brightness((200, 100, 0), "50"), (100, 50, 0))

    def test_black_stays_black_at_any_brightness(self):
        self.assertEqual(apply_brightness((0, 0, 0), 100), (0, 0, 0))
        self.assertEqual(apply_brightness((0, 0, 0), 50), (0, 0, 0))


class EffectiveColorTests(unittest.TestCase):

    def test_colour_and_brightness_together(self):
        self.assertEqual(effective_color({"backlight": "red",
                                          "brightness": 50}), (128, 0, 0))

    def test_defaults_to_full_white(self):
        self.assertEqual(effective_color({}), (255, 255, 255))

    def test_missing_brightness_means_full(self):
        self.assertEqual(effective_color({"backlight": "#00ff00"}), (0, 255, 0))

    def test_missing_backlight_means_white(self):
        self.assertEqual(effective_color({"brightness": 0}), (0, 0, 0))

    def test_out_of_range_brightness_is_clamped(self):
        self.assertEqual(effective_color({"backlight": "blue",
                                          "brightness": 250}), (0, 0, 255))

    def test_a_bad_colour_is_reported_not_guessed(self):
        with self.assertRaises(ColorError):
            effective_color({"backlight": "puce"})

    def test_matches_the_default_config(self):
        self.assertEqual(effective_color(config.DEFAULTS), (255, 255, 255))


class MergeTests(unittest.TestCase):
    """_merge fills in the defaults without handing out the defaults."""

    def setUp(self):
        self.snapshot = copy.deepcopy(config.DEFAULTS)
        self.addCleanup(lambda: self.assertEqual(config.DEFAULTS, self.snapshot,
                                                 "DEFAULTS was mutated"))

    def test_an_empty_override_yields_the_defaults(self):
        self.assertEqual(config._merge(config.DEFAULTS, {}), config.DEFAULTS)

    def test_nested_dicts_are_merged_not_replaced(self):
        merged = config._merge(config.DEFAULTS, {"lcd": {"screen": "clock"}})
        self.assertEqual(merged["lcd"]["screen"], "clock")
        self.assertTrue(merged["lcd"]["enabled"])
        self.assertEqual(merged["lcd"]["refresh_seconds"], 1.0)

    def test_unknown_keys_are_kept(self):
        merged = config._merge(config.DEFAULTS, {"future_setting": 7})
        self.assertEqual(merged["future_setting"], 7)

    def test_a_scalar_replaces_a_dict(self):
        merged = config._merge(config.DEFAULTS, {"lcd": "off"})
        self.assertEqual(merged["lcd"], "off")

    def test_a_dict_replaces_a_scalar(self):
        merged = config._merge(config.DEFAULTS, {"backlight": {"r": 1}})
        self.assertEqual(merged["backlight"], {"r": 1})

    def test_lists_are_replaced_wholesale(self):
        merged = config._merge(config.DEFAULTS, {"lcd": {"cycle": ["clock"]}})
        self.assertEqual(merged["lcd"]["cycle"], ["clock"])

    def test_nested_dicts_are_not_shared_with_the_defaults(self):
        merged = config._merge(config.DEFAULTS, {})
        self.assertIsNot(merged["lcd"], config.DEFAULTS["lcd"])
        self.assertIsNot(merged["macros"], config.DEFAULTS["macros"])
        self.assertIsNot(merged["bindings"], config.DEFAULTS["bindings"])
        self.assertIsNot(merged["bindings"]["G1"],
                         config.DEFAULTS["bindings"]["G1"])

    def test_mutating_the_result_leaves_the_defaults_alone(self):
        merged = config._merge(config.DEFAULTS, {"backlight": "red"})
        merged["lcd"]["screen"] = "printer"
        merged["macros"]["mine"] = [{"keys": "cmd+c"}]
        merged["app_colors"]["Terminal"] = "green"
        merged["bindings"]["G1"]["name"] = "Mail"
        self.assertEqual(config.DEFAULTS["lcd"]["screen"], "status")
        self.assertEqual(config.DEFAULTS["macros"], {})
        self.assertEqual(config.DEFAULTS["app_colors"], {})
        self.assertEqual(config.DEFAULTS["bindings"]["G1"]["name"], "Safari")

    def test_the_override_is_not_captured_either(self):
        override = {"lcd": {"screen": "clock"}}
        merged = config._merge(config.DEFAULTS, override)
        merged["lcd"]["screen"] = "media"
        self.assertEqual(override["lcd"]["screen"], "clock")


class ConfigFileTestCase(unittest.TestCase):
    """Base class: redirect the module at a temporary directory."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        self.path = os.path.join(self.dir, "config.json")
        self.assertNotEqual(self.path,
                            os.path.expanduser("~/.config/g510/config.json"))
        patcher = mock.patch.multiple(config, CONFIG_DIR=self.dir,
                                      CONFIG_PATH=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The module remembers the last good config and the last parse error
        # between calls, so each test starts from a clean slate.
        self.reset_module_state()
        self.addCleanup(self.reset_module_state)
        snapshot = copy.deepcopy(config.DEFAULTS)
        self.addCleanup(lambda: self.assertEqual(config.DEFAULTS, snapshot,
                                                 "DEFAULTS was mutated"))

    def reset_module_state(self):
        config.last_error = None
        config._last_good = None

    def write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)

    def write_config(self, payload):
        self.write(json.dumps(payload))

    def written(self):
        with open(self.path) as handle:
            return json.load(handle)


class LoadTests(ConfigFileTestCase):

    def test_missing_file_gives_the_defaults(self):
        settings = config.load()
        self.assertEqual(settings["backlight"], "#ffffff")
        self.assertEqual(settings["lcd"]["screen"], "status")
        self.assertIsNone(config.last_error)

    def test_missing_file_does_not_create_one(self):
        config.load()
        self.assertFalse(os.path.exists(self.path))

    def test_a_partial_file_still_gets_every_default(self):
        self.write_config({"backlight": "#ff0000"})
        settings = config.load()
        self.assertEqual(settings["backlight"], "#ff0000")
        self.assertEqual(settings["lcd"]["refresh_seconds"], 1.0)
        self.assertEqual(settings["claude"]["rows"], ["five_hour", "seven_day"])

    def test_a_partial_nested_block_keeps_its_siblings(self):
        self.write_config({"lcd": {"screen": "clock"}})
        settings = config.load()
        self.assertEqual(settings["lcd"]["screen"], "clock")
        self.assertTrue(settings["lcd"]["enabled"])

    def test_mutating_a_loaded_config_never_reaches_the_defaults(self):
        self.write_config({"backlight": "#ff0000"})
        settings = config.load()
        settings["lcd"]["screen"] = "printer"
        settings["macros"]["recorded"] = [{"keys": "cmd+c"}]
        self.assertEqual(config.DEFAULTS["lcd"]["screen"], "status")
        self.assertEqual(config.DEFAULTS["macros"], {})
        self.assertEqual(config.load()["lcd"]["screen"], "status")

    def test_two_loads_do_not_share_state(self):
        self.write_config({"backlight": "#ff0000"})
        first, second = config.load(), config.load()
        first["lcd"]["screen"] = "media"
        self.assertEqual(second["lcd"]["screen"], "status")


class MalformedFileTests(ConfigFileTestCase):

    def test_broken_json_falls_back_to_the_defaults(self):
        self.write('{"backlight": "#ff0000"')
        settings = config.load()
        self.assertEqual(settings["backlight"], "#ffffff")
        self.assertIsNotNone(config.last_error)
        self.assertIn(self.path, config.last_error)

    def test_an_empty_file_is_treated_as_broken(self):
        self.write("")
        config.load()
        self.assertIsNotNone(config.last_error)

    def test_broken_json_falls_back_to_the_last_good_config(self):
        self.write_config({"backlight": "#112233", "brightness": 40})
        self.assertEqual(config.load()["backlight"], "#112233")
        self.write("{ this is not json")
        settings = config.load()
        self.assertEqual(settings["backlight"], "#112233")
        self.assertEqual(settings["brightness"], 40)
        self.assertIsNotNone(config.last_error)

    def test_the_fallback_is_a_copy_not_the_remembered_config(self):
        self.write_config({"backlight": "#112233"})
        config.load()
        self.write("{ broken")
        first = config.load()
        first["backlight"] = "#000000"
        self.assertEqual(config.load()["backlight"], "#112233")

    def test_repairing_the_file_clears_the_error(self):
        self.write("{ broken")
        config.load()
        self.assertIsNotNone(config.last_error)
        self.write_config({"backlight": "#00ff00"})
        self.assertEqual(config.load()["backlight"], "#00ff00")
        self.assertIsNone(config.last_error)

    def test_an_unreadable_file_is_reported_not_raised(self):
        os.mkdir(self.path)          # a directory where the file should be
        settings = config.load()
        self.assertEqual(settings["backlight"], "#ffffff")
        self.assertIsNotNone(config.last_error)
        self.assertIn("Could not read", config.last_error)

    def test_valid_json_that_is_not_an_object_is_survivable(self):
        # BUG: json.load() succeeds for `null`, a list or a bare string, and
        # _merge then raises AttributeError straight out of load(), which is
        # exactly the "a broken file takes down the agent" case load() exists
        # to prevent. Reported, not fixed.
        for text in ("null", "[1, 2]", '"hello"'):
            with self.subTest(text=text):
                self.write(text)
                settings = config.load()
                self.assertEqual(settings["backlight"], "#ffffff")
                self.assertIsNotNone(config.last_error)


class BankMigrationTests(ConfigFileTestCase):

    def test_three_banks_always_exist(self):
        self.assertEqual(sorted(config.load()["banks"]), ["1", "2", "3"])

    def test_bank_one_is_active_by_default(self):
        settings = config.load()
        self.assertEqual(settings["active_bank"], "1")
        self.assertIs(settings["bindings"], settings["banks"]["1"])

    def test_a_flat_bindings_set_migrates_into_bank_one(self):
        self.write_config({"bindings": {"G1": {"type": "app", "name": "Mail"}}})
        settings = config.load()
        self.assertEqual(settings["banks"]["1"]["G1"],
                         {"type": "app", "name": "Mail"})
        self.assertEqual(settings["banks"]["2"], {})
        self.assertEqual(settings["banks"]["3"], {})

    def test_migration_leaves_bindings_pointing_at_bank_one(self):
        self.write_config({"bindings": {"G1": {"type": "app", "name": "Mail"}}})
        settings = config.load()
        self.assertIs(settings["bindings"], settings["banks"]["1"])

    def test_an_existing_bank_one_is_not_overwritten_by_flat_bindings(self):
        self.write_config({
            "bindings": {"G1": {"type": "app", "name": "Old"}},
            "banks": {"1": {"G2": {"type": "app", "name": "New"}}},
        })
        settings = config.load()
        self.assertEqual(sorted(settings["banks"]["1"]), ["G2"])

    def test_bindings_is_a_live_view_of_the_active_bank(self):
        self.write_config({"active_bank": "2",
                           "banks": {"2": {"G5": {"type": "keys",
                                                  "keys": "cmd+c"}}}})
        settings = config.load()
        self.assertEqual(settings["active_bank"], "2")
        self.assertIs(settings["bindings"], settings["banks"]["2"])
        settings["bindings"]["G6"] = {"type": "keys", "keys": "cmd+v"}
        self.assertIn("G6", settings["banks"]["2"])

    def test_an_unknown_active_bank_falls_back_to_one(self):
        for value in ("9", "", "banana", 0, None, -1):
            with self.subTest(value=value):
                self.write_config({"active_bank": value})
                settings = config.load()
                self.assertEqual(settings["active_bank"], "1")
                self.assertIs(settings["bindings"], settings["banks"]["1"])

    def test_a_numeric_active_bank_is_accepted(self):
        self.write_config({"active_bank": 3})
        self.assertEqual(config.load()["active_bank"], "3")

    def test_banks_of_the_wrong_shape_are_rebuilt(self):
        for value in ("nonsense", [1, 2, 3], None, 7):
            with self.subTest(value=value):
                self.write_config({"banks": value})
                settings = config.load()
                self.assertEqual(sorted(settings["banks"]), ["1", "2", "3"])

    def test_a_single_bank_of_the_wrong_shape_is_rebuilt(self):
        self.write_config({"banks": {"2": ["G2"], "3": {"G3": {"type": "app",
                                                              "name": "Mail"}}}})
        settings = config.load()
        self.assertEqual(settings["banks"]["2"], {})
        self.assertEqual(settings["banks"]["3"],
                         {"G3": {"type": "app", "name": "Mail"}})

    def test_a_missing_bank_is_added(self):
        self.write_config({"banks": {"1": {"G1": {"type": "app",
                                                  "name": "Mail"}}}})
        settings = config.load()
        self.assertEqual(settings["banks"]["3"], {})
        self.assertEqual(sorted(settings["banks"]["1"]), ["G1"])


class SetBankTests(ConfigFileTestCase):

    def test_switching_bank_points_bindings_at_it(self):
        settings = config.set_bank(config.load(), "2")
        self.assertEqual(settings["active_bank"], "2")
        self.assertIs(settings["bindings"], settings["banks"]["2"])

    def test_returns_the_same_settings_object(self):
        settings = config.load()
        self.assertIs(config.set_bank(settings, "3"), settings)

    def test_bindings_added_after_switching_land_in_that_bank(self):
        settings = config.set_bank(config.load(), "2")
        settings["bindings"]["G7"] = {"type": "shell", "command": "say hi"}
        self.assertIn("G7", settings["banks"]["2"])
        self.assertNotIn("G7", settings["banks"]["1"])

    def test_switching_away_and_back_keeps_each_banks_bindings(self):
        settings = config.load()
        settings["bindings"]["G1"] = {"type": "app", "name": "Mail"}
        config.set_bank(settings, "2")
        settings["bindings"]["G1"] = {"type": "app", "name": "Safari"}
        config.set_bank(settings, "1")
        self.assertEqual(settings["bindings"]["G1"],
                         {"type": "app", "name": "Mail"})
        self.assertEqual(settings["banks"]["2"]["G1"],
                         {"type": "app", "name": "Safari"})

    def test_a_number_is_accepted(self):
        self.assertEqual(config.set_bank(config.load(), 2)["active_bank"], "2")

    def test_an_unknown_bank_is_refused(self):
        for value in ("4", "0", "", None, "one"):
            with self.subTest(value=value):
                settings = config.load()
                with self.assertRaises(ValueError):
                    config.set_bank(settings, value)
                self.assertEqual(settings["active_bank"], "1")

    def test_refusal_names_the_valid_banks(self):
        with self.assertRaises(ValueError) as caught:
            config.set_bank(config.load(), "4")
        self.assertIn("1", str(caught.exception))

    def test_works_on_a_settings_dict_that_has_no_banks_yet(self):
        settings = config.set_bank({}, "2")
        self.assertEqual(settings["banks"]["2"], {})
        self.assertIs(settings["bindings"], settings["banks"]["2"])


class SaveTests(ConfigFileTestCase):

    def test_saving_then_loading_round_trips(self):
        settings = config.load()
        settings["backlight"] = "#123456"
        settings["brightness"] = 42
        settings["lcd"]["screen"] = "clock"
        self.assertTrue(config.save(settings))
        reloaded = config.load()
        self.assertEqual(reloaded["backlight"], "#123456")
        self.assertEqual(reloaded["brightness"], 42)
        self.assertEqual(reloaded["lcd"]["screen"], "clock")

    def test_bindings_are_persisted_as_the_active_bank(self):
        settings = config.load()
        settings["bindings"]["G9"] = {"type": "shell", "command": "say hi"}
        config.save(settings)
        self.assertEqual(self.written()["banks"]["1"]["G9"],
                         {"type": "shell", "command": "say hi"})

    def test_no_second_copy_of_the_bindings_is_written(self):
        # `bindings` is a view of the active bank; writing both would leave two
        # copies to drift apart.
        settings = config.load()
        settings["bindings"]["G9"] = {"type": "app", "name": "Mail"}
        config.save(settings)
        self.assertNotIn("bindings", self.written())

    def test_the_active_bank_is_saved_into_the_right_slot(self):
        settings = config.set_bank(config.load(), "3")
        settings["bindings"]["G2"] = {"type": "app", "name": "Mail"}
        config.save(settings)
        reloaded = config.load()
        self.assertEqual(reloaded["active_bank"], "3")
        self.assertEqual(reloaded["banks"]["3"]["G2"],
                         {"type": "app", "name": "Mail"})
        self.assertEqual(reloaded["banks"]["2"], {})

    def test_the_other_banks_survive_a_save(self):
        settings = config.load()
        settings["banks"]["2"]["G4"] = {"type": "app", "name": "Mail"}
        config.save(settings)
        self.assertEqual(config.load()["banks"]["2"]["G4"],
                         {"type": "app", "name": "Mail"})

    def test_saving_does_not_mutate_the_config_handed_in(self):
        settings = config.load()
        settings["bindings"]["G1"] = {"type": "app", "name": "Mail"}
        config.save(settings)
        self.assertIn("bindings", settings)
        self.assertEqual(settings["bindings"]["G1"],
                         {"type": "app", "name": "Mail"})

    def test_the_file_is_readable_json(self):
        config.save(config.load())
        with open(self.path) as handle:
            text = handle.read()
        self.assertTrue(text.endswith("\n"))
        json.loads(text)

    def test_the_directory_is_created_if_it_is_missing(self):
        nested = os.path.join(self.dir, "nested", "g510")
        with mock.patch.multiple(config, CONFIG_DIR=nested,
                                 CONFIG_PATH=os.path.join(nested,
                                                          "config.json")):
            self.assertTrue(config.save({"backlight": "red"}))
            self.assertTrue(os.path.exists(os.path.join(nested, "config.json")))

    def test_no_temporary_files_are_left_behind(self):
        config.save(config.load())
        self.assertEqual(os.listdir(self.dir), ["config.json"])


class SaveRefusalTests(ConfigFileTestCase):
    """A background write must not clobber a half-finished hand edit."""

    def broken_file(self):
        text = '{"backlight": "#ff0000",'
        self.write(text)
        config.load()                       # records the parse failure
        self.assertIsNotNone(config.last_error)
        return text

    def test_save_is_refused_while_the_file_on_disk_is_broken(self):
        text = self.broken_file()
        self.assertFalse(config.save({"backlight": "#00ff00"}))
        with open(self.path) as handle:
            self.assertEqual(handle.read(), text)

    def test_a_forced_save_goes_through(self):
        self.broken_file()
        self.assertTrue(config.save({"backlight": "#00ff00"}, force=True))
        self.assertEqual(self.written()["backlight"], "#00ff00")

    def test_saving_works_again_once_the_file_parses(self):
        self.broken_file()
        self.write_config({"backlight": "#0000ff"})
        config.load()
        self.assertIsNone(config.last_error)
        self.assertTrue(config.save({"backlight": "#00ff00"}))
        self.assertEqual(self.written()["backlight"], "#00ff00")

    def test_a_refused_save_leaves_no_temporary_files(self):
        self.broken_file()
        config.save({"backlight": "#00ff00"})
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_an_unreadable_file_also_blocks_saving(self):
        os.mkdir(self.path)
        config.load()
        self.assertFalse(config.save({"backlight": "#00ff00"}))


class EnsureExistsTests(ConfigFileTestCase):

    def test_writes_the_file_the_first_time_only(self):
        self.assertTrue(config.ensure_exists())
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(config.ensure_exists())

    def test_does_not_overwrite_an_existing_file(self):
        self.write_config({"backlight": "#abcdef"})
        self.assertFalse(config.ensure_exists())
        self.assertEqual(self.written()["backlight"], "#abcdef")

    def test_what_it_writes_loads_back_as_the_defaults(self):
        config.ensure_exists()
        settings = config.load()
        self.assertIsNone(config.last_error)
        self.assertEqual(settings["backlight"], config.DEFAULTS["backlight"])
        self.assertEqual(settings["lcd"], config.DEFAULTS["lcd"])

    def test_the_starter_file_contains_the_default_bindings(self):
        # BUG: ensure_exists() saves DEFAULTS, which has no "banks" key, so
        # save() drops "bindings" and persists nothing in its place. The file
        # written "so there is something to edit" has no G-key bindings in it
        # at all. Reported, not fixed.
        config.ensure_exists()
        written = self.written()
        bindings = written.get("bindings") or written.get("banks", {}).get("1")
        self.assertEqual(bindings, config.DEFAULTS["bindings"])


class BankPersistenceTests(ConfigFileTestCase):

    def test_a_binding_added_to_bank_two_survives_a_restart(self):
        settings = config.set_bank(config.load(), "2")
        settings["bindings"]["G3"] = {"type": "keys", "keys": "cmd+shift+4"}
        config.save(settings)
        reloaded = config.load()
        self.assertEqual(reloaded["banks"]["2"]["G3"],
                         {"type": "keys", "keys": "cmd+shift+4"})

    def test_a_changed_binding_in_bank_one_survives_a_restart(self):
        settings = config.load()
        settings["bindings"]["G1"] = {"type": "app", "name": "Mail"}
        config.save(settings)
        self.assertEqual(config.load()["bindings"]["G1"],
                         {"type": "app", "name": "Mail"})

    def test_unbinding_every_key_in_bank_one_survives_a_restart(self):
        # BUG: save() writes the empty bank correctly, but on the way back in
        # _merge() re-injects DEFAULTS["bindings"] (the file has no "bindings"
        # key) and _migrate() then treats them as a legacy flat set and copies
        # them into the empty bank 1. Clearing bank 1 cannot be made to stick:
        # the factory bindings come back on the next load. Reported, not fixed.
        settings = config.load()
        settings["bindings"].clear()
        config.save(settings)
        self.assertEqual(self.written()["banks"]["1"], {})
        self.assertEqual(config.load()["banks"]["1"], {})


if __name__ == "__main__":
    unittest.main()
