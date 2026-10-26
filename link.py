"""Where the Vencord plugin talks to, on localhost.

The plugin runs inside Discord and the panel runs in another process, so
something has to carry between them. It cannot be the control socket the
rest of the agent speaks over - that is a unix socket, and a web page
cannot open one - and it cannot be Discord's own websocket, which belongs
to Discord. What is left is a small HTTP server bound to 127.0.0.1, and
that is this.

Two directions, and they are deliberately not the same shape:

  * **state comes in** with `POST /state`. The plugin pushes whenever what
    it has changed, so the newest one is always here and a push that never
    arrived costs one second rather than anything more.

  * **commands go out** with `GET /cmd?since=N`, held until there is
    something to hand over (up to `PULL_WAIT`) rather than dropped. The
    `since` is what the plugin has already run, so a plugin that was
    restarted mid-command asks again and is told once.

Only ever 127.0.0.1: nothing outside this machine can reach it, and the
plugin is the only thing that knows to look. `GET /status` answers the CLI
with what the last push said - this module never has to guess at Discord's
state itself, because it is told.

`reference` and `resolve`, at the top, are the other half of that: the
names a command line uses for one message out of the ones pushed, and
what they turn back into.
"""
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 47832
PULL_WAIT = 20.0          # how long one command call waits for a command
CONNECTED_AFTER = 15.0    # no state for this long and the plugin is gone
KEEP = 64                 # commands remembered if nobody comes to collect


