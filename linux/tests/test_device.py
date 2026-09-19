"""Frame packing, the synthetic input report, and the sysfs LED probing.

The Linux device layer never opens the keyboard for any of this: the frame
arithmetic is static, the input report is built out of key state this test
feeds in by hand, and the LED class is pointed at a temporary directory. No
/sys path is read or written anywhere below.
"""
import collections
import os
import shutil
import tempfile
import unittest
from unittest import mock

import device
from device import G510, pack_lcd

WIDTH, HEIGHT, PAGES = 160, 43, 6
HEADER = 32
FRAME = 992
MACRO_REPORT = 0x03

# evdev key codes the kernel's hid-lg-g15 sends, spelled out rather than
# imported: python3-evdev is not a test dependency.
G1 = 0x290
G18 = 0x290 + 17
MR = 0x2B0
M1, M2, M3 = 0x2B3, 0x2B4, 0x2B5
L1, L5 = 0x2B8, 0x2B8 + 4
EV_KEY = 0x01

_Event = collections.namedtuple("_Event", "type code value")


def blank():
    """A 160x43 bitmap with nothing lit."""
    return [[0] * WIDTH for _ in range(HEIGHT)]


def unpack(frame):
    """The inverse of pack_lcd: wire format back to rows of 0/1."""
    rows = blank()
    for page in range(PAGES):
        base = HEADER + page * WIDTH
        for x in range(WIDTH):
            byte = frame[base + x]
            for bit in range(8):
                y = page * 8 + bit
                if y < HEIGHT and byte & (1 << bit):
                    rows[y][x] = 1
    return rows


def macro_report(bits=0, lcd_byte=0, trailing=b""):
    """A report 0x03 frame carrying the given 24-bit field and display byte."""
    return bytes([MACRO_REPORT, bits & 0xFF, (bits >> 8) & 0xFF,
                  (bits >> 16) & 0xFF, lcd_byte]) + trailing


class _FakeKeys:
    """Stand-in for the evdev node: a pipe select() can see, plus canned events.

    read_raw() selects on the device's fd before reading it, so the fake needs
    a real readable descriptor. The pipe is never drained, which models the
    hardware nicely: the fd stays ready, and a poll that turns up no change
    still has to produce no report.
    """

    def __init__(self):
        self._read_fd, self._write_fd = os.pipe()
        self.fd = self._read_fd
        self.pending = []

    def press(self, *codes):
        self.feed(*[(code, 1) for code in codes])

    def release(self, *codes):
        self.feed(*[(code, 0) for code in codes])

    def feed(self, *events):
        """events are (code, value) pairs, or whole _Event tuples."""
        for event in events:
            if not isinstance(event, _Event):
                event = _Event(EV_KEY, event[0], event[1])
            self.pending.append(event)
        os.write(self._write_fd, b"x")

    def read(self):
        events, self.pending = self.pending, []
        return events

    def close(self):
        for handle in (self._read_fd, self._write_fd):
            try:
                os.close(handle)
            except OSError:
                pass


def bare_keyboard(keys=None):
    """A G510 with only the bitfields set up: nothing is opened.

    __init__ goes looking for a hidraw node and the backlight LED, neither of
    which exists on a machine with no keyboard plugged in, so the object is
    built directly and given just the attributes _apply() and read_raw() use.
    """
    keyboard = object.__new__(G510)
    keyboard._bits = 0
    keyboard._lcd_bits = 0
    keyboard._nonblocking = True
    keyboard._keys = keys
    return keyboard


