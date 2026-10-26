"""Tests for the Linux port: no keyboard, no uinput, no sysfs writes.

The Linux modules shadow the shared ones by sitting ahead of the repo root on
sys.path, exactly as linux/_path.py describes - so `import device` here has to
resolve to linux/device.py while `import config` still falls through to the
root. This does the same two inserts _path.setup() does, without importing
_path itself (which is only reachable once the path is already set).

Anything that is genuinely platform-neutral - screens, config, printer - is
already covered by the root suite and is not duplicated here; see README.md.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.dirname(HERE)
ROOT = os.path.dirname(LINUX)

for _path in (ROOT, LINUX):            # LINUX ends up first
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)