class _Server(ThreadingHTTPServer):
    """A listening socket that does not ask DNS who 127.0.0.1 is.

    `HTTPServer.server_bind` runs the address through `getfqdn` before it
    will listen at all, and here that reverse lookup stalls for thirty
    seconds - which is thirty seconds of startup for a server that is only
    ever reached by name it already has. The port is all it needs.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def reference(message):
    """The handle a command line can name one message by.

    A Discord id is nineteen digits: right as an id, hopeless as something
    to read off a listing and type back. So every line of
    `g510 discord history` is labelled with the *tail* of its id - short
    enough to read, long enough to be one message out of the hundreds the
    panel holds. `resolve` is what turns that label back into the id the
    plugin needs, so nothing here ever has to show the whole number.
    """
    return "#" + str(message.get("id") or "")[-6:]


def resolve(messages, target):
    """Which message id `target` means among those held, or why not.

    Commands arrive as words, and there are three kinds of word: nothing
    at all, `last`/`newest` for the one on top; a full id, which is
    already the answer; and a `#` reference printed by `history`, which is
    the tail of an id and so has to be looked for rather than believed.

    A tail can fit more than one message - ids grow at the end, so two can
    share six digits in a long session - and picking the first of them
    would send a reply to the wrong person. That, and finding nothing at
    all, come back as a reason instead of an id.

    Returns `(id, error)`: one of the two is always empty.
    """
    wanted = str(target or "").strip()
    if wanted.lower() in ("", "last", "newest"):
        if not messages:
            return "", "nothing to reply to yet"
        return str(messages[-1].get("id") or ""), None
    exact = [row for row in messages if str(row.get("id")) == wanted]
    if exact:
        return str(exact[0].get("id")), None
    tail = wanted.lstrip("#")
    fits = [row for row in messages if str(row.get("id")).endswith(tail)]
    if len(fits) == 1:
        return str(fits[0].get("id")), None
    if len(fits) > 1:
        return "", (f"{wanted!r} is the end of {len(fits)} messages held - "
                    f"give more of the id")
    return "", (f"no held message matches {wanted!r} - "
                f"g510 discord history lists them, older fetches more")


class Link:
    """The agent's end: a server, and the last thing Discord said."""

    def __init__(self, block=None, log=None):
        self.block = block or {}
        self.log = log or (lambda message: None)
        self._cond = threading.Condition()
        self._pending = []          # [[n, payload], ...], oldest first
        self._next = 1
        self._state = {}
        self._seen = 0.0
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
            self.error = f"link: {type(exc).__name__}: {exc}"
            self.log(self.error)
            return self
        server.daemon_threads = True
        self._server = server
        # A short poll: `stop` waits for the loop to notice, and half a
        # second of that on every agent restart is half a second of the
        # keyboard being unreachable.
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="g510-link", daemon=True)
        self._thread.start()
        self.error = None
        self.log(f"discord link listening on 127.0.0.1:{self.port}")
        return self

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._cond:
            self._pending = []
            self._cond.notify_all()
        return self

    # -- what the plugin says ----------------------------------------------

    def receive(self, payload):
        """One push from the plugin: replace what the panel knows.

        The push also carries the number of the last command the plugin
        ran, so whatever is at or below it is retired here rather than
        handed out a second time. Without that a plugin that restarts
        would start again at cursor zero and send every message in the
        queue twice.

        That same number also says how far the plugin has counted, which
        matters when *this* side is the one that restarted: numbering
        starts again at one here, while the plugin's cursor stays where
        it was, and every command filed below it would then be asked for
        with a number the plugin has already passed - collected by
        nobody, silently, for as long as the client stays up. So the
        counter is dragged forward to just past whatever the plugin says
        it has run, which is exactly the number no longer needed.
        """
        if not isinstance(payload, dict):
            return False
        ack = payload.get("ack")
        with self._cond:
            self._state = payload
            self._seen = time.time()
            if isinstance(ack, dict) and ack.get("n") is not None:
                try:
                    done = int(ack["n"])
                except (TypeError, ValueError):
                    done = -1
                if done >= 0:
                    self._pending = [item for item in self._pending
                                     if item[0] > done]
                    self._next = max(self._next, done + 1)
            self._cond.notify_all()
        return True

    def state(self):
        """The last pushed state - a dict, or nothing at all yet."""
        with self._cond:
            return dict(self._state)

    def connected(self):
        with self._cond:
            return bool(self._seen) and time.time() - self._seen < CONNECTED_AFTER

    def status(self):
        with self._cond:
            age = time.time() - self._seen if self._seen else None
            return {"connected": self.connected(), "error": self.error,
                    "port": self.port, "seen": age,
                    "waiting": len(self._pending),
                    "state": dict(self._state)}

    # -- what the panel asks for -------------------------------------------

    def send(self, payload):
        """Queue one command for the plugin. Returns the number it got."""
        if not isinstance(payload, dict):
            raise ValueError("a command must be a dict")
        with self._cond:
            number = self._next
            self._next += 1
            command = dict(payload)
            command["n"] = number
            self._pending.append([number, command])
            if len(self._pending) > KEEP:
                del self._pending[:-KEEP]
            self._cond.notify_all()
        return number

    def take(self, since, wait=PULL_WAIT):
        """Commands past `since`, waiting up to `wait` seconds for one."""
        deadline = time.time() + max(0.0, float(wait))
        with self._cond:
            while True:
                wanted = [command for number, command in self._pending
                          if number > since]
                if wanted:
                    return wanted
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    # -- the server ---------------------------------------------------------

    def _handler(self):
        link = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                # A line per request would bury the agent's log: the plugin
                # polls once a second for as long as Discord is running.
                return

            def _headers(self, code, length, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                # Discord's page is not this server's origin, and Vencord's
                # CSP patch only answers half of that: the fetch also has to
                # be allowed to read the reply.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.end_headers()

            def _write(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self._headers(code, len(body))
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._headers(200, 0)

            def do_POST(self):
                if urlparse(self.path).path != "/state":
                    self._write(404, {"ok": False, "error": "no such path"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, TypeError):
                    self._write(400, {"ok": False, "error": "not JSON"})
                    return
                self._write(200, {"ok": link.receive(payload)})

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/status":
                    self._write(200, link.status())
                    return
                if parsed.path != "/cmd":
                    self._write(404, {"ok": False, "error": "no such path"})
                    return
                try:
                    since = int((parse_qs(parsed.query).get("since") or
                                 ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                self._write(200, {"commands": link.take(since)})

        return Handler
