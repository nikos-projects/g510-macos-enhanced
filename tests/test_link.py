"""The Discord screen's other half: the link, and what draws from it.

A Vencord plugin inside Vesktop pushes what Discord has and collects what
the panel asks for over a server bound to 127.0.0.1. This covers that
server, the state it holds, the screen that draws that state, the name
matching `g510 discord open` does and the agent's half of the commands.

Two things are kept away from the real machine throughout: the config
file (patched at a temporary directory, as the other agent tests do) and
port 47832, which the running agent's own link holds - every server these
tests start binds a port nothing else is using.
"""
import json
import io
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import autoscreen
import cli
import config
import daemon
import device
import link
import screens


def free_port():
    """A port nobody has - released again before anybody can take it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def call(port, path, payload=None, method=None, timeout=5.0):
    """One request at the link, returning (code, headers, body dict)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            code, headers = response.status, response.headers
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        finally:
            error.close()
        code, headers = error.code, error.headers
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        parsed = body
    return code, headers, parsed


class LinkCoreTests(unittest.TestCase):
    """What the plugin says, and what the panel asks for - no server yet."""

    def setUp(self):
        self.link = link.Link({"port": free_port()})
        self.addCleanup(self.link.stop)

    def test_it_starts_with_nothing_said(self):
        self.assertEqual(self.link.state(), {})
        self.assertFalse(self.link.connected())

    def test_a_push_replaces_what_the_panel_knows(self):
        self.assertTrue(self.link.receive({"channel": {"name": "general"}}))
        self.assertTrue(self.link.receive({"channel": {"name": "random"}}))
        self.assertEqual(self.link.state()["channel"]["name"], "random")

    def test_a_push_that_is_not_a_dict_is_refused(self):
        self.assertFalse(self.link.receive(["not", "a", "state"]))
        self.assertEqual(self.link.state(), {})

    def test_a_push_marks_the_plugin_as_connected(self):
        self.link.receive({"v": 1})
        self.assertTrue(self.link.connected())
        # ...for a while: no push for `CONNECTED_AFTER` and Discord has gone
        # away, which is what the CLI's "not connected" has to mean.
        self.link._seen = time.time() - link.CONNECTED_AFTER - 1
        self.assertFalse(self.link.connected())

    def test_status_says_how_long_ago_the_last_push_was(self):
        self.link.receive({"v": 1})
        status = self.link.status()
        self.assertTrue(status["connected"])
        self.assertGreaterEqual(status["seen"], 0.0)
        self.assertEqual(status["port"], self.link.port)
        self.assertEqual(status["state"], {"v": 1})

    def test_a_restarted_agent_numbers_past_what_the_plugin_already_ran(self):
        # The client stays up across an agent restart, so the plugin is
        # still counting from twelve when this side begins again at one.
        # A command filed below that would be collected by nobody.
        self.link.receive({"v": 1, "ack": {"n": 12, "ok": True}})
        number = self.link.send({"cmd": "say", "text": "hi"})
        self.assertEqual(number, 13)
        self.assertEqual([c["n"] for c in self.link.take(12, wait=0)], [13])

    def test_commands_are_numbered_and_only_come_back_past_the_cursor(self):
        first = self.link.send({"cmd": "say", "text": "hi"})
        second = self.link.send({"cmd": "older"})
        self.assertLess(first, second)
        self.assertEqual([c["n"] for c in self.link.take(0, wait=0)],
                         [first, second])
        self.assertEqual([c["n"] for c in self.link.take(first, wait=0)],
                         [second])
        self.assertEqual(self.link.take(second, wait=0), [])

    def test_a_command_carries_its_fields_and_its_number(self):
        self.link.send({"cmd": "open", "id": "42"})
        [command] = self.link.take(0, wait=0)
        self.assertEqual(command, {"cmd": "open", "id": "42", "n": 1})

    def test_a_command_must_be_a_dict(self):
        with self.assertRaises(ValueError):
            self.link.send(["say", "hi"])

    def test_a_call_waits_for_a_command_that_has_not_arrived_yet(self):
        arrived = []

        def later():
            time.sleep(0.2)
            arrived.append(self.link.send({"cmd": "refresh"}))

        thread = threading.Thread(target=later)
        thread.start()
        start = time.time()
        commands = self.link.take(0, wait=5.0)
        thread.join()
        self.assertEqual([c["cmd"] for c in commands], ["refresh"])
        self.assertGreaterEqual(time.time() - start, 0.15)

    def test_it_gives_up_when_nothing_comes(self):
        start = time.time()
        self.assertEqual(self.link.take(0, wait=0.1), [])
        self.assertLess(time.time() - start, 2.0)

    def test_stopping_lets_go_of_everything_waiting(self):
        self.link.send({"cmd": "say"})
        self.link.stop()
        self.assertEqual(self.link.take(0, wait=0), [])
        self.assertIsNone(self.link._server)

    def test_a_port_nobody_set_falls_back_to_the_documented_one(self):
        self.assertEqual(link.Link({}).port, link.DEFAULT_PORT)
        self.assertEqual(link.Link({"port": "nonsense"}).port,
                         link.DEFAULT_PORT)
        self.assertEqual(link.Link({"port": None}).port, link.DEFAULT_PORT)

    def test_a_command_the_plugin_says_it_ran_is_retired(self):
        # The plugin restarts with its cursor back at zero, so what is
        # still queued here is the only thing it can pick up again - and a
        # message it already sent must not be in that.
        first = self.link.send({"cmd": "say", "text": "one"})
        second = self.link.send({"cmd": "say", "text": "two"})
        self.link.receive({"ack": {"n": first, "ok": True}})
        self.assertEqual([c["n"] for c in self.link.take(0, wait=0)],
                         [second])
        self.link.receive({"ack": {"n": second, "ok": True}})
        self.assertEqual(self.link.take(0, wait=0), [])

    def test_a_push_with_nothing_acked_retires_nothing(self):
        self.link.send({"cmd": "older"})
        self.link.receive({"ack": None})
        self.assertEqual(len(self.link.take(0, wait=0)), 1)
        self.link.receive({"v": 1})
        self.assertEqual(len(self.link.take(0, wait=0)), 1)


