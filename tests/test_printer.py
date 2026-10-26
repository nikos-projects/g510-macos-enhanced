"""Turning a Creality status snapshot into the handful of fields the LCD draws.

_normalise and describe_state work on a dict, so the printer does not have to
exist: the snapshots below are the shape the K1C's WebSocket sends.
"""
import unittest

import printer

# A first frame from the printer, trimmed to the fields that are read. The
# real thing carries 77 of them.
SNAPSHOT = {
    "model": "K1C",
    "hostname": "K1C-0a1b2c",
    "printFileName": "/usr/data/printer_data/gcodes/benchy_0.2mm_PLA.gcode",
    "printProgress": 47,
    "printLeftTime": 3720,
    "printJobTime": 5400,
    "layer": 87,
    "TotalLayer": 184,
    "nozzleTemp": 219.4,
    "targetNozzleTemp": 220,
    "bedTemp0": 59.8,
    "targetBedTemp0": 60,
    "state": 1,
}


class NormaliseTests(unittest.TestCase):

    def setUp(self):
        self.reading = printer._normalise(SNAPSHOT)

    def test_the_whole_snapshot(self):
        self.assertEqual(self.reading, {
            "model": "K1C",
            "file": "benchy_0.2mm_PLA",
            "progress": 47,
            "left": 3720,
            "elapsed": 5400,
            "layer": 87,
            "layers": 184,
            "nozzle": 219.4,
            "nozzle_target": 220.0,
            "bed": 59.8,
            "bed_target": 60.0,
            "state": 1,
        })

    def test_counts_are_whole_numbers(self):
        for key in ("progress", "left", "elapsed", "layer", "layers", "state"):
            with self.subTest(field=key):
                self.assertIsInstance(self.reading[key], int)

    def test_temperatures_keep_their_fraction(self):
        for key in ("nozzle", "nozzle_target", "bed", "bed_target"):
            with self.subTest(field=key):
                self.assertIsInstance(self.reading[key], float)

    def test_the_snapshot_is_not_modified(self):
        printer._normalise(SNAPSHOT)
        self.assertEqual(SNAPSHOT["printFileName"],
                         "/usr/data/printer_data/gcodes/benchy_0.2mm_PLA.gcode")

    def test_the_job_name_loses_its_directory_and_suffix(self):
        for name, expected in (
                ("/usr/data/printer_data/gcodes/thing.gcode", "thing"),
                ("thing.gcode", "thing"),
                ("thing.gco", "thing"),
                ("thing", "thing"),
                ("a.b.c.gcode", "a.b.c"),
                ("/gcodes/my print v2.gcode", "my print v2")):
            with self.subTest(name=name):
                self.assertEqual(
                    printer._normalise({"printFileName": name})["file"],
                    expected)

    def test_no_job_name(self):
        for value in (None, "", 0):
            with self.subTest(value=value):
                self.assertEqual(
                    printer._normalise({"printFileName": value})["file"], "")

    def test_an_empty_snapshot_reads_as_zeroes(self):
        reading = printer._normalise({})
        self.assertEqual(reading["model"], "printer")
        self.assertEqual(reading["file"], "")
        self.assertEqual(reading["progress"], 0)
        self.assertEqual(reading["left"], 0)
        self.assertEqual(reading["layers"], 0)
        self.assertEqual(reading["nozzle"], 0.0)
        self.assertEqual(reading["state"], 0)

    def test_the_model_falls_back_to_the_hostname_then_to_a_label(self):
        self.assertEqual(printer._normalise({"hostname": "K1C-0a1b"})["model"],
                         "K1C-0a1b")
        self.assertEqual(printer._normalise({"model": "", "hostname": ""})["model"],
                         "printer")

    def test_numbers_sent_as_strings_are_accepted(self):
        reading = printer._normalise({"printProgress": "47",
                                      "nozzleTemp": "219.4",
                                      "state": "1"})
        self.assertEqual(reading["progress"], 47)
        self.assertEqual(reading["nozzle"], 219.4)
        self.assertEqual(reading["state"], 1)

    def test_nulls_read_as_zero(self):
        reading = printer._normalise({"printProgress": None, "layer": None,
                                      "bedTemp0": None})
        self.assertEqual(reading["progress"], 0)
        self.assertEqual(reading["layer"], 0)
        self.assertEqual(reading["bed"], 0.0)

    def test_unparseable_numbers_read_as_zero_rather_than_raising(self):
        reading = printer._normalise({"printProgress": "n/a",
                                      "printLeftTime": [1, 2],
                                      "nozzleTemp": {"x": 1},
                                      "state": "idle"})
        self.assertEqual(reading["progress"], 0)
        self.assertEqual(reading["left"], 0)
        self.assertEqual(reading["nozzle"], 0.0)
        self.assertEqual(reading["state"], 0)

    def test_fractional_progress_is_truncated(self):
        self.assertEqual(printer._normalise({"printProgress": 47.9})["progress"], 47)

    def test_an_idle_printer(self):
        reading = printer._normalise({"model": "K1C", "state": 0,
                                      "printProgress": 0, "nozzleTemp": 24.5,
                                      "bedTemp0": 23.0})
        self.assertEqual(reading["progress"], 0)
        self.assertEqual(reading["state"], 0)
        self.assertEqual(reading["nozzle"], 24.5)

    def test_every_field_the_screen_reads_is_present(self):
        for key in ("model", "file", "progress", "left", "elapsed", "layer",
                    "layers", "nozzle", "bed", "state"):
            with self.subTest(field=key):
                self.assertIn(key, printer._normalise({}))