class PackLcdFrameTests(unittest.TestCase):
    """The frame the panel expects: 32-byte header, then 6 pages of 160."""

    def test_frame_is_992_bytes(self):
        self.assertEqual(len(pack_lcd(blank())), FRAME)
        self.assertEqual(FRAME, HEADER + PAGES * WIDTH)

    def test_frame_starts_with_the_lcd_report_id(self):
        self.assertEqual(pack_lcd(blank())[0], 0x03)

    def test_header_after_the_report_id_is_zero(self):
        frame = pack_lcd(blank())
        self.assertEqual(bytes(frame[1:HEADER]), bytes(HEADER - 1))

    def test_an_all_off_bitmap_lights_nothing(self):
        frame = pack_lcd(blank())
        self.assertEqual(bytes(frame[HEADER:]), bytes(FRAME - HEADER))

    def test_an_all_on_bitmap_fills_every_page_but_the_last(self):
        frame = pack_lcd([[1] * WIDTH for _ in range(HEIGHT)])
        for page in range(PAGES - 1):
            base = HEADER + page * WIDTH
            self.assertEqual(bytes(frame[base:base + WIDTH]), b"\xff" * WIDTH)
        # The panel is 43 rows, so the last page only holds rows 40-42.
        base = HEADER + 5 * WIDTH
        self.assertEqual(bytes(frame[base:base + WIDTH]), b"\x07" * WIDTH)

    def test_rows_forty_three_to_forty_seven_are_never_set(self):
        """The last page has eight bits but the panel stops at row 42.

        A bitmap taller than the panel must not bleed into bits 3-7 of the
        last page: those rows do not exist, and the firmware reads them.
        """
        pixels = [[1] * WIDTH for _ in range(48)]
        frame = pack_lcd(pixels)
        base = HEADER + 5 * WIDTH
        for x in range(WIDTH):
            with self.subTest(column=x):
                self.assertEqual(frame[base + x] & 0xF8, 0)

    def test_frame_can_be_handed_to_the_hidraw_node_as_bytes(self):
        self.assertEqual(len(bytes(pack_lcd(blank()))), FRAME)


class PackLcdPixelTests(unittest.TestCase):
    """One byte per column per page, least significant bit at the top."""

    def lit(self, *points):
        pixels = blank()
        for x, y in points:
            pixels[y][x] = 1
        return pack_lcd(pixels)

    def test_top_left_pixel_is_the_low_bit_of_the_first_column(self):
        frame = self.lit((0, 0))
        self.assertEqual(frame[HEADER], 0x01)

    def test_top_right_pixel_is_the_low_bit_of_the_last_column(self):
        frame = self.lit((WIDTH - 1, 0))
        self.assertEqual(frame[HEADER + WIDTH - 1], 0x01)
        self.assertEqual(frame[HEADER], 0x00)

    def test_bottom_left_pixel_is_the_third_bit_of_the_last_page(self):
        # Row 42 is page 5, bit 2.
        frame = self.lit((0, HEIGHT - 1))
        self.assertEqual(frame[HEADER + 5 * WIDTH], 0x04)

    def test_bottom_right_pixel_is_the_last_byte_of_the_last_page(self):
        frame = self.lit((WIDTH - 1, HEIGHT - 1))
        self.assertEqual(frame[HEADER + 5 * WIDTH + WIDTH - 1], 0x04)

    def test_the_least_significant_bit_is_the_top_row_of_a_page(self):
        for page in range(PAGES):
            with self.subTest(page=page):
                frame = self.lit((0, page * 8))
                self.assertEqual(frame[HEADER + page * WIDTH], 0x01)

    def test_bottom_of_the_first_page_is_the_high_bit(self):
        self.assertEqual(self.lit((0, 7))[HEADER], 0x80)

    def test_row_eight_starts_the_second_page(self):
        frame = self.lit((0, 8))
        self.assertEqual(frame[HEADER], 0x00)
        self.assertEqual(frame[HEADER + WIDTH], 0x01)

    def test_columns_are_independent(self):
        frame = self.lit((3, 1), (4, 2))
        self.assertEqual(frame[HEADER + 3], 0x02)
        self.assertEqual(frame[HEADER + 4], 0x04)

    def test_a_pixel_lights_exactly_one_bit(self):
        frame = self.lit((17, 21))
        self.assertEqual(sum(bin(byte).count("1") for byte in frame[HEADER:]), 1)

    def test_truthy_values_count_as_lit(self):
        pixels = blank()
        pixels[0][0] = True
        pixels[1][1] = 255
        frame = pack_lcd(pixels)
        self.assertEqual(frame[HEADER], 0x01)
        self.assertEqual(frame[HEADER + 1], 0x02)

    def test_round_trips_an_arbitrary_bitmap(self):
        pixels = blank()
        for y in range(HEIGHT):
            for x in range(WIDTH):
                pixels[y][x] = 1 if (x * 7 + y * 3) % 5 == 0 else 0
        self.assertEqual(unpack(pack_lcd(pixels)), pixels)

    def test_a_row_past_the_panel_is_never_read(self):
        pixels = blank()
        pixels.append(["boom"] * WIDTH)      # row 43 must be ignored
        self.assertEqual(len(pack_lcd(pixels)), FRAME)


