"""The PulseAudio beat tap: what a buffer of samples turns into.

Nothing here needs a sound server. The listener's whole job is to turn float32
mono into the same two numbers the CoreAudio listener produces, and
`beat.envelope` - pulled out of the root beat.py for exactly this - is what
makes "the same" a checkable claim rather than a comment.

The pipe is tested for real with select(), because the run loop's entire
reason for waking on music rather than on the next LCD redraw is that
descriptor: a listener whose pipe did not carry a byte would still count
beats, just up to a second late, which for a beat is the same as not at all.
"""
import os
import select
import struct
import time
import unittest
from unittest import mock

import beat


def chunk_of(values):
    """One buffer exactly as parec would hand it over: float32le, mono."""
    return struct.pack(f"<{len(values)}f", *values)


class Consume(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.listener = beat.Listener(log=self.logged.append)

    def tearDown(self):
        self.listener.stop()

    def test_a_buffer_becomes_a_peak_and_a_low_band(self):
        self.listener.consume(chunk_of([0.1, -0.9, 0.4, 0.0]))
        fresh, level, bass = self.listener.poll(time.time())
        self.assertTrue(fresh)
        self.assertAlmostEqual(level, 0.9, places=6)
        self.assertGreater(bass, 0.0)

    def test_the_listener_agrees_with_the_shared_helper(self):
        """The whole reason envelope() exists: a kick has to mean the same
        thing to the CoreAudio listener and to this one, or the two
        platforms would count beats against different rules."""
        values = [0.2, -0.7, 0.5, -0.1, 0.9]
        peak, low_band, _ = beat.envelope(values, self.listener._alpha, 0.0)
        self.listener.consume(chunk_of(values))
        fresh, level, bass = self.listener.poll(time.time())
        self.assertTrue(fresh)
        self.assertAlmostEqual(level, peak, places=6)
        self.assertAlmostEqual(bass, low_band, places=6)

    def test_the_louder_of_two_buffers_is_the_one_kept(self):
        """Losing the peak of a transient is exactly how a beat goes
        unnoticed, so a second buffer never replaces a louder first."""
        self.listener.consume(chunk_of([0.2, -0.2]))
        self.listener.consume(chunk_of([0.5, -0.9]))
        fresh, level, _bass = self.listener.poll(time.time())
        self.assertTrue(fresh)
        self.assertAlmostEqual(level, 0.9, places=6)

    def test_polling_takes_everything(self):
        self.listener.consume(chunk_of([0.4]))
        self.listener.poll(time.time())
        fresh, level, bass = self.listener.poll(time.time())
        self.assertFalse(fresh)
        self.assertEqual((level, bass), (0.0, 0.0))

    def test_a_buffer_too_short_to_hold_a_sample_is_ignored(self):
        self.listener.consume(b"\x01\x02")
        fresh, level, bass = self.listener.poll(time.time())
        self.assertFalse(fresh)
        self.assertEqual((level, bass), (0.0, 0.0))

    def test_an_odd_trailing_byte_does_not_shift_the_samples(self):
        """parec can hand over a partial frame. Dropping the ragged tail is
        the difference between one wrong sample and every sample after it
        being read at the wrong offset."""
        whole = chunk_of([0.3, -0.6])
        self.listener.consume(whole + b"\xff")
        _fresh, level, _bass = self.listener.poll(time.time())
        self.assertAlmostEqual(level, 0.6, places=6)


class Wakeup(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.listener = beat.Listener(log=self.logged.append)

    def tearDown(self):
        self.listener.stop()

    def test_fileno_is_selectable_and_idle_to_begin_with(self):
        fd = self.listener.fileno()
        self.assertIsInstance(fd, int)
        self.assertEqual(select.select([fd], [], [], 0)[0], [])

    def test_a_buffer_wakes_the_descriptor_and_polling_clears_it(self):
        self.listener.consume(chunk_of([0.5]))
        fd = self.listener.fileno()
        self.assertEqual(select.select([fd], [], [], 0)[0], [fd])
        self.listener.poll(time.time())
        self.assertEqual(select.select([fd], [], [], 0)[0], [])

    def test_only_the_first_buffer_wakes_it(self):
        """The pipe is a wake-up, not a counter: ten bytes cost ten reads
        and one wake, and an unpolled byte must never turn into a spin."""
        self.listener.consume(chunk_of([0.5]))
        self.listener.consume(chunk_of([0.7]))
        fd = self.listener.fileno()
        self.assertEqual(select.select([fd], [], [], 0)[0], [fd])
        self.listener.poll(time.time())
        self.assertEqual(select.select([fd], [], [], 0)[0], [])

    def test_stopping_closes_the_descriptor(self):
        self.listener.stop()
        self.assertIsNone(self.listener.fileno())

    def test_stopping_twice_is_not_an_error(self):
        self.listener.stop()
        self.listener.stop()

    def test_a_stopped_listener_can_be_started_again(self):
        """stop() closes the pipe; start() has to notice and make a new one,
        or a restarted tap would count beats while never waking the loop
        with them."""
        self.listener.stop()
        with mock.patch.object(beat.shutil, "which", return_value=None):
            self.assertFalse(self.listener.start())
        self.assertIsNotNone(self.listener.fileno())
        self.assertIn("parec", self.listener.error)


class StartFailure(unittest.TestCase):
    def test_no_parc_names_the_package_to_install(self):
        """A machine with no parec gets a log line naming the package and a
        backlight that does not move - not a traceback and an agent that
        refuses to run."""
        logged = []
        listener = beat.Listener(log=logged.append)
        try:
            with mock.patch.object(beat.shutil, "which", return_value=None):
                self.assertFalse(listener.start())
            self.assertIn("parec", listener.error)
            self.assertIn("pulseaudio", listener.error.lower())
            self.assertTrue(any("beat tap" in line for line in logged))
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
