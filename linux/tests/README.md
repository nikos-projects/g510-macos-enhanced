# Linux port tests

Run them from the repo root with `python3 -m pytest linux/tests -q` (or
`python3 -m unittest discover -t . -s linux/tests`); `__init__.py` puts
`linux/` ahead of the repo root on `sys.path`, exactly as `linux/_path.py`
does, so `import device` here is the Linux one. Nothing below opens the
keyboard, writes to `/sys` or `/dev`, creates a uinput device or needs
`python3-evdev` - the LED tests use a temporary directory, the input reports
are synthesised from key events the tests feed in by hand, and `actions.py`
hard-codes its keycode table precisely so chord parsing works with the evdev
bindings absent. The root suite's `tests/test_screens.py`, `tests/test_config.py`
and `tests/test_printer.py` are platform-neutral and still apply - run them
with `PYTHONPATH=linux:. python3 -m pytest tests/test_screens.py
tests/test_config.py tests/test_printer.py -q` to exercise them against the
Linux modules. Its `tests/test_actions.py` and `tests/test_recorder.py`
cannot run here at all: both `import Quartz` at module scope and assert macOS
virtual keycodes (`a` is 0 there, 30 here), so they are ported rather than
reused, and `tests/test_device.py` assumes the hidapi handle this port
deliberately does not open.