class ApplyKeyEventTests(unittest.TestCase):
    """Folding evdev key events into the 24-bit field and the display byte."""

    def setUp(self):
        self.keyboard = bare_keyboard()

    def test_a_gkey_sets_its_own_bit(self):
        self.keyboard._apply(G1, 1)
        self.assertEqual(self.keyboard._bits, 1 << 0)

    def test_every_gkey_has_its_own_bit(self):
        for index in range(device.GKEY_COUNT):
            with self.subTest(gkey=index + 1):
                keyboard = bare_keyboard()
                keyboard._apply(device.KEY_MACRO1 + index, 1)
                self.assertEqual(keyboard._bits, 1 << index)

    def test_a_release_clears_the_bit(self):
        self.keyboard._apply(G1, 1)
        self.keyboard._apply(G1, 0)
        self.assertEqual(self.keyboard._bits, 0)

    def test_it_reports_whether_anything_moved(self):
        self.assertTrue(self.keyboard._apply(G1, 1))
        self.assertFalse(self.keyboard._apply(G1, 1))      # already held
        self.assertTrue(self.keyboard._apply(G1, 0))
        self.assertFalse(self.keyboard._apply(G1, 0))

    def test_an_unrelated_key_moves_nothing(self):
        # KEY_A is not a macro key; the kernel sends it on the other node.
        self.assertFalse(self.keyboard._apply(30, 1))
        self.assertEqual((self.keyboard._bits, self.keyboard._lcd_bits), (0, 0))

    def test_the_display_keys_use_a_separate_field(self):
        self.keyboard._apply(L1, 1)
        self.assertEqual(self.keyboard._bits, 0)
        self.assertEqual(self.keyboard._lcd_bits, device.LCD_KEY_BITS["L1"])


