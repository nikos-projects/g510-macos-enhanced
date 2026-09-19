"""Run the root suites that are portable against the Linux modules.

tests/test_screens.py and tests/test_printer.py test logic that is the same
on both platforms, so they are worth running here - but they cannot simply be
pointed at this tree. The root tests/__init__.py does an unconditional
sys.path.insert(0, repo_root), which lands ahead of anything PYTHONPATH or
_path.py arranged, so a bare `import screens` in those files would pull in the
macOS one and die on `import hid`.

Importing the Linux modules first gets round it: once they are in sys.modules
under their bare names, the root test modules' own imports find them there and
never consult sys.path at all. Then the TestCase subclasses are copied into
this module so pytest collects them.

tests/test_config.py is deliberately not among them. Its assertions name the
macOS defaults ("Safari", "Terminal"), which linux/config.py replaces by
design, so it is a test of the root defaults rather than of shared logic.
"""
import importlib.util
import os
import sys
import unittest

import _path

_LINUX, _ROOT = _path.setup()

# Pinned before the root suites load. Unused by name on purpose.
import config      # noqa: E402,F401
import device      # noqa: E402,F401
import lcd         # noqa: E402,F401
import printer     # noqa: E402,F401
import screens     # noqa: E402,F401

PORTABLE = ("test_screens", "test_printer")


def _load(name):
    path = os.path.join(_ROOT, "tests", name + ".py")
    spec = importlib.util.spec_from_file_location("g510_root_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


for _name in PORTABLE:
    for _key, _value in vars(_load(_name)).items():
        if isinstance(_value, type) and issubclass(_value, unittest.TestCase):
            globals()[_name + "_" + _key] = _value
