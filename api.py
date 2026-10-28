"""The API any program on this Mac can hook into - P8.

The agent has always had a protocol: a unix socket, JSON lines, one
request one reply, around nineteen commands. What it never had was an
*interface* - nothing was written down, a client could not ask what
existed, the only events were G-key presses, and the sole way in was a
socket whose path a Node script has to be told about. This is that
interface, and it is deliberately the same dispatcher underneath: the
socket and this both call `Daemon.handle_request`, so the two cannot
drift into disagreeing about what a command means.

Why HTTP, on 127.0.0.1, with no token:

  * **HTTP** because "anyone can hook into" is mostly not written in
    Python. A browser, `curl`, a Raycast extension, Home Assistant and
    an embed in somebody's own app all expect it, and `http.server` is
    in the standard library, so this costs no dependency.
  * **127.0.0.1** because nothing outside this machine can reach it.
  * **No token** because the user decided the machine is the boundary:
    a program that can run here was installed on purpose. What that
    does mean, and it is worth saying out loud rather than burying it,
    is that *any* local process can drive the keyboard, read the config
    - which holds shell commands bound to G-keys - and fire one of
    those bindings. That is the trade. `/v1` says the same thing in a
    comment a client will read.

**What a program can do here:** read every setting and every piece of
live state; write any setting; run any command the CLI can; draw one
frame on the panel; register screens of its own that the user can then
pick from the menu, the cycle or `g510 lcd <name>` for as long as the
program keeps pushing to them (see `extern.py`); and subscribe to
everything that happens, as it happens.

Events are Server-Sent Events rather than a websocket: one direction,
plain text, `~20` lines of server code and no library. A client that
wants to act on the keyboard presses a key; one that wants to *send*
opens a connection and posts. Two transports would be two things to
get wrong.
"""
import json
import queue
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import extern

DEFAULT_PORT = 7420

# How long a client may hold an events connection without saying
# anything, and how often the server says "still here" anyway. The
# keepalive is what stops a firewall or a sleeping laptop from making a
# subscriber silently dead.
PING = 15.0
BODY_LIMIT = 256 * 1024

# Everything the API is, for `GET /v1`. A client should be able to find
# out what exists without being told in advance - that is most of what
# separates an interface from a private arrangement.
INDEX = {
    "name": "g510",
    "what": "the Logitech G510 keyboard this Mac is using",
    "note": "127.0.0.1 only, and unauthenticated: anything on this "
            "machine can read the config and drive the keyboard",
    "routes": {
        "GET /v1": "this list",
        "GET /v1/state": "everything live: backlight, screen, app, timers, "
                         "discord, beat, menu",
        "GET /v1/config": "the whole config file, defaults merged in",
        "PUT /v1/config": "merge a partial config over the current one",
        "POST /v1/command": "{\"cmd\": ...} - every command the CLI sends",
        "POST /v1/lcd": "{\"ops\": [...]} draw one frame, now, and leave "
                        "it up",
        "GET /v1/screens": "the screens that exist, and which are outside",
        "POST /v1/screens": "{\"name\": ...} register your own screen, "
                            "back with a handle",
        "PUT /v1/screens/<name>": "{\"handle\": ..., \"ops\": [...]} draw it",
        "POST /v1/screens/<name>/heartbeat": "keep your screen alive "
                                             "without drawing",
        "POST /v1/screens/<name>/show": "put the display on that screen",
        "DELETE /v1/screens/<name>": "{\"handle\": ...} take it down",
        "GET /v1/events": "Server-Sent Events: everything, as it happens",
    },
    "drawing": {
        "ops": ["text", "centered", "rect", "bar"],
        "panel": "160x43 pixels, 1-bit, origin top-left",
        "example": [
            {"op": "centered", "s": "WEATHER", "y": 2, "size": 10},
            {"op": "text", "s": "12c  rain", "x": 4, "y": 20, "size": 14},
            {"op": "bar", "x": 4, "y": 34, "w": 152, "h": 7,
             "fraction": 0.6},
        ],
    },
}