class SyntheticReportTests(unittest.TestCase):
    """read_raw() rebuilds the report the macOS hardware would have sent.

    The decoders are the shared ones, unchanged from the macOS build, so the
    round trip here is the actual contract between the two platforms: key
    events in, a five-byte report out, the same names back.
    """

    def setUp(self):
        self.keys = _FakeKeys()
        self.addCleanup(self.keys.close)
        self.keyboard = bare_keyboard(self.keys)

    def report(self, *events):
        """Feed key events and return the report read_raw() produces."""
        self.keys.feed(*events)
        return self.keyboard.read_raw()

    def test_a_report_is_five_bytes_of_macro_report(self):
        report = self.report((G1, 1))
        self.assertEqual(len(report), 5)
        self.assertEqual(report[0], device.INPUT_MACRO_KEYS)

    def test_g1_decodes_back_to_g1(self):
        self.assertEqual(G510.decode_gkeys(self.report((G1, 1))), {"G1"})

    def test_g18_decodes_back_to_g18(self):
        self.assertEqual(G510.decode_gkeys(self.report((G18, 1))), {"G18"})

    def test_every_gkey_survives_the_round_trip(self):
        for index in range(device.GKEY_COUNT):
            with self.subTest(gkey=index + 1):
                keys = _FakeKeys()
                self.addCleanup(keys.close)
                keyboard = bare_keyboard(keys)
                keys.press(device.KEY_MACRO1 + index)
                self.assertEqual(G510.decode_gkeys(keyboard.read_raw()),
                                 {f"G{index + 1}"})

    def test_m1_and_m3_decode_as_mode_keys(self):
        self.assertEqual(G510.decode_mode_keys(self.report((M1, 1))), {"M1"})
        second = self.report((M1, 0), (M3, 1))
        self.assertEqual(G510.decode_mode_keys(second), {"M3"})
        self.assertEqual(G510.decode_gkeys(second), set())

    def test_every_mode_key_survives_the_round_trip(self):
        for code, name in ((M1, "M1"), (M2, "M2"), (M3, "M3"), (MR, "MR")):
            with self.subTest(key=name):
                keys = _FakeKeys()
                self.addCleanup(keys.close)
                keyboard = bare_keyboard(keys)
                keys.press(code)
                report = keyboard.read_raw()
                self.assertEqual(G510.decode_mode_keys(report), {name})
                self.assertEqual(G510.decode_gkeys(report), set())

    def test_mr_decodes_as_a_mode_key_not_a_gkey(self):
        report = self.report((MR, 1))
        self.assertEqual(G510.decode_mode_keys(report), {"MR"})
        self.assertEqual(G510.decode_gkeys(report), set())

    def test_l1_and_l5_decode_as_display_keys(self):
        self.assertEqual(G510.decode_lcd_keys(self.report((L1, 1))), {"L1"})
        self.assertEqual(G510.decode_lcd_keys(self.report((L1, 0), (L5, 1))),
                         {"L5"})

    def test_every_display_key_survives_the_round_trip(self):
        for index in range(5):
            with self.subTest(key=f"L{index + 1}"):
                keys = _FakeKeys()
                self.addCleanup(keys.close)
                keyboard = bare_keyboard(keys)
                keys.press(device.KEY_KBD_LCD_MENU1 + index)
                self.assertEqual(G510.decode_lcd_keys(keyboard.read_raw()),
                                 {f"L{index + 1}"})

    def test_several_keys_held_at_once(self):
        report = self.report((G1, 1), (G18, 1), (M3, 1), (L5, 1))
        self.assertEqual(G510.decode_gkeys(report), {"G1", "G18"})
        self.assertEqual(G510.decode_mode_keys(report), {"M3"})
        self.assertEqual(G510.decode_lcd_keys(report), {"L5"})

    def test_a_release_clears_the_bit_in_the_next_report(self):
        first = self.report((G1, 1), (G18, 1))
        self.assertEqual(G510.decode_gkeys(first), {"G1", "G18"})
        second = self.report((G1, 0))
        self.assertEqual(G510.decode_gkeys(second), {"G18"})
        third = self.report((G18, 0))
        self.assertEqual(G510.decode_gkeys(third), set())

    def test_nothing_new_produces_no_report(self):
        """Like the hardware, a report only comes out when something moved."""
        self.assertIsNotNone(self.report((G1, 1)))
        self.assertIsNone(self.keyboard.read_raw())          # no new events
        self.assertIsNone(self.report((G1, 1)))              # still held

    def test_autorepeat_is_not_a_change(self):
        self.report((G1, 1))
        self.assertIsNone(self.report((G1, 2)))

    def test_events_that_are_not_key_events_are_ignored(self):
        self.report((G1, 1))
        self.assertIsNone(self.report(_Event(0x00, 0x00, 0)))   # EV_SYN

    def test_the_game_switch_is_never_reported(self):
        """hid-lg-g15 does not expose the joystick switch, so GAME never sets.

        The name survives in MODE_KEY_BITS so macOS configs still load; no
        evdev code maps onto it.
        """
        self.assertNotIn(device.MODE_KEY_BITS["GAME"],
                         set(device.KEYCODE_BITS.values()))
        report = self.report((G1, 1), (M1, 1), (MR, 1))
        self.assertNotIn("GAME", G510.decode_mode_keys(report))

    def test_a_synthesised_report_is_not_keystrokes(self):
        self.assertFalse(G510.carries_keystrokes(self.report((G1, 1))))


