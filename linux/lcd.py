"""Render text and graphics for the G510's 160x43 monochrome LCD.

Drawing goes through Pillow, which gives us real font rendering and metrics
rather than a hand-rolled bitmap font - the same reason the macOS build draws
through CoreGraphics/CoreText. Pillow already measures from the top left, so
unlike the CoreGraphics surface nothing here has to be flipped.
"""
from PIL import Image, ImageDraw, ImageFont

from device import LCD_HEIGHT, LCD_WIDTH

# The macOS build asks for Menlo-Bold. DejaVu Sans Mono Bold is the nearest
# face every Debian box already has (fonts-dejavu-core), and its advance width
# is 0.602em against Menlo's 0.6023em - so a line that fitted the panel on a
# Mac still fits here and the screens need no re-tuning.
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
THRESHOLD = 110          # grey level above which a pixel counts as lit

# An amber-on-dark panel, matching how the real display reads.
PREVIEW_LIT = (255, 190, 60)
PREVIEW_DARK = (28, 26, 22)

_font_cache = {}


def _load_font(name, size):
    """A Pillow face for (name, size), loaded once.

    A frame is a dozen text and measure calls and the daemon redraws every
    second; opening a FreeType face that often is pure waste.
    """
    key = (name, round(float(size), 2))
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    # Callers may still be passing a macOS family name like b"Menlo-Bold",
    # which means nothing to FreeType. Anything that is not a readable font
    # file falls back rather than raising on a screen nobody can see.
    wanted = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
    face = None
    for candidate in (wanted, DEFAULT_FONT):
        try:
            face = ImageFont.truetype(candidate, float(size))
            break
        except (OSError, ValueError):
            continue
    if face is None:
        face = _builtin_font(size)
    _font_cache[key] = face
    return face


def _builtin_font(size):
    """Pillow's own font, for a system with no DejaVu installed.

    It is a fixed bitmap face on older Pillow builds, so the size is a request
    rather than a promise and the panel will look cramped - but it draws.
    """
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


class Canvas:
    """A 160x43 1-bit drawing surface backed by an 8-bit Pillow image."""

    def __init__(self):
        # Greyscale rather than 1-bit: text is drawn anti-aliased and the
        # THRESHOLD below decides what counts as lit, exactly as on macOS.
        self.image = Image.new("L", (LCD_WIDTH, LCD_HEIGHT), 0)
        self.draw = ImageDraw.Draw(self.image)
        self.clear()

    def clear(self):
        self.draw.rectangle([0, 0, LCD_WIDTH - 1, LCD_HEIGHT - 1], fill=0)

    def text(self, string, x, y, size=12.0, font=DEFAULT_FONT):
        """Draw a string with its baseline at (x, y), measured from the top.

        macOS hands CoreGraphics the bottom-left point (x, LCD_HEIGHT-y-size),
        which lands the baseline `size` pixels below y once converted back to
        top-down coordinates - so y is really the top of the em box. Pillow
        needs no flip, but that offset has to be kept or every screen creeps
        up by a line.
        """
        face = _load_font(font, size)
        if hasattr(face, "getmetrics"):
            self.draw.text((x, y + size), string, fill=255, font=face,
                           anchor="ls")
        else:
            # Bitmap faces have no anchors at all; their top-left origin is
            # the closest thing to the em box top we can ask for.
            self.draw.text((x, y), string, fill=255, font=face)

    def centered(self, string, y, size=12.0, font=DEFAULT_FONT):
        """Draw a string horizontally centred, baseline y from the top."""
        width = self.measure(string, size, font)
        self.text(string, max(0, (LCD_WIDTH - width) / 2), y, size, font)

    def measure(self, string, size=12.0, font=DEFAULT_FONT):
        """Width in pixels the string will occupy."""
        face = _load_font(font, size)
        try:
            return face.getlength(string)
        except AttributeError:
            # Very old Pillow: fall back to the drawn extent, which for a
            # fixed-width face is the same number.
            return face.getbbox(string)[2]

    def rect(self, x, y, width, height, filled=True):
        box = [x, y, x + width - 1, y + height - 1]
        if filled:
            self.draw.rectangle(box, fill=255)
        else:
            self.draw.rectangle(box, outline=255, width=1)

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
        """A Pillow image of the current canvas, for showing the LCD on screen.

        Nearest-neighbour on purpose: the point of a preview is to see the
        pixels the panel will actually light, not a smoothed guess at them.
        """
        image = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT))
        image.putdata([PREVIEW_LIT if value else PREVIEW_DARK
                       for row in self.pixels() for value in row])
        if scale > 1:
            image = image.resize((LCD_WIDTH * scale, LCD_HEIGHT * scale),
                                 Image.NEAREST)
        return image

    def to_ascii(self, lit="#", dark=" "):
        """Text art of the panel, so the LCD can be previewed in a terminal."""
        return "\n".join("".join(lit if value else dark for value in row)
                         for row in self.pixels())

    def pixels(self):
        """Threshold the canvas into rows of 0/1 ready for device.pack_lcd."""
        raw = self.image.tobytes()
        return [[1 if raw[y * LCD_WIDTH + x] > THRESHOLD else 0
                 for x in range(LCD_WIDTH)]
                for y in range(LCD_HEIGHT)]
