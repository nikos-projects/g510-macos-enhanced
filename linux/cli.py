"""The shared CLI, with the handful of commands that only make sense on a Mac.

See _shared.py for why this amends rather than forks. Nearly all of cli.py is
argument parsing and config editing, which is the same on both systems; what
is wrong here is a short list. It talks to launchd, it enumerates HID
interfaces with hidapi, it offers a menu bar app, and it asks the user to flip
a switch the kernel never reports.

The root module's main() looks its handler up in COMMANDS and prints USAGE,
both out of the root module's own globals - so those two are changed in place
below rather than rebound, which would leave the root's copies untouched.
"""
import os
import subprocess
import sys
import time

import _shared

import actions
import config
import control
import device

_root = _shared.load("cli")
_shared.reexport(_root, globals())

UNIT = "g510.service"

# How the driver ended up exposing the backlight, as _RgbLed probes it. Which
# one you get is a property of the kernel, not of the keyboard, so info says
# which - it is the first thing to know when a colour does not take.
LED_MODES = {
    "color": "color attribute, lg-g15's own #rrggbb",
    "multi": "multi_intensity, the multicolour LED class",
    "mono": "brightness only - this kernel gives no colour",
}

# packaging/g510 is the real entry point on Linux: it works out where the
# modules live from its own path and puts linux/ ahead of the root.
ENTRY_POINT = os.path.join(APP_DIR, "packaging", "g510")


# -- systemd ---------------------------------------------------------------

