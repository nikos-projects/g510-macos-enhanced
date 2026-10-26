"""Render text and graphics for the G510's 160x43 monochrome LCD.

Drawing goes through CoreText so real font rendering and metrics come out of
it rather than a hand-rolled bitmap font - and so that *any* script draws as
itself. The previous path used CGContextSelectFont with MacRoman and encoded
every string with `encode("mac_roman", "replace")`, which had no Cyrillic,
no Greek outside ASCII, no CJK and no emoji in it: each of those came out as a
question mark, which is what made the panel look older than it is.

CoreText needs the foreground colour spelled out as an attribute - it does not
take the context's fill colour the way CGContextShowTextAtPoint did - and a
character the chosen font has no glyph for needs `CTFontCreateForString`, which
asks macOS for a font that does have one.
"""
import Quartz
from Quartz import CGBitmapContextCreate, CGColorSpaceCreateDeviceGray, CGRectMake

import CoreText
from CoreFoundation import CFRange
from Foundation import NSMutableAttributedString

from device import LCD_HEIGHT, LCD_WIDTH

DEFAULT_FONT = "Menlo-Bold"
FONT = DEFAULT_FONT           # what the config asked for; `lcd.font`
THRESHOLD = 110          # grey level above which a pixel counts as lit

_fonts = {}              # (name, size) -> CTFont, so a screen of labels
_lines = {}              # (text, size, font) -> CTLine   costs one build each
_white = None


def set_font(name):
    """Draw everything from now on with this font - the config's `lcd.font`.

    An empty or unknown name falls back to the default rather than to
    nothing: a panel with no text on it is worse than one in the wrong face.
    """
    global FONT
    FONT = str(name or "").strip() or DEFAULT_FONT
    _fonts.clear()
    _lines.clear()


def _font(name, size):
    key = (str(name), float(size))
    found = _fonts.get(key)
    if found is None:
        try:
            found = CoreText.CTFontCreateWithName(key[0], key[1], None)
        except Exception:
            found = None
        if not found:
            found = CoreText.CTFontCreateWithName(DEFAULT_FONT, key[1], None)
        _fonts[key] = found
    return found


def _foreground():
    global _white
    if _white is None:
        _white = Quartz.CGColorCreateGenericGray(1.0, 1.0)
    return _white


def _covers(font, char):
    """Whether `font` has a real glyph for every character of `char`.

    Glyph 0 is .notdef - the empty box - so a font that technically answers
    but draws nothing still counts as not covering it.
    """
    try:
        found, glyphs = CoreText.CTFontGetGlyphsForCharacters(
            font, char, None, len(char))
    except Exception:
        return False
    return bool(found) and all(glyphs)


def _fallback(base, char):
    """A font that does have `char`, when the chosen one does not.

    Asked one character at a time, so the range handed to CoreText is always
    the whole of what is being asked about - a range over the middle of a
    string can straddle two scripts and answer for neither.
    """
    units = len(char.encode("utf-16-le")) // 2
    try:
        found = CoreText.CTFontCreateForString(base, char, CFRange(0, units))
    except Exception:
        return base
    return found or base


def _runs(base, text):
    """[(start, end, font)] over `text`, in UTF-16 units as a CFRange counts.

    One run per change of font: Cyrillic stays in the chosen font, and only
    the characters it cannot draw borrow another. Grouping by the font's name
    rather than by object identity, because two calls for the same family
    come back as two different CF objects.
    """
    runs, current, start, units = [], base, 0, 0
    previous = None
    for char in text:
        width = len(char.encode("utf-16-le")) // 2
        font = base if _covers(base, char) else _fallback(base, char)
        name = "base" if font is base else str(CoreText.CTFontCopyPostScriptName(font))
        if previous is not None and name != previous:
            runs.append((start, units, current))
            start = units
        current, previous = font, name
        units += width
    if previous is not None:
        runs.append((start, units, current))
    return runs


