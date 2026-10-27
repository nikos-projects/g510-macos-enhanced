"""The Discord screen with keys: picking channels, answering messages.

P6 put Discord's conversation on the panel as a read-only notification.
This is the other half - the browser (`browse.Browser`) and everything the
agent does to drive it: the L3 gestures that start it, the event-tap keys
routed into it, the render precedence that shows it, and the CLI switch
`g510 discord browse`.

Two things are kept away from the real machine throughout, exactly as the
other agent tests do: the config file (patched at a temporary directory)
and port 47832 (the running agent's own link) - the browser here is fed
by a `FakeLink` whose state is written by hand, so every pane and cursor
can be driven without Discord running at all.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

import autoscreen
import browse
import cli
import config
import daemon
import lcd
import screens


# -- fixtures -----------------------------------------------------------------

def state(**over):
    """A push from the plugin, as the browser sees it.

    Three channels in the DM list, two messages in the open one, and one
    suggestion answer that has already been given - enough to walk every
    pane without a real Discord behind any of it.
    """
    base = {
        "channel": {"id": "1", "name": "general"},
        "list": "@me",
        "lists": [{"id": "@me", "name": "DMs", "current": True},
                  {"id": "9", "name": "Game", "current": False}],
        "channels": [
            {"id": "1", "name": "general", "unread": False,
             "mentions": 0, "current": True},
            {"id": "2", "name": "random", "unread": True,
             "mentions": 0, "current": False},
            {"id": "3", "name": "memes", "unread": False,
             "mentions": 2, "current": False},
        ],
        "messages": [{"id": "41", "author": "mira", "text": "hi"},
                     {"id": "42", "author": "kai", "text": "hello there"}],
        "has_more": False,
        "suggest": {"for": "", "items": []},
        "ack": None,
    }
    base.update(over)
    return base


def people():
    """The answer to one mention being written: two people, one role."""
    return {"for": "mi", "items": [
        {"label": "mira", "insert": "<@100>"},
        {"label": "midnight", "insert": "<@101>"},
        {"label": "mods", "insert": "<@&200>"},
    ]}


class IdleReader:
    """A machine nobody has touched, so the ordinary rules pick a screen
    without asking macOS anything."""

    def read(self, block, rules=None, wait=False):
        return {"idle": 400.0, "playing": False, "game": False,
                "fullscreen": False, "app": "Finder",
                "bundle": "com.apple.finder", "running": (),
                "commands": {}}


class FakeTap:
    """The keys the event tap took, handed over when the loop asks."""

    def __init__(self, keys=()):
        self.keys = list(keys)

    def drain(self):
        out, self.keys = self.keys, []
        return out


class FakeLink:
    """A link that answers without listening: what the agent is given."""

    def __init__(self, status):
        self._status = status
        self.sent = []

    def status(self):
        return self._status

    def state(self):
        return self._status.get("state") or {}

    def send(self, payload):
        self.sent.append(payload)
        return 99


class StubCanvas:
    """Measures eight pixels a character and writes down what it drew."""

    def __init__(self, per_char=8):
        self.per_char = per_char
        self.drawn = []
        self.placed = []                 # (text, y, size): where it went

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char

    def text(self, string, x, y, size=12.0, font=None):
        self.drawn.append(str(string))
        self.placed.append((str(string), y, size))

    def centered(self, string, y, size=12.0, font=None):
        self.drawn.append(str(string))


class AgentTests(unittest.TestCase):
    """The agent with a temporary config, a keyboard that only takes frames
    and the event tap stubbed out - it would need Input Monitoring."""

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
        self.agent.autoscreen = autoscreen.AutoScreen(IdleReader())

    def press(self, name, now, times=1):
        """Press a display key `times` times, close together."""
        for index in range(times):
            self.agent.note_gesture(name, now=now + index * 0.1)

    def hold(self, name, start, held_for):
        """A key pressed at `start` and *kept* down, the loop ticking under
        it, then let go."""
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) | {name}
        self.agent.note_gesture(name, now=start)
        moment = start
        while moment < start + held_for:
            moment += 0.05
            self.agent.refresh_gestures(moment)
        self.agent.lcd_pressed = set(self.agent.lcd_pressed) - {name}
        return moment

    def connected(self, **over):
        """A link whose plugin is up, with the state it pushed."""
        base = {"connected": True, "error": None, "port": 0, "seen": 0.1,
                "waiting": 0, "state": state(**over)}
        return FakeLink(base)

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")

    def draw(self, moment):
        """One redraw, and the screen it drew."""
        drawn = []

        def render(name, _state):
            drawn.append(name)
            return b"\x00" * 4096

        with mock.patch.object(screens, "render", render), \
                mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = moment - 100.0
            self.agent.refresh_lcd(moment)
        return drawn[-1] if drawn else None

    # -- the gestures that start it

    def test_three_presses_of_L3_put_the_screen_up_and_take_the_keys(self):
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.assertEqual(self.screen(), "discord")
        self.assertTrue(self.agent.browse.active)
        self.assertTrue(self.agent.browse.wants("up", None))   # arrows are its

    def test_holding_L3_starts_the_browser_again_after_esc(self):
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.assertTrue(self.agent.browse.active)
        self.agent.feed_browse("esc", None)                    # hand keys back
        self.assertFalse(self.agent.browse.active)
        moment = self.hold("L3", 200.0, daemon.HOLD_SECONDS + 0.1)
        self.assertTrue(self.agent.browse.active)
        self.assertGreater(moment, 200.0)

    def test_a_hold_of_L2_is_still_the_menu(self):
        self.agent.config["lcd_keys"] = {"L2": "menu"}
        self.hold("L2", 100.0, daemon.HOLD_SECONDS + 0.1)
        self.assertFalse(self.agent.menu.closed)

    # -- which keys belong to the panel

    def test_browsing_takes_the_arrows_and_lets_the_letters_through(self):
        self.agent.browse.open(now=100.0)
        self.assertTrue(self.agent.browse.wants("up", None))
        self.assertTrue(self.agent.browse.wants("enter", None))
        self.assertTrue(self.agent.browse.wants("esc", None))
        self.assertFalse(self.agent.browse.wants("a", "a"))    # the game's
        self.assertFalse(self.agent.browse.wants("f1", None))

    def test_writing_a_compose_takes_the_lot(self):
        self.agent.browse.open(now=100.0)
        self.agent.browse.pane = browse.PANE_COMPOSE
        self.assertTrue(self.agent.browse.wants("a", "a"))
        self.assertTrue(self.agent.browse.wants("backspace", None))
        self.assertTrue(self.agent.browse.wants("enter", None))
        self.assertFalse(self.agent.browse.wants("f1", None))

    def test_the_menu_wins_over_the_browser(self):
        self.agent.browse.open(now=100.0)
        self.agent.open_menu()
        self.agent.menu_typing = False
        self.assertTrue(self.agent.wants_menu_key("up", None))
        self.agent.browse.pane = browse.PANE_COMPOSE
        self.assertFalse(self.agent.wants_menu_key("a", "a"))  # a list, not a field

    def test_keys_routed_to_the_browser_move_the_cursor(self):
        self.agent.link = self.connected()
        self.agent.browse.open(now=100.0)
        self.agent.keytap = FakeTap([("down", None), ("down", None)])
        self.agent.refresh_menu(100.0)
        self.assertEqual(self.agent.browse.channel_id, "2")
        self.assertEqual(self.agent.browse.pane, browse.PANE_CHANNELS)

    def test_esc_hands_the_keys_back_and_the_screen_stays(self):
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.agent.keytap = FakeTap([("esc", None)])
        self.agent.refresh_menu(200.0)
        self.assertFalse(self.agent.browse.active)
        self.assertEqual(self.screen(), "discord")   # still a viewer

    # -- the browser must not outlive the screen it drives

    def test_the_display_moving_to_another_screen_closes_the_browser(self):
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.assertTrue(self.agent.browse.active)
        self.agent.mutate(lambda s: s["lcd"].update(screen="status"))
        self.draw(300.0)
        self.assertFalse(self.agent.browse.active)

    def test_switching_the_display_off_closes_the_browser(self):
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.agent.mutate(lambda s: s["lcd"].update(enabled=False))
        self.draw(300.0)
        self.assertFalse(self.agent.browse.active)

    def test_the_browser_holds_the_display_so_autoscreen_stands_down(self):
        self.agent.link = self.connected()
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        # Autoscreen had picked something, which is the state it is always
        # in once the rules have run: while the browser has the display it
        # is let go of, so nothing switches under the pane being driven.
        self.agent.autoscreen.chosen = "status"
        self.draw(300.0)
        self.assertIsNone(self.agent.autoscreen.chosen)

    # -- two things it will not take over

    def test_a_pinned_note_refuses_the_browser_and_says_why(self):
        self.agent.note = "buy milk"
        self.assertFalse(self.agent.start_browse())
        self.assertFalse(self.agent.browse.active)

    def test_a_recording_refuses_the_browser(self):
        self.agent.record_state = "recording"
        self.assertFalse(self.agent.start_browse())
        self.assertFalse(self.agent.browse.active)

    def test_pinning_a_note_while_browsing_closes_the_browser(self):
        self.agent.browse.open(now=100.0)
        self.assertTrue(self.agent.toggle_note())      # three presses of L5
        self.assertFalse(self.agent.browse.active)     # the note has the panel
        self.agent.feed_note("b", "b")
        self.agent.feed_note("u", "u")
        self.agent.feed_note("y", "y")
        self.agent.feed_note("enter", None)
        self.assertEqual(self.agent.note, "buy")

    # -- what the render shows

    def test_the_browser_view_is_what_the_discord_screen_draws(self):
        self.agent.link = self.connected()
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.agent.browse.update(self.agent.link.state(), 200.0)
        self.assertEqual(self.draw(300.0), "discord")
        self.assertFalse(self.agent.state.get("browse"))   # popped after

    # -- what must not cover it

    def browsing_with_a_banner(self, moment=1000.0):
        """The browser up on the Discord screen, and a message to show."""
        self.agent.mutate(lambda s: s.setdefault("lcd", {}).update(
            screen="discord"))
        self.agent.browse.open(now=moment)
        self.agent.alerts.push("message", {"text": "hello"}, 10.0, now=moment)
        return moment

    def test_a_message_does_not_take_the_panel_from_the_browser(self):
        # The browser has the keys precisely so a message can be read, so
        # a banner over the top of it would cover the very text it was
        # opened for.
        moment = self.browsing_with_a_banner()
        with mock.patch.object(daemon, "log") as said:
            self.assertEqual(self.draw(moment), "discord")
            self.assertEqual(self.draw(moment + 1.0), "discord")
        self.assertFalse(self.agent.alerting)
        # Said once for the banner rather than once a redraw: the log
        # would otherwise fill with the same sentence every second.
        held = [call.args[0] for call in said.call_args_list]
        self.assertEqual(
            held.count("banner held back - the browser has the keys"), 1)

    def test_the_message_takes_the_panel_once_the_keys_are_handed_back(self):
        # It only waited: still inside its own seconds, it is what the
        # panel shows the moment there is nothing else on it.
        moment = self.browsing_with_a_banner()
        self.agent.browse.close()
        self.assertEqual(self.draw(moment + 1.0), "message")
        self.assertTrue(self.agent.alerting)

    def test_a_message_that_aged_out_while_browsing_is_never_shown(self):
        # Nobody could have seen it, so it does not come back to be read
        # over a conversation that has moved on since.
        moment = self.browsing_with_a_banner()
        self.assertEqual(self.draw(moment), "discord")
        self.agent.browse.close()
        self.assertNotEqual(self.draw(moment + 11.0), "message")
        self.assertFalse(self.agent.alerting)

    # -- the CLI switch

    def test_the_shell_can_ask_whether_the_keys_are_taken(self):
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True, "active": True}):
            cli.cmd_discord(["browse"])
        self.assertIn("is on", self.stdout())

    def test_the_shell_can_start_and_stop_the_browser(self):
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True,
                                             "active": True}) as ask:
            cli.cmd_discord(["browse", "on"])
        ask.assert_called_once_with("vencord", action="browse", on=True)
        with mock.patch.object(cli, "agent_reply",
                               return_value={"ok": True,
                                             "active": False}) as ask:
            cli.cmd_discord(["browse", "off"])
        ask.assert_called_once_with("vencord", action="browse", on=False)

    def test_the_agent_answers_the_browse_command_without_a_plugin(self):
        self.agent.link = None
        reply = self.agent.vencord_command({"action": "browse"})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["active"])
        self.assertTrue(self.agent.vencord_command(
            {"action": "browse", "on": True})["active"])
        self.assertFalse(self.agent.vencord_command(
            {"action": "browse", "on": False})["active"])

    def stdout(self):
        return self._captured.getvalue()

    def run(self, result=None):
        self._captured = io.StringIO()
        with mock.patch("sys.stdout", new=self._captured):
            return super().run(result)


class BrowserTests(unittest.TestCase):
    """The state machine on its own: one key, one push, one view."""

    def setUp(self):
        self.browser = browse.Browser()
        self.browser.open(now=100.0)
        self.browser.update(state(), 100.0)

    # -- the channels pane

    def test_down_starts_at_the_top_and_up_at_the_bottom(self):
        self.browser.press("down", now=101.0)
        self.assertEqual(self.browser.channel_id, "1")
        self.browser.channel_id = None
        self.browser.press("up", now=102.0)
        self.assertEqual(self.browser.channel_id, "3")

    def test_the_cursor_walks_the_list_and_wraps(self):
        self.browser.channel_id = "3"
        self.browser.press("down", now=101.0)
        self.assertEqual(self.browser.channel_id, "1")     # wrapped to the top
        self.browser.press("up", now=102.0)
        self.assertEqual(self.browser.channel_id, "3")

    def test_enter_on_a_channel_opens_it_and_moves_to_the_messages(self):
        self.browser.channel_id = "2"
        actions = self.browser.press("enter", now=101.0)
        self.assertEqual(actions, [("open", "2")])
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)
        self.assertEqual(self.browser.pending_open, "2")

    def test_left_and_right_walk_the_lists_of_channels(self):
        self.browser.press("right", now=101.0)
        self.assertEqual(self.browser.list_id, "9")
        self.assertEqual(self.browser.pending_list, "9")
        self.assertEqual(self.browser.press("right", now=102.0),
                         [("list", "@me")])                # wrapped back

    def test_a_list_switch_asks_the_plugin_and_waits_for_the_answer(self):
        self.browser.press("right", now=101.0)
        view = self.browser.view()
        self.assertEqual(view.get("waiting"), "...")
        self.browser.update(state(list="9", channels=[]), 101.5)
        self.assertIsNone(self.browser.pending_list)

    # -- the messages pane

    def test_up_and_down_move_between_messages(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "42"
        self.browser.press("up", now=101.0)
        self.assertEqual(self.browser.message_id, "41")
        self.browser.press("down", now=102.0)
        self.assertEqual(self.browser.message_id, "42")

    def test_up_at_the_oldest_asks_for_the_page_behind_it_once(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.update(state(has_more=True), 100.0)
        first = self.browser.press("up", now=101.0)
        again = self.browser.press("up", now=101.1)   # a key held down
        self.assertEqual(first, [("older", browse.PAGE)])
        self.assertEqual(again, [])                   # one page at a time
        later = self.browser.press("up", now=102.0)   # past the gap: another
        self.assertEqual(later, [("older", 2 * browse.PAGE)])

    def test_the_page_grows_the_further_back_you_climb(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.update(state(has_more=True), 100.0)
        sizes = []
        for at in (101.0, 102.0, 103.0, 104.0, 105.0):
            sizes += [n for _, n in self.browser.press("up", now=at)]
        self.assertEqual(sizes,
                         [browse.PAGE, 2 * browse.PAGE,
                          browse.PAGE_MAX, browse.PAGE_MAX,
                          browse.PAGE_MAX])           # Discord stops at 100

    def test_another_channel_starts_the_pages_small_again(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.update(state(has_more=True), 100.0)
        for at in (101.0, 102.0, 103.0):
            self.browser.press("up", now=at)
        self.assertEqual(self.browser.older_n, 3)
        self.browser.update(state(channel={"id": "2", "name": "random"},
                                  has_more=True), 104.0)
        self.assertEqual(self.browser.older_n, 0)
        self.assertEqual(self.browser.press("up", now=105.0),
                         [("older", browse.PAGE)])

    def test_climbing_to_the_oldest_line_asks_before_there_is_nowhere_left(self):
        rows = [{"id": str(n), "author": "mira", "text": "hi"}
                for n in range(1, 6)]                 # oldest first
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.update(state(messages=rows, has_more=True), 100.0)
        self.browser.message_id = "4"                 # three from the oldest
        self.assertEqual(self.browser.press("up", now=101.0),
                         [("older", browse.PAGE)])    # asked on the way in
        self.assertEqual(self.browser.message_id, "3") # and still moved
        self.browser.message_id = "5"                 # the newest, no ask
        self.assertEqual(self.browser.press("up", now=102.0), [])
        self.assertEqual(self.browser.message_id, "4")
        self.browser.message_id = "4"                 # at the edge again
        self.assertEqual(self.browser.press("up", now=103.0),
                         [("older", 2 * browse.PAGE)])  # past the gap
        self.assertEqual(self.browser.message_id, "3")

    def test_the_prefetch_stops_once_something_else_says_no(self):
        rows = [{"id": str(n), "author": "mira", "text": "hi"}
                for n in range(1, 6)]
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.update(state(messages=rows), 100.0)
        self.browser.message_id = "4"
        self.assertEqual(self.browser.press("up", now=101.0), [])
        self.assertEqual(self.browser.message_id, "3")

    def test_enter_answers_the_message_and_right_writes_a_new_one(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.press("enter", now=101.0)
        self.assertEqual(self.browser.pane, browse.PANE_COMPOSE)
        self.assertEqual(self.browser.reply_to, "41")
        self.assertEqual(self.browser.reply_name, "mira")
        self.browser.press("esc", now=102.0)
        self.browser.press("right", now=103.0)
        self.assertEqual(self.browser.pane, browse.PANE_COMPOSE)
        self.assertIsNone(self.browser.reply_to)

    def test_left_from_the_messages_goes_back_to_the_channels(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.press("left", now=101.0)
        self.assertEqual(self.browser.pane, browse.PANE_CHANNELS)

    # -- reading a message longer than the panel

    def reading(self, now=100.0, **over):
        """The messages pane up with a message on show, at a known moment."""
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.update(state(**over), now)
        return self.browser

    def test_a_highlighted_message_starts_at_its_first_line(self):
        self.reading(now=100.0)
        self.assertEqual(self.browser.view()["scroll"], 0)

    def test_the_first_lines_are_held_still_before_the_text_climbs(self):
        self.reading(now=100.0)
        held = 100.0 + browse.SCROLL_LEAD - 0.1
        self.browser.update(state(), held)
        self.assertEqual(self.browser.view()["scroll"], 0)
        self.browser.update(state(), 100.0 + browse.SCROLL_LEAD)
        self.assertEqual(self.browser.view()["scroll"], 1)
        climbed = 100.0 + browse.SCROLL_LEAD + browse.SCROLL_LAG
        self.browser.update(state(), climbed)
        self.assertEqual(self.browser.view()["scroll"], 2)

    def test_moving_to_another_message_reads_it_from_the_top(self):
        self.reading(now=100.0)
        climbed = 100.0 + browse.SCROLL_LEAD + browse.SCROLL_LAG
        self.browser.update(state(), climbed)
        self.assertGreater(self.browser.view()["scroll"], 0)
        self.browser.press("up", now=103.0)          # the message before it
        self.browser.update(state(), 103.0)
        self.assertEqual(self.browser.view()["scroll"], 0)

    def test_a_new_message_under_the_cursor_is_read_from_the_top(self):
        self.reading(now=100.0)
        climbed = 100.0 + browse.SCROLL_LEAD + browse.SCROLL_LAG
        self.browser.update(state(), climbed)
        self.assertGreater(self.browser.view()["scroll"], 0)
        arrived = state(messages=[{"id": "41", "author": "mira",
                                   "text": "hi"},
                                  {"id": "43", "author": "niko",
                                   "text": "new"}])
        self.browser.update(arrived, 103.0)
        self.assertEqual(self.browser.view()["author"], "niko")
        self.assertEqual(self.browser.view()["scroll"], 0)

    def test_an_empty_channel_has_nothing_to_climb(self):
        self.reading(now=100.0, messages=[])
        self.browser.update(state(messages=[]), 100.0 + browse.SCROLL_LEAD)
        self.browser.update(state(messages=[]), 100.0 + browse.SCROLL_LEAD * 4)
        self.assertEqual(self.browser.scroll, 0)

    def test_letting_go_of_the_keys_stops_the_reading(self):
        self.reading(now=100.0)
        climbed = 100.0 + browse.SCROLL_LEAD + browse.SCROLL_LAG
        self.browser.update(state(), climbed)
        self.assertGreater(self.browser.scroll, 0)
        self.browser.close()
        self.browser.update(state(), 104.0)
        self.assertEqual(self.browser.scroll, 0)

    def test_a_very_long_message_stops_climbing_rather_than_running_away(self):
        self.reading(now=100.0)
        moment = 100.0
        while self.browser.scroll < browse.SCROLL_MAX:
            moment += browse.SCROLL_LAG
            self.browser.update(state(), moment)
        self.assertEqual(self.browser.scroll, browse.SCROLL_MAX)
        self.browser.update(state(), moment + 600.0)
        self.assertEqual(self.browser.scroll, browse.SCROLL_MAX)

    # -- the compose pane

    def test_writing_builds_the_draft_and_sends_it(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.press("right", now=101.0)
        for letter, when in (("h", 102.0), ("i", 103.0)):
            self.browser.press(letter, letter, now=when)
        self.assertEqual(self.browser.draft, "hi")
        self.assertEqual(self.browser.press("enter", now=104.0),
                         [("say", "hi")])
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)
        self.assertEqual(self.browser.draft, "")

    def test_enter_on_a_compose_answers_the_message_it_was_started_on(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.press("enter", now=101.0)
        for letter, when in (("h", 102.0), ("i", 103.0)):
            self.browser.press(letter, letter, now=when)
        self.assertEqual(self.browser.press("enter", now=104.0),
                         [("reply", "41", "hi")])

    def test_esc_throws_the_draft_away(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "half a sent"
        self.browser.press("esc", now=101.0)
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)
        self.assertEqual(self.browser.draft, "")

    def test_backspace_edits_the_draft(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hi"
        self.browser.press("backspace", now=101.0)
        self.assertEqual(self.browser.draft, "h")

    def test_enter_on_an_empty_compose_just_closes_it(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.assertEqual(self.browser.press("enter", now=101.0), [])
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)

    def test_an_unfinished_compose_times_out_back_to_the_messages(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "never finished"
        self.browser.touch = 100.0
        self.browser.abandon()
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)
        self.assertEqual(self.browser.draft, "")

    # -- the mention picker

    def test_an_at_sign_asks_the_plugin_for_whoever_fits(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.press("@", "@", now=101.0)
        self.assertEqual(self.browser.press("m", "m", now=102.0),
                         [("suggest", "m")])
        self.assertEqual(self.browser.asked, "m")

    def test_narrowing_the_word_asks_once_more_and_moving_picks_asks_never(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.press("@", "@", now=101.0)
        self.assertEqual(self.browser.press("m", "m", now=102.0),
                         [("suggest", "m")])
        # A narrower word is a narrower question, so it is asked again -
        # the answer has to keep up with what is being written.
        self.assertEqual(self.browser.press("i", "i", now=103.0),
                         [("suggest", "mi")])
        # But a key that leaves the word alone - arrowing through the
        # offers - costs the plugin nothing.
        self.browser.update(state(suggest=people()), 103.5)
        self.assertEqual(self.browser.press("down", now=104.0), [])
        self.assertEqual(self.browser.press("down", now=104.1), [])

    def test_a_letter_not_after_an_at_sign_asks_for_nobody(self):
        self.browser.pane = browse.PANE_COMPOSE
        for letter, when in (("h", 101.0), ("i", 102.0)):
            self.assertEqual(self.browser.press(letter, letter, now=when), [])

    def test_an_address_in_the_middle_of_a_word_is_not_a_mention(self):
        self.browser.pane = browse.PANE_COMPOSE
        for letter, when in (("s", 101.0), ("o", 102.0), ("m", 103.0),
                             ("e", 104.0), ("@", 105.0), ("x", 106.0)):
            self.browser.press(letter, letter, now=when)
        self.assertEqual(self.browser.asked, "")      # nobody was asked for

    def test_the_offered_names_are_the_answer_to_the_word_being_written(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        labels = [row["label"] for row in self.browser.picks()]
        self.assertEqual(labels, ["mira", "midnight", "mods"])

    def test_an_answer_for_a_word_that_moved_on_is_ignored(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @so"          # the word has moved on
        self.browser.update(state(suggest=people()), 100.0)   # for "mi"
        self.assertEqual(self.browser.picks(), [])

    def test_arrowing_through_the_offers_and_entering_picks_one(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        self.browser.press("down", now=101.0)   # the first offer
        self.browser.press("down", now=102.0)   # the second
        self.assertEqual(self.browser.press("enter", now=103.0), [])
        self.assertEqual(self.browser.draft, "hey <@101> ")
        self.browser.draft += "there"
        self.assertEqual(self.browser.press("enter", now=104.0),
                         [("say", "hey <@101> there")])

    def test_up_from_nothing_takes_the_last_offer(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        self.browser.press("up", now=101.0)
        self.assertEqual(self.browser.pick, 2)           # "mods", the last

    def test_esc_clears_the_offer_first_and_the_draft_second(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        self.browser.press("down", now=101.0)
        self.browser.press("esc", now=102.0)
        self.assertIsNone(self.browser.pick)             # the offer is gone
        self.assertEqual(self.browser.draft, "hey @mi")  # the draft is not
        self.browser.press("esc", now=103.0)
        self.assertEqual(self.browser.pane, browse.PANE_MESSAGES)
        self.assertEqual(self.browser.draft, "")

    def test_the_panel_reads_a_mention_as_its_name(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        self.browser.press("down", now=101.0)
        self.browser.press("enter", now=102.0)
        self.assertEqual(self.browser.say_it(self.browser.draft),
                         "hey @mira ")

    def test_a_code_nobody_named_is_left_alone(self):
        self.assertEqual(self.browser.say_it("see <@999> there"),
                         "see <@999> there")

    # -- what the plugin said

    def test_a_cursor_on_a_channel_that_is_gone_falls_to_the_top(self):
        self.browser.channel_id = "3"
        self.browser.update(state(channels=state()["channels"][:2]), 101.0)
        self.assertEqual(self.browser.channel_id, "1")

    def test_a_cursor_on_a_message_that_is_gone_falls_to_the_newest(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.update(state(messages=[state()["messages"][1]]), 101.0)
        self.assertEqual(self.browser.message_id, None)   # the newest one

    def test_a_plugin_that_says_it_followed_the_client_brings_the_list_back(self):
        self.browser.list_id = "9"
        self.browser.pending_list = None
        self.browser.update(state(list="@me"), 101.0)
        self.assertEqual(self.browser.list_id, "@me")

    def test_a_refused_send_shows_why_for_a_few_seconds(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hi"
        self.browser.press("enter", now=101.0)
        self.browser.sent(7, now=101.0)
        self.browser.update(state(ack={"n": 7, "ok": False,
                                       "error": "no permissions - sorry"}),
                            101.5)
        self.assertEqual(self.browser.error, "no permissions")
        self.browser.update(state(), 101.5 + browse.ERROR_SECONDS)
        self.assertIsNone(self.browser.error)

    def test_a_send_that_went_out_says_nothing(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hi"
        self.browser.press("enter", now=101.0)
        self.browser.sent(7, now=101.0)
        self.browser.update(state(ack={"n": 7, "ok": True}), 101.5)
        self.assertIsNone(self.browser.error)
        self.assertIsNone(self.browser.sent_n)

    def test_closing_lets_go_of_the_draft_and_the_keys(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "unfinished"
        self.browser.close()
        self.assertFalse(self.browser.active)
        self.assertEqual(self.browser.pane, browse.PANE_CHANNELS)
        self.assertEqual(self.browser.draft, "")
        self.assertEqual(self.browser.press("up", now=101.0), [])
        self.assertFalse(self.browser.wants("up", None))

    # -- what the screen is shown

    def test_the_channels_view_holds_the_cursor_in_the_middle(self):
        self.browser.channel_id = "3"
        view = self.browser.view()
        self.assertEqual(view["mode"], "channels")
        self.assertEqual(view["count"], "3/3")
        self.assertEqual([row["name"] for row in view["rows"]],
                         ["general", "random", "memes"])
        self.assertEqual(view["rows"][2]["mark"], ">")     # the cursor
        self.assertEqual(view["rows"][1]["mark"], "*")     # unread
        self.assertEqual(view["rows"][0]["mark"], ".")     # the open one

    def test_the_messages_view_shows_the_message_under_the_cursor(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        view = self.browser.view()
        self.assertEqual(view["author"], "mira")
        self.assertEqual(view["text"], "hi")
        self.assertEqual(view["count"], "1/2")

    def test_the_compose_view_shows_the_draft_and_who_it_answers(self):
        self.browser.pane = browse.PANE_MESSAGES
        self.browser.message_id = "41"
        self.browser.press("enter", now=101.0)
        for letter, when in (("h", 102.0), ("i", 103.0)):
            self.browser.press(letter, letter, now=when)
        view = self.browser.view()
        self.assertEqual(view["mode"], "compose")
        self.assertEqual(view["draft"], "hi")
        self.assertEqual(view["reply"], "mira")

    def test_the_compose_view_lists_the_offers_with_the_cursor_on_one(self):
        self.browser.pane = browse.PANE_COMPOSE
        self.browser.draft = "hey @mi"
        self.browser.update(state(suggest=people()), 100.0)
        self.browser.press("down", now=101.0)
        view = self.browser.view()
        self.assertEqual(len(view["picks"]), 3)
        self.assertTrue(view["picks"][0]["cursor"])
        self.assertEqual(view["picks"][0]["label"], "@mira")
        self.assertEqual(view["count"], "1/3")

    def test_an_empty_channel_says_so_rather_than_showing_nothing(self):
        self.browser.update(state(channels=[]), 100.0)
        view = self.browser.view()
        self.assertEqual(view["rows"], [])
        self.assertNotIn("count", view)


class ScreenTests(unittest.TestCase):
    """The pixels the browser's view turns into."""

    def test_the_channels_pane_draws_three_marked_rows(self):
        canvas = StubCanvas()
        screens._browse(canvas, {
            "mode": "channels", "title": "DMs", "count": "2/3",
            "rows": [{"mark": " ", "name": "general"},
                     {"mark": ">", "name": "random"},
                     {"mark": "@", "name": "memes"}]})
        self.assertIn("DMs", canvas.drawn)
        self.assertIn("> random", canvas.drawn)
        self.assertIn("@ memes", canvas.drawn)

    def test_the_messages_pane_draws_the_author_and_its_words(self):
        canvas = StubCanvas()
        screens._browse(canvas, {
            "mode": "messages", "title": "general", "count": "1/2",
            "author": "mira", "text": "hello there"})
        self.assertIn("> mira", canvas.drawn)
        self.assertIn("hello there", canvas.drawn)

    def test_without_a_scroll_it_shows_exactly_the_two_lines_it_used_to(self):
        # The window is new; what it shows when nothing has climbed is not.
        canvas = StubCanvas()
        text = " ".join(f"word{index}" for index in range(40))
        screens._browse(canvas, {"mode": "messages", "title": "general",
                                 "author": "mira", "text": text})
        self.assertEqual(canvas.drawn[-2:],
                         screens._wrap(StubCanvas(), text, 10, 2))

    def test_a_message_longer_than_the_panel_is_read_a_window_at_a_time(self):
        canvas = StubCanvas()
        text = " ".join(f"word{index}" for index in range(40))
        screens._browse(canvas, {"mode": "messages", "title": "general",
                                 "author": "mira", "text": text, "scroll": 3})
        lines = screens._wrap(StubCanvas(), text, 10, None)
        self.assertGreater(len(lines), 6)
        self.assertEqual(canvas.drawn[-2], lines[3])       # the third onwards
        # The line under it is the next one along. The " ..." saying there
        # is more below may have trimmed its end, so it is matched by
        # where it starts rather than all of it.
        self.assertTrue(canvas.drawn[-1].startswith(lines[4][:8]))
        self.assertNotIn(lines[0], canvas.drawn)           # the top is gone

    def test_the_window_is_held_inside_what_there_is_to_show(self):
        canvas = StubCanvas()
        lines = [f"line{index}" for index in range(10)]
        self.assertEqual(screens._window(canvas, lines, 3),
                         ["line3", "line4 ..."])
        # Past the end: the last two lines, not a blank panel.
        self.assertEqual(screens._window(canvas, lines, 99),
                         ["line8", "line9"])
        self.assertEqual(screens._window(canvas, ["only"], 99), ["only"])
        self.assertEqual(screens._window(canvas, [], 0), [])
        # Whatever the browser's count is - it counts wrapped lines it
        # cannot see, so only the panel can put a number on it.
        self.assertEqual(screens._window(canvas, lines, None), ["line0",
                                                                "line1 ..."])
        self.assertEqual(screens._window(canvas, lines, "nonsense"),
                         ["line0", "line1 ..."])

    def test_every_line_of_a_message_still_fits_on_the_panel(self):
        canvas = StubCanvas()
        text = " ".join(f"word{index}" for index in range(40))
        screens._browse(canvas, {"mode": "messages", "title": "general",
                                 "author": "louie", "text": text,
                                 "scroll": 3, "error": "it did not send"})
        # A line drawn at `y` puts its ink at `y + 1 .. y + size + 1`, so
        # one that reaches past the last row has the bottom of its letters
        # cut off by the edge - which is what the second line used to do at
        # y=36, where five rows of a nine-row line were all that was left.
        for said, y, size in canvas.placed:
            self.assertLessEqual(y + size, lcd.LCD_HEIGHT - 2, said)
        # And both lines of the message are on it.
        lines = screens._wrap(StubCanvas(), text, 10, None)
        self.assertEqual([said for said, y, _size in canvas.placed if y >= 21],
                         screens._window(StubCanvas(), lines, 3))

    def test_the_compose_pane_keeps_the_end_of_a_long_draft(self):
        canvas = StubCanvas()
        screens._browse(canvas, {
            "mode": "compose", "title": "general", "reply": None,
            "draft": "a" * 60})
        # The caret is at the end of the line, so the end is what survives.
        self.assertTrue(canvas.drawn[-1].endswith("_"))

    def test_the_offers_take_the_rows_under_the_draft(self):
        canvas = StubCanvas()
        screens._browse(canvas, {
            "mode": "compose", "title": "general",
            "draft": "hey @mi", "count": "1/3",
            "picks": [{"cursor": True, "label": "@mira"},
                      {"cursor": False, "label": "@midnight"}]})
        self.assertIn("> @mira", canvas.drawn)
        self.assertIn("  @midnight", canvas.drawn)

    def test_a_refusal_from_the_plugin_is_drawn_over_the_second_row(self):
        canvas = StubCanvas()
        screens._browse(canvas, {
            "mode": "channels", "title": "DMs",
            "rows": [{"mark": ">", "name": "general"}],
            "error": "no permissions"})
        self.assertIn("!no permissions", canvas.drawn)

    def test_the_discord_screen_prefers_the_browser_view(self):
        canvas = StubCanvas()
        screens.screen_discord(canvas, {
            "browse": {"mode": "channels", "title": "DMs",
                       "rows": [{"mark": ">", "name": "general"}]},
            "discord": {"channel": {"name": "something else"}}})
        self.assertIn("> general", canvas.drawn)
        self.assertNotIn("something else", canvas.drawn)

    def test_without_a_browser_the_discord_screen_is_still_the_old_one(self):
        canvas = StubCanvas()
        screens.screen_discord(canvas, {
            "discord": {"channel": {"id": "1", "name": "general"},
                        "messages": [{"id": "41", "author": "mira",
                                      "text": "hi"}]}})
        self.assertIn("mira", canvas.drawn)


if __name__ == "__main__":
    unittest.main()
