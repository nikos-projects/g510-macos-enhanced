"""The public API (P8): the HTTP face, and the screen registry behind it.

Two halves, and the reason both are tested is that either could quietly
stop being true of the rest of the agent. The registry writes into
`screens.SCREENS`, so an outside program's screen is a screen *everywhere*
at once - that dict is what the cycle, the menu, `g510 lcd`, the Settings
pickers and the autoscreen rules all read, and a test that only checked
the registry would not notice if it stopped being the same dict. The HTTP
face calls the same `Daemon.handle_request` the control socket calls, so
a test that drives it over HTTP is checking the two faces agree.

Everything is kept off the real machine the way the other agent tests do
it: the config at a temporary directory, the keyboard a mock, the audio
tap, chat reader and Discord link never opened, and every server bound to
a port nothing else has. 7420 belongs to the running agent, so no test
goes near it.
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import api
import config
import daemon
import extern
import screens


def free_port():
    """A port nobody has - released again before anybody can take it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def call(port, path, payload=None, method=None, timeout=5.0):
    """One request at the API, returning (code, headers, body dict)."""
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


class StubCanvas:
    """Eight pixels a character, and a list of everything it was given."""

    def __init__(self, per_char=8):
        self.per_char = per_char
        self.drawn = []
        self.shapes = []

    def measure(self, string, size=12.0, font=None):
        return len(string) * self.per_char

    def text(self, string, x, y, size=12.0, font=None):
        self.drawn.append(str(string))

    def centered(self, string, y, size=12.0, font=None):
        self.drawn.append(str(string))

    def rect(self, x, y, w, h, filled=True):
        self.shapes.append(("rect", x, y, w, h, filled))

    def bar(self, x, y, w, h, fraction=0.0):
        self.shapes.append(("bar", x, y, w, h, fraction))


# -- the registry ----------------------------------------------------------

