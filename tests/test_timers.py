"""Countdowns, the sound at the end of one, and the panel they borrow.

The whole chain: a length of time as it is written in the config, the timers
that are running, the interruption that takes the display for a few seconds
without disturbing what was underneath it, the screens that draw each of
those, and the agent wiring them together. The agent is redirected at a
temporary config, so the real one is never touched.
"""
import os
import tempfile
import unittest
from unittest import mock

import actions
import alerts
import autoscreen
import config
import daemon
import device
import screens
import timers


class StubCanvas:
    """Measures eight pixels a character and writes down what it was given."""

    def __init__(self, per_char=8):
        self.per_char = per_char
        self.drawn = []

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char

    def text(self, string, x, y, size=12.0, font=None):
        self.drawn.append(str(string))

    def centered(self, string, y, size=12.0, font=None):
        self.drawn.append(str(string))


def shown(canvas):
    return " | ".join(canvas.drawn)


class DurationTests(unittest.TestCase):
    """A length of time the way it is written in the config."""

    def test_the_written_forms(self):
        for text, seconds in (("90s", 90), ("5m", 300), ("1h", 3600),
                              ("1h30m", 5400), ("1h05m30s", 3930),
                              ("25M", 1500), ("  10m  ", 600)):
            with self.subTest(text=text):
                self.assertEqual(timers.parse_duration(text), seconds)

    def test_a_bare_number_is_seconds(self):
        # "10" is ten seconds, because that is what a bare number means;
        # ten minutes is "10m". The other way round would make 5 mean five
        # hours somewhere else in the config.
        self.assertEqual(timers.parse_duration("10"), 10)
        self.assertEqual(timers.parse_duration(10), 10)
        self.assertEqual(timers.parse_duration(90.5), 90.5)

    def test_a_fraction_of_a_minute(self):
        self.assertEqual(timers.parse_duration("1.5m"), 90)

    def test_nonsense_is_refused(self):
        for text in ("", "   ", "soon", "5x", "m5", "10 minutes", "-5m",
                     "0", "0s", 0, None, True):
            with self.subTest(text=text):
                with self.assertRaises(timers.DurationError):
                    timers.parse_duration(text)

    def test_the_error_says_what_would_work(self):
        with self.assertRaises(timers.DurationError) as caught:
            timers.parse_duration("a while")
        self.assertIn("90s, 5m, 1h30m", str(caught.exception))


class CountdownTextTests(unittest.TestCase):
    """What a running countdown shows on the panel."""

    def test_seconds_only_below_an_hour(self):
        self.assertEqual(timers.left(1720), "28:40")
        self.assertEqual(timers.left(0), "0:00")
        self.assertEqual(timers.left(9), "0:09")

    def test_hours_get_a_column_of_their_own(self):
        self.assertEqual(timers.left(3600), "1:00:00")
        self.assertEqual(timers.left(5400), "1:30:00")

    def test_a_negative_remainder_shows_as_zero(self):
        self.assertEqual(timers.left(-30), "0:00")

    def test_it_always_fits_the_panel(self):
        for seconds in (0, 59, 60, 3599, 3600, 360000):
            with self.subTest(seconds=seconds):
                self.assertLessEqual(len(timers.left(seconds)), 9)


class PresetTests(unittest.TestCase):
    """Naming a duration instead of spelling it out."""

    PRESETS = {"pomodoro": "25m", "hour": "1h"}

    def test_a_preset_name_resolves(self):
        self.assertEqual(timers.resolve("pomodoro", self.PRESETS), 1500)

    def test_a_length_of_time_resolves_too(self):
        self.assertEqual(timers.resolve("10m", self.PRESETS), 600)

    def test_a_number_resolves(self):
        self.assertEqual(timers.resolve(30, self.PRESETS), 30)

    def test_the_field_a_binding_uses_does_not_matter(self):
        self.assertEqual(timers.duration_for({"seconds": "5m"}), 300)
        self.assertEqual(timers.duration_for({"preset": "hour"}, self.PRESETS),
                         3600)
        # `g510 bind G5 timer pomodoro` writes the name into `seconds`, and
        # it has to mean the same as writing it into `preset`.
        self.assertEqual(timers.duration_for({"seconds": "pomodoro"},
                                             self.PRESETS), 1500)

    def test_a_preset_that_does_not_exist_says_what_does(self):
        with self.assertRaises(timers.DurationError) as caught:
            timers.duration_for({"preset": "tea"}, self.PRESETS)
        message = str(caught.exception)
        self.assertIn("tea", message)
        self.assertIn("pomodoro", message)

    def test_a_binding_with_neither_field_is_refused(self):
        with self.assertRaises(timers.DurationError):
            timers.duration_for({"type": "timer"})


