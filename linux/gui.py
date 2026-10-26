"""A tray icon and a settings window for the G510, in Qt.

The macOS build is a menu bar item with a window behind it, and this is the
same shape on Plasma for the same reason: the things changed often - the
backlight, which bank the M-keys are on, what the LCD shows - are one click
from the tray, and everything with more to it is a tab in the window.

This is a client and nothing more. It never opens the keyboard: every action
goes through control.py, which forwards to the running agent over its socket
or opens the device itself when no agent is there. So the GUI can do what the
command line can do and not one thing more, and two of them can be open at
once without fighting over the device.

    gui.py            the tray, the window, the frame around the tabs
    gui_preview.py    the LCD, redrawn on screen
    gui_common.py     guard() and the Bus the panes talk over
    gui_bindings.py   the Keys tab
    gui_settings.py   the Screens, Printer, Gauges and About tabs
"""
import subprocess
import sys
from functools import partial

import _path

# gui.py is an entry point as well as a module - `python3 linux/gui.py` has to
# work from a checkout - and every Linux entry point puts this directory ahead
# of the shared modules before importing any of them. See _path.py.
_path.setup()

import cli
import config
import control
import ipc
import screens
from gui_common import Bus, guard
from gui_preview import LcdPreview

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QColorDialog,
                               QComboBox, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMenu, QPushButton, QRadioButton,
                               QSizePolicy, QSlider, QStatusBar,
                               QSystemTrayIcon, QTabWidget, QVBoxLayout,
                               QWidget)

# The four panes the other half of the GUI provides, imported behind a guard.
# They are the part most likely to be missing or mid-edit, and a window that
# refuses to open because one tab does not import is a window nobody can use
# to fix anything. A failed import costs its tabs and a line in the status
# bar; the backlight, the bank and the preview still work.
PANE_ERRORS = []

try:
    from gui_bindings import BindingsPane
except ImportError as exc:
    BindingsPane = None
    PANE_ERRORS.append(f"Keys: {exc}")

try:
    from gui_settings import (AboutPane, GaugesPane, PrinterPane, ScreensPane)
except ImportError as exc:
    AboutPane = GaugesPane = PrinterPane = ScreensPane = None
    PANE_ERRORS.append(f"Screens, Printer, Gauges, About: {exc}")

# As widgets.py names them on the Mac, so the two builds read the same.
SCREEN_LABELS = {
    "status": "Status", "clock": "Clock", "claude": "Claude Usage",
    "media": "Now Playing", "printer": "3D Printer", "gkeys": "G-key Echo",
    # Wayland gives an unprivileged client no way to read the focused window,
    # so this one draws a permanent blank here. It is still offered, because
    # the screen exists and a config copied from a Mac can select it.
    "app": "Active App (macOS only)",
}

# NAMED_COLORS is a lookup, not an order, and it holds "black" as a second
# spelling of "off" - so the menu takes the useful ones in a useful order and
# appends anything a later config.py adds.
MENU_COLORS = ([name for name in ("white", "red", "orange", "yellow", "green",
                                  "cyan", "blue", "purple", "magenta", "pink",
                                  "off") if name in config.NAMED_COLORS]
               + [name for name in sorted(config.NAMED_COLORS)
                  if name not in ("white", "red", "orange", "yellow", "green",
                                  "cyan", "blue", "purple", "magenta", "pink",
                                  "off", "black")])

POLL_MS = 2000          # how often the keyboard is checked for still being there


def screen_label(name):
    return SCREEN_LABELS.get(name, name.title())


def settings_color(settings):
    """The chosen backlight colour, surviving a hand-edited config."""
    try:
        return config.parse_color(settings.get("backlight", "#ffffff"))
    except config.ColorError:
        return (255, 255, 255)


# -- writes ----------------------------------------------------------------
#
# Every config write in the GUI goes through here, for the reason window.py
# gives on the Mac: the agent and the CLI write this file too, so a caller
# loads it fresh, changes the one thing it owns and saves that. Saving a copy
# held since the window opened would revert a bank the keyboard switched or a
# macro MR recorded in the meantime.

def store(bus, settings):
    """Persist, tell a running agent, tell the panes. False if refused."""
    if not config.save(settings):
        bus.status.emit(config.last_error
                        or f"Could not write {config.CONFIG_PATH}")
        return False
    cli.reload_agent()
    bus.announce(settings)
    return True


