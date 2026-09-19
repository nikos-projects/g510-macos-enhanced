"""The shared control module, minus the one call that is macOS-only.

See _shared.py for why this is amended rather than forked.
"""
import _shared

_root = _shared.load("control")
_shared.reexport(_root, globals())


def iso_swap(_enable):
    """Not a thing on Linux.

    The macOS version exists because the G510 declares CountryCode 0, so
    macOS cannot tell an ISO board from an ANSI one and guesses wrong. It
    fixes that with a hidutil mapping. Linux has no such guess to correct:
    the layout comes from XKB, set per seat in the desktop's own keyboard
    settings, and hidutil does not exist here - without this override the
    shared version would die on FileNotFoundError.
    """
    raise ControlError(
        "There is no ISO/ANSI swap to make on Linux. The layout comes from "
        "XKB - set it in System Settings > Keyboard, or with localectl.")

_shared.adopt(_root, globals())