class StoreTests(unittest.TestCase):
    """The countdowns that are running, and the ones that have run out."""

    def setUp(self):
        self.store = timers.Store(clock=lambda: 5000.0)

    def test_a_timer_runs_for_as_long_as_it_says(self):
        self.store.start(90, label="tea", now=1000.0)
        running = self.store.running(now=1030.0)
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["label"], "tea")
        self.assertEqual(running[0]["left"], 60.0)

    def test_the_one_about_to_end_is_shown_first(self):
        self.store.start(10, name="long", now=0.0)
        self.store.start(5, name="short", now=0.0)
        names = [item["name"] for item in self.store.running(now=1.0)]
        self.assertEqual(names, ["short", "long"])

    def test_a_timer_that_has_run_out_comes_off_the_list(self):
        self.store.start(10, now=0.0)
        self.assertEqual(self.store.running(now=11.0), [])
        self.assertEqual(len(self.store.due(now=5.0)), 0)
        self.assertEqual(len(self.store.due(now=11.0)), 1)

    def test_a_timer_only_goes_off_once(self):
        self.store.start(10, now=0.0)
        self.assertEqual(len(self.store.due(now=11.0)), 1)
        self.assertEqual(self.store.due(now=12.0), [])

    def test_each_timer_knows_how_long_it_was_for(self):
        self.store.start(10, label="tea", now=0.0)
        ended = self.store.due(now=20.0)
        self.assertEqual(ended[0]["seconds"], 10)
        self.assertEqual(ended[0]["label"], "tea")
        self.assertEqual(ended[0]["until"], 10.0)

    def test_a_zero_length_timer_is_refused(self):
        for seconds in (0, -1):
            with self.subTest(seconds=seconds):
                with self.assertRaises(timers.DurationError):
                    self.store.start(seconds, now=0.0)

    def test_cancelling_by_position(self):
        self.store.start(10, name="long", now=0.0)
        self.store.start(5, name="short", now=0.0)
        gone = self.store.cancel("1")           # the one ending first
        self.assertEqual(gone[0]["name"], "short")
        self.assertEqual([item["name"] for item in self.store.running(now=0.0)],
                         ["long"])

    def test_cancelling_by_id_and_by_label(self):
        self.store.start(10, label="tea", now=0.0)
        self.assertEqual(len(self.store.cancel("tea")), 1)
        self.assertEqual(self.store.running(now=0.0), [])
        first = self.store.start(10, now=0.0)
        self.assertEqual(self.store.cancel(str(first["id"])), [first])

    def test_cancelling_all_of_them(self):
        for _ in range(3):
            self.store.start(10, now=0.0)
        self.assertEqual(len(self.store.cancel("all")), 3)
        self.assertEqual(self.store.running(now=0.0), [])

    def test_cancelling_something_that_is_not_there_says_so(self):
        self.assertEqual(self.store.cancel("7"), [])
        self.assertEqual(self.store.cancel(""), [])

    def test_the_module_level_store_starts_empty(self):
        timers.STORE.clear()
        self.assertEqual(timers.STORE.running(), [])


class SoundTests(unittest.TestCase):
    """The ring at the end of a countdown."""

    def test_a_system_sound_is_found_by_name(self):
        self.assertEqual(alerts.sound_path("Glass"),
                         "/System/Library/Sounds/Glass.aiff")

    def test_an_absolute_path_is_taken_as_it_is(self):
        self.assertEqual(alerts.sound_path("/System/Library/Sounds/Glass.aiff"),
                         "/System/Library/Sounds/Glass.aiff")

    def test_an_absolute_path_to_nothing_is_still_nothing(self):
        self.assertIsNone(alerts.sound_path("/tmp/own.aiff"))

    def test_nothing_rings_for_a_sound_that_does_not_exist(self):
        self.assertIsNone(alerts.sound_path(""))
        self.assertIsNone(alerts.sound_path(None))
        self.assertIsNone(alerts.sound_path("NotASound"))

    def test_a_missing_sound_is_quiet_rather_than_an_error(self):
        # afplay is spawned, not run: a sound that never finishes must not
        # hold up the agent's next key press.
        self.assertFalse(alerts.play_sound("NotASound"))


