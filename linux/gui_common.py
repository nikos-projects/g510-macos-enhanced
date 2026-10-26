"""The bits the GUI panes share: error handling and a change bus.

The GUI never touches the keyboard itself. control.py already talks to the
running agent over its unix socket, and falls back to opening the device
directly when the agent is not running, so every pane goes through that and
gets the same behaviour the command line has.

What the panes do need in common is somewhere to put failures - a keyboard on
a dock can vanish mid-click, and a pane should grey out rather than raise -
and a way to hear that someone else changed the config, so two panes showing
the same bank do not disagree.
"""
import control


def guard(work, *args, **kwargs):
    """Run a control call. Returns (ok, value) with the error text on failure.

    Every control call can fail the same three ways: the keyboard is gone, the
    agent is not running, or the agent said no. The GUI treats all three as
    "show the message, stay usable", so they collapse into one return shape.
    """
    try:
        return True, work(*args, **kwargs)
    except (control.ControlError, OSError, ValueError) as exc:
        return False, str(exc)


try:
    from PySide6.QtCore import QObject, Signal
except ImportError as exc:                    # pragma: no cover - import guard
    raise ImportError(
        "The GUI needs PySide6: apt install python3-pyside6.qtwidgets") from exc


class Bus(QObject):
    """One shared signal hub, so panes do not have to know about each other.

    config_changed carries the freshly loaded settings dict. device_changed
    carries True when the keyboard is reachable. status carries a line for the
    window's status bar - panes report failures there rather than in dialogs,
    because a disconnected dock should not throw modal boxes at anyone.
    """

    config_changed = Signal(dict)
    device_changed = Signal(bool)
    status = Signal(str)

    def announce(self, settings):
        self.config_changed.emit(settings)

    def report(self, ok, value):
        """Push a guard() result at the status bar. Returns ok, for chaining."""
        if not ok:
            self.status.emit(value)
        return ok