class OutsideScreens(unittest.TestCase):
    """A program registers a screen, pushes to it, and takes it away."""

    def setUp(self):
        extern.reset()
        self.addCleanup(extern.reset)

    def live(self):
        return set(screens.SCREENS) - set(self.built_in)

    built_in = tuple(screens.SCREENS)

    def test_registering_makes_it_a_screen_everywhere(self):
        # The one thing this module exists to do. `SCREENS` is read by
        # the cycle, the menu, `g510 lcd`, the Settings pickers and the
        # autoscreen rules - one dict, so one write makes it real in all
        # of them, and this asserts it is *the same dict* rather than a
        # copy this module keeps in step by hand.
        self.assertNotIn("weather", screens.SCREENS)
        extern.register("weather", title="outside")
        self.assertIn("weather", screens.SCREENS)
        self.assertEqual(self.live(), {"weather"})
        # ...and the menu, which takes its names straight from it.
        self.assertIn("weather", tuple(screens.SCREENS))

    def test_it_draws_what_the_program_pushed(self):
        name = extern.register("weather")
        self.assertTrue(extern.frame("weather", name,
                                     [{"op": "text", "s": "12c",
                                       "x": 4, "y": 20, "size": 14}]))
        canvas = StubCanvas()
        screens.SCREENS["weather"](canvas, {})
        self.assertEqual(canvas.drawn, ["12c"])

    def test_every_operation_it_was_handed_lands_on_the_canvas(self):
        name = extern.register("shapes")
        extern.frame("shapes", name, [
            {"op": "centered", "s": "TOP", "y": 2, "size": 10},
            {"op": "rect", "x": 4, "y": 4, "w": 20, "h": 6},
            {"op": "bar", "x": 4, "y": 30, "w": 100, "h": 8,
             "fraction": 0.5}])
        canvas = StubCanvas()
        screens.SCREENS["shapes"](canvas, {})
        self.assertEqual(canvas.drawn, ["TOP"])
        self.assertEqual([shape[0] for shape in canvas.shapes], ["rect", "bar"])

    def test_a_screen_nobody_has_pushed_to_says_so(self):
        # Never a blank panel: a program that registered and then forgot
        # to draw leaves a screen that explains itself, which is a
        # better hour than one spent wondering whether it is running.
        extern.register("halfdone", title="HALFDONE")
        canvas = StubCanvas()
        screens.SCREENS["halfdone"](canvas, {})
        self.assertTrue(canvas.drawn)
        self.assertIn("HALFDONE", canvas.drawn)
        self.assertIn("waiting for its program", canvas.drawn)

    def test_two_programs_cannot_both_own_a_name(self):
        # The handle is not a secret - anything on this machine can read
        # everything - it is here so two programs that both want "weather"
        # do not silently draw over each other.
        extern.register("weather")
        with self.assertRaises(ValueError):
            extern.register("weather")

    def test_a_name_is_free_again_once_its_program_has_gone(self):
        # The same thing, after the first program stopped pushing. A
        # crashed program cannot unregister, which is the whole reason
        # there is an expiry at all.
        now = time.time()
        extern.register("weather", now=now)
        extern.sweep(now + extern.TTL - 1)
        self.assertIn("weather", screens.SCREENS)
        extern.sweep(now + extern.TTL + 1)
        self.assertNotIn("weather", screens.SCREENS)
        # ...and the second program gets it without an argument.
        self.assertTrue(extern.register("weather", now=now + extern.TTL + 2))

    def test_a_program_that_goes_on_pushing_keeps_its_screen(self):
        now = time.time()
        name = extern.register("weather", now=now)
        for moment in range(1, 6):
            later = now + moment * extern.TTL / 4
            self.assertTrue(extern.frame("weather", name, [], now=later))
        gone = extern.sweep(now + extern.TTL + 1)
        self.assertEqual(gone, [])
        self.assertIn("weather", screens.SCREENS)

    def test_a_heartbeat_is_enough_to_keep_it_up(self):
        # A program computing rather than drawing should not lose its
        # screen for the length of one busy stretch.
        now = time.time()
        extern.register("renderfarm", now=now)
        self.assertTrue(extern.alive("renderfarm", now + extern.TTL - 1))
        self.assertEqual(extern.sweep(now + extern.TTL), [])
        self.assertFalse(extern.alive("weather", now))

    def test_only_the_owner_can_draw_on_it(self):
        name = extern.register("weather")
        self.assertFalse(extern.frame("weather", "not-the-handle", []))
        self.assertTrue(extern.frame("weather", name,
                                     [{"op": "text", "s": "hi", "x": 0,
                                       "y": 0, "size": 10}]))
        canvas = StubCanvas()
        screens.SCREENS["weather"](canvas, {})
        self.assertEqual(canvas.drawn, ["hi"])

    def test_only_the_owner_can_take_it_down(self):
        name = extern.register("weather")
        self.assertFalse(extern.drop("weather", "not-the-handle"))
        self.assertIn("weather", screens.SCREENS)
        # With no handle at all the caller says it is sure, which is what
        # the agent itself does when a program has clearly gone.
        self.assertTrue(extern.drop("weather"))
        self.assertNotIn("weather", screens.SCREENS)

    def test_a_name_has_to_be_a_name(self):
        for bad in ("", "   ", "two words", "a/b", "a!!b"):
            with self.assertRaises(ValueError):
                extern.register(bad)
        self.assertEqual(extern.names(), [])
        # A long name is shortened rather than refused: the panel is 160
        # pixels wide, so anything past about twenty characters is the
        # program's problem anyway, and taking the name anyway is kinder
        # than an error the user has to go and edit.
        self.assertTrue(extern.register("a" * 40))
        self.assertEqual(extern.names(), ["a" * 24])

    def test_an_operation_it_does_not_have_is_refused_with_a_reason(self):
        with self.assertRaises(ValueError) as caught:
            extern.clean([{"op": "photograph", "s": "no"}])
        self.assertIn("photograph", str(caught.exception))
        with self.assertRaises(ValueError):
            extern.clean("not a list")
        with self.assertRaises(ValueError):
            extern.clean([{"op": "text"}] * (extern.MAX_OPS + 1))

    def test_a_frame_that_reaches_past_the_panel_is_clamped_not_refused(self):
        # Clamped rather than rejected: a program that draws a pixel
        # past the edge means its own bug, and the rest of its frame is
        # still worth showing.
        cleaned = extern.clean([{"op": "rect", "x": 9999, "y": -5, "w": 400,
                                 "h": 99}])
        self.assertEqual(cleaned, [{"op": "rect", "x": 160.0, "y": 0.0,
                                    "w": 160.0, "h": 43.0, "fill": True}])

    def test_a_one_shot_draw_does_not_claim_a_screen_name(self):
        # A toast should not have to fight over a name, and should not
        # turn up in the cycle afterwards: it is on the panel, and it is
        # not a screen anybody chose.
        extern.draw_once([{"op": "text", "s": "hello", "x": 2, "y": 2}])
        self.assertNotIn(extern.RESERVED, screens.SCREENS)
        self.assertIn(extern.RESERVED, screens.SCREENS_INTERNAL)
        canvas = StubCanvas()
        screens.SCREENS_INTERNAL[extern.RESERVED](canvas, {})
        self.assertEqual(canvas.drawn, ["hello"])

    def test_drawing_twice_replaces_rather_than_adds(self):
        name = extern.register("ticker")
        extern.frame("ticker", name, [{"op": "text", "s": "one", "x": 0,
                                       "y": 0, "size": 10}])
        extern.frame("ticker", name, [{"op": "text", "s": "two", "x": 0,
                                       "y": 0, "size": 10}])
        canvas = StubCanvas()
        screens.SCREENS["ticker"](canvas, {})
        self.assertEqual(canvas.drawn, ["two"])

    def test_reset_forgets_everything(self):
        extern.register("weather")
        extern.draw_once([])
        extern.reset()
        self.assertEqual(extern.names(), [])
        self.assertNotIn("weather", screens.SCREENS)
        self.assertNotIn(extern.RESERVED, screens.SCREENS_INTERNAL)

    def test_describe_says_what_is_up_and_for_how_long(self):
        extern.register("weather", title="outside")
        self.assertEqual(extern.describe()["weather"]["title"], "outside")
        # "How long since anything was pushed" is the question a user
        # has about a screen they did not put there.
        self.assertLess(extern.describe()["weather"]["at"], 2.0)