class AlertTests(unittest.TestCase):
    """An interruption taking the panel for a while, then handing it back."""

    def setUp(self):
        self.now = 1000.0
        self.rang = []
        self.panel = alerts.Alerts(play=self.rang.append,
                                   clock=lambda: self.now)

    def test_something_pushed_is_what_the_panel_shows(self):
        self.panel.push("timeralert", {"label": "tea"}, seconds=10)
        current = self.panel.current()
        self.assertEqual(current["screen"], "timeralert")
        self.assertEqual(current["data"], {"label": "tea"})

    def test_it_hands_the_panel_back_when_its_time_is_up(self):
        self.panel.push("timeralert", {}, seconds=10)
        self.assertIsNotNone(self.panel.current(1005.0))
        self.assertIsNone(self.panel.current(1010.0))
        self.assertIsNone(self.panel.current())

    def test_the_newest_interruption_wins(self):
        self.panel.push("timeralert", {"label": "tea"}, seconds=5)
        self.panel.push("message", {"text": "hello"}, seconds=10, now=1002.0)
        # The alarm is still on, but the message came after it: whoever
        # shouted last is the one on the panel.
        self.assertEqual(self.panel.current(1003.0)["screen"], "message")
        # ...and when that goes, the alarm underneath has run out too, so
        # the panel goes back to what it was showing before any of this.
        self.assertIsNone(self.panel.current(1013.0))

    def test_the_sound_rings_even_when_there_is_no_room_to_show_it(self):
        # The display may be off or busy recording; hearing about a countdown
        # late beats not hearing about it at all.
        self.panel.push("timeralert", {"label": "tea"}, seconds=0,
                        sound="Glass")
        self.assertIsNone(self.panel.current())
        self.assertEqual(self.rang, ["Glass"])

    def test_an_expired_interruption_does_not_come_back(self):
        self.panel.push("timeralert", {}, seconds=5)
        self.assertIsNone(self.panel.current(1006.0))
        self.panel.clear()
        self.assertIsNone(self.panel.current())


class ScreenTests(unittest.TestCase):
    """The three panels an interruption draws."""

    def test_the_alarm_says_what_it_was_for(self):
        canvas = StubCanvas()
        screens.screen_timeralert(canvas, {"alert": {
            "phase": "done", "label": "tea", "name": "quick", "seconds": 300}})
        drawn = shown(canvas)
        self.assertIn("TIME'S UP", drawn)
        self.assertIn("tea", drawn)
        self.assertIn("5:00", drawn)

    def test_starting_one_says_when_it_will_end(self):
        canvas = StubCanvas()
        screens.screen_timeralert(canvas, {"alert": {
            "phase": "started", "label": "pomodoro", "seconds": 1500,
            "until": 0.0}})
        drawn = shown(canvas)
        self.assertIn("TIMER STARTED", drawn)
        self.assertIn("pomodoro", drawn)
        self.assertIn("25:00", drawn)

    def test_a_message_is_attributed_and_wrapped(self):
        canvas = StubCanvas()
        screens.screen_message(canvas, {"alert": {
            "app": "Vesktop", "channel": "general", "author": "Niko",
            "text": "one two three four five six seven eight nine ten "
                    "eleven twelve thirteen fourteen fifteen sixteen",
            "time": "20:19"}})
        drawn = shown(canvas)
        self.assertIn("VESKTOP", drawn)
        self.assertIn("Niko", drawn)
        self.assertIn("general", drawn)
        self.assertIn("...", drawn)

    def test_a_count_of_what_is_waiting_has_no_author_to_show(self):
        canvas = StubCanvas()
        screens.screen_message(canvas, {"alert": {
            "app": "Discord", "text": "3 unread", "title": "(3) Discord",
            "author": ""}})
        drawn = shown(canvas)
        self.assertIn("3 unread", drawn)
        self.assertIn("(3) Discord", drawn)

    def test_an_empty_message_does_not_raise(self):
        for data in ({}, {"alert": {}}, {"alert": {"author": "Niko"}}):
            with self.subTest(data=data):
                screens.screen_message(StubCanvas(), data)

    def test_the_running_countdowns_are_listed(self):
        import time
        timers.STORE.clear()
        self.addCleanup(timers.STORE.clear)
        timers.STORE.start(90, label="tea", now=time.time())
        canvas = StubCanvas()
        screens.screen_timers(canvas, {})
        drawn = shown(canvas)
        self.assertIn("TIMERS", drawn)
        self.assertIn("tea", drawn)
        self.assertIn("1:30", drawn)

    def test_with_nothing_running_it_says_how_to_start_one(self):
        timers.STORE.clear()
        canvas = StubCanvas()
        screens.screen_timers(canvas, {})
        self.assertIn("nothing running", shown(canvas))

    def test_the_alarm_panels_are_not_in_the_cycle(self):
        # A rule or a key must not be able to ask for the alarm panel: it is
        # only ever up because something interrupted the screen below it.
        for name in ("timeralert", "message"):
            with self.subTest(name=name):
                self.assertIn(name, screens.SCREENS_INTERNAL)
                self.assertNotIn(name, screens.SCREENS)
                self.assertFalse(autoscreen.is_screen(name))
        self.assertIn("timers", screens.SCREENS)
        self.assertTrue(autoscreen.is_screen("timers"))

    def test_every_interruption_screen_renders(self):
        for name, state in (("timeralert", {"alert": {"phase": "done"}}),
                            ("message", {"alert": {}})):
            with self.subTest(name=name):
                rows = screens.render(name, state)
                self.assertEqual(len(rows), device.LCD_HEIGHT)
                self.assertTrue(all(len(row) == device.LCD_WIDTH
                                    for row in rows))


