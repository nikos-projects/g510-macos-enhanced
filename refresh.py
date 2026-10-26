"""Keep slow lookups off the caller's thread.

The agent's main loop polls the G-keys and redraws the display in the same
pass, so anything slow on the render path delays key presses - a subprocess
costs 100ms, and a printer that stops answering would block for the whole
connect timeout. These values are all "what is true right now, roughly", so
the caller takes the last answer immediately and a worker fetches the next one.
"""
import threading
import time


class Background:
    """A value refreshed off-thread, never blocking the reader."""

    def __init__(self, fetch, interval):
        self.fetch = fetch
        self.interval = interval
        self.value = None
        self.fetched = 0.0
        self.error = None
        self._running = False
        self._lock = threading.Lock()

    def get(self):
        """The most recent value, kicking off a refresh if it has aged out."""
        if time.time() - self.fetched >= self.interval:
            self._start()
        return self.value

    def get_now(self):
        """Fetch synchronously. For one-shot callers like the CLI."""
        self._run()
        return self.value

    def _start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            value, error = self.fetch()
            self.value, self.error = value, error
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.fetched = time.time()
            with self._lock:
                self._running = False