class _Server(ThreadingHTTPServer):
    """A listening socket that does not ask DNS who 127.0.0.1 is.

    Same reason as `link._Server`: `server_bind` runs the address through
    `getfqdn`, and the reverse lookup stalls here for thirty seconds -
    thirty seconds of startup for a server only ever reached by an
    address it already has.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Hub:
    """Fan-out of events, so any number of programs can watch at once.

    The existing `watch` stream hands one G-key list to one client and
    forgets it. This keeps every subscriber's own queue instead: a slow
    client is slow on its own, and a queue nobody drains is dropped from
    rather than allowed to become the reason the agent runs out of
    memory.
    """

    def __init__(self, keep=256):
        self.keep = keep
        self._lock = threading.Lock()
        self._subs = []
        self._since = 0

    def subscribe(self):
        """A queue this caller will be handed every event on."""
        item = {"queue": queue.Queue(maxsize=self.keep), "dead": False}
        with self._lock:
            self._subs.append(item)
        return item

    def unsubscribe(self, item):
        with self._lock:
            if item in self._subs:
                self._subs.remove(item)

    def publish(self, kind, data=None):
        """Tell every subscriber. Never raises: a caller with a keyboard
        in its hand must not be able to lose it to a broken pipe."""
        with self._lock:
            self._since += 1
            event = {"event": kind, "n": self._since,
                     "at": round(time.time(), 3)}
            if data is not None:
                event["data"] = data
            subs = list(self._subs)
        line = json.dumps(event, default=str)
        for item in subs:
            try:
                item["queue"].put_nowait(line)
            except queue.Full:
                # Falling behind is this client's problem; dropping its
                # oldest event keeps the agent's memory flat regardless
                # of how many programs are watching.
                try:
                    item["queue"].get_nowait()
                    item["queue"].put_nowait(line)
                except queue.Empty:
                    pass
        return event

    @property
    def since(self):
        with self._lock:
            return self._since


class Api:
    """The agent's end: a server, and the event hub behind it."""

    def __init__(self, daemon, block=None, log=None):
        self.daemon = daemon
        self.block = block or {}
        self.log = log or (lambda message: None)
        self.hub = Hub()
        self._server = None
        self._thread = None
        self.error = None

    @property
    def enabled(self):
        return bool(self.block.get("enabled", True))

    @property
    def port(self):
        try:
            return int(self.block.get("port", DEFAULT_PORT) or DEFAULT_PORT)
        except (TypeError, ValueError):
            return DEFAULT_PORT

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Listen, or record why not. Never raises: a busy port is a state."""
        if self._server is not None or not self.enabled:
            return self
        handler = self._handler()
        try:
            server = _Server(("127.0.0.1", self.port), handler)
        except OSError as exc:
            self.error = f"api: {type(exc).__name__}: {exc}"
            self.log(self.error)
            return self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="g510-api", daemon=True)
        self._thread.start()
        self.error = None
        self.log(f"api listening on http://127.0.0.1:{self.port}/v1")
        return self

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        return self

    # -- what a client can ask for -----------------------------------------

    def state(self):
        """Everything live, read the way the rest of the agent reads it.

        Only what the daemon really holds. The printer and gauge screens
        read their own sources when they draw, so there is no cached
        state here to report - a program that wants those asks the
        config, or runs the same command the screen runs.
        """
        import screens
        import timers
        daemon = self.daemon
        settings = daemon.config
        lcd = settings.get("lcd", {})
        block = settings.get("beat") or {}
        return {
            "backlight": list(daemon.keyboard.get_backlight())
                         if daemon.keyboard else None,
            "brightness": settings.get("brightness"),
            "lcd": {"enabled": bool(lcd.get("enabled", True)),
                    "screen": lcd.get("screen", "status"),
                    "cycle": lcd.get("cycle")},
            "app": daemon.current_app,
            "screens": list(screens.SCREENS),
            "beat": {"enabled": bool(block.get("enabled", False)),
                     "listening": daemon.beat is not None},
            "menu": not daemon.menu.closed,
            "note": daemon.note,
            "frozen": bool(getattr(daemon.autoscreen, "frozen", False)),
            "discord": daemon.link.state() if daemon.link else {},
            "timers": timers.STORE.running(),
        }

    def write_config(self, patch):
        """Merge a partial config over the current one, then reload.

        A merge rather than a replace so a program can set one number
        without having to send back every other setting it did not mean
        to touch - which is also how the file itself works, since a
        partial file still gets the defaults.
        """
        if not isinstance(patch, dict):
            raise ValueError("the body must be a JSON object")
        daemon = self.daemon

        def apply(settings):
            merged = config._merge(settings, patch)
            settings.clear()
            settings.update(merged)

        daemon.mutate(apply)
        daemon.reload_config()
        self.hub.publish("config", patch)
        return {"ok": True, "config": patch}

    def show(self, name):
        """Move the display to `name` - what three presses of L3 do.

        Only to a screen that exists. `render` would quietly fall back
        to `status` for a name it does not know, so asking for a typo
        would look like success while showing the clock, and a program
        would go on drawing to a screen nobody is looking at.
        """
        import screens
        name = str(name)
        if name not in screens.SCREENS:
            raise ValueError(f"no screen called {name!r} - try "
                             f"{', '.join(screens.SCREENS)}")
        daemon = self.daemon

        def apply(settings):
            target = settings.setdefault("lcd", {})
            target["screen"], target["enabled"] = name, True

        daemon.mutate(apply)
        daemon.last_lcd = 0.0
        self.hub.publish("screen", {"screen": name})
        return {"ok": True, "screen": name}

    def draw(self, ops, screen=None):
        """Show one frame on the panel, now.

        Drawn into `extern`'s reserved slot rather than by claiming a
        screen name: a program putting something up for a moment should
        not have to pick a name, keep a handle, and police a collision
        with a screen somebody else already owns. Naming one is what
        `POST /v1/screens` is for.
        """
        name = extern.draw_once(ops)
        if screen:
            # Asked to leave it on one of theirs instead, which is how a
            # program says "and stay there" without a second request.
            self.show(screen)
            return {"ok": True, "screen": str(screen)}
        daemon = self.daemon

        def apply(settings):
            target = settings.setdefault("lcd", {})
            target["screen"], target["enabled"] = name, True

        daemon.mutate(apply)
        daemon.last_lcd = 0.0
        self.hub.publish("draw", {"screen": name})
        return {"ok": True, "screen": name}

    # -- the routes --------------------------------------------------------

    def _handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "g510"

            def log_message(self, _fmt, *_args):
                pass          # a busy API is not a log line per request

            # -- plumbing --

            def reply(self, code, payload):
                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > BODY_LIMIT:
                    raise ValueError(f"body is over {BODY_LIMIT} bytes")
                if not length:
                    return {}
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(parsed, dict):
                    raise ValueError("the body must be a JSON object")
                return parsed

            def run(self, payload):
                """One request through the same dispatcher the socket uses."""
                replies = []
                try:
                    for reply in api.daemon.handle_request(payload):
                        replies.append(reply)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    return 400, {"ok": False, "error": str(exc)}
                if not replies:
                    return 502, {"ok": False, "error": "no reply"}
                return 200, replies[0] if len(replies) == 1 else replies

            # -- verbs --

            def do_GET(self):
                try:
                    self.route("GET")
                except ValueError as exc:
                    self.reply(400, {"ok": False, "error": str(exc)})
                except Exception as exc:              # pragma: no cover
                    self.reply(500, {"ok": False, "error": str(exc)})

            def do_PUT(self):
                try:
                    self.route("PUT")
                except ValueError as exc:
                    self.reply(400, {"ok": False, "error": str(exc)})
                except Exception as exc:              # pragma: no cover
                    self.reply(500, {"ok": False, "error": str(exc)})

            def do_POST(self):
                try:
                    self.route("POST")
                except ValueError as exc:
                    self.reply(400, {"ok": False, "error": str(exc)})
                except Exception as exc:              # pragma: no cover
                    self.reply(500, {"ok": False, "error": str(exc)})

            def do_DELETE(self):
                try:
                    self.route("DELETE")
                except ValueError as exc:
                    self.reply(400, {"ok": False, "error": str(exc)})
                except Exception as exc:              # pragma: no cover
                    self.reply(500, {"ok": False, "error": str(exc)})

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type")
                self.end_headers()

            # -- where it goes --

            def route(self, verb):
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if parts[:1] != ["v1"]:
                    if verb == "GET" and not parts:
                        return self.reply(200, INDEX)
                    raise ValueError(f"unknown path {parsed.path!r} - "
                                     "everything lives under /v1")
                rest = parts[1:]

                if not rest:
                    return self.reply(200, INDEX)
                head = rest[0]

                if head == "state" and verb == "GET":
                    return self.reply(200, {"ok": True, "state": api.state()})

                if head == "config":
                    if verb == "GET":
                        return self.reply(200, {"ok": True,
                                                "config": api.daemon.config})
                    if verb == "PUT":
                        return self.reply(200, api.write_config(self.body()))
                    raise ValueError("config takes GET or PUT")

                if head == "command" and verb == "POST":
                    code, payload = self.run(self.body())
                    return self.reply(code, payload)

                if head == "screens":
                    return self.screens(verb, rest[1:], parsed)

                if head == "events" and verb == "GET":
                    return self.events()

                if head == "lcd" and verb == "POST":
                    payload = self.body()
                    return self.reply(200, api.draw(payload.get("ops", []),
                                                    payload.get("screen")))

                raise ValueError(f"no route {verb} /v1/{'/'.join(rest)}")

            def screens(self, verb, rest, parsed):
                """Everything about the screens an outside program owns."""
                if not rest:
                    if verb == "GET":
                        return self.reply(200, {"ok": True,
                                                "outside": extern.describe(),
                                                "live": extern.names()})
                    if verb == "POST":
                        payload = self.body()
                        name = str(payload.get("name") or "")
                        try:
                            handle = extern.register(
                                name, title=str(payload.get("title") or ""))
                        except ValueError as exc:
                            return self.reply(409, {"ok": False,
                                                    "error": str(exc)})
                        api.hub.publish("screen", {"name": name,
                                                   "up": True})
                        return self.reply(201, {"ok": True, "name": name,
                                                "handle": handle,
                                                "draw": f"PUT /v1/screens/"
                                                        f"{name}"})
                    raise ValueError("screens takes GET or POST")

                name, action = rest[0], (rest[1] if len(rest) > 1 else "")
                if action == "heartbeat" and verb == "POST":
                    return self.reply(200, {"ok": extern.alive(name)})
                if action == "show" and verb == "POST":
                    return self.reply(200, api.show(name))
                if verb == "PUT":
                    payload = self.body()
                    ok = extern.frame(name, str(payload.get("handle") or ""),
                                      payload.get("ops", []),
                                      title=payload.get("title"))
                    if not ok:
                        return self.reply(409, {"ok": False, "error":
                                                "no such screen, or the "
                                                "handle is not its owner's"})
                    return self.reply(200, {"ok": True})
                if verb == "DELETE":
                    ok = extern.drop(name, str(self.body().get("handle") or "")
                                     or None)
                    if ok:
                        api.hub.publish("screen", {"name": name, "up": False})
                        # The panel may be sitting on it. It is gone now,
                        # so the display moves on the way it would if the
                        # program had taken the screen down itself.
                        api.daemon.leave_screen(name)
                    return self.reply(200 if ok else 404,
                                      {"ok": ok, "name": name})
                if verb == "GET":
                    # What this one is, and how long since it was pushed
                    # to - the question a user really has about a screen
                    # they did not put there. Asking must not move the
                    # display, so this reads and the `show` above acts.
                    if name not in extern.names():
                        return self.reply(404, {"ok": False, "error":
                                                f"no screen called {name!r}"})
                    return self.reply(200, {"ok": True, "name": name,
                                            **extern.describe()[name]})
                raise ValueError(f"screens does not take {verb}")

            def events(self):
                """Server-Sent Events, held open until the client leaves."""
                item = api.hub.subscribe()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(
                        f"data: {json.dumps({'event': 'hello', 'n': api.hub.since})}\n\n"
                        .encode())
                    while True:
                        try:
                            line = item["queue"].get(timeout=PING)
                            self.wfile.write(f"data: {line}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    api.hub.unsubscribe(item)

        return Handler