class DecodeGkeysTests(unittest.TestCase):

    def test_first_and_last_gkey(self):
        self.assertEqual(G510.decode_gkeys(macro_report(1 << 0)), {"G1"})
        self.assertEqual(G510.decode_gkeys(macro_report(1 << 17)), {"G18"})

    def test_several_at_once(self):
        bits = (1 << 0) | (1 << 4) | (1 << 17)
        self.assertEqual(G510.decode_gkeys(macro_report(bits)),
                         {"G1", "G5", "G18"})

    def test_nothing_pressed_is_an_empty_set_not_none(self):
        self.assertEqual(G510.decode_gkeys(macro_report(0)), set())

    def test_mode_key_bits_are_not_gkeys(self):
        for bit in (18, 19, 20, 21, 22, 23):
            with self.subTest(bit=bit):
                self.assertEqual(G510.decode_gkeys(macro_report(1 << bit)),
                                 set())

    def test_other_report_ids_are_rejected(self):
        for report_id in (0x00, 0x01, 0x02, 0x04, 0xFF):
            with self.subTest(report=report_id):
                data = bytes([report_id, 0xFF, 0xFF, 0x03, 0x00])
                self.assertIsNone(G510.decode_gkeys(data))

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03", b"\x03\x01", b"\x03\x01\x00"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_gkeys(data))


class DecodeModeKeysTests(unittest.TestCase):

    def test_mode_key_bit_positions(self):
        for name, bit in (("GAME", 18), ("M1", 20), ("M2", 21), ("M3", 22),
                          ("MR", 23)):
            with self.subTest(key=name):
                self.assertEqual(G510.decode_mode_keys(macro_report(1 << bit)),
                                 {name})

    def test_gkeys_are_not_mode_keys(self):
        bits = (1 << 0) | (1 << 17)
        self.assertEqual(G510.decode_mode_keys(macro_report(bits)), set())

    def test_bit_nineteen_is_unmapped(self):
        self.assertEqual(G510.decode_mode_keys(macro_report(1 << 19)), set())

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03\x00\x00"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_mode_keys(data))


class DecodeLcdKeysTests(unittest.TestCase):

    def test_display_key_bits(self):
        for name, bit in (("L1", 0x01), ("L2", 0x02), ("L3", 0x04),
                          ("L4", 0x08), ("L5", 0x10)):
            with self.subTest(key=name):
                self.assertEqual(
                    G510.decode_lcd_keys(macro_report(lcd_byte=bit)), {name})

    def test_two_soft_keys_at_once(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=0x03)),
                         {"L1", "L2"})

    def test_unmapped_bits_of_byte_four_are_ignored(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=0xE0)),
                         set())

    def test_gkey_bits_do_not_reach_the_display_keys(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(bits=0xFFFFFF)),
                         set())

    def test_a_report_without_byte_four_is_rejected(self):
        self.assertIsNone(G510.decode_lcd_keys(b"\x03\x00\x00\x00"))

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_lcd_keys(data))


