"""Canvas geometry for the 160x43 panel.

Pillow draws from the top left and so does this canvas - unlike the macOS
CoreGraphics surface, nothing is flipped - which is the one thing most likely
to go wrong in the port and the reason the origin is pinned down here so
hard. Drawing only; nothing is sent to a device.
"""
import os
import unittest

import device
import lcd
from lcd import Canvas

WIDTH, HEIGHT = 160, 43

HAS_FONT = os.path.exists(lcd.DEFAULT_FONT)


class PixelBufferTests(unittest.TestCase):

    def setUp(self):
        self.canvas = Canvas()

    def test_the_canvas_is_the_size_of_the_panel(self):
        self.assertEqual((lcd.LCD_WIDTH, lcd.LCD_HEIGHT), (WIDTH, HEIGHT))
        self.assertEqual((device.LCD_WIDTH, device.LCD_HEIGHT), (WIDTH, HEIGHT))

    def test_pixels_is_forty_three_rows_of_a_hundred_and_sixty(self):
        rows = self.canvas.pixels()
        self.assertEqual(len(rows), HEIGHT)
        for y, row in enumerate(rows):
            with self.subTest(row=y):
                self.assertEqual(len(row), WIDTH)

    def test_pixels_are_plain_ints(self):
        self.canvas.rect(0, 0, 2, 2)
        rows = self.canvas.pixels()
        self.assertEqual(set(value for row in rows for value in row), {0, 1})
        self.assertIsInstance(rows[0][0], int)

    def test_a_new_canvas_is_blank(self):
        self.assertEqual(sum(sum(row) for row in self.canvas.pixels()), 0)

    def test_clear_zeroes_everything(self):
        self.canvas.rect(0, 0, WIDTH, HEIGHT)
        self.assertGreater(sum(sum(row) for row in self.canvas.pixels()), 0)
        self.canvas.clear()
        self.assertEqual(sum(sum(row) for row in self.canvas.pixels()), 0)

    def test_the_frame_packs_without_complaint(self):
        """pixels() feeds device.pack_lcd directly, so the shapes must agree."""
        self.canvas.rect(1, 1, 4, 4)
        self.assertEqual(len(device.pack_lcd(self.canvas.pixels())),
                         device.LCD_FRAME_LEN)


class RectTests(unittest.TestCase):
    """The origin is the top left, with no flip anywhere."""

    def setUp(self):
        self.canvas = Canvas()

    def lit(self):
        rows = self.canvas.pixels()
        return {(x, y) for y in range(HEIGHT) for x in range(WIDTH)
                if rows[y][x]}

    def test_a_filled_rect_lands_where_it_was_asked_for(self):
        self.canvas.rect(3, 5, 4, 2)
        self.assertEqual(self.lit(), {(x, y) for x in range(3, 7)
                                      for y in range(5, 7)})

    def test_the_first_pixel_is_the_top_left_corner(self):
        self.canvas.rect(0, 0, 1, 1)
        self.assertEqual(self.lit(), {(0, 0)})

    def test_a_rect_at_the_bottom_stays_at_the_bottom(self):
        self.canvas.rect(0, HEIGHT - 1, 1, 1)
        self.assertEqual(self.lit(), {(0, HEIGHT - 1)})

    def test_a_full_width_rect_reaches_the_last_column(self):
        self.canvas.rect(0, 0, WIDTH, 1)
        self.assertEqual(self.lit(), {(x, 0) for x in range(WIDTH)})

    def test_an_outlined_rect_is_hollow(self):
        self.canvas.rect(2, 2, 5, 4, filled=False)
        lit = self.lit()
        self.assertIn((2, 2), lit)
        self.assertIn((6, 5), lit)          # x + width - 1, y + height - 1
        self.assertNotIn((4, 3), lit)       # the middle

    def test_a_filled_rect_is_solid(self):
        self.canvas.rect(2, 2, 5, 4, filled=True)
        self.assertIn((4, 3), self.lit())

    def test_whole_screen(self):
        self.canvas.rect(0, 0, WIDTH, HEIGHT)
        self.assertEqual(len(self.lit()), WIDTH * HEIGHT)