class LinkServerTests(unittest.TestCase):
    """The same link, reached over HTTP the way the plugin reaches it."""

    def setUp(self):
        self.link = link.Link({"port": free_port()}).start()
        self.addCleanup(self.link.stop)
        self.assertIsNone(self.link.error)

    def test_it_binds_without_hanging(self):
        # `HTTPServer.server_bind` runs the address through getfqdn, and
        # here that reverse lookup stalls for half a minute - which would be
        # half a minute of every agent start.
        start = time.time()
        other = link.Link({"port": free_port()}).start()
        other.stop()
        self.assertLess(time.time() - start, 3.0)

    def test_the_plugin_pushes_its_state(self):
        code, headers, body = call(self.link.port, "/state",
                                   {"channel": {"name": "general"}})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.link.state()["channel"]["name"], "general")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_a_body_that_is_not_json_is_refused(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.link.port}/state", data=b"{nope")
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
        self.assertEqual(code, 400)

    def test_nothing_else_posts_here(self):
        code, _headers, body = call(self.link.port, "/whatever", {"a": 1})
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_status_is_readable_by_the_cli(self):
        self.link.receive({"channels": [{"id": "1", "name": "general"}]})
        code, _headers, body = call(self.link.port, "/status")
        self.assertEqual(code, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["state"]["channels"][0]["name"], "general")

    def test_the_plugin_collects_what_is_waiting_for_it(self):
        number = self.link.send({"cmd": "older", "limit": 30})
        code, _headers, body = call(self.link.port, "/cmd?since=0")
        self.assertEqual(code, 200)
        self.assertEqual([c["n"] for c in body["commands"]], [number])
        self.assertEqual(body["commands"][0]["limit"], 30)

    def test_a_cursor_already_past_a_command_leaves_it_alone(self):
        self.link.send({"cmd": "say", "text": "one"})
        second = self.link.send({"cmd": "say", "text": "two"})
        _code, _headers, body = call(self.link.port, "/cmd?since=1")
        self.assertEqual([c["text"] for c in body["commands"]], ["two"])
        self.assertEqual(body["commands"][0]["n"], second)

    def test_a_malformed_cursor_is_read_as_the_beginning(self):
        self.link.send({"cmd": "refresh"})
        _code, _headers, body = call(self.link.port, "/cmd?since=junk")
        self.assertEqual(len(body["commands"]), 1)

    def test_a_poll_holds_until_a_command_turns_up(self):
        collected = {}

        def poll():
            collected["body"] = call(self.link.port, "/cmd?since=0",
                                     timeout=10.0)[2]

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.3)
        self.link.send({"cmd": "say", "text": "late"})
        thread.join(timeout=10.0)
        self.assertEqual([c["text"] for c in
                          collected["body"]["commands"]], ["late"])

    def test_the_browser_cors_preflight_is_answered(self):
        request = urllib.request.Request(f"http://127.0.0.1:"
                                         f"{self.link.port}/state",
                                         method="OPTIONS")
        with urllib.request.urlopen(request, timeout=5.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"],
                             "*")
            self.assertIn("POST",
                          response.headers["Access-Control-Allow-Methods"])

    def test_it_only_ever_listens_on_localhost(self):
        # Bound to 127.0.0.1 rather than to every interface, so nothing on
        # the network can read a conversation off this machine.
        self.assertEqual(self.link._server.server_address[0], "127.0.0.1")