def apply_color(bus, rgb):
    """Set the backlight and remember it, as `g510 color` does."""
    settings = config.load()
    settings["backlight"] = config.format_color(rgb)
    if not store(bus, settings):
        return
    bus.report(*guard(control.set_backlight, config.effective_color(settings)))


def apply_brightness(bus, level):
    """The G510 has no brightness register; this scales the chosen colour."""
    settings = config.load()
    settings["brightness"] = int(level)
    if not store(bus, settings):
        return
    bus.report(*guard(control.set_backlight, config.effective_color(settings)))


def apply_bank(bus, name):
    """Switch the M1/M2/M3 binding bank, the way `g510 bank` does.

    When the agent is up it owns the bank - it lights the M-key LED and
    reloads the bindings against it - so this asks the agent rather than
    editing the config behind its back and leaving the two disagreeing.
    """
    if control.daemon_running():
        ok, reply = guard(ipc.request, {"cmd": "set_bank", "bank": str(name)})
        if not bus.report(ok, reply):
            return
        if not reply.get("ok"):
            bus.status.emit(reply.get("error", "the agent refused the bank"))
            return
        bus.announce(config.load())
        return
    settings = config.set_bank(config.load(), str(name))
    if not store(bus, settings):
        return
    bus.report(*guard(control.set_mkeys, [f"m{name}"]))


def apply_screen(bus, name):
    """Pick what the LCD shows.

    The agent redraws the panel every second, so a one-shot draw would be
    painted over immediately - the config is what makes it stick, and the
    draw is only so the change is visible before the next tick.
    """
    settings = config.load()
    lcd = settings.setdefault("lcd", {})
    lcd["screen"] = name
    lcd["enabled"] = True
    if not store(bus, settings):
        return
    bus.report(*guard(control.show_screen, name))


# -- icons -----------------------------------------------------------------

