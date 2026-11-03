"""The beat tap, from a PulseAudio monitor instead of CoreAudio.

See _shared.py for why this amends rather than forks: the Detector, the
sensitivity knob and the pulse arithmetic are shared, and only the way
the levels are *heard* differs.

macOS gets them from a CoreAudio process tap - the operating system
hands the mix over. Linux has no such thing for free, but every
 PulseAudio or PipeWire system exposes the same idea as a monitor
source: the bytes the default sink is about to play, which is exactly
what the CoreAudio tap hears. `parec` reads one, and the reader thread
below turns it into the same two numbers the macOS listener produces -
the peak of the buffer and its low band, through the one shared
`beat.envelope` so a kick means the same thing on both platforms.

Two things this keeps from the CoreAudio contract, because the agent is
written against them:

  * `start()` reports whether it got as far as hearing anything rather
    than raising - a machine with no `parec` gets a log line naming the
    package to install and a backlight that does not move.
  * `poll(now)` is the only way levels leave this file, and it is
    called from the agent's loop thread, so everything the reader
    thread writes goes through one lock.

The reader thread also holds a self-pipe: it writes a byte as each
buffer lands and the agent's loop waits on the read end alongside the
keyboard. Without it the loop would only notice music at its next
redraw - up to a second late, which for a beat is the same as not at
all. `poll` drains the pipe, so an unpolled byte costs one extra wake
and never a spin.
"""
import os
import shutil
import signal
import struct
import subprocess
import threading

import _shared

_root = _shared.load("beat")
_shared.reexport(_root, globals())

RATE = 48000.0          # parec is asked for this; the split assumes it
CHUNK_FRAMES = 1024     # ~21ms of audio, the same order as a CoreAudio buffer
CHUNK_BYTES = CHUNK_FRAMES * 4     # float32, mono


class Listener:
    """The monitor source, as two numbers per buffer.

    Same shape as the CoreAudio listener in the root beat.py: construct,
    `start()`, `poll()` from the loop, `stop()`. The pipe makes it
    waitable - `fileno()` is what the Linux run loop selects on.
    """

    def __init__(self, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.level = 0.0          # loudest sample since the last poll
        self.bass = 0.0           # loudest sample of its low band
        self.fresh = False        # whether there is one to hand over
        self._alpha = _root._lowpass_alpha(_root.BASS_HZ, RATE)
        self._lowpass = 0.0       # the filter's memory, reader thread only
        self._proc = None
        self._thread = None
        self._running = False
        self._read, self._write = os.pipe()
        os.set_blocking(self._read, False)
        os.set_blocking(self._write, False)
        self.error = None

    # -- life ------------------------------------------------------------

    def start(self):
        """Start reading the monitor. False (with `error`) when it cannot."""
        if self._read is None:
            # stop() closed the wake-up pipe, so a listener that is started
            # again needs a new one. Without this a restart would look like
            # it had worked while every buffer failed to wake the loop, and
            # the pulse would only land at the next redraw.
            self._read, self._write = os.pipe()
            os.set_blocking(self._read, False)
            os.set_blocking(self._write, False)
            with self.lock:
                self._lowpass = 0.0
        self.error = None
        parec = shutil.which("parec")
        pactl = shutil.which("pactl")
        if not parec or not pactl:
            self.error = ("no parec - beat needs the PulseAudio tools "
                          "(apt install pulseaudio-utils / pacman -S libpulse)")
            self.log(f"beat tap could not be started: {self.error}")
            return False
        try:
            sink = subprocess.run(
                [pactl, "get-default-sink"], capture_output=True,
                text=True, timeout=5.0, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            self.error = f"pactl would not say what the default sink is: {exc}"
            self.log(f"beat tap could not be started: {self.error}")
            return False
        if not sink:
            self.error = "there is no default sink to listen to"
            self.log(f"beat tap could not be started: {self.error}")
            return False
        try:
            self._proc = subprocess.Popen(
                [parec, f"--device={sink}.monitor",
                 "--format=float32le", f"--rate={int(RATE)}",
                 "--channels=1", "--latency=50ms", "--no-shm"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.error = f"parec could not be started: {exc}"
            self.log(f"beat tap could not be started: {self.error}")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="g510-beat")
        self._thread.start()
        # parec exits at once when the monitor name is wrong or the sound
        # server is not running, and an hour of silent non-detection
        # would look like the detector had stopped working.
        if self._proc.poll() is not None:
            self.error = f"parec exited at once (status {self._proc.returncode})"
            self.log(f"beat tap could not be started: {self.error}")
            self.stop()
            return False
        self.log("beat tap listening")
        return True

    def stop(self):
        """Let the tap go. Safe to call twice, and safe after a half start."""
        self._running = False
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        for fd in (self._read, self._write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._read = self._write = None

    # -- the reader ------------------------------------------------------

    def _read_loop(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while self._running:
            try:
                chunk = proc.stdout.read(CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break                   # parec went away; the log says so
            self.consume(chunk)
        if self._running:
            # Ended while we thought it was running: a sound server that
            # restarted, say. One line, then the loop carries on without
            # a beat rather than retrying in a tight circle.
            self.log("beat tap ended - the backlight will not pulse")

    def consume(self, chunk):
        """One buffer of float32 mono: store its peaks, wake the loop."""
        if len(chunk) < 4:
            return
        usable = len(chunk) - (len(chunk) % 4)
        # float32le on every Mac and PC this runs on; struct would swap
        # byte order on a big-endian host, and there isn't one to worry
        # about - the reader is written to say so rather than assume.
        values = struct.unpack(f"<{usable // 4}f", chunk[:usable])
        # The one-pole's memory belongs to the reader thread alone - it is
        # this method that advances it and nothing else that reads it - so it
        # is taken and put back without the lock. What poll() hands over is
        # the shared part.
        peak, bass, low = envelope(values, self._alpha, self._lowpass)
        self._lowpass = low
        with self.lock:
            if not self.fresh or peak > self.level:
                self.level = peak
            if not self.fresh or bass > self.bass:
                self.bass = bass
            first = not self.fresh
            self.fresh = True
        if first and self._write is not None:
            try:
                os.write(self._write, b"\x01")   # one byte, never blocks
            except (OSError, ValueError):
                pass

    # -- the agent's side ------------------------------------------------

    def fileno(self):
        """The read end of the wake-up pipe, or None once stopped."""
        return self._read

    def poll(self, now):
        """(whether a buffer arrived, the loudest sample in it, the
        loudest sample of its low band) - and the pipe drained."""
        if self._read is not None:
            try:
                while os.read(self._read, 4096):
                    pass                # consume every pending wake-up
            except (OSError, ValueError):
                pass                    # empty, or already closed
        with self.lock:
            fresh, self.fresh = self.fresh, False
            level, self.level = self.level, 0.0
            bass, self.bass = self.bass, 0.0
        return fresh, level, bass