class CarriesKeystrokesTests(unittest.TestCase):
    """Reports 0x01 and 0x02 carry what the user typed.

    Nothing on Linux produces one - the kernel keeps typing to itself - but
    this is the privacy guard the shared daemon checks before it logs or
    broadcasts anything, so it has to keep meaning what it means on macOS.
    """

    def test_boot_keyboard_report_is_keystrokes(self):
        self.assertTrue(G510.carries_keystrokes(
            b"\x01\x02\x00\x04\x00\x00\x00\x00\x00"))

    def test_second_keystroke_report_is_keystrokes(self):
        self.assertTrue(G510.carries_keystrokes(b"\x02\x00\x00\x00"))

    def test_one_byte_keystroke_reports_are_still_keystrokes(self):
        self.assertTrue(G510.carries_keystrokes(b"\x01"))
        self.assertTrue(G510.carries_keystrokes(b"\x02"))

    def test_macro_report_is_not_keystrokes(self):
        self.assertFalse(G510.carries_keystrokes(macro_report(1 << 3)))

    def test_every_report_id_is_classified(self):
        for report_id in range(256):
            with self.subTest(report=report_id):
                data = bytes([report_id]) + b"\x00" * 7
                self.assertEqual(G510.carries_keystrokes(data),
                                 report_id in (0x01, 0x02))

    def test_empty_and_missing_input_is_not_keystrokes(self):
        for data in (None, b"", bytearray(), []):
            with self.subTest(data=data):
                self.assertFalse(G510.carries_keystrokes(data))

    def test_return_value_is_a_plain_bool(self):
        self.assertIs(G510.carries_keystrokes(b"\x01\x00"), True)
        self.assertIs(G510.carries_keystrokes(b"\x03\x00"), False)

    def test_accepts_a_list_of_ints(self):
        self.assertTrue(G510.carries_keystrokes(bytearray(b"\x01\x00\x04")))
        self.assertTrue(G510.carries_keystrokes([0x01, 0x00, 0x04]))
        self.assertFalse(G510.carries_keystrokes([0x03, 0x00, 0x00]))

    def test_keystroke_reports_are_not_mistaken_for_key_presses(self):
        typed = b"\x01\x00\x00\x04\x00\x00\x00\x00\x00"
        self.assertIsNone(G510.decode_gkeys(typed))
        self.assertIsNone(G510.decode_mode_keys(typed))
        self.assertIsNone(G510.decode_lcd_keys(typed))