class DescribeStateTests(unittest.TestCase):

    def test_no_reading_at_all(self):
        self.assertEqual(printer.describe_state(None), "offline")
        self.assertEqual(printer.describe_state({}), "offline")

    def test_the_observed_states(self):
        self.assertEqual(printer.describe_state({"state": 0}), "idle")
        self.assertEqual(printer.describe_state({"state": 1}), "printing")
        self.assertEqual(printer.describe_state({"state": 2}), "paused")
        self.assertEqual(printer.describe_state({"state": 3}), "stopped")

    def test_a_finished_job_reads_as_done(self):
        self.assertEqual(printer.describe_state({"state": 3, "progress": 100}),
                         "done")

    def test_a_job_stopped_part_way_is_not_done(self):
        self.assertEqual(printer.describe_state({"state": 3, "progress": 62}),
                         "stopped")

    def test_only_a_stopped_printer_can_be_done(self):
        self.assertEqual(printer.describe_state({"state": 1, "progress": 100}),
                         "printing")

    def test_an_unrecognised_state_shows_its_number(self):
        # The codes are observed rather than documented, so nothing is guessed.
        self.assertEqual(printer.describe_state({"state": 7}), "state 7")
        self.assertEqual(printer.describe_state({"state": -1}), "state -1")

    def test_a_reading_without_a_state_does_not_crash(self):
        self.assertIsInstance(printer.describe_state({"progress": 10}), str)

    def test_the_realistic_snapshot(self):
        self.assertEqual(printer.describe_state(printer._normalise(SNAPSHOT)),
                         "printing")


class IsPrintingTests(unittest.TestCase):

    def test_printing(self):
        self.assertTrue(printer.is_printing({"state": 1}))

    def test_not_printing(self):
        for state in (0, 2, 3, 7):
            with self.subTest(state=state):
                self.assertFalse(printer.is_printing({"state": state}))

    def test_no_reading(self):
        self.assertFalse(printer.is_printing(None))
        self.assertFalse(printer.is_printing({}))

    def test_returns_a_plain_bool(self):
        self.assertIs(printer.is_printing({"state": 1}), True)
        self.assertIs(printer.is_printing(None), False)


if __name__ == "__main__":
    unittest.main()