class LinkRefusalTests(unittest.TestCase):
    """Switched off, or a port somebody else already has."""

    def test_switched_off_it_listens_nowhere(self):
        endpoint = link.Link({"enabled": False, "port": free_port()}).start()
        self.addCleanup(endpoint.stop)
        self.assertIsNone(endpoint._server)
        self.assertIsNone(endpoint.error)

    def test_a_port_already_taken_is_a_state_not_a_crash(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        endpoint = link.Link({"port": taken.getsockname()[1]}).start()
        self.addCleanup(endpoint.stop)
        self.assertIsNone(endpoint._server)
        self.assertIn("Address already in use", endpoint.error)

    def test_stopping_something_that_never_started_is_fine(self):
        self.assertIsNone(link.Link({"enabled": False}).stop()._server)


class FindChannelTests(unittest.TestCase):
    """What a name on a command line is allowed to mean."""

    CHANNELS = [
        {"id": "10", "name": "random"},
        {"id": "11", "name": "General"},
        {"id": "12", "name": "Discord Dev"},
        {"id": "13", "name": "Dev notes"},
        {"id": "14", "name": "announcements"},
        {"id": "15", "name": "random stuff"},
    ]

    def find(self, wanted):
        return daemon._find_channel(self.CHANNELS, wanted)

    def test_an_id_is_taken_as_itself(self):
        self.assertEqual(self.find("12")["id"], "12")

    def test_the_name_matches_whatever_case_it_was_written_in(self):
        self.assertEqual(self.find("general")["id"], "11")
        self.assertEqual(self.find("General")["id"], "11")

    def test_a_prefix_of_one_name_is_enough(self):
        self.assertEqual(self.find("ann")["id"], "14")

    def test_the_last_part_of_a_two_word_name_alone_still_matches(self):
        # The panel only has room for so much of a name, so a channel is
        # also asked for by whatever its name ends with.
        self.assertEqual(self.find("stuff")["id"], "15")
        self.assertEqual(self.find("notes")["id"], "13")

    def test_two_channels_sharing_a_word_are_refused_with_the_names(self):
        answer = self.find("d")           # Discord Dev, Dev notes, ...
        self.assertIsInstance(answer, str)
        self.assertIn("matches", answer)
        self.assertIn("Discord Dev", answer)
        self.assertIn("Dev notes", answer)

    def test_nothing_that_fits_is_refused_rather_than_guessed(self):
        answer = self.find("telegram")
        self.assertIsInstance(answer, str)
        self.assertIn("telegram", answer)
        self.assertIn("g510 discord channels", answer)

    def test_nothing_at_all_says_what_to_do_instead(self):
        self.assertIn("g510 discord channels", self.find(""))

    def test_every_channel_here_is_reachable_by_name(self):
        for entry in self.CHANNELS:
            with self.subTest(name=entry["name"]):
                self.assertEqual(self.find(entry["name"])["id"], entry["id"])


class MessageReferenceTests(unittest.TestCase):
    """The handle `history` prints, and what `reply` makes of it.

    The two halves of one idea: a line on a terminal has to name a
    message in a way a person can read back, and the plugin needs the
    nineteen digit id that message actually has.
    """

    HELD = [{"id": "111111", "author": "mira", "text": "one", "time": "10:30"},
            {"id": "222222", "author": "win", "text": "two", "time": "10:31"},
            {"id": "333333", "author": "niko", "text": "three",
             "time": "10:32"}]

    def test_a_reference_is_the_tail_of_the_id(self):
        self.assertEqual(link.reference({"id": "1423456789012345678"}),
                         "#" + "1423456789012345678"[-6:])

    def test_a_message_with_no_id_is_not_named_after_anything(self):
        self.assertEqual(link.reference({}), "#")

    def test_last_is_the_newest_message_held(self):
        for word in ("", "last", "newest", "  LAST  "):
            self.assertEqual(link.resolve(self.HELD, word),
                             ("333333", None))

    def test_nothing_held_says_so_rather_than_replying_to_nobody(self):
        identifier, error = link.resolve([], "last")
        self.assertEqual(identifier, "")
        self.assertIn("nothing to reply to", error)

    def test_a_whole_id_is_taken_as_itself(self):
        self.assertEqual(link.resolve(self.HELD, "222222"), ("222222", None))

    def test_a_reference_is_looked_up_in_what_is_held(self):
        self.assertEqual(link.resolve(self.HELD, "#111111"), ("111111", None))

    def test_a_short_tail_is_enough_when_it_fits_one_message(self):
        self.assertEqual(link.resolve(self.HELD, "111"), ("111111", None))

    def test_a_tail_two_messages_share_is_refused_with_a_count(self):
        shared = [{"id": "1000041"}, {"id": "2000041"}, {"id": "777777"}]
        identifier, error = link.resolve(shared, "#000041")
        self.assertEqual(identifier, "")
        self.assertIn("2 messages", error)

    def test_nothing_matching_points_at_history_and_older(self):
        identifier, error = link.resolve(self.HELD, "#999999")
        self.assertEqual(identifier, "")
        self.assertIn("history", error)
        self.assertIn("older", error)


class DiscordHistoryLineTests(unittest.TestCase):
    """What `g510 discord history` prints - and the reference it hands out.

    Reading a conversation and answering one particular message out of it
    is the same gesture from the shell: the listing has to carry whatever
    `reply` will accept, or the second half is unreachable.
    """

    def setUp(self):
        self.out = io.StringIO()
        patcher = mock.patch("sys.stdout", new=self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, messages, has_more=False):
        reply = {"ok": True, "connected": True,
                 "state": {"channel": {"name": "general"},
                           "messages": messages, "has_more": has_more}}
        with mock.patch.object(cli, "_client", return_value=reply):
            cli._discord_client("history", [])
        return self.out.getvalue()

    def test_every_line_carries_the_reference_of_its_message(self):
        out = self.read([{"id": "111111", "author": "mira", "text": "hi",
                          "time": "10:31"},
                         {"id": "222222", "author": "win", "text": "later",
                          "time": "10:32"}])
        self.assertIn("    #111111 10:31", out)
        self.assertIn("    #222222 10:32", out)

    def test_the_listing_ends_with_how_to_answer_one(self):
        out = self.read([{"id": "111111", "author": "mira", "text": "hi",
                          "time": "10:31"}])
        self.assertIn("reply to one: g510 discord reply #111111 <text>", out)

    def test_the_references_it_prints_are_the_ones_reply_accepts(self):
        message = {"id": "1423456789012345678", "author": "mira",
                   "text": "hi", "time": "10:31"}
        identifier, problem = link.resolve([message],
                                           link.reference(message))
        self.assertIsNone(problem)
        self.assertEqual(identifier, message["id"])

    def test_a_listing_with_nothing_read_yet_says_which_command_to_use(self):
        self.assertIn("nothing read yet", self.read([]))


class ScreenDiscordTests(unittest.TestCase):
    """The three lines the panel gives the conversation."""

    def setUp(self):
        self.canvas = StubCanvas()

    def draw(self, state):
        self.canvas = StubCanvas()
        screens.screen_discord(self.canvas, {"discord": state})
        return self.canvas.drawn

    @staticmethod
    def pushed(**parts):
        state = {"channel": None, "channels": [], "messages": [],
                 "has_more": False}
        state.update(parts)
        return state

    def test_it_is_a_screen_that_can_be_asked_for(self):
        # In SCREENS so `g510 lcd discord` and three presses of L3 can name
        # it - but not in the default cycle, so it never turns up on its own.
        self.assertIn("discord", screens.SCREENS)
        self.assertNotIn("discord", config.DEFAULTS["lcd"]["cycle"])

    def test_nothing_pushed_yet_says_who_it_is_waiting_for(self):
        drawn = self.draw({})
        self.assertIn("DISCORD", drawn)
        self.assertIn("waiting for Vesktop", drawn)

    def test_a_channel_with_nothing_loaded_offers_the_list(self):
        drawn = self.draw(self.pushed(
            channel={"id": "1", "name": "general"}, channels=[]))
        self.assertTrue(drawn[2].startswith("connected"))

    def test_the_newest_message_is_shown_with_its_channel(self):
        drawn = self.draw(self.pushed(
            channel={"id": "1", "name": "general"},
            channels=[{"id": "1", "name": "general", "current": True}],
            messages=[{"id": "9", "author": "mira", "text": "afternoon",
                       "time": "10:31", "self": False}]))
        self.assertIn("general", drawn)
        self.assertIn("mira", drawn)
        self.assertIn("afternoon", drawn)

    def test_older_history_being_available_is_marked(self):
        drawn = self.draw(self.pushed(
            channel={"id": "1", "name": "general"},
            messages=[{"id": "9", "author": "mira", "text": "hi",
                       "time": "10:31"}],
            has_more=True))
        self.assertTrue(any(text.startswith("<") for text in drawn))

    def test_the_three_channels_that_fit_are_marked(self):
        drawn = self.draw(self.pushed(channels=[
            {"id": "1", "name": "here", "current": True},
            {"id": "2", "name": "loud", "mentions": 3},
            {"id": "3", "name": "quiet"},
            {"id": "4", "name": "unread", "unread": True},
        ]))
        self.assertIn(">here", drawn)
        self.assertIn("@loud", drawn)
        self.assertIn(" quiet", drawn)
        self.assertNotIn("*unread", drawn)      # only three rows fit

    def test_the_top_line_says_which_server_these_channels_are_in(self):
        # The rows carry the channel alone - a "Server #channel" prefix on
        # each one was trimmed to the same few characters by every row of
        # a long-named server, so the list looked like it never scrolled.
        drawn = self.draw(self.pushed(
            list="9",
            lists=[{"id": "@me", "name": "DMs", "current": False},
                   {"id": "9", "name": "Game", "current": True}],
            channels=[{"id": "1", "name": "here", "current": True}]))
        self.assertTrue(drawn[0].startswith("Game"), drawn[0])
        self.assertIn(">here", drawn)

    def test_a_list_nobody_named_says_what_it_is_anyway(self):
        drawn = self.draw(self.pushed(
            list="9", channels=[{"id": "1", "name": "here"}]))
        self.assertEqual(drawn[0], "DISCORD")

    def test_it_renders_to_a_whole_frame(self):
        rows = screens.render("discord", {})
        self.assertEqual(len(rows), device.LCD_HEIGHT)
        self.assertTrue(all(len(row) == device.LCD_WIDTH for row in rows))

    def test_another_screens_do_not_care_that_the_state_is_missing(self):
        # `state["discord"]` is set for every screen, and every other screen
        # ignores it - which the shared render path is what proves.
        for name in ("status", "clock", "timers"):
            with self.subTest(name=name):
                rows = screens.render(name, {"discord": {}})
                self.assertEqual(len(rows), device.LCD_HEIGHT)


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


class IdleReader:
    """A machine nobody has touched, so the rules want the clock."""

    def read(self, block, rules=None, wait=False):
        return {"idle": 400.0, "playing": False, "game": False,
                "fullscreen": False, "app": "Finder",
                "bundle": "com.apple.finder", "running": (),
                "commands": {}}


class FakeLink:
    """A link that answers without listening: what the agent is given."""

    def __init__(self, status):
        self._status = status
        self.sent = []
        self.stopped = False

    def status(self):
        return self._status

    def state(self):
        return self._status.get("state") or {}

    def send(self, payload):
        self.sent.append(payload)
        return 99

    def stop(self):
        self.stopped = True

    @property
    def port(self):
        return self._status.get("port")


class AgentTests(unittest.TestCase):
    """Three presses of L3, and what `g510 discord ...` asks the agent.

    Redirected at a temporary config, so the real one is never touched.
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
            patcher = mock.patch.object(daemon.Daemon, target,
                                        return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()
        self.agent.autoscreen = autoscreen.AutoScreen(IdleReader())

    def press(self, name, now, times=1):
        for index in range(times):
            self.agent.note_gesture(name, now=now + index * 0.1)

    def screen(self):
        return (self.agent.config.get("lcd") or {}).get("screen")

    def connected(self, **state):
        """A link whose plugin is up, with the state it pushed."""
        base = {"connected": True, "error": None, "port": 0, "seen": 0.1,
                "waiting": 0,
                "state": {"channel": {"id": "1", "name": "general"},
                          "channels": [{"id": "1", "name": "general"},
                                       {"id": "2", "name": "random"}],
                          "messages": [{"id": "41", "author": "mira",
                                        "text": "hi", "time": "10:31"}],
                          "ack": None}}
        base["state"].update(state)
        return FakeLink(base)

    # -- the gesture

    def test_three_presses_of_L3_put_the_discord_screen_up(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L3", 100.0, times=3)
        self.agent.refresh_gestures(100.2 + daemon.GESTURE_HOLD)
        self.assertEqual(fired, [])              # the triple, not three singles
        self.assertEqual(self.screen(), "discord")
        self.assertTrue(self.agent.config["lcd"]["enabled"])
        self.assertEqual(self.agent.last_lcd, 0.0)   # draw it at once

    def test_it_picks_the_screen_by_hand_so_autoscreen_keeps_its_distance(self):
        # The rules have been running and have chosen something, which is
        # the state the real agent is always in when a gesture arrives.
        self.assertIsNone(self.agent.refresh_autoscreen(190.0))
        self.press("L3", 200.0, times=3)
        self.assertEqual(self.screen(), "discord")
        self.assertIsNone(self.agent.refresh_autoscreen(210.0))
        self.assertEqual(self.screen(), "discord")
        self.assertGreater(self.agent.autoscreen.until, 210.0)
        # Still there a minute later, well inside the hands-off window.
        self.agent.refresh_autoscreen(250.0)
        self.assertEqual(self.screen(), "discord")

    def test_a_single_press_of_L3_is_still_ordinary(self):
        fired = []
        self.agent.fire_lcd_key = lambda name: fired.append(name)
        self.press("L3", 100.0)
        self.agent.refresh_gestures(100.0 + daemon.GESTURE_HOLD)
        self.assertEqual(fired, ["L3"])
        self.assertNotEqual(self.screen(), "discord")

    # -- the server the agent keeps for the plugin

    def test_the_server_follows_the_config(self):
        port = free_port()
        self.agent.config["vencord"] = {"enabled": True, "port": port}
        self.agent.sync_link()
        self.addCleanup(lambda: self.agent.link and self.agent.link.stop())
        self.assertIsNotNone(self.agent.link)
        self.assertEqual(self.agent.link.port, port)
        running = self.agent.link

        # The same port again: nothing to restart, same object back.
        self.agent.config["vencord"] = {"enabled": True, "port": port}
        self.agent.sync_link()
        self.assertIs(self.agent.link, running)

        # A different port: it has to move, and let go of the old one.
        moved = free_port()
        self.agent.config["vencord"] = {"enabled": True, "port": moved}
        self.agent.sync_link()
        self.addCleanup(lambda: self.agent.link and self.agent.link.stop())
        self.assertEqual(self.agent.link.port, moved)
        self.assertIsNone(running._server)

        # Off: no server at all.
        self.agent.config["vencord"] = {"enabled": False, "port": moved}
        self.agent.sync_link()
        self.assertIsNone(self.agent.link)

    def test_the_screen_draws_what_the_plugin_last_pushed(self):
        self.agent.link = self.connected(channels=[])
        self.agent.config.setdefault("lcd", {})["screen"] = "discord"
        seen = {}

        def render(name, state):
            seen["name"] = name
            seen["state"] = dict(state)
            return b"\x00" * 4096

        with mock.patch.object(screens, "render", render), \
                mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = 999.0
            self.agent.refresh_lcd(1000.0)
        self.assertEqual(seen["name"], "discord")
        self.assertEqual(seen["state"]["discord"]["channel"]["name"],
                         "general")

    def test_with_no_server_the_screen_says_so_rather_than_breaking(self):
        self.agent.link = None
        self.agent.config.setdefault("lcd", {})["screen"] = "discord"
        with mock.patch.object(self.agent, "refresh_autoscreen"):
            self.agent.last_lcd = 999.0
            self.agent.refresh_lcd(1000.0)      # must not raise

    # -- what `g510 discord ...` asks for

    def test_state_is_readable_with_the_plugin_up(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "state"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["connected"])
        self.assertEqual(reply["state"]["channel"]["name"], "general")

    def test_a_command_while_the_plugin_is_off_says_which_way_round(self):
        self.agent.link = None
        reply = self.agent.vencord_command({"action": "say", "text": "hi"})
        self.assertFalse(reply["ok"])
        self.assertIn("Vencord", reply["error"])

    def test_a_command_while_the_plugin_is_silent_says_so(self):
        self.agent.link = FakeLink({"connected": False, "state": {},
                                    "waiting": 0, "port": 0, "seen": None,
                                    "error": None})
        reply = self.agent.vencord_command({"action": "older"})
        self.assertFalse(reply["ok"])
        self.assertIn("Vesktop", reply["error"])

    def test_open_resolves_a_name_to_an_id(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "open", "name": "random"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.agent.link.sent,
                         [{"cmd": "open", "id": "2"}])
        self.assertEqual(reply["channel"]["id"], "2")

    def test_open_with_an_ambiguous_name_sends_nothing(self):
        self.agent.link = self.connected(channels=[
            {"id": "1", "name": "a"}, {"id": "2", "name": "b"}])
        reply = self.agent.vencord_command({"action": "open", "name": ""})
        self.assertFalse(reply["ok"])
        self.assertEqual(self.agent.link.sent, [])

    def test_open_can_ask_for_the_dm_list_rather_than_a_channel(self):
        # "friends" is a route rather than a channel, so it never reaches
        # the name matching - it is what the panel shows as nothing open.
        for wanted in ("friends", "@me", "dms"):
            with self.subTest(wanted=wanted):
                self.agent.link = self.connected()
                reply = self.agent.vencord_command({"action": "open",
                                                    "name": wanted})
                self.assertTrue(reply["ok"])
                self.assertEqual(self.agent.link.sent,
                                 [{"cmd": "open", "id": "@me"}])
                self.assertEqual(reply["channel"]["name"], "friends")

    def test_older_asks_for_a_page_at_a_time(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "older"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.agent.link.sent[0],
                         {"cmd": "older", "limit": 30})
        self.agent.vencord_command({"action": "older", "limit": 5})
        self.assertEqual(self.agent.link.sent[1]["limit"], 5)

    def test_say_needs_something_to_say(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "say", "text": "   "})
        self.assertFalse(reply["ok"])
        self.assertEqual(self.agent.link.sent, [])
        reply = self.agent.vencord_command({"action": "say", "text": "hi"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.agent.link.sent, [{"cmd": "say", "text": "hi"}])

    def test_reply_defaults_to_the_newest_message(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "reply", "id": "last",
                                            "text": "no"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.agent.link.sent,
                         [{"cmd": "reply", "id": "41", "text": "no"}])

    def test_reply_with_nothing_to_reply_to_sends_nothing(self):
        self.agent.link = self.connected(messages=[])
        reply = self.agent.vencord_command({"action": "reply", "id": "last",
                                            "text": "no"})
        self.assertFalse(reply["ok"])
        self.assertEqual(self.agent.link.sent, [])

    def test_reply_resolves_the_reference_history_prints(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command(
            {"action": "reply", "id": link.reference({"id": "41"}),
             "text": "no"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.agent.link.sent,
                         [{"cmd": "reply", "id": "41", "text": "no"}])

    def test_reply_to_a_reference_that_is_not_held_sends_nothing(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "reply",
                                            "id": "#999999", "text": "no"})
        self.assertFalse(reply["ok"])
        self.assertIn("history", reply["error"])
        self.assertEqual(self.agent.link.sent, [])

    def test_reply_to_a_reference_two_messages_share_is_refused(self):
        self.agent.link = self.connected(messages=[
            {"id": "1000041", "author": "mira", "text": "one", "time": "10:30"},
            {"id": "2000041", "author": "win", "text": "two", "time": "10:31"}])
        reply = self.agent.vencord_command({"action": "reply",
                                            "id": "#000041", "text": "no"})
        self.assertFalse(reply["ok"])
        self.assertIn("2 messages", reply["error"])
        self.assertEqual(self.agent.link.sent, [])

    def test_an_action_nobody_wrote_is_refused_with_the_list(self):
        self.agent.link = self.connected()
        reply = self.agent.vencord_command({"action": "delete-everything"})
        self.assertFalse(reply["ok"])
        self.assertIn("state open older say reply", reply["error"])
        self.assertEqual(self.agent.link.sent, [])


if __name__ == "__main__":
    unittest.main()