def agent_loaded():
    """Whether systemd has the agent running.

    `is-active --quiet` exits 0 only when the service is actually up; enabled
    but stopped, or not installed at all, is a non-zero exit. That is what the
    launchctl probe this replaces meant too. A machine with no systemd at all
    simply has no agent loaded, which is true rather than an error.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", UNIT],
            capture_output=True)
    except OSError:
        return False
    return result.returncode == 0


# The root module's own code calls agent_loaded() out of its globals, so give
# it this one as well - the same reason DEFAULTS is amended in place.
_root.agent_loaded = agent_loaded


def _require_unit():
    """Stop with something useful unless systemd can see the unit.

    Without this, `systemctl --user enable` on a checkout that has never been
    installed fails with "Unit g510.service does not exist", which is true and
    tells nobody what to do about it.
    """
    try:
        result = subprocess.run(["systemctl", "--user", "cat", UNIT],
                                capture_output=True, text=True)
    except OSError as exc:
        sys.exit(f"Could not run systemctl ({exc}). The agent is a systemd "
                 "user service here; without systemd, run it in the "
                 "foreground with: g510 run")
    if result.returncode != 0:
        sys.exit(f"systemd has no {UNIT}.\n"
                 "  from a checkout:  linux/install.sh\n"
                 "  from the .deb:    the unit ships in it; if it is "
                 "installed, try systemctl --user daemon-reload")


def cmd_start(_args):
    """Enable and start the user service.

    enable --now rather than start alone, because the macOS command this
    replaces wrote a plist with RunAtLoad and the user expects `start` to mean
    "and at every login from now on".
    """
    _require_unit()
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", UNIT],
        capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"systemctl refused: {result.stderr.strip()}")
    print("agent started; it will come back at login\n"
          f"  log: journalctl --user -u {UNIT} -f")


def cmd_stop(_args):
    """Stop the service and take it out of the login set."""
    _require_unit()
    result = subprocess.run(
        ["systemctl", "--user", "disable", "--now", UNIT],
        capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"systemctl refused: {result.stderr.strip()}")
    print("agent stopped and disabled")


def cmd_status(_args):
    """Hand the question to systemctl, which answers it better than we can.

    No return code check: `status` exits 3 for a unit that is simply not
    running, which is an answer and not a failure. The log lines the macOS
    version tailed out of a file are in what systemctl prints.
    """
    _require_unit()
    subprocess.run(["systemctl", "--user", "status", UNIT, "--no-pager"])
    print(f"  config: {config.CONFIG_PATH}")


# -- commands --------------------------------------------------------------

def cmd_info(_args):
    """What is attached, and which kernel interface each part of it uses.

    The shared version enumerates the HID interfaces with hidapi to find the
    vendor one, because on macOS that is the only way in. Here the kernel has
    already sorted that out, so the useful facts are which nodes the driver
    produced - and hidapi is not installed, which is the other reason this
    cannot run as it stands.

    The macOS non-exclusive-open warning is gone with it: device.py never
    claims the interface, so there is nothing to warn about.
    """
    manufacturer, product = guard(control.device_info)
    print(f"  {manufacturer} {product}")
    print(f"  backlight:  {config.format_color(guard(control.get_backlight))}")

    path = device.find_path()
    print(f"  lcd:        {path or 'no hidraw node for interface 1'}")

    # Constructing an _RgbLed only stats sysfs - it opens nothing and needs no
    # keyboard - so this answers even when the device is unwritable.
    mode = device._RgbLed(device.BACKLIGHT_LED).mode
    leds = os.path.join(device.SYSFS_LEDS, device.BACKLIGHT_LED)
    if mode:
        print(f"  leds:       {leds}  ({LED_MODES[mode]})")
    else:
        print(f"  leds:       {leds} is not there; "
              "hid-lg-g15 is probably not loaded")

    # Out of /proc, not evdev: this has to answer on a machine that has not
    # installed python3-evdev yet, which is exactly when it is asked.
    keys = actions.input_node(device.GAMING_KEYS_NAME)
    print(f"  g-keys:     "
          + (keys or f"no input device named {device.GAMING_KEYS_NAME!r}"))

    print(f"  config:     {config.CONFIG_PATH}")
    print(f"  agent:      {'running' if control.daemon_running() else 'not running'}")


def cmd_verify(_args):
    """As the shared version, minus the game-mode switch.

    Every G-key, M-key and display-key step is the shared one; only the two
    ends differ. The agent is stopped through systemd rather than launchd, and
    the joystick switch is not asked for at all: hid-lg-g15 does not report
    that bit (device.MODE_KEY_BITS keeps the name, nothing ever sets it), so
    the prompt could only ever end in a mismatch the user cannot fix.
    """
    was_running = agent_loaded()
    if was_running:
        print("Stopping the agent so pressing keys does not fire bindings.\n")
        subprocess.run(["systemctl", "--user", "stop", UNIT],
                       capture_output=True)
        time.sleep(1.0)
    problems = []
    try:
        stream = control.watch_gkeys()
        print("Press each key as asked. Ctrl-C to stop.\n")

        for label in VERIFY_GKEYS:
            print(f"  press the key marked {label} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        for label, marking in VERIFY_MODE:
            print(f"  press the key marked {marking} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        for label, description in VERIFY_LCD:
            print(f"  press {description} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        print("\n  The joystick switch (top left) is not checked: the kernel "
              "driver\n  does not report it, so there is nothing to read.")
    except control.ControlError as exc:
        print(f"\n{exc}")
        return
    except KeyboardInterrupt:
        print("\n  stopped early")
    finally:
        _verify_leds(problems)
        if was_running:
            subprocess.run(["systemctl", "--user", "start", UNIT],
                           capture_output=True)
            print("\nAgent restarted.")

    print()
    if problems:
        print(f"{len(problems)} mismatch(es) - the mapping does not match the "
              "keyboard:")
        for line in problems:
            print(f"  {line}")
    else:
        print("Everything checked matches the markings on the keyboard.")


def cmd_install(_args):
    """Put `g510` on PATH from a source checkout.

    Not the shared install_cli_tool(): that writes a shim calling
    venv/bin/python, and the Linux port has no virtualenv - the two libraries
    it needs come from apt. A symlink to packaging/g510 is enough, because
    that script finds the modules from its own realpath.

    Anyone who installed the .deb or ran linux/install.sh already has this and
    does not need the command.
    """
    if not os.path.isfile(ENTRY_POINT):
        sys.exit(f"No entry point at {ENTRY_POINT} - this looks like an "
                 "installed copy, in which case g510 is already on your PATH.")
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    if os.path.lexists(BIN_PATH):
        os.remove(BIN_PATH)
    os.symlink(ENTRY_POINT, BIN_PATH)
    print(f"installed {BIN_PATH} -> {ENTRY_POINT}")
    print("  only needed for a source checkout; the .deb and "
          "linux/install.sh\n  put g510 on your PATH themselves")
    if os.path.dirname(BIN_PATH) not in os.environ.get("PATH", "").split(":"):
        print(f"  {os.path.dirname(BIN_PATH)} is not on your PATH; add it with:")
        print(f'  echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.bashrc')


def cmd_gui(args):
    """Open the Qt window, or the tray alone with --tray.

    Imported here rather than at the top of the file so the command line keeps
    working on a machine with no Qt on it. PySide6 is a Recommends, not a
    Depends, because the agent and the CLI have no use for it.
    """
    try:
        import gui
    except ImportError as exc:
        sys.exit(f"{exc}\n"
                 "  apt install python3-pyside6.qtwidgets python3-pyside6.qtsvg")
    sys.exit(gui.main(args))


# cmd_iso is deliberately not overridden. It already wraps control.iso_swap in
# its own try/except control.ControlError and exits with the message, so the
# Linux iso_swap's explanation - that the layout comes from XKB - reaches the
# user as one clean line rather than a traceback.


# -- wiring ----------------------------------------------------------------

# main() does COMMANDS.get() against the root module's globals, and this is
# that same dict object, reached through reexport. Mutating it in place is
# what makes the overrides take; rebinding the name would change nothing.
COMMANDS["info"] = cmd_info
COMMANDS["start"] = cmd_start
COMMANDS["stop"] = cmd_stop
COMMANDS["status"] = cmd_status
COMMANDS["verify"] = cmd_verify
COMMANDS["install"] = cmd_install
COMMANDS["gui"] = cmd_gui

# USAGE is a string, so there is nothing to mutate in place - main() prints
# the root module's own global and would never see a rebinding here. Hence the
# explicit assignment below. Only the lines that are now untrue differ: the
# platform, the agent wording, and the two commands that do nothing on Linux
# (gui, and iso, which exits explaining that XKB owns the layout).
USAGE = """g510 - control a Logitech G510 keyboard on Linux

  g510 info                     device, interfaces, current state
  g510 color [colour]           show or set the backlight (#rrggbb or a name)
  g510 brightness [0-100]       show or set backlight brightness
  g510 poweron <colour>         colour the keyboard powers on with
  g510 mkeys [m1 m2 m3 mr]      light M-key LEDs (no args clears them)
  g510 watch [--raw]            print G-key presses live

  g510 lcd <screen>             one screen: status clock claude media
                                printer gauge gkeys
  g510 lcd text "a" "b" "c"     draw up to 3 lines of text
  g510 lcd clear                blank the display
  g510 next                     step to the next screen in the cycle

  g510 bank [1|2|3]             show or switch the binding bank (M1/M2/M3)
  g510 printer [host|off]       3D printer the display reads from
  g510 claude [rows...]         which usage windows the Claude screen shows
  g510 bindings                 list G-key bindings for the active bank
  g510 bind G1 app firefox      bind a key: app | keys | shell | text | none
  g510 record G5 [name]         record a keystroke macro onto a G-key
  g510 macros [delete <name>]   list or remove recorded macros
  g510 profile "App" G1 ...     per-app binding overrides
  g510 config                   path to the config file
  g510 permissions              check access to /dev/uinput and /dev/input
  g510 verify                   check the key mapping against the markings
  g510 gauge add <label> <cmd>  a bar fed by any command printing a number
  g510 install                  put the g510 command on your PATH

  g510 start | stop | status    the agent, as a systemd user service
  g510 run                      run the agent in the foreground (ctrl-C stops)
  g510 gui [--tray]             the window, or just the tray icon

colours: """ + ", ".join(sorted(config.NAMED_COLORS))

# `switch` and `leds` are dropped from the usage rather than the command
# table: the commands still parse and still edit the config, they just drive
# nothing here. The joystick switch is not reported by hid-lg-g15, and the
# lock LEDs belong to the kernel and the desktop, so device.LockLeds is a
# no-op. Listing them would be advertising two dead ends.


def cmd_iso(args):
    """Report that there is nothing to swap, without writing to the config.

    The root version saves `iso_keyboard` before it calls control.iso_swap,
    so on Linux - where that call always raises - it would leave a setting
    behind that no code reads. Overriding the whole command is simpler than
    unpicking the ordering, and there is nothing here worth keeping.
    """
    _ = args
    print("  ISO key swap: not applicable on Linux")
    print("\n  macOS needs this because the G510 declares no locale, so it")
    print("  guesses ANSI and swaps the key left of 1 with the one beside the")
    print("  left shift. Linux takes the layout from XKB instead.")
    print("\n  If those keys are wrong, set the layout in System Settings >")
    print("  Keyboard, or with localectl - not here.")


COMMANDS["iso"] = cmd_iso

_root.USAGE = USAGE

_shared.adopt(_root, globals())