def _line(string, size, font):
    """A CTLine for the string, or None for an empty one.

    Cached: a screen redraws the same handful of labels every second, and
    building the attributed string is the whole cost of drawing text.
    """
    text = str(string)
    if not text:
        return None
    name = font if font else FONT
    key = (text, float(size), str(name))
    found = _lines.get(key)
    if found is not None:
        return found
    base = _font(name, size)
    attributed = NSMutableAttributedString.alloc().initWithString_(text)
    colour = _foreground()
    for start, end, run_font in _runs(base, text):
        span = (start, end - start)
        attributed.addAttribute_value_range_(CoreText.kCTFontAttributeName,
                                             run_font, span)
        attributed.addAttribute_value_range_(
            CoreText.kCTForegroundColorAttributeName, colour, span)
    line = CoreText.CTLineCreateWithAttributedString(attributed)
    if len(_lines) > 512:
        _lines.clear()
    _lines[key] = line
    return line


class Canvas:
    """A 160x43 1-bit drawing surface backed by a CoreGraphics context."""

    def __init__(self):
        colorspace = CGColorSpaceCreateDeviceGray()
        self.ctx = CGBitmapContextCreate(
            None, LCD_WIDTH, LCD_HEIGHT, 8, LCD_WIDTH, colorspace, 0)
        self.clear()

    def clear(self):
        Quartz.CGContextSetGrayFillColor(self.ctx, 0.0, 1.0)
        Quartz.CGContextFillRect(self.ctx, CGRectMake(0, 0, LCD_WIDTH, LCD_HEIGHT))

    def text(self, string, x, y, size=12.0, font=None):
        """Draw a string with its baseline at (x, y), measured from the top."""
        line = _line(string, size, font)
        if line is None:
            return
        Quartz.CGContextSetTextPosition(self.ctx, x, LCD_HEIGHT - y - size)
        CoreText.CTLineDraw(line, self.ctx)

    def centered(self, string, y, size=12.0, font=None):
        """Draw a string horizontally centred, baseline y from the top."""
        width = self.measure(string, size, font)
        self.text(string, max(0, (LCD_WIDTH - width) / 2), y, size, font)

    def measure(self, string, size=12.0, font=None):
        """Width in pixels the string will occupy."""
        line = _line(string, size, font)
        if line is None:
            return 0.0
        return float(CoreText.CTLineGetTypographicBounds(line, None, None,
                                                          None)[0])

    def rect(self, x, y, width, height, filled=True):
        Quartz.CGContextSetGrayFillColor(self.ctx, 1.0, 1.0)
        Quartz.CGContextSetGrayStrokeColor(self.ctx, 1.0, 1.0)
        box = CGRectMake(x, LCD_HEIGHT - y - height, width, height)
        if filled:
            Quartz.CGContextFillRect(self.ctx, box)
        else:
            Quartz.CGContextSetLineWidth(self.ctx, 1.0)
            Quartz.CGContextStrokeRect(self.ctx, box)

    def bar(self, x, y, width, height, fraction):
        """An outlined progress bar filled to `fraction` (0..1).

        The fill is inset by two on every side, so on a short bar the height
        has to be floored or there is nothing left to draw.
        """
        self.rect(x, y, width, height, filled=False)
        inner = max(0, min(1.0, fraction)) * (width - 4)
        if inner >= 1:
            self.rect(x + 2, y + 2, int(inner), max(1, height - 4), filled=True)

    def preview(self, scale=2):
        """An NSImage of the current canvas, for showing the LCD on screen."""
        from AppKit import NSBitmapImageRep, NSImage, NSMakeSize
        rows = self.pixels()
        width, height = LCD_WIDTH * scale, LCD_HEIGHT * scale
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False, "NSDeviceRGBColorSpace",
            width * 4, 32)
        data = rep.bitmapData()
        # An amber-on-dark panel, matching how the real display reads.
        lit = (255, 190, 60, 255)
        dark = (28, 26, 22, 255)
        buffer = bytearray(width * height * 4)
        for y in range(height):
            source = rows[y // scale]
            base = y * width * 4
            for x in range(width):
                colour = lit if source[x // scale] else dark
                offset = base + x * 4
                buffer[offset:offset + 4] = bytes(colour)
        data[:] = bytes(buffer)
        image = NSImage.alloc().initWithSize_(NSMakeSize(width, height))
        image.addRepresentation_(rep)
        return image

    def pixels(self):
        """Threshold the canvas into rows of 0/1 ready for device.pack_lcd."""
        raw = Quartz.CGBitmapContextGetData(self.ctx).as_buffer(
            LCD_WIDTH * LCD_HEIGHT)[:]
        return [[1 if raw[y * LCD_WIDTH + x] > THRESHOLD else 0
                 for x in range(LCD_WIDTH)]
                for y in range(LCD_HEIGHT)]
