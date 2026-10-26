"""The formatting helpers behind the LCD screens.

Only the pure ones: the screens themselves read the system, but the strings
they draw come from here.
"""
import datetime
import unittest

import screens
from screens import clock


def stamp(**offset):
    """An ISO timestamp that far from now, as the Claude usage data carries."""
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(**offset)
    return when.isoformat().replace("+00:00", "Z")


class StubCanvas:
    """A canvas with predictable metrics: every character is eight wide.

    Real metrics come from CoreText and vary by font and size; _fit only needs
    something that can measure, so this keeps the arithmetic checkable.
    """

    def __init__(self, per_char=8):
        self.per_char = per_char

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char


class ClockTests(unittest.TestCase):
    """Elapsed track time, m:ss."""

    def test_the_documented_example(self):
        self.assertEqual(clock(612), "10:12")

    def test_seconds_are_zero_padded(self):
        self.assertEqual(clock(61), "1:01")
        self.assertEqual(clock(9), "0:09")

    def test_zero(self):
        self.assertEqual(clock(0), "0:00")

    def test_exactly_a_minute(self):
        self.assertEqual(clock(60), "1:00")

    def test_minutes_run_past_sixty_rather_than_becoming_hours(self):
        self.assertEqual(clock(3599), "59:59")
        self.assertEqual(clock(3600), "60:00")
        self.assertEqual(clock(7325), "122:05")

    def test_fractions_are_dropped(self):
        self.assertEqual(clock(90.9), "1:30")

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(clock("612"), "10:12")

    def test_a_negative_position_shows_as_zero(self):
        self.assertEqual(clock(-5), "0:00")


class DurationTests(unittest.TestCase):
    """Time remaining on a print job."""

    def test_the_documented_examples(self):
        self.assertEqual(screens._duration(1720), "28m")
        self.assertEqual(screens._duration(7200), "2h00")

    def test_minutes_only_below_an_hour(self):
        self.assertEqual(screens._duration(60), "1m")
        self.assertEqual(screens._duration(3599), "59m")

    def test_part_minutes_are_dropped(self):
        self.assertEqual(screens._duration(59), "0m")
        self.assertEqual(screens._duration(0), "0m")

    def test_hours_pad_their_minutes(self):
        self.assertEqual(screens._duration(3600), "1h00")
        self.assertEqual(screens._duration(3660), "1h01")
        self.assertEqual(screens._duration(7380), "2h03")

    def test_long_jobs(self):
        self.assertEqual(screens._duration(36 * 3600 + 30 * 60), "36h30")

    def test_fractions_are_dropped(self):
        self.assertEqual(screens._duration(119.9), "1m")

    def test_a_negative_estimate_shows_as_zero(self):
        self.assertEqual(screens._duration(-60), "0m")

    def test_it_never_exceeds_the_room_on_the_panel(self):
        for seconds in (0, 59, 3599, 3600, 359999):
            with self.subTest(seconds=seconds):
                self.assertLessEqual(len(screens._duration(seconds)), 6)


class UntilTests(unittest.TestCase):
    """How long until a usage window resets."""

    def test_nothing_to_show(self):
        self.assertIsNone(screens.until(None))
        self.assertIsNone(screens.until(""))

    def test_unparseable_timestamps_are_not_shown(self):
        for text in ("not a date", "2026-13-45T99:00:00Z", "soon", "12345"):
            with self.subTest(text=text):
                self.assertIsNone(screens.until(text))

    def test_a_past_reset_reads_as_now(self):
        self.assertEqual(screens.until("2001-01-01T00:00:00Z"), "now")
        self.assertEqual(screens.until(stamp(minutes=-5)), "now")

    def test_minutes(self):
        self.assertEqual(screens.until(stamp(minutes=5, seconds=30)), "5m")

    def test_under_a_minute(self):
        self.assertEqual(screens.until(stamp(seconds=30)), "0m")

    def test_hours_and_minutes(self):
        self.assertEqual(screens.until(stamp(hours=1, minutes=30, seconds=30)),
                         "1h30m")

    def test_the_minutes_part_is_padded(self):
        self.assertEqual(screens.until(stamp(hours=4, minutes=5, seconds=30)),
                         "4h05m")

    def test_days(self):
        self.assertEqual(screens.until(stamp(hours=26, seconds=30)), "1d2h")
        self.assertEqual(screens.until(stamp(days=6, hours=23, seconds=30)),
                         "6d23h")

    def test_a_zulu_timestamp_is_understood(self):
        self.assertIsNotNone(screens.until("2099-01-01T00:00:00Z"))

    def test_an_offset_timestamp_is_understood(self):
        when = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=2))
        ) + datetime.timedelta(minutes=90, seconds=30)
        self.assertEqual(screens.until(when.isoformat()), "1h30m")

    def test_it_never_exceeds_the_room_on_the_panel(self):
        for text in (stamp(seconds=30), stamp(hours=4, minutes=5),
                     stamp(days=400)):
            with self.subTest(text=text):
                self.assertLessEqual(len(screens.until(text)), 7)

    def test_a_timestamp_without_a_timezone_is_not_shown(self):
        # BUG: until() promises a string "or None", but an ISO timestamp with
        # no timezone parses fine and then raises TypeError on the subtraction
        # ("can't subtract offset-naive and offset-aware datetimes"), taking
        # the Claude screen's render with it. Reported, not fixed.
        self.assertIsNone(screens.until("2099-01-01T00:00:00"))


