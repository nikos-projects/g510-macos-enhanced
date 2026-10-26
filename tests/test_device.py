"""Report packing and decoding for the G510's vendor HID interface.

Everything here is static frame arithmetic, so none of it needs a keyboard
plugged in.
"""
import unittest

import device
from device import G510, pack_lcd

WIDTH, HEIGHT, PAGES = 160, 43, 6
HEADER = 32
FRAME = 992
MACRO_REPORT = 0x03


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


def macro_report(bits=0, lcd_byte=0, trailing=b"\x00\x00\x00"):
    """A report 0x03 frame carrying the given 24-bit field and display byte."""
    return bytes([MACRO_REPORT, bits & 0xFF, (bits >> 8) & 0xFF,
                  (bits >> 16) & 0xFF, lcd_byte]) + trailing


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

    def test_blank_bitmap_lights_nothing(self):
        frame = pack_lcd(blank())
        self.assertEqual(bytes(frame[HEADER:]), bytes(FRAME - HEADER))

    def test_frame_can_be_handed_to_hidapi_as_bytes(self):
        self.assertEqual(len(bytes(pack_lcd(blank()))), FRAME)


class PackLcdPixelTests(unittest.TestCase):
    """One byte per column per page, least significant bit at the top."""

    def lit(self, *points):
        pixels = blank()
        for x, y in points:
            pixels[y][x] = 1
        return pack_lcd(pixels)

    def test_top_left_pixel_is_the_low_bit_of_the_first_column(self):
        self.assertEqual(self.lit((0, 0))[HEADER], 0x01)

    def test_bottom_of_first_page_is_the_high_bit(self):
        self.assertEqual(self.lit((0, 7))[HEADER], 0x80)

    def test_row_eight_starts_the_second_page(self):
        frame = self.lit((0, 8))
        self.assertEqual(frame[HEADER], 0x00)
        self.assertEqual(frame[HEADER + WIDTH], 0x01)

    def test_last_row_lands_in_the_third_bit_of_the_last_page(self):
        # Row 42 is page 5, bit 2.
        frame = self.lit((0, HEIGHT - 1))
        self.assertEqual(frame[HEADER + 5 * WIDTH], 0x04)

    def test_last_column_is_the_last_byte_of_its_page(self):
        frame = self.lit((WIDTH - 1, 0))
        self.assertEqual(frame[HEADER + WIDTH - 1], 0x01)
        self.assertEqual(frame[HEADER], 0x00)

    def test_columns_are_independent(self):
        frame = self.lit((3, 1), (4, 2))
        self.assertEqual(frame[HEADER + 3], 0x02)
        self.assertEqual(frame[HEADER + 4], 0x04)

    def test_a_pixel_lights_exactly_one_bit(self):
        frame = self.lit((17, 21))
        self.assertEqual(sum(bin(byte).count("1") for byte in frame[HEADER:]), 1)

    def test_everything_lit_fills_every_page_but_the_last(self):
        pixels = [[1] * WIDTH for _ in range(HEIGHT)]
        frame = pack_lcd(pixels)
        for page in range(PAGES - 1):
            base = HEADER + page * WIDTH
            self.assertEqual(bytes(frame[base:base + WIDTH]), b"\xff" * WIDTH)
        # The panel is 43 rows, so the last page only holds rows 40-42.
        base = HEADER + 5 * WIDTH
        self.assertEqual(bytes(frame[base:base + WIDTH]), b"\x07" * WIDTH)

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


class DecodeGkeysTests(unittest.TestCase):

    def test_first_and_last_gkey(self):
        self.assertEqual(G510.decode_gkeys(macro_report(1 << 0)), {"G1"})
        self.assertEqual(G510.decode_gkeys(macro_report(1 << 17)), {"G18"})

    def test_every_gkey_has_its_own_bit(self):
        for index in range(18):
            with self.subTest(bit=index):
                self.assertEqual(G510.decode_gkeys(macro_report(1 << index)),
                                 {f"G{index + 1}"})

    def test_several_at_once(self):
        bits = (1 << 0) | (1 << 4) | (1 << 17)
        self.assertEqual(G510.decode_gkeys(macro_report(bits)),
                         {"G1", "G5", "G18"})

    def test_nothing_pressed_is_an_empty_set_not_none(self):
        self.assertEqual(G510.decode_gkeys(macro_report(0)), set())

    def test_mode_key_bits_are_not_gkeys(self):
        for bit in (18, 19, 20, 21, 22, 23):
            with self.subTest(bit=bit):
                self.assertEqual(G510.decode_gkeys(macro_report(1 << bit)), set())

    def test_game_switch_alongside_a_gkey_still_yields_the_gkey(self):
        bits = (1 << 2) | (1 << 18) | (1 << 20)
        self.assertEqual(G510.decode_gkeys(macro_report(bits)), {"G3"})

    def test_other_report_ids_are_rejected(self):
        for report_id in (0x00, 0x01, 0x02, 0x04, 0xFF):
            with self.subTest(report=report_id):
                data = bytes([report_id, 0xFF, 0xFF, 0x03, 0x00])
                self.assertIsNone(G510.decode_gkeys(data))

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03", b"\x03\x01", b"\x03\x01\x00"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_gkeys(data))

    def test_a_four_byte_report_is_enough(self):
        self.assertEqual(G510.decode_gkeys(b"\x03\x01\x00\x00"), {"G1"})

    def test_accepts_a_list_of_ints_as_hidapi_returns(self):
        self.assertEqual(G510.decode_gkeys([0x03, 0x02, 0x00, 0x00]), {"G2"})


