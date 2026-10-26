"""The LCD, mirrored in the window.

The panel is the part of this keyboard nobody can see from the desk it is
sitting under, so the window shows it. It shows it by drawing the screens
again rather than reading anything back: the agent owns the display, and a
preview that had to ask for a frame would either fight it for the device or
go blank whenever the keyboard was unplugged.

Nothing here touches the keyboard. The only cost of a preview is the same
screen function the daemon calls, onto a Canvas of our own.
"""
from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

import screens
from device import LCD_HEIGHT, LCD_WIDTH
from lcd import Canvas, PREVIEW_DARK

# What the daemon uses for the real panel (config lcd.refresh_seconds). A
# preview that updated faster than the keyboard does would be showing a clock
# the keyboard is not showing yet.
REFRESH_MS = 1000


def draw(name, state):
    """A Canvas with the named screen drawn on it.

    screens.render() is the obvious call and deliberately not the one made:
    it returns the packed 0/1 rows that go out over the wire, and the preview
    needs the greyscale surface underneath them. The lookup is render()'s own,
    so an unknown name falls back to the status screen exactly as it does
    there, and a screen the daemon keeps to itself - record - still previews.
    """
    canvas = Canvas()
    drawer = (screens.SCREENS.get(name) or screens.SCREENS_INTERNAL.get(name)
              or screens.screen_status)
    drawer(canvas, state or {})
    return canvas


def to_pixmap(image):
    """A QPixmap from a Pillow image, without a file in the middle.

    QImage wraps the buffer it is handed rather than copying it, and `raw` is
    a temporary that dies with this call - so the copy() is not tidiness, it
    is the difference between a picture and a use-after-free.
    """
    raw = image.tobytes()
    frame = QImage(raw, image.width, image.height, image.width * 3,
                   QImage.Format_RGB888)
    return QPixmap.fromImage(frame.copy())


class LcdPreview(QWidget):
    """The 160x43 panel at `scale`, amber on dark, its pixels left square.

    Canvas.preview() already scales nearest-neighbour, which is the point: the
    thing worth seeing is which pixels the panel will light, not a smoothed
    guess at them. So the widget is fixed at the scaled size and blits.
    """

    def __init__(self, scale=2, parent=None):
        super().__init__(parent)
        self.scale = max(1, int(scale))
        self.screen_name = "status"
        self.state = {}
        # The last frame that rendered. A screen reads the outside world - a
        # media player, a printer on the LAN, whatever command a gauge runs -
        # so any of them can throw, and blanking on a bad frame would flicker
        # at whatever rate the failure happens to come at.
        self._frame = None
        self.setFixedSize(LCD_WIDTH * self.scale, LCD_HEIGHT * self.scale)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def set_screen(self, name, state=None):
        """Show another screen, redrawn now rather than at the next tick."""
        self.screen_name = name
        if state is not None:
            self.state = state
        self.refresh()

    def start(self, interval_ms=None):
        """Begin refreshing. Callers stop it when the window is not visible."""
        if interval_ms:
            self.timer.setInterval(int(interval_ms))
        self.timer.start()

    def stop(self):
        self.timer.stop()

    @Slot()
    def refresh(self):
        try:
            image = draw(self.screen_name, self.state).preview(self.scale)
        except Exception:
            # Broad on purpose. A screen is the one place in this GUI that
            # runs something it did not write - a user's gauge command, a
            # printer's JSON, playerctl's output - and none of that is worth
            # taking the window down for. The last good frame stays up and
            # nothing is reported: a screen that is failing every second
            # would otherwise own the status bar.
            return
        self._frame = to_pixmap(image)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        if self._frame is None:
            painter.fillRect(self.rect(), QColor(*PREVIEW_DARK))
        else:
            painter.drawPixmap(0, 0, self._frame)
        # The panel's own dark is close enough to a dark desktop theme that
        # without an edge the preview has no shape at all.
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