class HookTests(unittest.TestCase):
    """A binding kind only the agent can carry out."""

    def test_the_agent_hands_itself_to_the_dispatch(self):
        quiet = mock.patch.object(daemon, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        agent = daemon.Daemon()
        agent.timers.clear()
        self.addCleanup(agent.timers.clear)
        self.assertIn("timer", agent.hooks)

        started = []
        agent.hooks = {"timer": lambda binding: started.append(binding)}
        actions.dispatch({"type": "timer", "seconds": "5m"}, hooks=agent.hooks)
        self.assertEqual(started[0]["seconds"], "5m")

    def test_without_an_agent_a_timer_binding_says_it_cannot(self):
        with self.assertRaises(actions.ActionError):
            actions.dispatch({"type": "timer", "seconds": "5m"})

    def test_a_timer_binding_describes_itself(self):
        self.assertEqual(actions.describe({"type": "timer", "seconds": "10m"}),
                         "timer 10m")
        self.assertEqual(actions.describe({"type": "timer", "preset": "tea"}),
                         "timer tea")
        self.assertEqual(actions.describe({"type": "timer"}), "timer ?")


class AgentTimerTests(unittest.TestCase):
    """What the agent does when a key starts a countdown."""

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
        self.now = 1000.0
        self.rang = []
        self.agent = daemon.Daemon()
        self.agent.alerts = alerts.Alerts(play=self.rang.append,
                                          clock=lambda: self.now)
        self.agent.keyboard = FakeKeyboard()
        self.agent.timers.clear()
        self.addCleanup(self.agent.timers.clear)

    def draw(self, moment=None):
        """Run one redraw, and say which screen it drew."""
        moment = self.now if moment is None else moment
        drawn = []

        def render(name, _state):
            drawn.append(name)
            return b"\x00" * 4096

        with mock.patch.object(screens, "render", render), \
                mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = moment - 100.0
            self.agent.refresh_lcd(moment)
        return drawn[-1] if drawn else None

    def test_a_key_press_starts_a_countdown_and_says_so_at_once(self):
        self.agent.fire_timer({"type": "timer", "seconds": "10s",
                               "label": "tea"})
        running = self.agent.timers.running(now=self.now)
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["label"], "tea")
        current = self.agent.alerts.current()
        self.assertEqual(current["screen"], "timeralert")
        self.assertEqual(current["data"]["phase"], "started")
        # Starting is a nod, not the ring: the ring is for the end of it.
        self.assertEqual(self.rang, [])

    def test_a_preset_name_is_all_a_key_has_to_know(self):
        self.agent.fire_timer({"type": "timer", "preset": "pomodoro"})
        running = self.agent.timers.running(now=self.now)
        self.assertEqual(running[0]["seconds"], 1500)

    def test_a_binding_a_typo_broke_is_logged_not_raised(self):
        self.agent.fire_timer({"type": "timer", "seconds": "a while"})
        self.assertEqual(self.agent.timers.running(now=self.now), [])

    def test_timers_can_be_turned_off_whole(self):
        self.agent.mutate(lambda s: s.setdefault("timers", {}).update(
            enabled=False))
        self.agent.fire_timer({"type": "timer", "seconds": "10s"})
        self.assertEqual(self.agent.timers.running(now=self.now), [])

    def test_when_one_runs_out_it_rings_and_takes_the_display(self):
        self.agent.timers.start(5, label="tea", now=990.0)     # out at 995
        self.agent.refresh_alerts(self.now)
        self.assertEqual(self.rang, ["Glass"])
        current = self.agent.alerts.current()
        self.assertEqual(current["screen"], "timeralert")
        self.assertEqual(current["data"]["phase"], "done")
        self.assertEqual(current["data"]["label"], "tea")
        # ...and the screen it took it from is untouched underneath.
        self.assertEqual(self.screen(), "status")

    def test_the_screen_it_took_comes_back_when_the_alarm_is_over(self):
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(
            screen="media"))
        self.agent.timers.start(5, now=990.0)
        self.agent.refresh_alerts(self.now)
        self.assertEqual(self.draw(), "timeralert")
        self.assertEqual(self.screen(), "media")
        self.now = 1011.0                                  # ten seconds on
        self.assertEqual(self.draw(), "media")
        self.assertEqual(self.screen(), "media")

    def test_a_message_takes_the_display_for_ten_seconds(self):
        self.agent.watcher = FakeWatcher([{
            "kind": "message", "app": "Vesktop", "channel": "general",
            "author": "Niko", "text": "hello", "time": "20:19"}])
        self.agent.refresh_alerts(self.now)
        self.assertEqual(self.rang, ["Glass"])
        current = self.agent.alerts.current()
        self.assertEqual(current["screen"], "message")
        self.assertEqual(current["data"]["text"], "hello")
        self.assertEqual(self.screen(), "status")
        self.assertEqual(self.draw(), "message")
        self.now = 1011.0
        self.assertEqual(self.draw(), "status")

    def test_a_banner_holds_the_panel_for_its_own_number_of_seconds(self):
        # The store's copy arrives five seconds late already, so it is
        # given a dwell time of its own rather than borrowing the chat
        # reader's - and an event that asks for nothing else keeps the
        # number everybody else uses.
        self.agent.mutate(lambda s: s.setdefault("notifications", {}).update(
            show_seconds=10.0, banners_seconds=3.0))
        self.agent.watcher = FakeWatcher([{
            "kind": "banner", "app": "Messages", "author": "Mum",
            "text": "tea?", "channel": "10:11", "time": "10:11",
            "seconds": 3.0}])
        self.agent.refresh_alerts(self.now)
        current = self.agent.alerts.current()
        self.assertEqual(current["screen"], "message")
        self.assertEqual(current["data"]["text"], "tea?")
        self.assertEqual(current["data"]["channel"], "10:11")
        self.now += 4.0
        self.assertIsNone(self.agent.alerts.current())   # three seconds on

    def test_an_event_that_names_no_duration_keeps_the_usual_one(self):
        self.agent.mutate(lambda s: s.setdefault("notifications", {}).update(
            show_seconds=10.0, banners_seconds=3.0))
        self.agent.watcher = FakeWatcher([{
            "kind": "message", "app": "Vesktop", "author": "Niko",
            "text": "hello"}])
        self.agent.refresh_alerts(self.now)
        self.now += 4.0
        self.assertIsNotNone(self.agent.alerts.current())   # ten seconds on

    def test_an_unread_count_arriving_alone_is_still_shown(self):
        self.agent.watcher = FakeWatcher([{
            "kind": "unread", "app": "Discord", "count": 3,
            "channel": "general", "title": "(3) Discord"}])
        self.agent.refresh_alerts(self.now)
        current = self.agent.alerts.current()
        self.assertEqual(current["screen"], "message")
        self.assertEqual(current["data"]["text"], "3 unread")

    def test_notifications_can_be_turned_off_whole(self):
        self.agent.mutate(lambda s: s.setdefault("notifications", {}).update(
            enabled=False))
        self.agent.watcher = FakeWatcher([{"kind": "message", "text": "hi"}])
        self.agent.refresh_alerts(self.now)
        self.assertIsNone(self.agent.alerts.current())

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")