class RgbLedModeTests(unittest.TestCase):
    """Which sysfs interface the backlight LED is driven through.

    Every case here is a temporary directory holding the attribute files a
    given kernel would expose; SYSFS_LEDS is pointed at it, so nothing under
    /sys is opened, let alone written.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        patch = mock.patch.object(device, "SYSFS_LEDS", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def led(self, name="led", **files):
        """An _RgbLed over a directory holding exactly these attributes."""
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        for attribute, content in files.items():
            with open(os.path.join(path, attribute), "w") as handle:
                handle.write(content)
        return device._RgbLed(name)

    def read(self, name, attribute):
        with open(os.path.join(self.root, name, attribute)) as handle:
            return handle.read().strip()

    # -- probing -----------------------------------------------------------

    def test_a_color_attribute_alone_is_the_color_mode(self):
        self.assertEqual(self.led(color="#000000").mode, "color")

    def test_multi_intensity_and_multi_index_are_the_multi_mode(self):
        led = self.led(multi_intensity="0 0 0", multi_index="red green blue")
        self.assertEqual(led.mode, "multi")

    def test_brightness_alone_is_the_mono_mode(self):
        self.assertEqual(self.led(brightness="0").mode, "mono")

    def test_a_directory_with_none_of_them_has_no_mode(self):
        self.assertIsNone(self.led().mode)

    def test_a_missing_directory_has_no_mode(self):
        self.assertIsNone(device._RgbLed("not-there").mode)

    def test_color_wins_over_the_others(self):
        led = self.led(color="#010203", multi_intensity="0 0 0",
                       multi_index="red green blue", brightness="1")
        self.assertEqual(led.mode, "color")

    def test_multi_wins_over_plain_brightness(self):
        led = self.led(multi_intensity="0 0 0", multi_index="red green blue",
                       brightness="1")
        self.assertEqual(led.mode, "multi")

    # -- get ---------------------------------------------------------------

    def test_get_parses_a_hash_prefixed_colour(self):
        self.assertEqual(self.led(color="#ff8000\n").get(), (255, 128, 0))

    def test_get_parses_a_colour_without_the_hash(self):
        self.assertEqual(self.led(color="00ff10").get(), (0, 255, 16))

    def test_get_parses_multi_intensity_in_index_order(self):
        led = self.led(multi_intensity="10 20 30",
                       multi_index="red green blue")
        self.assertEqual(led.get(), (10, 20, 30))

    def test_get_honours_a_shuffled_multi_index(self):
        led = self.led(multi_intensity="10 20 30",
                       multi_index="blue green red")
        self.assertEqual(led.get(), (30, 20, 10))

    def test_get_assumes_rgb_when_multi_index_is_unreadable(self):
        led = self.led(multi_intensity="1 2 3")
        self.assertEqual(led.get(), (1, 2, 3))

    def test_get_spreads_a_mono_brightness_over_all_three_channels(self):
        self.assertEqual(self.led(brightness="128").get(), (128, 128, 128))

    def test_get_without_an_interface_is_an_oserror(self):
        with self.assertRaises(OSError):
            self.led().get()

    # -- set ---------------------------------------------------------------

    def test_set_writes_a_hash_prefixed_lowercase_colour(self):
        led = self.led("one", color="#000000")
        led.set((255, 128, 0))
        self.assertEqual(self.read("one", "color"), "#ff8000")

    def test_set_pads_single_digit_channels(self):
        led = self.led("one", color="#000000")
        led.set((1, 2, 3))
        self.assertEqual(self.read("one", "color"), "#010203")

    def test_set_writes_multi_intensity_in_index_order(self):
        led = self.led("two", multi_intensity="0 0 0",
                       multi_index="blue green red", brightness="7")
        led.set((10, 20, 30))
        self.assertEqual(self.read("two", "multi_intensity"), "30 20 10")

    def test_set_rewrites_the_brightness_so_the_colour_lands(self):
        """The multicolour class only pushes intensity on a brightness write."""
        led = self.led("two", multi_intensity="0 0 0",
                       multi_index="red green blue", brightness="7")
        led.set((1, 2, 3))
        self.assertEqual(self.read("two", "multi_intensity"), "1 2 3")
        self.assertEqual(self.read("two", "brightness"), "7")

    def test_set_on_a_mono_led_uses_the_brightest_channel(self):
        led = self.led("three", brightness="0")
        led.set((10, 200, 30))
        self.assertEqual(self.read("three", "brightness"), "200")

    def test_set_clamps_out_of_range_channels(self):
        led = self.led("one", color="#000000")
        led.set((-5, 300, 12.9))
        self.assertEqual(self.read("one", "color"), "#00ff0c")

    def test_set_without_an_interface_is_an_oserror(self):
        with self.assertRaises(OSError):
            self.led().set((1, 2, 3))

    def test_set_then_get_round_trips_in_every_mode(self):
        cases = (
            self.led("rt-color", color="#000000"),
            self.led("rt-multi", multi_intensity="0 0 0",
                     multi_index="red green blue", brightness="4"),
        )
        for led in cases:
            with self.subTest(mode=led.mode):
                led.set((3, 200, 17))
                self.assertEqual(led.get(), (3, 200, 17))


class LockLedsTests(unittest.TestCase):
    """The Num/Scroll Lock indicators are a deliberate no-op on Linux."""

    def test_setting_them_reports_that_nothing_changed(self):
        leds = device.LockLeds()
        self.assertFalse(leds.set(num=True, scroll=True))
        leds.close()


class PanelConstantTests(unittest.TestCase):
    """The geometry and tables the arithmetic above assumes."""

    def test_panel_geometry(self):
        self.assertEqual((device.LCD_WIDTH, device.LCD_HEIGHT), (WIDTH, HEIGHT))
        self.assertEqual(device.LCD_PAGES, PAGES)
        self.assertEqual(device.LCD_FRAME_LEN, FRAME)
        self.assertEqual(device.GKEY_COUNT, 18)

    def test_it_is_the_linux_device_module(self):
        """linux/ has to be ahead of the repo root, or this tests macOS."""
        self.assertTrue(
            os.path.dirname(os.path.abspath(device.__file__)).endswith("/linux"))
        self.assertTrue(hasattr(device, "KEYCODE_BITS"))

    def test_the_keycode_table_covers_every_macro_key(self):
        self.assertEqual(len(device.KEYCODE_BITS), device.GKEY_COUNT + 4)
        self.assertEqual(len(device.KEYCODE_LCD_BITS), 5)

    def test_nothing_is_opened_exclusively(self):
        self.assertTrue(device.NON_EXCLUSIVE)


if __name__ == "__main__":
    unittest.main()
