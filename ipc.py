"""Control socket so one process can own the keyboard.

hidapi seizes the HID device exclusively on macOS, so the daemon holds the only
handle and the CLI and GUI ask it to act on their behalf. When no daemon is
running they fall back to opening the device themselves.
"""
import json
import os
import socket
import threading

SOCKET_PATH = os.path.expanduser("~/.config/g510/daemon.sock")
TIMEOUT = 2.0


def is_running():
    """True if a daemon is listening on the control socket."""
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(TIMEOUT)
        client.connect(SOCKET_PATH)
        client.close()
        return True
    except OSError:
        return False


def request(payload, stream=False):
    """Send one request. Returns the reply dict, or a line iterator if stream."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(None if stream else TIMEOUT)
    client.connect(SOCKET_PATH)
    client.sendall((json.dumps(payload) + "\n").encode())
    handle = client.makefile("r")
    if stream:
        return _stream(client, handle)
    try:
        line = handle.readline()
        return json.loads(line) if line else {"ok": False, "error": "no reply"}
    finally:
        client.close()


def _stream(client, handle):
    try:
        for line in handle:
            if line.strip():
                yield json.loads(line)
    finally:
        client.close()


class Server:
    """Accepts control connections and hands each request to a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = None
        self.thread = None
        self.running = False

    def start(self):
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(SOCKET_PATH)
        self.sock.listen(8)
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        """Handle one client. A client hanging up mid-stream is routine."""
        handle = None
        try:
            # Bound the wait for the request line so a client that connects
            # and says nothing cannot pin a thread for ever. Cleared before
            # streaming, where a timeout mid-read would corrupt the file
            # wrapper's state.
            conn.settimeout(TIMEOUT)
            handle = conn.makefile("rw")
            line = handle.readline()
            conn.settimeout(None)
            if not line.strip():
                return
            payload = json.loads(line)
            if not isinstance(payload, dict):
                handle.write(json.dumps(
                    {"ok": False, "error": "request must be an object"}) + "\n")
                handle.flush()
                return
            for reply in self.handler(payload):
                handle.write(json.dumps(reply) + "\n")
                handle.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError):
            pass
        finally:
            for closeable in (handle, conn):
                try:
                    if closeable is not None:
                        closeable.close()
                except OSError:
                    pass

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