class BarTests(unittest.TestCase):
    """An outlined bar with the fill inset by two on every side."""

    def setUp(self):
        self.canvas = Canvas()

    def rows(self):
        return self.canvas.pixels()

    def filled_columns(self, y=4):
        return [x for x in range(WIDTH) if self.rows()[y][x]]

    def test_the_outline_is_drawn_whatever_the_fraction(self):
        self.canvas.bar(0, 0, 20, 10, 0.0)
        rows = self.rows()
        self.assertTrue(all(rows[0][x] for x in range(20)))
        self.assertTrue(all(rows[9][x] for x in range(20)))
        self.assertTrue(rows[5][0] and rows[5][19])

    def test_an_empty_bar_has_nothing_inside_it(self):
        self.canvas.bar(0, 0, 20, 10, 0.0)
        self.assertEqual(self.filled_columns(), [0, 19])

    def test_a_negative_fraction_is_clamped_to_empty(self):
        self.canvas.bar(0, 0, 20, 10, -3.0)
        self.assertEqual(self.filled_columns(), [0, 19])

    def test_a_full_bar_is_inset_by_two_on_every_side(self):
        self.canvas.bar(0, 0, 20, 10, 1.0)
        # The outline sits at x=0 and x=19; the fill runs from x=2 to x=17.
        self.assertEqual(self.filled_columns(),
                         [0] + list(range(2, 18)) + [19])
        rows = self.rows()
        self.assertFalse(rows[1][5])        # the inset row above the fill
        self.assertFalse(rows[8][5])        # and below it
        self.assertTrue(rows[2][5] and rows[7][5])

    def test_a_fraction_over_one_is_clamped_to_full(self):
        self.canvas.bar(0, 0, 20, 10, 5.0)
        full = self.rows()
        self.canvas.clear()
        self.canvas.bar(0, 0, 20, 10, 1.0)
        self.assertEqual(full, self.rows())

    def test_half_a_bar_fills_half_the_inner_width(self):
        self.canvas.bar(0, 0, 20, 10, 0.5)
        # 20 - 4 inner columns, half of them, starting two in.
        self.assertEqual(self.filled_columns(), [0] + list(range(2, 10)) + [19])

    def test_the_fill_grows_with_the_fraction(self):
        widths = []
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            self.canvas.clear()
            self.canvas.bar(0, 0, 40, 10, fraction)
            widths.append(len(self.filled_columns()))
        self.assertEqual(widths, sorted(widths))
        self.assertLess(widths[0], widths[-1])

    def test_a_sliver_too_small_to_draw_is_left_out(self):
        # Less than one inner column: there is nothing to draw, and rounding
        # it up would show a bar that is not really there.
        self.canvas.bar(0, 0, 20, 10, 0.01)
        self.assertEqual(self.filled_columns(), [0, 19])

    def test_a_short_bar_still_draws_a_fill(self):
        self.canvas.bar(0, 0, 20, 4, 1.0)
        self.assertTrue(any(self.rows()[2][x] for x in range(2, 16)))


class MeasureTests(unittest.TestCase):

    def setUp(self):
        self.canvas = Canvas()

    def test_nothing_measures_zero(self):
        self.assertEqual(self.canvas.measure(""), 0)

    def test_width_grows_with_the_string(self):
        widths = [self.canvas.measure("x" * n) for n in range(0, 8)]
        self.assertEqual(widths, sorted(widths))
        for shorter, longer in zip(widths, widths[1:]):
            self.assertLess(shorter, longer)

    def test_width_grows_with_the_size(self):
        small = self.canvas.measure("hello", 8.0)
        large = self.canvas.measure("hello", 16.0)
        self.assertLess(small, large)

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_the_face_is_monospaced(self):
        one = self.canvas.measure("m")
        self.assertAlmostEqual(self.canvas.measure("mmmm"), one * 4, places=3)
        self.assertAlmostEqual(self.canvas.measure("iiii"), one * 4, places=3)

    def test_an_unknown_font_falls_back_rather_than_raising(self):
        """Screens copied from the Mac still ask for b'Menlo-Bold'."""
        self.assertGreater(self.canvas.measure("hello", 12.0, b"Menlo-Bold"), 0)
        self.assertGreater(self.canvas.measure("hello", 12.0, "/no/such.ttf"), 0)

    def test_a_line_that_fits_the_panel_measures_under_its_width(self):
        self.assertLess(self.canvas.measure("x" * 20, 8.0), WIDTH)


