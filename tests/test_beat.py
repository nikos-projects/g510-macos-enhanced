"""The backlight dipping to the beat of the music (P5).

Two halves, tested apart because they fail apart. The detector is a pure
function of levels and times, so a beat can be shown to be found - and
shown not to be counted twice, or counted all through a loud chorus -
with no audio device anywhere near it. The agent's half is driven here
exactly the way the poll loop drives it: a fake tap deciding what
arrives, a mocked keyboard recording what was written, and the config
redirected at a temporary directory so the real one is never touched.
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import beat
import cli
import config
import daemon
import screens


class FakeTap:
    """The listener with nothing to hear: the test decides what arrives."""

    def __init__(self):
        self.levels = []
        self.stopped = False

    def poll(self, now):
        if self.levels:
            return True, self.levels.pop(0)
        return False, 0.0

    def stop(self):
        self.stopped = True


class DetectorTests(unittest.TestCase):
    """What a beat looks like, on levels with no audio behind them."""

    def setUp(self):
        self.detector = beat.Detector()

    def push(self, now, level):
        return self.detector.push(level, now)

    def test_the_first_level_is_never_a_beat(self):
        # It seeds both averages at itself, so there is nothing for it
        # to have jumped above.
        self.assertFalse(self.push(100.0, 0.5))

    def test_room_tone_is_not_a_beat(self):
        for step in range(12):
            self.assertFalse(self.push(100.0 + step * 0.02, 0.01))

    def test_a_level_arriving_faster_than_it_can_be_averaged_away_is_a_beat(self):
        self.push(100.0, 0.02)
        self.assertFalse(self.push(100.05, 0.02))
        self.assertTrue(self.push(100.10, 0.6))

    def test_one_hit_counts_once(self):
        # A second level high enough to keep fast clear of slow, while
        # the gap is still open, belongs to the first hit.
        self.push(100.0, 0.02)
        self.push(100.10, 0.6)
        self.assertFalse(self.push(100.15, 0.7))
        self.assertFalse(self.push(100.25, 0.7))

    def test_a_loud_passage_does_not_pulse_on_every_level(self):
        # Fast stays clear of slow for as long as this lasts, so firing
        # on "fast is ahead" rather than on the rise would strobe the
        # backlight through the whole chorus at the rate of the gap.
        self.push(100.0, 0.02)
        self.push(100.10, 0.6)
        fired = 0
        now = 100.15
        while now < 104.0:
            if self.push(now, 0.6):
                fired += 1
            now += 0.02
        self.assertEqual(fired, 0)

    def test_the_next_hit_after_the_level_falls_away_counts(self):
        self.push(100.0, 0.02)
        self.push(100.10, 0.6)
        for step in range(1, 21):
            self.push(100.10 + step * 0.05, 0.05)   # the kick decays
        self.assertTrue(self.push(101.30, 0.7))

    def test_a_level_that_is_not_a_number_is_not_a_beat(self):
        self.assertFalse(self.detector.push("loud", 100.0))


class PulseSettingsTests(unittest.TestCase):
    """The two numbers, clamped the same way everywhere agrees on."""

    def test_it_will_not_dip_below_zero_or_climb_forever(self):
        self.assertEqual(beat.pulse_settings({"amount": -5, "decay": 0}),
                         (0.0, 0.05))
        self.assertEqual(beat.pulse_settings({"amount": 400, "decay": 99}),
                         (100.0, 5.0))

    def test_a_config_that_is_not_numbers_falls_back_to_the_defaults(self):
        self.assertEqual(
            beat.pulse_settings({"amount": "lots", "decay": None}),
            (60.0, 0.35))

    def test_the_defaults_are_what_a_missing_block_says(self):
        self.assertEqual(beat.pulse_settings({}), (60.0, 0.35))


class SensitivityTests(unittest.TestCase):
    """The one knob, and what it does to what counts as a hit."""

    def detector(self, sensitivity):
        return beat.Detector(sensitivity)

    def arrive(self, detector, level):
        """A settled quiet spell, then one arrival: was it a beat?

        The averages have to be given time to agree with each other
        first, or the test is measuring how far two averages that both
        started ten milliseconds ago have drifted rather than what a
        hit looks like against a background.
        """
        detector.push(0.10, 0.0)
        detector.push(0.10, 0.1)
        return detector.push(level, 0.5)

    def test_the_middle_of_the_range_is_the_numbers_as_written(self):
        # 50 has to be exactly RATIO and FLOOR, or turning the knob to
        # the default would not mean "as it was before the knob".
        detector = self.detector(beat.DEFAULT_SENSITIVITY)
        self.assertEqual(detector.ratio, beat.RATIO)
        self.assertEqual(detector.floor, beat.FLOOR)

    def test_turning_it_up_makes_a_smaller_rise_count(self):
        # A gentle rise the default ignores is counted at the top,
        # where almost any rise above the slow average is enough.
        self.assertFalse(self.arrive(self.detector(50), 0.11))
        self.assertTrue(self.arrive(self.detector(100), 0.11))

    def test_a_loud_hit_counts_at_the_middle_of_the_range(self):
        # The hit that the default is tuned for still counts - and at
        # the top, where it counts too, nothing has become *more*
        # reluctant: the knob only ever widens what is caught.
        self.assertTrue(self.arrive(self.detector(50), 0.40))
        self.assertTrue(self.arrive(self.detector(100), 0.40))

    def test_turning_it_down_makes_a_loud_hit_stop_counting(self):
        # The same 0.40 hit, at the bottom of the range, is not enough:
        # the slow average has to be cleared by twice as much again.
        self.assertFalse(self.arrive(self.detector(0), 0.40))

    def test_it_still_wants_a_rise_at_the_top_of_the_range(self):
        # Sensitivity 100 is the most eager setting, not a licence to
        # call every level a beat - the ratio must stay at or above
        # 1.0, which is what "the fast average is ahead" means.
        detector = self.detector(100)
        self.assertGreaterEqual(detector.ratio, 1.0)
        self.assertGreaterEqual(detector.floor, 0.0)

    def test_it_is_clamped_to_what_the_menu_and_the_cli_allow(self):
        for value, want in ((-20, 0.0), (400, 100.0), (75, 75.0)):
            self.assertEqual(beat.sensitivity_of({"sensitivity": value}),
                             want)
        self.assertEqual(beat.sensitivity_of({}), float(beat.DEFAULT_SENSITIVITY))

    def test_a_config_that_is_not_a_number_is_the_default_not_the_dullest(self):
        # Guessing "off" for a typo would be a quieter surprise than
        # guessing "50", and it would be the wrong one.
        self.assertEqual(beat.sensitivity_of({"sensitivity": "loud"}),
                         float(beat.DEFAULT_SENSITIVITY))
        self.assertEqual(beat.Detector("loud").sensitivity,
                         float(beat.DEFAULT_SENSITIVITY))


class TheSwitch(unittest.TestCase):
    """Off until asked for, and the tap follows the switch."""

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
            patcher = mock.patch.object(daemon.Daemon, target,
                                        return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()

    def test_it_is_off_until_it_is_asked_for(self):
        # Off by default because it opens a system-wide audio tap: a
        # thing to opt into rather than to find already running.
        self.assertFalse(config.DEFAULTS["beat"]["enabled"])
        self.assertFalse(config.load()["beat"]["enabled"])
        self.agent.sync_beat()
        self.assertIsNone(self.agent.beat)

    def test_saying_so_opens_the_tap_once_and_closes_it_again(self):
        listener = mock.Mock()
        listener.start.return_value = True
        with mock.patch.object(beat, "Listener",
                               return_value=listener) as made:
            self.agent.config["beat"] = {"enabled": True, "amount": 60,
                                         "decay": 0.35}
            self.agent.sync_beat()
            self.assertIs(self.agent.beat, listener)
            self.agent.sync_beat()              # already open, not again
            self.assertEqual(made.call_count, 1)
            self.agent.config["beat"]["enabled"] = False
            self.agent.sync_beat()
        listener.stop.assert_called_once()
        self.assertIsNone(self.agent.beat)

    def test_the_knob_is_applied_on_a_reload_without_reopening_the_tap(self):
        # Turning sensitivity up is a spinner, not a reason to tear the
        # audio tap down and up again - that costs a permission round
        # trip and a moment of silence, for a number.
        listener = mock.Mock()
        listener.start.return_value = True
        with mock.patch.object(beat, "Listener", return_value=listener) as made:
            self.agent.config["beat"] = {"enabled": True, "amount": 60,
                                         "decay": 0.35, "sensitivity": 20}
            self.agent.sync_beat()
            self.assertEqual(self.agent.beat_detector.sensitivity, 20.0)
            self.agent.config["beat"]["sensitivity"] = 90
            self.agent.sync_beat()
        self.assertEqual(made.call_count, 1)
        self.assertEqual(self.agent.beat_detector.sensitivity, 90.0)

    def test_turning_the_knob_keeps_what_the_detector_already_heard(self):
        # The averages are what the last second of music actually
        # sounded like. Restarting them would make the next hit a miss.
        # The tap has to be open first - with it closed the agent never
        # feeds the detector, so there is nothing to keep.
        listener = mock.Mock()
        listener.start.return_value = True
        with mock.patch.object(beat, "Listener", return_value=listener):
            self.agent.config["beat"] = {"enabled": True, "sensitivity": 50}
            self.agent.sync_beat()
            self.agent.beat_detector.push(0.20, 1.0)
            self.agent.beat_detector.push(0.55, 1.1)
            heard = (self.agent.beat_detector.fast,
                     self.agent.beat_detector.slow)
            self.agent.config["beat"]["sensitivity"] = 95
            self.agent.sync_beat()
        self.assertEqual((self.agent.beat_detector.fast,
                          self.agent.beat_detector.slow), heard)
        self.assertEqual(self.agent.beat_detector.sensitivity, 95.0)

    def test_a_tap_that_will_not_open_is_a_log_line_not_a_crash(self):
        listener = mock.Mock()
        listener.start.return_value = False
        with mock.patch.object(beat, "Listener", return_value=listener):
            self.agent.config["beat"] = {"enabled": True, "amount": 60,
                                         "decay": 0.35}
            self.agent.sync_beat()
        # No listener, so the loop has nothing to poll and the backlight
        # is left alone rather than dipped by nothing.
        self.assertIsNone(self.agent.beat)
        self.agent.keyboard.set_backlight.assert_not_called()
        self.agent.refresh_beat(100.0)
        self.agent.keyboard.set_backlight.assert_not_called()


class TheDip(unittest.TestCase):
    """The agent's half: a beat in, a dip out, and the colour back."""

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
            patcher = mock.patch.object(daemon.Daemon, target,
                                        return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()
        self.base = self.agent.base_color

    def listen(self, amount=50, decay=1.0):
        """A tap that hears exactly what the test tells it to."""
        self.agent.config["beat"] = {"enabled": True, "amount": amount,
                                     "decay": decay}
        self.agent.beat = FakeTap()
        return self.agent.beat

    def written(self, index=-1):
        return self.agent.keyboard.set_backlight.call_args_list[index][0][0]

    def test_a_beat_dips_and_the_dip_climbs_back(self):
        tap = self.listen(amount=50, decay=1.0)
        tap.levels = [0.02, 0.6]           # seed the averages, then hit
        self.agent.refresh_beat(100.0)
        self.assertEqual(self.agent.keyboard.set_backlight.call_count, 0)
        self.agent.refresh_beat(100.1)
        self.assertEqual(self.written(),
                         config.apply_brightness(self.base, 50))
        self.agent.refresh_beat(100.6)      # halfway up, no new level
        self.assertEqual(self.written(),
                         config.apply_brightness(self.base, 75))

    def test_the_climb_is_paced_rather_than_written_on_every_tick(self):
        tap = self.listen(amount=50, decay=1.0)
        tap.levels = [0.02, 0.6]
        self.agent.refresh_beat(100.0)
        self.agent.refresh_beat(100.1)
        self.agent.refresh_beat(100.11)     # inside one write interval
        self.assertEqual(self.agent.keyboard.set_backlight.call_count, 1)

    def test_when_the_dip_is_over_the_colour_comes_back(self):
        tap = self.listen(amount=50, decay=1.0)
        tap.levels = [0.02, 0.6]
        self.agent.refresh_beat(100.0)
        self.agent.refresh_beat(100.1)
        self.agent.refresh_beat(101.2)      # past pulse_end
        self.assertEqual(self.written(), self.base)
        self.assertEqual(self.agent.pulse_end, 0.0)
        self.agent.refresh_beat(102.0)      # and nothing keeps writing
        self.assertEqual(self.agent.keyboard.set_backlight.call_count, 2)

    def test_switching_it_off_mid_dip_puts_the_colour_back(self):
        tap = self.listen(amount=40, decay=5.0)
        tap.levels = [0.02, 0.6]
        self.agent.refresh_beat(100.0)
        self.agent.refresh_beat(100.1)
        self.agent.keyboard.reset_mock()
        self.agent.config["beat"]["enabled"] = False
        self.agent.refresh_beat(100.3)
        self.assertTrue(tap.stopped)
        self.assertEqual(self.agent.pulse_end, 0.0)
        self.agent.keyboard.set_backlight.assert_called_once_with(self.base)

    def test_an_app_coming_forward_does_not_break_the_dip(self):
        tap = self.listen(amount=50, decay=1.0)
        tap.levels = [0.02, 0.6]
        self.agent.refresh_beat(100.0)
        self.agent.refresh_beat(100.1)
        self.agent.keyboard.reset_mock()
        with mock.patch.object(screens, "frontmost_app",
                               return_value="Safari"):
            self.agent.last_app_check = 0.0
            self.agent.refresh_app_color(100.5)     # inside the dip
        self.assertEqual(self.agent.current_app, "Safari")
        self.agent.keyboard.set_backlight.assert_not_called()
        # The pulse puts the colour back itself when it runs out, and
        # that is the first moment the new app's colour can be written
        # without a full-bright frame landing in the middle of a dip.
        with mock.patch.object(screens, "frontmost_app",
                               return_value="Music"):
            self.agent.refresh_app_color(102.0)     # after it
        self.agent.keyboard.set_backlight.assert_called_once_with(self.base)

    def test_an_app_coming_forward_writes_when_nothing_is_pulsing(self):
        self.agent.beat = None
        with mock.patch.object(screens, "frontmost_app",
                               return_value="Safari"):
            self.agent.last_app_check = 0.0
            self.agent.refresh_app_color(100.0)
        self.agent.keyboard.set_backlight.assert_called_once_with(self.base)


class CommandLineTests(unittest.TestCase):
    """What `g510 beat ...` writes, and what it says back."""

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

    def beat(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_beat(list(args))
        return out.getvalue().strip()

    def test_no_arguments_just_says_where_it_stands(self):
        self.assertEqual(self.beat(),
                         "beat off, dip to 60%, back in 0.35s, sens 50")

    def test_saying_on_writes_it_and_says_so(self):
        self.assertEqual(self.beat("on", "amount", "40"),
                         "beat on, dip to 40%, back in 0.35s, sens 50")
        saved = config.load()["beat"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["amount"], 40)

    def test_the_numbers_can_come_in_any_order(self):
        self.beat("decay", "1.5", "off", "amount", "80", "sens", "70")
        saved = config.load()["beat"]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["amount"], 80)
        self.assertEqual(saved["decay"], 1.5)
        self.assertEqual(saved["sensitivity"], 70)

    def test_a_dip_outside_the_range_is_refused(self):
        with self.assertRaises(SystemExit):
            self.beat("amount", "200")
        # Refused before anything was written, so the setting is still
        # the default it started at.
        self.assertEqual(config.load()["beat"]["amount"], 60)

    def test_a_climb_outside_the_range_is_refused(self):
        with self.assertRaises(SystemExit):
            self.beat("decay", "30")

    def test_a_word_it_does_not_know_says_what_to_type(self):
        with self.assertRaises(SystemExit) as caught:
            self.beat("maybe", "3")
        self.assertIn("g510 beat", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
