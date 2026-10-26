"""What the panel draws, now that it draws real text.

The LCD is a 160x43 one-bit bitmap, so these tests read pixels rather than
strings: the thing that was broken was a character turning into `?`, and the
only honest way to catch that is to draw the character and look at what came
out. Everything here is pure - no device is opened.
"""
import unittest

import lcd
import screens
from device import LCD_HEIGHT, LCD_WIDTH


def draw(string, size=11.0, x=2, y=20):
    """Draw one string and return (lit pixels, rows)."""
    canvas = lcd.Canvas()
    canvas.text(string, x, y, size)
    rows = canvas.pixels()
    return sum(sum(row) for row in rows), rows


class DrawingTests(unittest.TestCase):
    """Every script the font - or a fallback font - can draw."""

    def test_cyrillic_comes_out_as_cyrillic_not_question_marks(self):
        real, real_rows = draw("Привет")
        fake, fake_rows = draw("??????")
        self.assertGreater(real, 20)
        self.assertNotEqual(real_rows, fake_rows)

    def test_a_cyrillic_sentence_has_more_to_show_than_a_question(self):
        # Same length either way: if the panel is only drawing one glyph's
        # worth of pixels, something fell back to nothing.
        weather, _ = draw("Погода +12°", 11.0)
        marks, _ = draw("??????????", 11.0)
        self.assertGreater(weather, marks)

    def test_cjk_and_emoji_are_not_blank(self):
        for text in ("日本語", "hi 🎮 ok", "Ω ≈ ∑"):
            with self.subTest(text=text):
                lit, _ = draw(text)
                self.assertGreater(lit, 10)

    def test_latin_still_draws(self):
        lit, _ = draw("Hello, world")
        self.assertGreater(lit, 40)

    def test_a_monospaced_font_gives_every_cyrillic_letter_the_same_width(self):
        canvas = lcd.Canvas()
        self.assertEqual(canvas.measure("Ш", 11.0),
                         canvas.measure("Щ", 11.0))
        self.assertGreater(canvas.measure("Привет", 11.0), 0)
        self.assertGreater(canvas.measure("Привет", 11.0),
                           canvas.measure("Приве", 11.0))

    def test_nothing_to_draw_is_zero_not_an_error(self):
        self.assertEqual(lcd.Canvas().measure("", 11.0), 0.0)
        lit, _ = draw("")
        self.assertEqual(lit, 0)


class FontChoiceTests(unittest.TestCase):
    """The config's `lcd.font`."""

    def setUp(self):
        self.was = lcd.FONT
        self.addCleanup(lcd.set_font, self.was)

    def test_it_takes_any_name_macos_knows(self):
        lcd.set_font("Menlo")
        self.assertEqual(lcd.FONT, "Menlo")
        self.assertGreater(draw("Привет", 11.0)[0], 20)

    def test_nothing_set_falls_back_to_the_default(self):
        for empty in ("", "   ", None):
            with self.subTest(empty=empty):
                lcd.set_font(empty)
                self.assertEqual(lcd.FONT, lcd.DEFAULT_FONT)

    def test_a_name_it_cannot_find_draws_anyway(self):
        # A panel with no text on it is worse than one in the wrong face.
        lcd.set_font("NoSuchFontAtAll")
        self.assertGreater(draw("Привет", 11.0)[0], 20)


class EveryScreenTests(unittest.TestCase):
    """Nothing that draws a screen broke when the text path did."""

    def test_every_screen_still_renders_a_full_panel(self):
        for name in screens.SCREENS:
            with self.subTest(name=name):
                rows = screens.render(name, {})
                self.assertEqual(len(rows), LCD_HEIGHT)
                self.assertTrue(all(len(row) == LCD_WIDTH for row in rows))
                self.assertGreater(sum(sum(row) for row in rows), 0)


if __name__ == "__main__":
    unittest.main()
