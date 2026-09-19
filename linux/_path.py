"""Import model for the Linux port.

The macOS modules at the repo root split cleanly into two groups: the ones
that touch the platform (device, lcd, actions, recorder, screens) and the ones
that are plain Python (config, daemon, ipc, control, printer, layout, refresh).

Rather than fork all of it, this package is placed ahead of the repo root on
sys.path. A bare `import device` inside daemon.py then resolves to the Linux
device.py sitting next to this file, while `import config` falls through to the
shared one at the root. The daemon is shared; only the hardware under it swaps.

Every Linux entry point calls setup() before importing anything else.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def setup():
    """Put the Linux modules ahead of the shared ones on sys.path."""
    for path in (ROOT, HERE):          # HERE ends up first
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    return HERE, ROOT
