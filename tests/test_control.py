"""The watch stream has to surface every key the keyboard sends."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import control
import device


class WatchCoverageTests(unittest.TestCase):
    """A caller watching for a key press means any key, not only the G-keys.

    The direct path once decoded G-keys alone, so the mode keys and the keys
    around the display never produced an event at all - which made `g510
    verify` hang the moment it asked for M1.
    """

    REPORTS = {
        "G1": "0301000000",
        "G18": "0300000200",
        "M1": "0300001000",
        "M3": "0300004000",
        "MR": "0300008000",
        "GAME": "0300000400",
        "L2": "0300000002",
        "L5": "0300000010",
    }

    def decoded(self, report):
        found = set()
        for decode in (device.G510.decode_gkeys,
                       device.G510.decode_mode_keys,
                       device.G510.decode_lcd_keys):
            found |= decode(bytes.fromhex(report)) or set()
        return found

    def test_every_key_group_is_decoded_by_one_of_them(self):
        for name, report in self.REPORTS.items():
            with self.subTest(key=name):
                self.assertIn(name, self.decoded(report))

    def test_the_watch_path_uses_all_three_decoders(self):
        source = open(control.__file__).read()
        watch = source[source.index("def watch_gkeys"):]
        for decoder in ("decode_gkeys", "decode_mode_keys", "decode_lcd_keys"):
            with self.subTest(decoder=decoder):
                self.assertIn(decoder, watch)

    def test_a_keystroke_report_yields_nothing(self):
        typed = bytes([0x01, 0, 0x1b, 0, 0, 0, 0, 0])
        self.assertTrue(device.G510.carries_keystrokes(typed))
        self.assertFalse(self.decoded(typed.hex()))


if __name__ == "__main__":
    unittest.main()
