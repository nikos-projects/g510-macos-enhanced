"""The four settings panes: the display, the printer, the gauges, and About.

These are the Linux answers to pane_display.py and pane_agent.py on the Mac,
split up rather than stacked into one scrolling column: Qt gives a tab bar
cheaply and a 320x43 display has more to configure here than there, because
the Linux build has to expose the cycle and all five soft keys rather than
four fixed screen pickers.

Nothing in here opens the keyboard. Anything that touches it goes through
gui_common.guard and control, and everything else is the config file, which
the agent re-reads when it is told to.

Two calls could block the event loop for seconds - a printer that has gone
away costs the connect timeout per backend, and a gauge command is somebody
else's program - so both run on a worker thread and come back as a signal.
"""
import gzip
import os
import subprocess
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSpinBox, QTableWidget, QVBoxLayout, QWidget)

import actions
import cli
import config
import control
import device
import gui_common
import printer
import screens

# The Mac keeps these in widgets.py, which imports AppKit, so they are
# restated rather than shared. "app" is listed but flagged: the screen still
# renders, it just has nothing to say here. See screens.frontmost_app.
SCREEN_LABELS = {
    "status": "Status", "clock": "Clock", "claude": "Claude Usage",
    "media": "Now Playing", "printer": "3D Printer", "gauge": "Gauges",
    "gkeys": "G-key Echo", "app": "Active App (nothing to show on Wayland)",
}

LCD_KEYS = ("L1", "L2", "L3", "L4", "L5")

# cli.cmd_gauge stops at three, because that is what the display has room
# for. Saying so here as well keeps the two front ends agreeing.
MAX_GAUGES = 3


def screen_label(name):
    return SCREEN_LABELS.get(name, str(name).replace("_", " ").title())


def save(bus, settings):
    """Persist, reload the agent, and tell the other panes.

    The same three steps cli.save_config + cli.reload_agent do, in the same
    order. config.save() refuses while the file on disk is unparseable -
    someone is hand-editing it - and that is the case worth reporting.
    """
    if not config.save(settings):
        bus.status.emit(config.last_error
                        or f"Could not write {config.CONFIG_PATH}")
        return False
    cli.reload_agent()
    bus.announce(config.load())
    return True