class FakeWatcher:
    """A chat reader that hands over a fixed list of events."""

    def __init__(self, events):
        self.events = list(events)

    def drain(self):
        gone, self.events = self.events, []
        return gone


class FakeKeyboard:
    """Takes frames instead of putting them on the panel."""

    def __init__(self):
        self.frames = []

    def send_lcd(self, frame):
        self.frames.append(frame)


class AgentCommandTests(unittest.TestCase):
    """What `g510 timer ...` asks the agent to do over the socket."""

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
        self.now = 1000.0
        self.rang = []
        self.agent = daemon.Daemon()
        self.agent.alerts = alerts.Alerts(play=self.rang.append,
                                          clock=lambda: self.now)
        self.agent.timers.clear()
        self.addCleanup(self.agent.timers.clear)

    def ask(self, **payload):
        payload.setdefault("cmd", "timer")
        return self.agent.handle_request(payload).__next__()

    def test_listing_says_what_is_running_and_how_it_is_set_up(self):
        reply = self.ask(action="list")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["enabled"])
        self.assertIn("pomodoro", reply["presets"])
        self.assertEqual(reply["timers"], [])

    def test_starting_by_a_preset_name(self):
        reply = self.ask(action="start", value="pomodoro", label="focus")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["timer"]["seconds"], 1500)
        self.assertEqual(reply["timer"]["label"], "focus")
        self.assertEqual(len(reply["timers"]), 1)

    def test_starting_by_a_length_of_time(self):
        reply = self.ask(action="start", value="90s")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["timer"]["seconds"], 90)

    def test_starting_something_that_is_not_a_length_of_time(self):
        reply = self.ask(action="start", value="whenever")
        self.assertFalse(reply["ok"])
        self.assertIn("90s, 5m, 1h30m", reply["error"])

    def test_cancelling_one(self):
        self.ask(action="start", value="10m", label="tea")
        reply = self.ask(action="cancel", value="tea")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["cancelled"][0]["label"], "tea")
        self.assertEqual(reply["timers"], [])

    def test_cancelling_something_that_is_not_running(self):
        reply = self.ask(action="cancel", value="4")
        self.assertFalse(reply["ok"])
        self.assertIn("4", reply["error"])

    def test_an_action_nobody_wrote_is_refused(self):
        reply = self.ask(action="teleport")
        self.assertFalse(reply["ok"])

    def test_a_timer_works_with_the_keyboard_unplugged(self):
        # Every other command answers "keyboard not connected" first; the
        # countdowns do not need the keyboard, and should not be gated on it.
        self.assertIsNone(self.agent.keyboard)
        self.assertTrue(self.ask(action="start", value="5m")["ok"])

    def test_the_chat_reader_follows_the_config(self):
        # The reader itself is left out of it: starting one here would hold a
        # real chat app's window open for the length of the test.
        made = []

        class StubWatcher:
            def __init__(self, block, log=None):
                self.block = block
                self.reader = type("Reader", (), {"block": block})()
                self.banners = type("Banners", (), {"block": block})()
                self.running = False
                made.append(self)

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

        with mock.patch.object(daemon.notify, "Watcher", StubWatcher):
            self.agent.watcher = None
            self.agent.sync_watcher()
            watcher = self.agent.watcher
            self.assertTrue(watcher.running)
            # Told again with the config as it now stands: still the same
            # reader, so what it has already learned survives a reload.
            self.agent.sync_watcher()
            self.assertIs(self.agent.watcher, watcher)
            self.assertEqual(len(made), 1)
            self.agent.mutate(lambda s: s.setdefault("notifications", {}).update(
                enabled=False))
            self.agent.sync_watcher()
            self.assertIsNone(self.agent.watcher)
            self.assertFalse(watcher.running)


if __name__ == "__main__":
    unittest.main()