class DecodeModeKeysTests(unittest.TestCase):

    def test_mode_key_bit_positions(self):
        for name, bit in (("GAME", 18), ("M1", 20), ("M2", 21), ("M3", 22),
                          ("MR", 23)):
            with self.subTest(key=name):
                self.assertEqual(G510.decode_mode_keys(macro_report(1 << bit)),
                                 {name})

    def test_bank_key_held_with_the_game_switch(self):
        bits = (1 << 18) | (1 << 21)
        self.assertEqual(G510.decode_mode_keys(macro_report(bits)),
                         {"GAME", "M2"})

    def test_gkeys_are_not_mode_keys(self):
        bits = (1 << 0) | (1 << 17)
        self.assertEqual(G510.decode_mode_keys(macro_report(bits)), set())

    def test_bit_nineteen_is_unmapped(self):
        self.assertEqual(G510.decode_mode_keys(macro_report(1 << 19)), set())

    def test_nothing_pressed_is_an_empty_set_not_none(self):
        self.assertEqual(G510.decode_mode_keys(macro_report(0)), set())

    def test_other_report_ids_are_rejected(self):
        self.assertIsNone(G510.decode_mode_keys(b"\x01\x00\x00\xff"))

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03\x00\x00"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_mode_keys(data))


class DecodeLcdKeysTests(unittest.TestCase):

    def test_display_key_bits(self):
        for name, bit in (("L1", 0x01), ("L2", 0x02), ("L3", 0x04),
                          ("L4", 0x08), ("L5", 0x10)):
            with self.subTest(key=name):
                self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=bit)),
                                 {name})

    def test_two_soft_keys_at_once(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=0x03)),
                         {"L1", "L2"})

    def test_unmapped_bits_of_byte_four_are_ignored(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=0xE0)), set())

    def test_nothing_pressed_is_an_empty_set_not_none(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(lcd_byte=0x00)), set())

    def test_gkey_bits_do_not_reach_the_display_keys(self):
        self.assertEqual(G510.decode_lcd_keys(macro_report(bits=0xFFFFFF)), set())

    def test_a_report_without_byte_four_is_rejected(self):
        self.assertIsNone(G510.decode_lcd_keys(b"\x03\x00\x00\x00"))

    def test_other_report_ids_are_rejected(self):
        self.assertIsNone(G510.decode_lcd_keys(b"\x02\x00\x00\x00\x01"))

    def test_short_and_missing_reports(self):
        for data in (None, b"", b"\x03"):
            with self.subTest(data=data):
                self.assertIsNone(G510.decode_lcd_keys(data))


class CarriesKeystrokesTests(unittest.TestCase):
    """Reports 0x01 and 0x02 carry what the user typed.

    This is the privacy guarantee the rest of the code leans on before it logs
    or broadcasts anything, so it has to be right for short and malformed input
    as well as for well-formed reports.
    """

    def test_boot_keyboard_report_is_keystrokes(self):
        # 0x01, modifiers, reserved, then up to six keycodes.
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

    def test_accepts_the_shapes_hidapi_hands_back(self):
        self.assertTrue(G510.carries_keystrokes(bytearray(b"\x01\x00\x04")))
        self.assertTrue(G510.carries_keystrokes([0x01, 0x00, 0x04]))
        self.assertFalse(G510.carries_keystrokes([0x03, 0x00, 0x00]))

    def test_keystroke_reports_are_not_mistaken_for_key_presses(self):
        # A typed 'a' must not be decoded as G-keys, mode keys or soft keys.
        typed = b"\x01\x00\x00\x04\x00\x00\x00\x00\x00"
        self.assertIsNone(G510.decode_gkeys(typed))
        self.assertIsNone(G510.decode_mode_keys(typed))
        self.assertIsNone(G510.decode_lcd_keys(typed))


class PanelConstantTests(unittest.TestCase):
    """The panel geometry the packing arithmetic above assumes."""

    def test_panel_geometry(self):
        self.assertEqual((device.LCD_WIDTH, device.LCD_HEIGHT), (WIDTH, HEIGHT))
        self.assertEqual(device.LCD_PAGES, PAGES)
        self.assertEqual(device.LCD_FRAME_LEN, FRAME)
        self.assertEqual(device.GKEY_COUNT, 18)


if __name__ == "__main__":
    unittest.main()