class FitTests(unittest.TestCase):
    """Trimming a string to the 160px panel, with an ellipsis if it had to go."""

    LIMIT = 156          # LCD_WIDTH - 4

    def setUp(self):
        self.canvas = StubCanvas()

    def fit(self, string, size=12):
        return screens._fit(self.canvas, string, size)

    def test_a_string_that_fits_is_left_alone(self):
        self.assertEqual(self.fit("NOW PLAYING"), "NOW PLAYING")

    def test_an_empty_string_is_left_alone(self):
        self.assertEqual(self.fit(""), "")

    def test_a_string_that_exactly_fills_the_panel_is_left_alone(self):
        exact = "x" * (self.LIMIT // self.canvas.per_char)
        self.assertEqual(self.fit(exact), exact)

    def test_a_long_string_is_trimmed(self):
        long = "a really quite long track title that will not fit"
        result = self.fit(long)
        self.assertNotEqual(result, long)
        self.assertTrue(result.endswith("..."))
        self.assertTrue(long.startswith(result[:-3]))

    def test_the_trimmed_string_fits(self):
        result = self.fit("a really quite long track title that will not fit")
        self.assertLessEqual(self.canvas.measure(result), self.LIMIT)

    def test_it_keeps_as_much_as_it_can(self):
        # 19 characters fit; the 20th costs three more for the ellipsis.
        result = self.fit("x" * 40)
        self.assertEqual(result, "x" * 16 + "...")

    def test_a_larger_size_trims_sooner(self):
        big = screens._fit(StubCanvas(per_char=16), "x" * 40, 24)
        small = self.fit("x" * 40)
        self.assertLess(len(big), len(small))

    def test_a_string_that_cannot_fit_at_all_still_terminates(self):
        # Nothing fits, not even the ellipsis: it must give up, not loop.
        self.assertEqual(screens._fit(StubCanvas(per_char=100), "hello", 12),
                         "...")

    def test_it_works_with_the_real_canvas(self):
        from lcd import Canvas
        canvas = Canvas()
        limit = self.LIMIT
        self.assertEqual(screens._fit(canvas, "short", 12), "short")
        result = screens._fit(canvas, "an extremely long line of text that "
                                      "will certainly not fit on the panel", 12)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(canvas.measure(result, 12), limit)


class ReconcileTests(unittest.TestCase):
    """Merging a now-playing poll into the running count of elapsed time.

    The count ticks along on the wall clock between polls, but the player's
    own number arrives in bursts and can repeat itself, so a poll is only
    believed when it carries news.
    """

    def setUp(self):
        screens._media_anchor.update(key=None, elapsed=0.0, at=0.0,
                                     playing=None)

    @staticmethod
    def reading(**fields):
        base = {"title": "Ballin'", "artist": "Mustard", "app": "spotify",
                "elapsed": 0.0, "duration": 200.0, "playing": True}
        base.update(fields)
        return base

    def test_the_first_poll_sets_the_count(self):
        moved = screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        self.assertEqual(moved["elapsed"], 30)

    def test_the_count_moves_on_between_polls(self):
        screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        for second in (101.0, 102.0, 103.0):
            moved = screens._reconcile(self.reading(elapsed=30), 100.0, second)
            self.assertEqual(moved["elapsed"], second - 70)

    def test_a_poll_that_repeats_itself_does_not_wind_the_count_back(self):
        # Spotify holds one position for a stretch and then jumps to catch
        # up. Resetting the count to the repeated number is what made the
        # bar count three seconds ahead and snap back again, forever.
        screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        screens._reconcile(self.reading(elapsed=30), 100.0, 102.0)
        moved = screens._reconcile(self.reading(elapsed=30), 103.0, 103.0)
        self.assertEqual(moved["elapsed"], 33)

    def test_a_poll_that_lags_a_long_way_behind_is_still_ignored(self):
        # A player that fell silent for a while reports a position far back;
        # it is stale, not a backwards seek.
        reading = self.reading(elapsed=30, duration=600)
        screens._reconcile(reading, 100.0, 100.0)
        moved = screens._reconcile(dict(reading), 300.0, 300.0)
        self.assertEqual(moved["elapsed"], 230)

    def test_a_seek_forward_is_taken_up_at_once(self):
        screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        moved = screens._reconcile(self.reading(elapsed=90), 104.0, 104.0)
        self.assertEqual(moved["elapsed"], 90)
        moved = screens._reconcile(self.reading(elapsed=90), 104.0, 105.0)
        self.assertEqual(moved["elapsed"], 91)

    def test_a_seek_backwards_survives_a_pause(self):
        # While playing there is telling a lagging player from a real seek
        # backwards, so the count wins - until pausing settles it.
        screens._reconcile(self.reading(elapsed=90), 100.0, 100.0)
        moved = screens._reconcile(self.reading(elapsed=20), 103.0, 103.0)
        self.assertEqual(moved["elapsed"], 93)
        moved = screens._reconcile(self.reading(elapsed=20, playing=False),
                                   104.0, 104.0)
        self.assertEqual(moved["elapsed"], 20)

    def test_pausing_shows_the_players_own_number(self):
        screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        moved = screens._reconcile(self.reading(elapsed=42, playing=False),
                                   106.0, 107.0)
        self.assertEqual(moved["elapsed"], 42)

    def test_resuming_starts_from_where_the_player_says_it_is(self):
        screens._reconcile(self.reading(elapsed=42, playing=False), 100.0,
                           100.0)
        moved = screens._reconcile(self.reading(elapsed=42), 108.0, 108.0)
        self.assertEqual(moved["elapsed"], 42)
        moved = screens._reconcile(self.reading(elapsed=42), 108.0, 109.0)
        self.assertEqual(moved["elapsed"], 43)

    def test_a_new_track_starts_the_count_again(self):
        screens._reconcile(self.reading(elapsed=190), 100.0, 100.0)
        moved = screens._reconcile(self.reading(title="The Box", elapsed=4),
                                   102.0, 102.0)
        self.assertEqual(moved["elapsed"], 4)

    def test_it_never_runs_past_the_end_of_the_track(self):
        screens._reconcile(self.reading(elapsed=199), 100.0, 100.0)
        moved = screens._reconcile(self.reading(elapsed=199), 101.0, 160.0)
        self.assertEqual(moved["elapsed"], 200)

    def test_a_track_of_unknown_length_still_moves_on(self):
        reading = self.reading(elapsed=30, duration=0)
        screens._reconcile(reading, 100.0, 100.0)
        moved = screens._reconcile(dict(reading), 100.0, 103.0)
        self.assertEqual(moved["elapsed"], 33)

    def test_nothing_playing(self):
        self.assertIsNone(screens._reconcile(None, 100.0, 100.0))

    def test_nothing_playing_forgets_the_track(self):
        screens._reconcile(self.reading(elapsed=30), 100.0, 100.0)
        screens._reconcile(None, 101.0, 101.0)
        moved = screens._reconcile(self.reading(elapsed=31), 102.0, 102.0)
        self.assertEqual(moved["elapsed"], 31)

    def test_the_cached_reading_is_left_alone(self):
        reading = self.reading(elapsed=30)
        screens._reconcile(reading, 100.0, 100.0)
        screens._reconcile(reading, 100.0, 105.0)
        self.assertEqual(reading["elapsed"], 30)


class PositionTests(unittest.TestCase):
    """The one-line answer Music and Spotify give about their own position."""

    def test_a_playing_player(self):
        self.assertEqual(screens._parse_position("42.5|180.435|true"),
                         (42.5, 180.435, True))

    def test_a_paused_player(self):
        self.assertEqual(screens._parse_position("42|180|false"),
                         (42.0, 180.0, False))

    def test_a_player_with_no_track_to_ask_about(self):
        # player position and the track's duration both fail then, so
        # there is nothing but the error text to look at.
        self.assertIsNone(screens._parse_position(""))
        self.assertIsNone(screens._parse_position("\n"))
        self.assertIsNone(screens._parse_position(
            "execution error: Not ready. (-600)"))

    def test_a_half_answer_is_no_answer(self):
        self.assertIsNone(screens._parse_position("42.5|180.435"))
        self.assertIsNone(screens._parse_position("42.5|180.435|true|junk"))
        self.assertIsNone(screens._parse_position("42.5||true"))
        self.assertIsNone(screens._parse_position("42.5|180.435|maybe"))

    def test_a_german_session_writes_commas(self):
        # AppleScript uses the session's decimal separator, so this is
        # what arrives when the Mac behind the daemon is set to German.
        self.assertEqual(screens._parse_position("42,5|180,435|true"),
                         (42.5, 180.435, True))


if __name__ == "__main__":
    unittest.main()
