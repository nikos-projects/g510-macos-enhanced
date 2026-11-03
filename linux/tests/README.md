# Linux port tests

Run them from the repo root:

    python3 -m unittest discover -t . -s linux/tests

`__init__.py` puts `linux/` ahead of the repo root on `sys.path`, exactly as
`linux/_path.py` does, so `import device` here is the Linux one while
`import config` still falls through to the shared one. Use the **system**
`python3`, not the macOS venv: the suite needs Pillow (`linux/lcd.py` draws
through it) and the venv does not have it.

Nothing below opens the keyboard, writes to `/sys` or `/dev`, creates a uinput
device or needs `python3-evdev`. The LED tests use a temporary directory, the
input reports are synthesised from key events the tests feed in by hand, and
`linux/actions.py` hard-codes its keycode table precisely so chord parsing
works with the evdev bindings absent.

## What is in here

| File | Covers |
| --- | --- |
| `test_shared.py` | the root suites that are portable, run against the Linux modules |
| `test_device.py` | the sysfs/LED half of the device layer |
| `test_lcd.py` | Pillow rendering, thresholds, the font |
| `test_actions.py` | chord parsing, binding descriptions, dispatch routing |
| `test_recorder.py` | macro record and replay |
| `test_keytap.py` | which keys the menu takes, and the XKB layout sources |
| `test_beat.py` | the PulseAudio tap: buffers in, peaks and wake-ups out |
| `test_daemon.py` | the run loop: what each pass owes, and what it waits on |

`test_shared.py` loads `tests/test_screens.py`, `tests/test_printer.py` and
`tests/test_control.py` from the root suite by path, so they exercise the Linux
modules rather than the macOS ones. It does this by importing the Linux modules
first and copying the `TestCase` subclasses in, because the root
`tests/__init__.py` does an unconditional `sys.path.insert(0, repo_root)` that
would otherwise win and pull in `import hid`.

`tests/test_config.py` is deliberately not among them: its assertions name the
macOS defaults ("Safari", "Terminal"), which `linux/config.py` replaces by
design, so it tests the root defaults rather than shared logic.

The root suite's `tests/test_actions.py`, `tests/test_recorder.py` and
`tests/test_device.py` cannot run against this tree at all: they assume the
hidapi handle and the macOS keycodes this port deliberately does not open, so
they are ported rather than reused - which is why there is a
`test_actions.py`/`test_recorder.py`/`test_device.py` here as well as there.
