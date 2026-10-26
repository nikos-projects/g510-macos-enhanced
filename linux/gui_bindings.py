"""The Bindings pane: what each of the 18 G-keys does in the active bank.

The macOS equivalent (pane_keys.py) lists the keys in a table and opens a
modal sheet to change one. A sheet per edit is a lot of clicks for eighteen
rows, and Qt lets a widget live in a cell, so the table holds the editor: a
type combo and a value field on every row, changed in place.

Two things the Mac pane has are deliberately missing.

Per-app overrides are gone because they cannot work: screens.frontmost_app()
is a permanent None on Wayland, so an override keyed on the focused window
would never fire. The About pane says so rather than this one offering a
control that does nothing.

Macro recording is gone because there is no clean route to it from here. The
recorder grabs the G510's evdev node for the length of a recording
(EVIOCGRAB, see recorder.py) and blocks until Escape; cli.cmd_record runs it
in-process, and the agent's own recorder is reached by pressing MR on the
keyboard, not over the control socket - daemon.run_command has no `record`
command to ask for. Driving it from the GUI would therefore mean this process
seizing the keyboard out from under its own event loop, so the button is left
out. `g510 record G5`, or the MR key, still work.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QTableWidget, QVBoxLayout,
                               QWidget)

import actions
import cli
import config
import device

# gui_common is not imported here on purpose: this pane never reaches the
# keyboard, so it has nothing to put through guard(). A binding is config,
# and the agent picks it up on the reload below.

# Exactly what cli.cmd_bind accepts, in the same order it lists them, plus
# the field name each type stores its value under. `macro` is not offered:
# it is what the recorder writes, and only appears on a row that already
# holds one (see _fill_row).
BINDING_TYPES = ("app", "keys", "shell", "text", "none")

FIELDS = {"app": "name", "keys": "keys", "shell": "command", "text": "text",
          "macro": "name"}

# Linux examples rather than the Mac pane's Safari and cmd-chords. A desktop
# id works as well as a binary name - actions.launch_app takes either.
HINTS = {
    "app": "firefox, or org.kde.konsole.desktop",
    "keys": "a key chord, e.g. ctrl+shift+t",
    "shell": "a shell command, e.g. loginctl lock-session",
    "text": "text to type",
    "macro": "name of a recorded macro",
    "none": "nothing - clears the key",
}


class BindingsPane(QWidget):
    """One row per G-key, edited in place, saved as you go."""

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        # Set while the table is being repopulated from a config, so the
        # signals that fire as cells are filled do not save what they just
        # read back over the file.
        self._loading = False
        self.rows = []

        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.heading)

        note = QLabel("The bank is chosen in the window above. Changes are "
                      "saved as you make them; an empty value clears the key.")
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)

        self.table = QTableWidget(device.GKEY_COUNT, 4, self)
        self.table.setHorizontalHeaderLabels(["Key", "Type", "Value", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        self._build_rows()
        bus.config_changed.connect(self.repopulate)
        # Populated now as well as on the bus, so the pane is never briefly
        # blank if it is shown before anyone announces.
        self.repopulate(config.load())

    # -- building ----------------------------------------------------------

    def _build_rows(self):
        """Put the widgets in once. Repopulating only changes their values.

        Rebuilding the cell widgets on every config change would drop
        whatever the user is halfway through typing and steal the focus, and
        the agent announces a config whenever anything else saves one.
        """
        for index in range(device.GKEY_COUNT):
            key = f"G{index + 1}"
            label = QLabel(key)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-family: monospace;")
            self.table.setCellWidget(index, 0, _padded(label))

            combo = QComboBox()
            combo.addItems(BINDING_TYPES)
            combo.currentTextChanged.connect(
                lambda _text, row=index: self._type_changed(row))
            self.table.setCellWidget(index, 1, _padded(combo))

            field = QLineEdit()
            field.setClearButtonEnabled(True)
            field.textChanged.connect(
                lambda _text, row=index: self._validate(row))
            field.editingFinished.connect(
                lambda row=index: self._commit(row))

            message = QLabel()
            message.setWordWrap(True)
            message.setVisible(False)

            cell = QWidget()
            inner = QVBoxLayout(cell)
            inner.setContentsMargins(4, 3, 4, 3)
            inner.setSpacing(1)
            inner.addWidget(field)
            inner.addWidget(message)
            self.table.setCellWidget(index, 2, cell)

            clear = QPushButton("Clear")
            clear.setToolTip(f"Unbind {key}")
            clear.clicked.connect(lambda _checked, row=index: self._clear(row))
            self.table.setCellWidget(index, 3, _padded(clear))

            self.rows.append({"key": key, "combo": combo, "field": field,
                              "message": message})

    # -- state -------------------------------------------------------------

    def repopulate(self, settings):
        """Show whatever bank the config says is active."""
        self._loading = True
        try:
            bank = str(settings.get("active_bank", "1"))
            bindings = settings.get("bindings") or {}
            bound = sum(1 for row in self.rows if bindings.get(row["key"]))
            self.heading.setText(
                f"Bank M{bank} - {bound} of {device.GKEY_COUNT} keys bound")
            for row in self.rows:
                self._fill_row(row, bindings.get(row["key"]) or {})
        finally:
            self._loading = False
        self.table.resizeRowsToContents()

    def _fill_row(self, row, binding):
        combo, field = row["combo"], row["field"]
        kind = binding.get("type") if isinstance(binding, dict) else None
        kind = kind if kind in FIELDS else "none"

        # A macro is not something this pane can author, but a key may
        # already hold one - the MR key and `g510 record` both write them.
        # Offering it on that row alone keeps it visible and replaceable
        # instead of being silently overwritten by whatever the combo
        # happened to be showing.
        has_macro = combo.count() > len(BINDING_TYPES)
        if kind == "macro" and not has_macro:
            combo.addItem("macro")
        elif kind != "macro" and has_macro:
            combo.removeItem(len(BINDING_TYPES))

        combo.setCurrentText(kind)
        field.setText("" if kind == "none"
                      else str(binding.get(FIELDS[kind], "") or ""))
        field.setPlaceholderText(HINTS[kind])
        field.setEnabled(kind != "none")
        field.setReadOnly(kind == "macro")
        if kind == "macro":
            steps = (config.load().get("macros") or {}).get(
                binding.get("name")) or []
            self._say(row, f"recorded macro, {len(steps)} steps - pick "
                           "another type to replace it")
        else:
            self._say(row, "")

    def _say(self, row, text, bad=False):
        message = row["message"]
        message.setText(text)
        message.setVisible(bool(text))
        # Palette names would be better than a colour, but Qt has no role for
        # "this is wrong"; red on both Breeze themes is legible enough.
        message.setStyleSheet("color: #da4453;" if bad else "")
        message.setEnabled(not bad)

    # -- editing -----------------------------------------------------------

    def _validate(self, index):
        """Check a chord as it is typed. Does not save; _commit does that.

        Worth doing live because an unparseable chord is silent otherwise -
        the key simply does nothing when pressed, hours later. Two of the
        shipped defaults were wrong this way until a test caught them.
        """
        if self._loading:
            return
        row = self.rows[index]
        if row["combo"].currentText() != "keys":
            return
        value = row["field"].text().strip()
        if not value:
            self._say(row, "")
            return
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            self._say(row, str(exc), bad=True)
            return
        self._say(row, "")

    def _type_changed(self, index):
        if self._loading:
            return
        row = self.rows[index]
        kind = row["combo"].currentText()
        row["field"].setPlaceholderText(HINTS.get(kind, ""))
        row["field"].setEnabled(kind != "none")
        row["field"].setReadOnly(False)
        if kind == "none":
            row["field"].clear()
        self._validate(index)
        self._commit(index)

    def _clear(self, index):
        row = self.rows[index]
        row["combo"].setCurrentText("none")
        # setCurrentText has already run _type_changed for everything but a
        # row that was on "none" to begin with, where there is nothing to do.

    def _commit(self, index):
        """Write one row to the config, if it is worth writing."""
        if self._loading:
            return
        row = self.rows[index]
        key, kind = row["key"], row["combo"].currentText()
        value = row["field"].text().strip()
        if kind == "macro":
            return                       # read-only; nothing here to save
        if kind == "keys" and value:
            try:
                actions.parse_chord(value)
            except actions.ActionError as exc:
                # Left unsaved on purpose: a chord that will not parse is a
                # key that does nothing, and the config should keep whatever
                # last worked until this one is fixed.
                self._say(row, str(exc), bad=True)
                return

        settings = config.load()
        bindings = settings.setdefault("bindings", {})
        if kind == "none" or not value:
            if bindings.pop(key, None) is None:
                return                   # already unbound; no write needed
            told = f"{key} cleared"
        else:
            binding = {"type": kind, FIELDS[kind]: value}
            if bindings.get(key) == binding:
                return
            bindings[key] = binding
            told = f"{key} -> {actions.describe(binding)}"
        if self._save(settings):
            self.bus.status.emit(told)

    def _save(self, settings):
        """Persist and tell everyone else, the way the CLI's save_config does.

        config.save() refuses while the file on disk is unparseable, which is
        the case worth reporting: someone is hand-editing it and a background
        write would clobber them.
        """
        if not config.save(settings):
            self.bus.status.emit(
                config.last_error or f"Could not write {config.CONFIG_PATH}")
            return False
        cli.reload_agent()
        self.bus.announce(config.load())
        return True


def _padded(widget):
    """Wrap a cell widget so it does not sit flush against the grid line."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(6, 3, 6, 3)
    layout.addWidget(widget)
    holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    return holder