def swatch(rgb, size=16):
    """A filled circle, for the colour entries in the menu.

    The hex is noise at a glance and the colour is not, which is why the Mac
    build draws these too (widgets.swatch_image).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(*rgb))
    painter.setPen(QPen(QColor(128, 128, 128, 160), 1))
    painter.drawEllipse(1, 1, size - 3, size - 3)
    painter.end()
    return QIcon(pixmap)


def keyboard_icon(rgb, connected=True):
    """A keyboard in the current backlight colour, drawn rather than shipped.

    Drawn because the alternative is an icon file that has to be found at
    runtime from a checkout, a .deb and a systemd unit alike - and because a
    fixed icon could not do the useful half of this, which is showing the
    colour the keyboard is lit in. The body is always outlined: `off` is
    black, and a black icon on a dark panel is no icon.
    """
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setOpacity(1.0 if connected else 0.4)
    painter.setBrush(QColor(*rgb))
    painter.setPen(QPen(QColor(190, 190, 190, 200), 4))
    painter.drawRoundedRect(4, 16, 56, 32, 7, 7)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 120))
    for row, (left, count) in enumerate(((11, 6), (13, 6), (18, 4))):
        for column in range(count):
            painter.drawRect(left + column * 7, 23 + row * 8, 5, 5)
    painter.end()
    return QIcon(pixmap)


# -- tray ------------------------------------------------------------------

class Tray(QSystemTrayIcon):
    """The glanceable half: state, and the handful of one-click changes.

    The menu is built once and only its checkmarks and labels are refreshed,
    rather than rebuilt per open as the AppKit version does. Qt owns the
    actions it is given, and tearing them down under a menu that is opening
    is a good way to delete something that is about to be clicked.
    """

    def __init__(self, bus, window=None, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.window = window
        self.settings = config.load()
        self.connected = True
        self.build()
        self.sync()
        self.activated.connect(self.clicked)
        bus.config_changed.connect(self.adopt)
        bus.device_changed.connect(self.set_connected)

    # -- construction ------------------------------------------------------

    def build(self):
        menu = QMenu()
        menu.aboutToShow.connect(self.sync)

        self.header = menu.addAction("G510")
        self.header.setEnabled(False)
        menu.addSeparator()

        backlight = menu.addMenu("Backlight")
        self.color_actions = {}
        for name in MENU_COLORS:
            rgb = config.NAMED_COLORS[name]
            action = backlight.addAction(name.title())
            action.setCheckable(True)
            action.setIcon(swatch(rgb))
            action.triggered.connect(partial(self.pick_color, rgb))
            self.color_actions[name] = action
        backlight.addSeparator()
        backlight.addAction("Custom...").triggered.connect(self.pick_custom)
        self.backlight_menu = backlight

        bank = menu.addMenu("Bank")
        banks = QActionGroup(self)
        banks.setExclusive(True)
        self.bank_actions = {}
        for name in config.BANKS:
            action = bank.addAction(f"M{name}")
            action.setCheckable(True)
            banks.addAction(action)
            action.triggered.connect(partial(self.pick_bank, name))
            self.bank_actions[name] = action
        self.bank_menu = bank

        screen = menu.addMenu("Screen")
        chosen = QActionGroup(self)
        chosen.setExclusive(True)
        self.screen_actions = {}
        for name in screens.SCREENS:
            action = screen.addAction(screen_label(name))
            action.setCheckable(True)
            chosen.addAction(action)
            action.triggered.connect(partial(self.pick_screen, name))
            self.screen_actions[name] = action

        menu.addSeparator()
        self.agent_action = menu.addAction("Pause agent")
        self.agent_action.triggered.connect(self.toggle_agent)
        menu.addAction("Open window").triggered.connect(self.open_window)
        menu.addAction("Quit").triggered.connect(self.quit)

        self.menu = menu
        self.setContextMenu(menu)

    # -- state -------------------------------------------------------------

    def sync(self):
        """Re-read and put the menu back in step with the config."""
        self.settings = config.load()
        self.adopt(self.settings)

    def adopt(self, settings):
        self.settings = settings
        rgb = settings_color(settings)
        current = config.format_color(rgb)
        for name, action in self.color_actions.items():
            action.setChecked(
                config.format_color(config.NAMED_COLORS[name]) == current)

        brightness = int(settings.get("brightness", 100))
        self.backlight_menu.setTitle(
            "Backlight" if brightness == 100 else f"Backlight  {brightness}%")

        bank = str(settings.get("active_bank", "1"))
        for name, action in self.bank_actions.items():
            action.setChecked(name == bank)
        self.bank_menu.setTitle(f"Bank  M{bank}")

        lcd = settings.get("lcd") or {}
        active = lcd.get("screen", "status")
        for name, action in self.screen_actions.items():
            action.setChecked(lcd.get("enabled", True) and name == active)

        running = cli.agent_loaded()
        self.agent_action.setText("Pause agent" if running else "Resume agent")
        self.header.setText(
            "G510 connected" if self.connected else "G510 not found")
        # The colour the user chose, not the brightness-scaled one: at 0% the
        # keyboard is dark but the tray still has to be something you can see.
        self.setIcon(keyboard_icon(rgb, self.connected))
        self.setToolTip("G510 keyboard" if self.connected
                        else "G510 keyboard not found")

    def set_connected(self, present):
        self.connected = bool(present)
        self.adopt(self.settings)

    # -- actions -----------------------------------------------------------

    def pick_color(self, rgb, _checked=False):
        apply_color(self.bus, rgb)
        self.sync()

    def pick_custom(self, _checked=False):
        colour = QColorDialog.getColor(
            QColor(*settings_color(self.settings)), None, "Backlight colour")
        if not colour.isValid():
            return
        apply_color(self.bus, (colour.red(), colour.green(), colour.blue()))
        self.sync()

    def pick_bank(self, name, _checked=False):
        apply_bank(self.bus, name)
        self.sync()

    def pick_screen(self, name, _checked=False):
        apply_screen(self.bus, name)
        self.sync()

    def toggle_agent(self, _checked=False):
        """Stop or start the service, leaving whether it runs at login alone.

        `g510 stop` disables the unit as well, because the macOS command it
        replaced wrote a plist with RunAtLoad and stop meant "and not at the
        next login either". Pausing from a tray means the other thing, so
        this is the plain stop/start pair `g510 verify` uses while it needs
        the keys to itself.
        """
        running = cli.agent_loaded()
        verb = "stop" if running else "start"
        try:
            result = subprocess.run(["systemctl", "--user", verb, cli.UNIT],
                                    capture_output=True, text=True)
        except OSError as exc:
            self.bus.status.emit(
                f"Could not run systemctl ({exc}). Without systemd, run the "
                "agent in the foreground with: g510 run")
            return
        if result.returncode != 0:
            self.bus.status.emit(f"systemctl refused: {result.stderr.strip()}")
            return
        self.bus.status.emit(
            "Agent paused, so pressing a G-key does not fire its binding"
            if running else "Agent restarted.")
        self.sync()

    def open_window(self, _checked=False):
        if self.window is None:
            return
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def clicked(self, reason):
        """Left click opens the window; Plasma leaves the right one to us."""
        if reason == QSystemTrayIcon.Trigger:
            self.open_window()

    def quit(self, _checked=False):
        QApplication.instance().quit()


# -- the keyboard being there ----------------------------------------------

class Watch(QObject):
    """Poll for the keyboard, because it is driven from elsewhere too.

    control.present() is the cheap question - a socket the agent holds, or a
    hidraw node in sysfs - and it is the only one worth asking on a timer.
    Anything more would mean talking to the device to find out whether we can
    talk to the device.
    """

    def __init__(self, bus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.present = None
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        self.check()

    def check(self):
        ok, present = guard(control.present)
        present = bool(ok and present)
        if present == self.present:
            return                      # only the changes are worth announcing
        self.present = present
        self.bus.device_changed.emit(present)


# -- window ----------------------------------------------------------------

class MainWindow(QMainWindow):
    """Everything the tray does not: the panel, the sliders, the tabs.

    The top of the window is the state of the keyboard - what the LCD shows,
    what colour it is lit, which bank it is on - and the tabs below are the
    setup behind that. Same split as the Mac window, with a tab bar where it
    has a segmented control.
    """

    def __init__(self, bus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.settings = config.load()
        self.connected = True
        self.setWindowTitle("G510")
        self.build()
        self.watch = Watch(bus, self)
        bus.config_changed.connect(self.adopt)
        bus.device_changed.connect(self.set_connected)
        bus.status.connect(self.say)
        self.adopt(self.settings)
        if PANE_ERRORS:
            self.say("Some tabs could not be loaded - " + "; ".join(PANE_ERRORS))
        elif config.last_error:
            self.say(config.last_error)

    # -- construction ------------------------------------------------------

    def build(self):
        root = QWidget()
        column = QVBoxLayout(root)
        column.setContentsMargins(12, 12, 12, 8)
        column.setSpacing(10)
        column.addLayout(self.build_display())
        column.addWidget(self.rule())
        column.addLayout(self.build_backlight())
        column.addLayout(self.build_bank())
        column.addWidget(self.build_tabs(), 1)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.resize(560, 620)

    def rule(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def build_display(self):
        row = QHBoxLayout()
        row.setSpacing(14)
        self.preview = LcdPreview(2)
        row.addWidget(self.preview, 0, Qt.AlignTop)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(QLabel("Display"))
        self.screen_box = QComboBox()
        for name in screens.SCREENS:
            self.screen_box.addItem(screen_label(name), name)
        self.screen_box.activated.connect(self.screen_chosen)
        side.addWidget(self.screen_box)
        self.showing = QLabel("")
        self.showing.setEnabled(False)          # a caption, not a control
        side.addWidget(self.showing)
        side.addStretch(1)
        row.addLayout(side, 1)
        return row

    def build_backlight(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Backlight"))
        self.color_button = QPushButton()
        self.color_button.setFixedSize(56, 26)
        self.color_button.setToolTip("Pick the backlight colour")
        self.color_button.clicked.connect(self.color_chosen)
        row.addWidget(self.color_button)
        self.color_label = QLabel("")
        self.color_label.setMinimumWidth(72)
        row.addWidget(self.color_label)

        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setPageStep(10)
        self.brightness.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # The label follows the handle, but the config write and the USB round
        # trip behind it wait for the drag to end. Applying on every pixel
        # would save the file a hundred times across one swipe.
        self.brightness.valueChanged.connect(self.brightness_moved)
        self.brightness.sliderReleased.connect(self.brightness_settled)
        row.addWidget(self.brightness, 1)
        self.brightness_label = QLabel("")
        self.brightness_label.setMinimumWidth(42)
        row.addWidget(self.brightness_label)
        return row

    def build_bank(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Bank"))
        self.banks = QButtonGroup(self)
        self.bank_buttons = {}
        for name in config.BANKS:
            button = QRadioButton(f"M{name}")
            self.banks.addButton(button)
            button.clicked.connect(partial(self.bank_chosen, name))
            row.addWidget(button)
            self.bank_buttons[name] = button
        row.addStretch(1)
        return row

    def build_tabs(self):
        """The tabs from the pane modules, each one optional.

        A pane can fail twice: its module may not import, which is caught at
        the top of this file, or its constructor may throw on a config it did
        not expect. Both cost that tab and a line in the status bar. The
        except is broad because a half-written pane can raise anything, and
        none of it is worth the window for.
        """
        self.tabs = QTabWidget()
        for label, pane in (("Keys", BindingsPane), ("Screens", ScreensPane),
                            ("Printer", PrinterPane), ("Gauges", GaugesPane),
                            ("About", AboutPane)):
            if pane is None:
                continue
            try:
                self.tabs.addTab(pane(self.bus), label)
            except Exception as exc:
                PANE_ERRORS.append(f"{label}: {exc}")
        return self.tabs

    # -- state -------------------------------------------------------------

    def adopt(self, settings):
        """Put the controls back in step with the config.

        Signals are blocked while setting them: the agent, the CLI and the
        tray all write this file, so this runs on changes the window did not
        make, and a control that reacted to being told the truth would write
        it straight back out again.
        """
        self.settings = settings
        rgb = settings_color(settings)
        self.color_button.setStyleSheet(
            "background-color: {}; border: 1px solid palette(mid);".format(
                config.format_color(rgb)))
        self.color_label.setText(config.format_color(rgb))

        brightness = int(settings.get("brightness", 100))
        self.brightness.blockSignals(True)
        self.brightness.setValue(brightness)
        self.brightness.blockSignals(False)
        self.brightness_label.setText(f"{brightness}%")

        bank = str(settings.get("active_bank", "1"))
        for name, button in self.bank_buttons.items():
            button.blockSignals(True)
            button.setChecked(name == bank)
            button.blockSignals(False)

        lcd = settings.get("lcd") or {}
        name = lcd.get("screen", "status")
        index = self.screen_box.findData(name)
        if index >= 0:
            self.screen_box.blockSignals(True)
            self.screen_box.setCurrentIndex(index)
            self.screen_box.blockSignals(False)
        self.preview.set_screen(name)
        self.showing.setText("showing now" if lcd.get("enabled", True)
                             else "the display is blank")

    def set_connected(self, present):
        """Grey out what cannot be done, rather than letting it fail.

        Only the controls that reach the keyboard. The tabs stay live: a
        binding or a printer host is worth editing with the keyboard on
        another desk, and takes effect when it comes back.
        """
        self.connected = bool(present)
        for widget in (self.color_button, self.brightness, self.screen_box):
            widget.setEnabled(self.connected)
        for button in self.bank_buttons.values():
            button.setEnabled(self.connected)
        self.say("" if self.connected else
                 "No G510 found. Plug it in, or check that hid-lg-g15 is "
                 "loaded: g510 info")

    def say(self, message):
        self.statusBar().showMessage(message)

    # -- actions -----------------------------------------------------------

    def color_chosen(self):
        colour = QColorDialog.getColor(
            QColor(*settings_color(self.settings)), self, "Backlight colour")
        if not colour.isValid():
            return
        apply_color(self.bus, (colour.red(), colour.green(), colour.blue()))

    def brightness_moved(self, value):
        self.brightness_label.setText(f"{value}%")
        if not self.brightness.isSliderDown():
            apply_brightness(self.bus, value)     # arrow keys, page up, a click

    def brightness_settled(self):
        apply_brightness(self.bus, self.brightness.value())

    def bank_chosen(self, name, _checked=False):
        apply_bank(self.bus, name)

    def screen_chosen(self, index):
        name = self.screen_box.itemData(index)
        self.preview.set_screen(name)
        apply_screen(self.bus, name)

    # -- visibility --------------------------------------------------------
    #
    # The window is closed far more often than the app is quit - that is what
    # the tray is for - so a hidden window must stop costing anything. The
    # device poll keeps running, because the tray icon still shows it.

    def showEvent(self, event):
        super().showEvent(event)
        self.adopt(config.load())
        self.preview.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.preview.stop()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    app = QApplication(sys.argv)
    # Plasma groups a window with its launcher and its tray item by the
    # desktop file name. Without these it is an unnamed python3 with the
    # generic icon, sitting on its own in the task manager.
    app.setApplicationName("G510")
    app.setApplicationDisplayName("G510")
    app.setDesktopFileName("g510")
    # Closing the window leaves the tray item running, which is the whole
    # point of having one.
    app.setQuitOnLastWindowClosed(False)

    bus = Bus()
    window = MainWindow(bus)
    tray = Tray(bus, window)
    tray.show()

    # A desktop with no tray - or one that has not started it yet - would
    # leave --tray with nothing on screen and no way back to it.
    wants_tray = "--tray" in argv and QSystemTrayIcon.isSystemTrayAvailable()
    if not wants_tray:
        window.show()
        if "--tray" in argv:
            bus.status.emit("No system tray here, so the window stays open")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