def in_background(work, done):
    """Run `work` off the event loop and hand its result to a Qt signal.

    `done` must be a bound Signal: emitting one from another thread is the
    supported way across threads in Qt, and it lands back on the GUI thread.
    """
    def run():
        try:
            done.emit(work())
        except Exception as exc:                 # a gauge runs arbitrary code
            done.emit(f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()


# -- the display -----------------------------------------------------------

class ScreensPane(QWidget):
    """What the LCD shows, in what order, and what the five keys under it do."""

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self._loading = False

        layout = QVBoxLayout(self)

        display = QGroupBox("Display")
        form = QFormLayout(display)
        self.enabled = QCheckBox("Draw on the LCD")
        self.enabled.toggled.connect(self._save_display)
        form.addRow(self.enabled)

        self.screen = QComboBox()
        for name in screens.SCREENS:
            self.screen.addItem(screen_label(name), name)
        self.screen.currentIndexChanged.connect(self._save_display)
        form.addRow("Showing", self.screen)

        self.refresh = QDoubleSpinBox()
        self.refresh.setRange(0.2, 60.0)
        self.refresh.setSingleStep(0.5)
        self.refresh.setDecimals(1)
        self.refresh.setSuffix(" s")
        self.refresh.setToolTip(
            "How often the agent redraws. The clock wants one second; a "
            "screen that shells out is cheaper slower.")
        # editingFinished, not valueChanged: this saves the config and
        # redraws the display, and valueChanged fires on every keystroke and
        # every click of the arrows.
        self.refresh.editingFinished.connect(self._save_display)
        form.addRow("Redraw every", self.refresh)
        layout.addWidget(display)

        cycle = QGroupBox("Cycle")
        inner = QVBoxLayout(cycle)
        hint = QLabel("Ticked screens are the ones 'next' walks through. "
                      "Drag to reorder.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        inner.addWidget(hint)
        self.cycle = QListWidget()
        self.cycle.setDragDropMode(QAbstractItemView.InternalMove)
        self.cycle.setDefaultDropAction(Qt.MoveAction)
        self.cycle.setSelectionMode(QAbstractItemView.SingleSelection)
        self.cycle.itemChanged.connect(self._save_cycle)
        # rowsMoved rather than a drop event: the move is only real once the
        # model has it, and a dropEvent override would fire before that.
        self.cycle.model().rowsMoved.connect(self._save_cycle)
        inner.addWidget(self.cycle)
        layout.addWidget(cycle, 1)

        keys = QGroupBox("Keys under the display")
        key_form = QFormLayout(keys)
        self.lcd_keys = {}
        for name in LCD_KEYS:
            combo = QComboBox()
            combo.addItem("Next screen", "next")
            combo.addItem("Previous screen", "prev")
            combo.addItem("Blank the display", "off")
            for screen in screens.SCREENS:
                combo.addItem(f"Show {screen_label(screen)}",
                              f"screen:{screen}")
            combo.currentIndexChanged.connect(self._save_lcd_keys)
            key_form.addRow(name, combo)
            self.lcd_keys[name] = combo
        layout.addWidget(keys)

        bus.config_changed.connect(self.repopulate)
        self.repopulate(config.load())

    def repopulate(self, settings):
        self._loading = True
        try:
            lcd = settings.get("lcd") or {}
            self.enabled.setChecked(bool(lcd.get("enabled", True)))
            self._select(self.screen, lcd.get("screen", "status"))
            self.refresh.setValue(float(lcd.get("refresh_seconds") or 1.0))

            # Configured order first, then everything else unticked, so the
            # list reads as the cycle with the leftovers underneath.
            chosen = [n for n in (lcd.get("cycle") or [])
                      if n in screens.SCREENS]
            rest = [n for n in screens.SCREENS if n not in chosen]
            self.cycle.clear()
            for name in chosen + rest:
                item = QListWidgetItem(screen_label(name))
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if name in chosen
                                   else Qt.Unchecked)
                self.cycle.addItem(item)

            mapping = settings.get("lcd_keys") or {}
            for name, combo in self.lcd_keys.items():
                self._select(combo, mapping.get(name) or "next")
        finally:
            self._loading = False

    def _select(self, combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _save_display(self):
        if self._loading:
            return
        settings = config.load()
        lcd = settings.setdefault("lcd", {})
        lcd["enabled"] = self.enabled.isChecked()
        lcd["screen"] = self.screen.currentData()
        lcd["refresh_seconds"] = round(self.refresh.value(), 1)
        if not save(self.bus, settings):
            return
        # Draw it now rather than waiting for the agent's next pass, so the
        # display agrees with the combo while the user is still looking.
        if lcd["enabled"]:
            self.bus.report(*gui_common.guard(control.show_screen,
                                              lcd["screen"]))
        else:
            self.bus.report(*gui_common.guard(control.clear_lcd))

    def _save_cycle(self, *_args):
        if self._loading:
            return
        chosen = []
        for index in range(self.cycle.count()):
            item = self.cycle.item(index)
            if item.checkState() == Qt.Checked:
                chosen.append(item.data(Qt.UserRole))
        settings = config.load()
        settings.setdefault("lcd", {})["cycle"] = chosen
        if save(self.bus, settings):
            self.bus.status.emit(
                "cycle: " + (", ".join(chosen) if chosen
                             else "nothing ticked - 'next' has nowhere to go"))

    def _save_lcd_keys(self):
        if self._loading:
            return
        settings = config.load()
        mapping = settings.setdefault("lcd_keys", {})
        for name, combo in self.lcd_keys.items():
            mapping[name] = combo.currentData()
        save(self.bus, settings)


# -- the printer -----------------------------------------------------------

class PrinterPane(QWidget):
    """The 3D printer the printer screen reads from."""

    tested = Signal(str)

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self._loading = False

        layout = QVBoxLayout(self)
        box = QGroupBox("Printer")
        form = QFormLayout(box)

        self.enabled = QCheckBox("Read a printer on the network")
        self.enabled.toggled.connect(self._save)
        form.addRow(self.enabled)

        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.50")
        self.host.editingFinished.connect(self._save)
        form.addRow("Address", self.host)

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setToolTip(
            "Creality's own firmware answers on 9999. Moonraker defaults to "
            "7125 and OctoPrint to 80, and each is used when that backend is "
            "picked by name rather than by auto.")
        self.port.editingFinished.connect(self._save)
        form.addRow("Port", self.port)

        self.kind = QComboBox()
        self.kind.addItems(printer.BACKENDS)
        self.kind.currentIndexChanged.connect(self._save)
        form.addRow("Firmware", self.kind)

        self.api_key = QLineEdit()
        # A credential: never on screen, never in a status line, and the only
        # thing said about it is whether one is set. OctoPrint and PrusaLink
        # refuse without it; Creality and Moonraker never ask.
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("only OctoPrint and PrusaLink need one")
        self.api_key.editingFinished.connect(self._save)
        form.addRow("API key", self.api_key)

        row = QHBoxLayout()
        self.test_button = QPushButton("Test")
        self.test_button.clicked.connect(self._test)
        row.addWidget(self.test_button)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.result, 1)
        form.addRow(row)

        layout.addWidget(box)
        layout.addStretch(1)

        self.tested.connect(self._show_result)
        bus.config_changed.connect(self.repopulate)
        self.repopulate(config.load())

    def repopulate(self, settings):
        self._loading = True
        try:
            block = settings.get("printer") or {}
            self.enabled.setChecked(bool(block.get("enabled")))
            self.host.setText(block.get("host") or "")
            self.port.setValue(int(block.get("port") or printer.DEFAULT_PORT))
            kind = (block.get("kind") or "auto").lower()
            self.kind.setCurrentText(
                kind if kind in printer.BACKENDS else "auto")
            self.api_key.setText(block.get("api_key") or "")
        finally:
            self._loading = False

    def _save(self):
        if self._loading:
            return
        settings = config.load()
        block = settings.setdefault("printer", {})
        block["enabled"] = self.enabled.isChecked()
        block["host"] = self.host.text().strip()
        block["port"] = int(self.port.value())
        block["kind"] = self.kind.currentText()
        block["api_key"] = self.api_key.text().strip()
        save(self.bus, settings)

    def _test(self):
        """Ask the printer, off the event loop.

        printer.status(force=True) is synchronous and tries each backend in
        turn on auto, so an address with nothing on it costs four connect
        timeouts. Saving first is deliberate: printer.settings() reads the
        config file, so an unsaved field would not be the one tested.
        """
        self._save()
        self.test_button.setEnabled(False)
        self.result.setText("asking...")
        in_background(_probe_printer, self.tested)

    def _show_result(self, text):
        self.test_button.setEnabled(True)
        self.result.setText(text)


def _probe_printer():
    """One line saying what the printer said, or why it said nothing."""
    reading = printer.status(force=True)
    if not reading:
        return printer.last_error() or "no reply, and no error to show for it"
    parts = [reading["model"], printer.describe_state(reading)]
    if printer.is_printing(reading):
        parts.append(f"{reading['progress']}%")
    if reading["file"]:
        parts.append(reading["file"])
    parts.append(f"nozzle {reading['nozzle']:.0f}, bed {reading['bed']:.0f}")
    return " - ".join(str(part) for part in parts)


# -- the gauges ------------------------------------------------------------

class GaugesPane(QWidget):
    """Bars on the display, each fed by a command that prints one number."""

    ran = Signal(str)

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self._loading = False

        layout = QVBoxLayout(self)
        note = QLabel(
            "Each gauge runs its command every few seconds and reads the "
            "first number it prints. The agent runs them, as you, with your "
            "shell and your environment - so a command here can do anything "
            "you can do at a terminal.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Label", "Command", "", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("Add a gauge")
        self.add_button.clicked.connect(self._add)
        row.addWidget(self.add_button)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.result, 1)
        layout.addLayout(row)

        self.ran.connect(self.result.setText)
        bus.config_changed.connect(self.repopulate)
        self.repopulate(config.load())

    def repopulate(self, settings):
        """Rebuild the rows. There are at most three, so this is cheap."""
        self._loading = True
        try:
            gauges = settings.get("gauges") or []
            self.table.setRowCount(len(gauges))
            for index, entry in enumerate(gauges):
                self._fill_row(index, entry)
            self.add_button.setEnabled(len(gauges) < MAX_GAUGES)
            self.add_button.setToolTip(
                "" if len(gauges) < MAX_GAUGES
                else f"{MAX_GAUGES} is all the display has room for")
        finally:
            self._loading = False
        self.table.resizeRowsToContents()

    def _fill_row(self, index, entry):
        label = QLineEdit(str(entry.get("label") or ""))
        label.setPlaceholderText("Disk")
        # The screen clips a label to eight characters, so the field does too
        # rather than letting someone type a name they will never see.
        label.setMaxLength(8)
        label.editingFinished.connect(self._save)
        self.table.setCellWidget(index, 0, label)

        command = QLineEdit(str(entry.get("command") or ""))
        command.setPlaceholderText("df --output=pcent / | tail -1")
        command.editingFinished.connect(self._save)
        self.table.setCellWidget(index, 1, command)

        run = QPushButton("Run")
        run.setToolTip("Run it now and show what it prints")
        run.clicked.connect(lambda _checked, row=index: self._run(row))
        self.table.setCellWidget(index, 2, run)

        remove = QPushButton("Remove")
        remove.clicked.connect(lambda _checked, row=index: self._remove(row))
        self.table.setCellWidget(index, 3, remove)

    def _rows(self):
        entries = []
        for index in range(self.table.rowCount()):
            label = self.table.cellWidget(index, 0).text().strip()
            command = self.table.cellWidget(index, 1).text().strip()
            if label or command:
                entries.append({"label": label or "?", "command": command})
        return entries

    def _save(self):
        if self._loading:
            return
        settings = config.load()
        settings["gauges"] = self._rows()
        save(self.bus, settings)

    def _add(self):
        settings = config.load()
        gauges = settings.setdefault("gauges", [])
        if len(gauges) >= MAX_GAUGES:
            return
        gauges.append({"label": "", "command": ""})
        save(self.bus, settings)

    def _remove(self, index):
        settings = config.load()
        gauges = settings.setdefault("gauges", [])
        if 0 <= index < len(gauges):
            gauges.pop(index)
        save(self.bus, settings)

    def _run(self, index):
        command = self.table.cellWidget(index, 1).text().strip()
        if not command:
            self.result.setText("Nothing to run - the command is empty.")
            return
        self.result.setText("running...")
        in_background(lambda: _run_gauge(command), self.ran)


def _run_gauge(command):
    """Run one gauge command and describe what came back.

    Deliberately the same shell, capture and ten-second timeout as
    screens._run_gauge, so what this prints is what the display will read.
    Where it differs is on failure: the screen shows a blank bar, and here
    the point is to say why.
    """
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "gave up after 10 seconds - the display would show nothing"
    except OSError as exc:
        return f"could not run it: {exc}"
    first = (result.stdout or "").strip().split("\n")[0].strip()
    if not first:
        stderr = (result.stderr or "").strip().split("\n")[0]
        return (f"printed nothing (exit {result.returncode})"
                + (f": {stderr}" if stderr else ""))
    try:
        value = float(first.rstrip("%"))
    except ValueError:
        return f"printed {first!r}, which is not a number - the bar stays empty"
    return f"printed {first!r} - the bar reads {value:g}"


# -- about -----------------------------------------------------------------

# Written in full rather than linked, because the two things people go
# looking for in a settings window are exactly the two the Linux build
# cannot do, and a missing control with no explanation reads as a bug.
CAVEATS = (
    "Per-app binding profiles are not available. Wayland gives an ordinary "
    "program no way to find out which window has focus, and there is no "
    "portal for it either, so a binding that applied only in one application "
    "could never know when to fire. The macOS build has them; this one uses "
    "the M1/M2/M3 banks instead, which you switch on the keyboard.",
    "The joystick switch at the top left does nothing. It is Logitech's "
    "game-mode switch, and the kernel's hid-lg-g15 driver does not report "
    "its position, so nothing here can see it move. The setting survives in "
    "the config file for the sake of a config shared with a Mac, and that "
    "is all it does.",
)


def version():
    """The packaged version, read from whichever changelog is present.

    There is no version constant anywhere in the tree - packaging/build-deb.sh
    takes it from the Debian changelog so there is one place to bump it - so
    this looks in the same two places rather than adding a second source of
    truth to drift.
    """
    source = os.path.join(cli.APP_DIR, "packaging", "debian", "changelog")
    try:
        with open(source) as handle:
            first = handle.readline()
    except OSError:
        installed = "/usr/share/doc/g510-linux/changelog.Debian.gz"
        try:
            with gzip.open(installed, "rt") as handle:
                first = handle.readline()
        except OSError:
            return "unknown"
    _, _, rest = first.partition("(")
    return rest.partition(")")[0] or "unknown"


class AboutPane(QWidget):
    """What is attached, where its pieces live, and what this build cannot do."""

    def __init__(self, bus):
        super().__init__()
        self.bus = bus

        layout = QVBoxLayout(self)
        box = QGroupBox("This machine")
        self.facts = QFormLayout(box)
        layout.addWidget(box)

        button = QPushButton("Look again")
        button.setToolTip("Re-probe the device; none of this comes from the "
                          "config file")
        button.clicked.connect(self.probe)
        layout.addWidget(button, 0, Qt.AlignLeft)

        notes = QGroupBox("What this build does not do")
        notes_layout = QVBoxLayout(notes)
        for text in CAVEATS:
            label = QLabel(text)
            label.setWordWrap(True)
            notes_layout.addWidget(label)
        layout.addWidget(notes)
        layout.addStretch(1)

        # The config only supplies the path, but reloading on it is how this
        # pane learns that the agent came up or went away.
        bus.config_changed.connect(lambda _settings: self.probe())
        self.probe()

    def probe(self):
        """Ask the system, not the keyboard.

        Every line here is the same one cli.cmd_info prints, and for the same
        reason: sysfs and /proc answer without opening the device, so this
        still says something useful when the keyboard is unplugged or the
        agent holds it.
        """
        while self.facts.rowCount():
            self.facts.removeRow(0)

        ok, info = gui_common.guard(control.device_info)
        self._row("Keyboard", " ".join(info) if ok else info, bad=not ok)

        path = device.find_path()
        self._row("LCD", path or "no hidraw node for interface 1",
                  bad=not path)

        # Constructing an _RgbLed only stats sysfs - it opens nothing and
        # needs no keyboard - so this answers even when the device is
        # unwritable. cli.cmd_info does exactly the same.
        leds = os.path.join(device.SYSFS_LEDS, device.BACKLIGHT_LED)
        mode = device._RgbLed(device.BACKLIGHT_LED).mode
        self._row("Backlight",
                  f"{leds}  ({cli.LED_MODES[mode]})" if mode
                  else f"{leds} is not there; hid-lg-g15 is probably not loaded",
                  bad=not mode)

        # Out of /proc rather than evdev, so this answers on a machine that
        # has not installed python3-evdev yet - which is exactly when it is
        # asked.
        keys = actions.input_node(device.GAMING_KEYS_NAME)
        self._row("G-keys", keys or
                  f"no input device named {device.GAMING_KEYS_NAME!r}",
                  bad=not keys)

        running = control.daemon_running()
        self._row("Agent",
                  "running - it is answering on its control socket" if running
                  else "not running, so the G-keys do nothing (g510 start)",
                  bad=not running)

        self._row("Config", config.CONFIG_PATH, bad=bool(config.last_error))
        if config.last_error:
            self._row("", config.last_error, bad=True)
        self._row("Version", version())

    def _row(self, name, value, bad=False):
        label = QLabel(str(value))
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if bad:
            label.setStyleSheet("color: #da4453;")
        self.facts.addRow(name, label)