class TextTests(unittest.TestCase):

    def setUp(self):
        self.canvas = Canvas()

    def lit_rows(self):
        rows = self.canvas.pixels()
        return [y for y in range(HEIGHT) if any(rows[y])]

    def lit_columns(self):
        rows = self.canvas.pixels()
        return [x for x in range(WIDTH) if any(rows[y][x] for y in range(HEIGHT))]

    def test_text_lights_pixels(self):
        self.canvas.text("Hello", 0, 10)
        self.assertGreater(sum(sum(row) for row in self.canvas.pixels()), 0)

    def test_an_empty_string_lights_nothing(self):
        self.canvas.text("", 0, 10)
        self.assertEqual(sum(sum(row) for row in self.canvas.pixels()), 0)

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_a_glyph_sits_in_the_em_box_below_y(self):
        """y is the top of the em box, so the baseline is y + size.

        The macOS build hands CoreGraphics a bottom-left point and gets the
        same result; if this drifts, every screen creeps up or down by a line.
        """
        self.canvas.text("X", 0, 0, size=12.0)
        rows = self.lit_rows()
        self.assertTrue(rows)
        self.assertGreaterEqual(min(rows), 0)
        self.assertEqual(max(rows), 11)      # the row above the baseline

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_the_same_glyph_moves_down_with_y(self):
        self.canvas.text("X", 0, 0, size=12.0)
        top = self.lit_rows()
        self.canvas.clear()
        self.canvas.text("X", 0, 10, size=12.0)
        moved = self.lit_rows()
        self.assertEqual(moved, [y + 10 for y in top])

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_text_starts_at_the_x_it_was_given(self):
        self.canvas.text("X", 0, 0, size=12.0)
        self.assertEqual(min(self.lit_columns()), 0)
        self.canvas.clear()
        self.canvas.text("X", 20, 0, size=12.0)
        self.assertEqual(min(self.lit_columns()), 20)

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_nothing_is_flipped(self):
        """A glyph near the top must not come out near the bottom."""
        self.canvas.text("X", 0, 0, size=12.0)
        self.assertLess(max(self.lit_rows()), HEIGHT // 2)
        self.canvas.clear()
        self.canvas.text("X", 0, HEIGHT - 13, size=12.0)
        self.assertGreater(min(self.lit_rows()), HEIGHT // 2)

    def test_text_off_the_panel_is_clipped_not_fatal(self):
        self.canvas.text("Hello", 150, 40)
        self.canvas.text("Hello", -20, -20)
        self.assertEqual(len(self.canvas.pixels()), HEIGHT)

    @unittest.skipUnless(HAS_FONT, "DejaVu Sans Mono is not installed")
    def test_centered_puts_the_string_in_the_middle(self):
        self.canvas.centered("XX", 10, size=12.0)
        columns = self.lit_columns()
        left = min(columns)
        right = WIDTH - 1 - max(columns)
        self.assertLessEqual(abs(left - right), 2)

    def test_centered_leaves_an_over_wide_string_at_the_left_edge(self):
        # A string wider than the panel would centre at a negative x, so it
        # is pinned to 0 instead; the glyph's own side bearing is all that
        # separates it from the edge.
        self.canvas.centered("x" * 60, 10, size=12.0)
        self.assertLessEqual(min(self.lit_columns()), 1)


class AsciiPreviewTests(unittest.TestCase):

    def setUp(self):
        self.canvas = Canvas()

    def test_it_is_forty_three_lines_of_a_hundred_and_sixty(self):
        lines = self.canvas.to_ascii().split("\n")
        self.assertEqual(len(lines), HEIGHT)
        for index, line in enumerate(lines):
            with self.subTest(line=index):
                self.assertEqual(len(line), WIDTH)

    def test_a_blank_canvas_is_all_spaces(self):
        self.assertEqual(set(self.canvas.to_ascii()), {" ", "\n"})

    def test_a_lit_pixel_shows_as_a_hash(self):
        self.canvas.rect(0, 0, 1, 1)
        lines = self.canvas.to_ascii().split("\n")
        self.assertEqual(lines[0][0], "#")
        self.assertEqual(lines[0][1], " ")

    def test_the_characters_are_adjustable(self):
        self.canvas.rect(0, 0, 1, 1)
        lines = self.canvas.to_ascii(lit="*", dark=".").split("\n")
        self.assertEqual(lines[0][0], "*")
        self.assertEqual(lines[0][1], ".")

    def test_it_matches_the_pixel_buffer(self):
        self.canvas.rect(4, 6, 3, 2)
        rows = self.canvas.pixels()
        lines = self.canvas.to_ascii().split("\n")
        for y in range(HEIGHT):
            with self.subTest(row=y):
                self.assertEqual(lines[y],
                                 "".join("#" if v else " " for v in rows[y]))


class ModuleTests(unittest.TestCase):

    def test_it_is_the_linux_lcd_module(self):
        self.assertTrue(
            os.path.dirname(os.path.abspath(lcd.__file__)).endswith("/linux"))

    def test_it_takes_its_geometry_from_the_device_module(self):
        self.assertIs(lcd.LCD_WIDTH, device.LCD_WIDTH)
        self.assertIs(lcd.LCD_HEIGHT, device.LCD_HEIGHT)


if __name__ == "__main__":
    unittest.main()