# -- the event hub ---------------------------------------------------------

class FanOut(unittest.TestCase):
    """Any number of programs watching, each at its own speed."""

    def test_every_subscriber_is_handed_every_event(self):
        hub = api.Hub()
        first, second = hub.subscribe(), hub.subscribe()
        hub.publish("beat")
        hub.publish("app", {"app": "Safari"})
        for item in (first, second):
            self.assertEqual(json.loads(item["queue"].get_nowait())["event"],
                             "beat")
            event = json.loads(item["queue"].get_nowait())
            self.assertEqual((event["event"], event["data"]["app"]),
                             ("app", "Safari"))

    def test_a_client_that_falls_behind_loses_its_oldest_not_the_newest(self):
        # A slow program is slow on its own. What it must not become is
        # the reason the agent grows without bound.
        hub = api.Hub(keep=2)
        item = hub.subscribe()
        for number in range(5):
            hub.publish("beat", {"n": number})
        self.assertEqual(item["queue"].qsize(), 2)
        self.assertEqual(json.loads(item["queue"].get_nowait())["data"]["n"], 3)
        self.assertEqual(json.loads(item["queue"].get_nowait())["data"]["n"], 4)

    def test_unsubscribing_stops_the_flow(self):
        hub = api.Hub()
        item = hub.subscribe()
        hub.unsubscribe(item)
        hub.publish("beat")
        self.assertTrue(item["queue"].empty())

    def test_events_are_numbered_so_a_client_can_tell_gap_from_start(self):
        hub = api.Hub()
        item = hub.subscribe()
        hub.publish("beat")
        hub.publish("beat")
        self.assertEqual([json.loads(item["queue"].get_nowait())["n"]
                          for _ in (0, 1)], [1, 2])
        self.assertEqual(hub.since, 2)


# -- an agent with an API on it --------------------------------------------

class ApiCase(unittest.TestCase):
    """A real agent, a keyboard that answers, and an API on a free port."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        paths = mock.patch.multiple(
            config, CONFIG_DIR=directory,
            CONFIG_PATH=os.path.join(directory, "config.json"))
        paths.start()
        self.addCleanup(paths.stop)
        extern.reset()
        self.addCleanup(extern.reset)
        patcher = mock.patch.object(daemon, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The three things a reload opens that must stay shut here: an
        # audio tap, a chat reader and a server on 47832 that the
        # running agent's own link already holds.
        for target in ("sync_watcher", "sync_link", "sync_beat"):
            patcher = mock.patch.object(daemon.Daemon, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = daemon.Daemon()
        self.agent.keyboard = mock.Mock()
        self.agent.keyboard.get_backlight.return_value = (10, 20, 30)
        self.port = free_port()
        self.api = api.Api(self.agent, {"enabled": True,
                                        "port": self.port}).start()
        self.addCleanup(self.api.stop)
        self.agent.api = self.api
        self.assertIsNone(self.api.error)

    def show(self, name):
        """Put a screen on the display the way a reload would."""
        self.agent.config.setdefault("lcd", {})["screen"] = name


class TheApiOnTheWire(ApiCase):
    """Every route, reached over HTTP the way a stranger would."""

    def test_it_answers_at_v1_and_says_what_it_is(self):
        # Discovery is most of what separates an interface from a private
        # arrangement: a program that has never read this tree should be
        # able to find out what exists.
        code, headers, body = call(self.port, "/")
        self.assertEqual(code, 200)
        self.assertEqual(body, api.INDEX)
        self.assertIn("GET /v1/state", body["routes"])
        self.assertIn("POST /v1/screens", body["routes"])
        self.assertIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(call(self.port, "/v1")[2], api.INDEX)

    def test_it_binds_without_hanging(self):
        # `HTTPServer.server_bind` runs the address through getfqdn, and
        # here that reverse lookup stalls for half a minute - half a
        # minute of every agent start, for a server only ever reached by
        # an address it already has.
        start = time.time()
        other = api.Api(self.agent, {"enabled": True,
                                     "port": free_port()}).start()
        other.stop()
        self.assertLess(time.time() - start, 3.0)

    def test_state_reports_what_the_agent_holds(self):
        code, _headers, body = call(self.port, "/v1/state")
        self.assertEqual(code, 200)
        state = body["state"]
        self.assertEqual(state["backlight"], [10, 20, 30])
        self.assertEqual(state["lcd"]["screen"], "status")
        self.assertIn("clock", state["screens"])
        self.assertFalse(state["beat"]["listening"])
        self.assertFalse(state["menu"])
        self.assertFalse(state["frozen"])

    def test_a_partial_config_write_leaves_everything_else_alone(self):
        # A program setting one number should not have to know what every
        # other setting is - the same way a partial config file still
        # gets the defaults.
        code, _headers, body = call(self.port, "/v1/config",
                                    {"brightness": 40}, method="PUT")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.agent.config["brightness"], 40)
        self.assertEqual(config.load()["brightness"], 40)
        # Untouched: whatever the file already said about something else.
        self.assertEqual(self.agent.config["lcd"]["cycle"],
                         config.load()["lcd"]["cycle"])

    def test_a_command_reaches_the_same_dispatcher_as_the_socket(self):
        code, _headers, body = call(self.port, "/v1/command",
                                    {"cmd": "get_backlight"})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"ok": True, "rgb": [10, 20, 30]})

    def test_a_command_the_agent_refuses_is_an_error_not_a_crash(self):
        # 200 with `ok` false, the same as the control socket answers:
        # the two faces must not disagree about what a reply is.
        code, _headers, body = call(self.port, "/v1/command",
                                    {"cmd": "teleport"})
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertIn("teleport", body["error"])

    def test_a_body_that_is_not_json_is_refused(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/config", data=b"{nope",
            method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5.0)
        error = caught.exception
        try:
            self.assertEqual(error.code, 400)
            self.assertIn("error", json.loads(error.read()))
        finally:
            error.close()

    def test_an_unknown_path_says_so(self):
        code, _headers, body = call(self.port, "/v1/teleport")
        self.assertEqual(code, 400)
        self.assertIn("/v1", body["error"])

    def test_a_browser_can_ask_what_it_may_do(self):
        code, headers, _body = call(self.port, "/v1/config", method="OPTIONS")
        self.assertEqual(code, 204)
        self.assertIn("PUT", headers["Access-Control-Allow-Methods"])

    def test_it_can_be_switched_off_and_back_on(self):
        self.api.stop()
        with self.assertRaises(urllib.error.URLError):
            call(self.port, "/v1")
        self.api = api.Api(self.agent, {"enabled": True,
                                        "port": self.port}).start()
        self.assertEqual(call(self.port, "/v1")[0], 200)


class ScreensOverHttp(ApiCase):
    """The half of P8 a program actually writes to."""

    def register(self, name="weather", title="outside"):
        code, _headers, body = call(self.port, "/v1/screens",
                                    {"name": name, "title": title},
                                    method="POST")
        return code, body

    def test_a_program_can_register_a_screen_and_it_becomes_selectable(self):
        code, body = self.register()
        self.assertEqual(code, 201)
        self.assertTrue(body["handle"])
        self.assertIn("weather", screens.SCREENS)
        # Listed, so a user can see what has appeared on their panel.
        code, _headers, listing = call(self.port, "/v1/screens")
        self.assertEqual(code, 200)
        self.assertEqual(listing["outside"]["weather"]["title"], "outside")
        self.assertEqual(listing["live"], ["weather"])

    def test_it_can_then_draw_on_it_and_the_screen_shows_it(self):
        _code, body = self.register()
        code, _headers, drawn = call(
            self.port, "/v1/screens/weather", {"handle": body["handle"],
                                               "ops": [{"op": "text",
                                                        "s": "12c",
                                                        "x": 4, "y": 20,
                                                        "size": 14}]},
            method="PUT")
        self.assertEqual(code, 200)
        self.assertTrue(drawn["ok"])
        canvas = StubCanvas()
        screens.SCREENS["weather"](canvas, {})
        self.assertEqual(canvas.drawn, ["12c"])

    def test_nobody_else_can_draw_on_it_or_take_it_down(self):
        _code, body = self.register()
        code, _headers, drawn = call(
            self.port, "/v1/screens/weather",
            {"handle": "imagine-i-guessed", "ops": []}, method="PUT")
        self.assertEqual(code, 409)
        code, _headers, dropped = call(
            self.port, "/v1/screens/weather",
            {"handle": "imagine-i-guessed"}, method="DELETE")
        self.assertEqual(code, 404)
        self.assertIn("weather", screens.SCREENS)
        # The real handle still works, so the program can clean up.
        code, _headers, dropped = call(
            self.port, "/v1/screens/weather",
            {"handle": body["handle"]}, method="DELETE")
        self.assertEqual((code, dropped["ok"]), (200, True))
        self.assertNotIn("weather", screens.SCREENS)

    def test_two_programs_after_the_same_name_get_an_argument(self):
        _code, _body = self.register()
        code, body = self.register()
        self.assertEqual(code, 409)
        self.assertIn("weather", body["error"])

    def test_a_screen_that_has_gone_can_be_registered_again(self):
        _code, body = self.register()
        call(self.port, "/v1/screens/weather", {"handle": body["handle"]},
             method="DELETE")
        code, again = self.register()
        self.assertEqual(code, 201)
        self.assertNotEqual(again["handle"], body["handle"])

    def test_a_program_that_goes_away_leaves_the_display_somewhere_sane(self):
        # The user is looking at a screen that no longer exists. Sitting
        # on the fallback nobody asked for is the wrong answer; moving
        # on to the next configured screen is the right one.
        _code, body = self.register()
        call(self.port, "/v1/screens/weather/show", method="POST")
        self.assertEqual(self.agent.config["lcd"]["screen"], "weather")
        call(self.port, "/v1/screens/weather", {"handle": body["handle"]},
             method="DELETE")
        self.assertEqual(self.agent.config["lcd"]["screen"], "status")

    def test_asking_what_a_screen_is_does_not_move_the_display(self):
        _code, _body = self.register()
        code, _headers, body = call(self.port, "/v1/screens/weather")
        self.assertEqual(code, 200)
        self.assertEqual(body["name"], "weather")
        self.assertEqual(self.agent.config["lcd"]["screen"], "status")
        self.assertEqual(call(self.port, "/v1/screens/ghost")[0], 404)

    def test_showing_a_screen_that_does_not_exist_is_refused(self):
        # `render` would quietly draw the clock for a name it does not
        # know, so a typo would look like success while showing the
        # wrong thing for ever.
        code, _headers, body = call(self.port, "/v1/screens/ghost/show",
                                    method="POST")
        self.assertEqual(code, 400)
        self.assertIn("ghost", body["error"])
        self.assertNotEqual(self.agent.config["lcd"]["screen"], "ghost")

    def test_a_heartbeat_keeps_a_screen_alive_without_drawing(self):
        _code, _body = self.register("renderfarm")
        code, _headers, body = call(
            self.port, "/v1/screens/renderfarm/heartbeat", method="POST")
        self.assertEqual((code, body["ok"]), (200, True))
        # A heartbeat for a screen that is not up says so rather than
        # quietly creating it: a program heartbeating before it has
        # registered has a bug, and this is where it finds out.
        code, _headers, body = call(
            self.port, "/v1/screens/nothere/heartbeat", method="POST")
        self.assertEqual((code, body["ok"]), (200, False))
        self.assertEqual(extern.names(), ["renderfarm"])

    def test_one_frame_can_be_drawn_without_claiming_a_screen(self):
        code, _headers, body = call(
            self.port, "/v1/lcd", {"ops": [{"op": "centered", "s": "HELLO",
                                            "y": 18, "size": 12}]},
            method="POST")
        self.assertEqual(code, 200)
        self.assertEqual(body["screen"], extern.RESERVED)
        self.assertEqual(self.agent.config["lcd"]["screen"],
                         extern.RESERVED)
        # On the panel, and not on offer: it is not a screen anybody
        # chose, so it stays out of the cycle and out of the menu.
        self.assertNotIn(extern.RESERVED, screens.SCREENS)
        canvas = StubCanvas()
        screens.SCREENS_INTERNAL[extern.RESERVED](canvas, {})
        self.assertEqual(canvas.drawn, ["HELLO"])

    def test_a_frame_that_is_not_drawable_is_refused(self):
        code, _headers, body = call(self.port, "/v1/lcd",
                                    {"ops": [{"op": "photograph"}]},
                                    method="POST")
        self.assertEqual(code, 400)
        self.assertIn("photograph", body["error"])

    def test_a_program_can_leave_the_display_on_its_own_screen(self):
        _code, body = self.register()
        code, _headers, shown = call(
            self.port, "/v1/lcd", {"ops": [], "screen": "weather"},
            method="POST")
        self.assertEqual(code, 200)
        self.assertEqual(shown["screen"], "weather")
        self.assertEqual(self.agent.config["lcd"]["screen"], "weather")


class Subscriber:
    """A program watching: one socket, read the way a client would.

    Kept as its own class rather than a TestCase so two of them can be
    open at once - which is the point of the fan-out.
    """

    def __init__(self, port):
        self.left = b""
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        self.sock.sendall(b"GET /v1/events HTTP/1.1\r\n"
                          b"Host: 127.0.0.1\r\n\r\n")

    def close(self):
        self.sock.close()

    def read(self):
        """One complete event off the stream, or None if it ended.

        Anything arriving after the event asked for is kept: `recv` has
        no idea where an event ends, and two of them in one read is a
        slow test rather than a bug in either side.
        """
        while b"\n\n" not in self.left:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.left += chunk
        head, _sep, self.left = self.left.partition(b"\n\n")
        for line in head.split(b"\n"):
            if line.startswith(b"data: "):
                return json.loads(line[len(b"data: "):])
        return None


class Watching(ApiCase):
    """Server-Sent Events, which is how a program finds out."""

    def subscriber(self):
        reader = Subscriber(self.port)
        self.addCleanup(reader.close)
        return reader

    def test_a_program_is_handed_what_happens_as_it_happens(self):
        sock = self.subscriber()
        self.assertEqual(sock.read()["event"], "hello")
        self.agent.publish("beat", {"amount": 60})
        event = sock.read()
        self.assertEqual(event["event"], "beat")
        self.assertEqual(event["data"], {"amount": 60})
        self.assertEqual(event["n"], 1)

    def test_two_programs_can_watch_at_once(self):
        first, second = self.subscriber(), self.subscriber()
        self.assertEqual(first.read()["event"], "hello")
        self.assertEqual(second.read()["event"], "hello")
        self.agent.publish("app", {"app": "Safari"})
        for sock in (first, second):
            event = sock.read()
            self.assertEqual(event["event"], "app")
            self.assertEqual(event["data"]["app"], "Safari")

    def test_a_key_press_reaches_the_subscribers(self):
        # The socket's own `watch` stream and the API share the fan-out,
        # so a program sees the presses the CLI's watchers see.
        sock = self.subscriber()
        self.assertEqual(sock.read()["event"], "hello")
        self.agent.broadcast({"event": "press", "key": "G1"})
        self.assertEqual(sock.read()["data"]["key"], "G1")


class TheAgentsHalf(ApiCase):
    """Where the agent itself has to do something for the API to work."""

    def test_changing_the_screen_tells_the_subscribers(self):
        reader = Subscriber(self.port)
        self.addCleanup(reader.close)
        self.assertEqual(reader.read()["event"], "hello")
        self.agent.advance_screen(1)
        self.assertEqual(reader.read()["event"], "screen")
        self.assertEqual(self.agent.config["lcd"]["screen"], "clock")

    def test_a_screen_whose_program_stopped_is_taken_down(self):
        now = time.time()
        extern.register("weather", now=now)
        self.show("weather")
        self.agent._swept = 0.0
        self.agent.sweep_screens(now + extern.TTL + 1)
        self.assertNotIn("weather", screens.SCREENS)
        self.assertEqual(self.agent.config["lcd"]["screen"], "status")

    def test_the_sweep_does_not_run_on_every_tick(self):
        # It is the loop that already ticks two hundred and fifty times
        # a second; the throttle is the point.
        extern.register("weather")
        self.agent._swept = time.time()
        self.agent.sweep_screens(time.time() + 0.01)
        self.assertIn("weather", screens.SCREENS)

    def test_shutting_down_takes_the_api_and_the_screens_with_it(self):
        self.agent.api = self.api
        extern.register("weather")
        self.agent.shutdown()
        self.assertIsNone(self.agent.api)
        self.assertNotIn("weather", screens.SCREENS)
        self.assertIsNone(self.api._server)

    def test_a_busy_port_is_a_state_not_a_dead_agent(self):
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        self.addCleanup(held.close)
        block = {"enabled": True, "port": held.getsockname()[1]}
        self.agent.api = api.Api(self.agent, block, log=mock.Mock()).start()
        self.addCleanup(self.agent.api.stop)
        self.assertIsNotNone(self.agent.api.error)
        self.assertTrue(self.agent.api.error.startswith("api:"))

    def test_it_follows_the_switch_and_the_port(self):
        self.agent.config["api"] = {"enabled": True, "port": self.port}
        self.agent.sync_api()
        self.assertIs(self.agent.api, self.api)     # same port: no restart
        self.agent.config["api"] = {"enabled": False}
        self.agent.sync_api()
        self.assertIsNone(self.agent.api)
        self.agent.config["api"] = {"enabled": True, "port": self.port}
        self.agent.sync_api()
        self.assertIsNotNone(self.agent.api)
        self.addCleanup(self.agent.api.stop)


class FromTheCommandLine(ApiCase):
    """`g510 api`, which is how the switch is reached by hand."""

    def api_line(self, *args):
        import cli
        import io
        import contextlib
        reloaded = mock.patch("cli.reload_agent")
        reloaded.start()
        self.addCleanup(reloaded.stop)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_api(list(args))
        return out.getvalue()

    def test_no_arguments_says_where_it_is(self):
        self.assertEqual(self.api_line().splitlines()[0],
                         "api on, http://127.0.0.1:7420/v1")

    def test_the_port_is_written_where_the_agent_will_find_it(self):
        self.api_line("port", "9999")
        self.assertEqual(config.load()["api"]["port"], 9999)
        self.assertIn("http://127.0.0.1:9999/v1", self.api_line())

    def test_it_can_be_switched_off(self):
        self.assertIn("api off", self.api_line("off"))
        self.assertFalse(config.load()["api"]["enabled"])

    def test_a_port_outside_the_range_is_refused(self):
        with self.assertRaises(SystemExit):
            self.api_line("port", "99999")


if __name__ == "__main__":
    unittest.main()
