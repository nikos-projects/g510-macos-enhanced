"""Turn a G-key binding into something that actually happens on Linux.

The macOS original posts CGEvents, which needs the Accessibility permission.
Here the equivalent is a uinput virtual keyboard: the kernel creates a second
keyboard that this process writes into, and the compositor (X11, or Wayland
via libinput) picks it up like any other board. The gate is therefore not a
permission dialog but file access - /dev/uinput to write, /dev/input/event*
to read - so the permission helpers below are real capability probes and
request_post_access() explains the udev rule rather than popping a prompt.

One consequence worth stating: uinput events are positional scancodes, and
the layout is applied downstream by the compositor. We cannot type "z" on a
German board by asking for a character; we can only press the key the kernel
calls KEY_Z. See layout_keys() for what is (and is not) done about that.
"""
import atexit
import os
import shutil
import subprocess
import time

# evdev is only needed to actually emit events. Import failure is kept as a
# message rather than an exception so that chord parsing, describe() and the
# recorder's spellings still work without python3-evdev installed - `g510
# bind` can then still validate a config on a machine that cannot type.
try:
    import evdev
    _EVDEV_ERROR = None
except Exception as _exc:          # ImportError, or a broken build of it
    evdev = None
    _EVDEV_ERROR = f"{type(_exc).__name__}: {_exc}"

UINPUT_PATH = "/dev/uinput"
UINPUT_NAME = "G510 virtual keyboard"
INPUT_DEVICES = "/proc/bus/input/devices"

# The main keyboard interface of the G510, as the kernel's hid-lg-g15 driver
# names it. The board also exposes consumer-control and G-key nodes; those
# carry the macro keys, not what the user types.
KEYBOARD_NAME = "Logitech G510 Gaming Keyboard"

# Key codes from linux/input-event-codes.h, hard-coded rather than read off
# evdev.ecodes: the numbers are kernel ABI and never move, and keeping them
# here is what lets this module be imported without evdev present.
#
# Unlike the macOS table these really are the kernel's own names for the
# physical keys, so the character spellings below are US positions: pressing
# KEY_Z on a German layout produces "y", exactly as it does on a real board.
KEY_CODES = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10,
    "0": 11, "-": 12, "=": 13, "[": 26, "]": 27, "\\": 43, ";": 39, "'": 40,
    "`": 41, ",": 51, ".": 52, "/": 53,
    # Named keys. The first spelling of each code is the one the recorder
    # shows back to the user, so aliases come second.
    "return": 28, "enter": 28, "tab": 15, "space": 57,
    "delete": 14, "backspace": 14, "escape": 1, "esc": 1,
    "forwarddelete": 111, "insert": 110,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "left": 105, "right": 106, "down": 108, "up": 103,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    # Print Screen is KEY_SYSRQ, which is what a stock Plasma binds
    # Spectacle to. "print" is the spelling on the keycap, so it goes first.
    "print": 99, "printscreen": 99, "sysrq": 99,
    "pause": 119, "menu": 127,
}

# Modifiers stay a bitmask, as on macOS, so that parse_chord still returns
# (keycode, flags) and a chord recorded by recorder.py round-trips through
# here unchanged. The bits are ours; only the mapping to real keys below is
# platform-specific.
MOD_CTRL = 1 << 0
MOD_ALT = 1 << 1
MOD_SHIFT = 1 << 2
MOD_SUPER = 1 << 3
MOD_FN = 1 << 4

# "cmd" and "command" are kept as aliases for super/meta. Configs written
# against the macOS build are full of cmd+c, and silently refusing to parse
# them would break every existing binding; on Linux they mean the Super key.
MODIFIER_FLAGS = {
    "cmd": MOD_SUPER,
    "command": MOD_SUPER,
    "super": MOD_SUPER,
    "meta": MOD_SUPER,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "option": MOD_ALT,
    "opt": MOD_ALT,
    "ctrl": MOD_CTRL,
    "control": MOD_CTRL,
    "fn": MOD_FN,
}

# Pressed in this order and released in reverse, so a chord looks to the
# compositor exactly like a human holding the modifiers down.
MODIFIER_KEYS = (
    (MOD_CTRL, 29),          # KEY_LEFTCTRL
    (MOD_ALT, 56),           # KEY_LEFTALT
    (MOD_SHIFT, 42),         # KEY_LEFTSHIFT
    (MOD_SUPER, 125),        # KEY_LEFTMETA
    (MOD_FN, 464),           # KEY_FN - accepted for old configs; most
)                            # keyboards resolve fn in firmware, so it is
                             # usually inert when synthesised.

# Press and release in the same instant get coalesced by some toolkits, and
# a few read the modifier state only on the key-down frame. 10ms is below
# human perception and well under one frame.
PRESS_HOLD = 0.01

# A freshly created uinput device is not open by the compositor yet, so the
# first events written to it go nowhere. Waiting once, at creation, is the
# standard fix.
DEVICE_SETTLE = 0.3


class ActionError(Exception):
    pass


class PermissionError_(ActionError):
    """Raised when a binding needs uinput and this process cannot get it."""


# Kept under the macOS name so the shared daemon and cli need no change. On
# Linux "accessibility" means write access to /dev/uinput.
NEEDS_ACCESSIBILITY = ("keys", "text", "macro")


def can_post_events():
    """True if this process may synthesise keystrokes.

    Probed rather than cached: the answer changes the moment the user adds
    the udev rule or logs back in after a group change, and a cached "no"
    would outlive the fix. Opening /dev/uinput is harmless - no device
    exists until UI_DEV_CREATE, which only _uinput() does.
    """
    if _device is not None:
        return True
    if evdev is None:
        return False
    try:
        handle = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(handle)
    return True


def can_read_input():
    """True if the G510's main keyboard event node can be read."""
    node = input_node()
    if node is None:
        return False
    try:
        handle = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(handle)
    return True


def request_post_access():
    """Explain how to get uinput access. Returns True if it is already there.

    There is no dialog to raise on Linux, and cli.py's `permissions` command
    calls this expecting it to say something useful, so the advice is printed
    here - this is the only place that command gives us to speak.
    """
    if can_post_events():
        return True
    print("\nOn Linux this is file access, not a permission prompt.")
    if evdev is None:
        print(f"\n  python3-evdev is missing ({_EVDEV_ERROR})")
        print("  Install it:  sudo apt install python3-evdev")
    print("\n  1. Make sure the uinput module is loaded:")
    print("       sudo modprobe uinput")
    print("       echo uinput | sudo tee /etc/modules-load.d/uinput.conf")
    print("  2. Give the input group access, as a udev rule in")
    print("     /etc/udev/rules.d/99-g510.rules:")
    print('       KERNEL=="uinput", MODE="0660", GROUP="input", '
          'OPTIONS+="static_node=uinput"')
    print('       SUBSYSTEM=="input", ATTRS{idVendor}=="046d", '
          'MODE="0660", GROUP="input"')
    print("  3. Join that group, then log out and back in:")
    print(f"       sudo usermod -aG input {_username()}")
    print("  4. Reload the rules (or replug the keyboard):")
    print("       sudo udevadm control --reload-rules && sudo udevadm trigger")
    return False


def _username():
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER", "$USER")


def input_node(name=KEYBOARD_NAME):
    """/dev/input/eventN for a device name, or None if it is not there.

    Read out of /proc rather than by walking evdev.list_devices(), because
    that has to open every node to ask its name - which fails on exactly the
    machines where this matters, the ones without input-group membership.
    """
    try:
        with open(INPUT_DEVICES, "r", errors="replace") as handle:
            blocks = handle.read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        found = None
        handlers = []
        for line in block.splitlines():
            if line.startswith("N: Name="):
                found = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
        if found != name:
            continue
        for handler in handlers:
            if handler.startswith("event"):
                return "/dev/input/" + handler
    return None


def _split_chord(chord):
    """Split on "+" while still allowing "+" itself to be the key.

    "cmd++" is cmd plus the plus key; "+" alone is just that key. Splitting
    naively drops it, which matters on any layout where + is a main key.
    """
    parts = [p.strip() for p in chord.split("+")]
    if parts and parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
        parts[-1] = "+" if parts[-1] == "" else parts[-1]
        if parts[-1] != "+":
            parts.append("+")
    parts = [p for p in parts if p]
    if not parts and chord.strip():
        parts = ["+"]
    return [p if len(p) == 1 else p.lower() for p in parts]


_layout_keys = None


def layout_keys():
    """{character: (keycode, needs_shift)} for the keyboard in use.

    Empty on Linux, and deliberately so. uinput injects scancodes below the
    layout: whatever we press, the compositor's own XKB map decides which
    character comes out, so asking for "the key that types ü" has no answer
    this side of the kernel. parse_chord therefore falls through to the
    positional names in KEY_CODES, which is what the kernel calls those keys
    anyway.

    The hook is kept because the shape is shared with the macOS build, and
    because a future XKB-aware resolver would plug in right here.
    """
    global _layout_keys
    if _layout_keys is None:
        _layout_keys = {}
    return _layout_keys


def parse_chord(chord):
    """'cmd+shift+4' -> (keycode, flags).

    Named keys - return, f1, left - and single characters both come out of
    KEY_CODES, which on Linux holds evdev KEY_* codes. A layout table, if one
    is ever installed, wins for single characters; see layout_keys().
    """
    if not isinstance(chord, str):
        raise ActionError(f"Expected a key chord, got {chord!r}")
    parts = _split_chord(chord)
    if not parts:
        raise ActionError(f"Empty key chord: {chord!r}")
    flags = 0
    for part in parts[:-1]:
        if part.lower() not in MODIFIER_FLAGS:
            raise ActionError(f"Unknown modifier {part!r} in {chord!r}")
        flags |= MODIFIER_FLAGS[part.lower()]
    key = parts[-1]
    if len(key) == 1:
        found = layout_keys().get(key) or layout_keys().get(key.lower())
        if found:
            keycode, needs_shift = found
            if needs_shift:
                flags |= MODIFIER_FLAGS["shift"]
            return keycode, flags
    if key.lower() not in KEY_CODES:
        raise ActionError(f"Unknown key {key!r} in {chord!r}")
    return KEY_CODES[key.lower()], flags


_device = None


def _uinput():
    """The virtual keyboard, created on first use and then kept open.

    Kept open on purpose: every create/destroy cycle makes the compositor
    re-enumerate a new keyboard, which costs the settle wait below and makes
    the first key of a macro disappear.
    """
    global _device
    if _device is not None:
        return _device
    if evdev is None:
        raise ActionError(
            "python3-evdev is not installed, so keystrokes cannot be sent "
            f"({_EVDEV_ERROR}) - install it with: sudo apt install python3-evdev")
    codes = sorted(set(KEY_CODES.values()) | {c for _, c in MODIFIER_KEYS})
    try:
        _device = evdev.UInput({evdev.ecodes.EV_KEY: codes}, name=UINPUT_NAME)
    except OSError as exc:
        raise PermissionError_(
            f"Could not open {UINPUT_PATH} ({exc.strerror or exc}) - "
            "run: g510 permissions")
    atexit.register(_close_uinput)
    time.sleep(DEVICE_SETTLE)
    return _device


def _close_uinput():
    """Destroy the virtual keyboard. Leaving it behind confuses the session."""
    global _device
    device, _device = _device, None
    if device is not None:
        try:
            device.close()
        except Exception:
            pass


def send_chord(chord):
    keycode, flags = parse_chord(chord)
    device = _uinput()
    held = [code for mask, code in MODIFIER_KEYS if flags & mask]
    for code in held:
        device.write(evdev.ecodes.EV_KEY, code, 1)
    device.write(evdev.ecodes.EV_KEY, keycode, 1)
    device.syn()
    time.sleep(PRESS_HOLD)
    device.write(evdev.ecodes.EV_KEY, keycode, 0)
    for code in reversed(held):
        device.write(evdev.ecodes.EV_KEY, code, 0)
    device.syn()


# Characters the ASCII fallback can reach, as shift + another key.
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}


def _text_helper():
    """The tool type_text() would use, or None if it is down to uinput.

    Order, and why:
      1. wtype, on Wayland. It speaks the virtual-keyboard protocol, so the
         compositor maps the string itself and any unicode works.
      2. xdotool, on X11 (and XWayland). Same idea through XTEST; it remaps
         a spare keysym for characters the layout cannot reach.
      3. nothing - uinput, which is scancodes only and so ASCII only.
    Both helpers are preferred over uinput because they are the only paths
    that are actually layout-independent, which is the whole promise of a
    "text" binding.
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wtype"):
        return "wtype"
    if os.environ.get("DISPLAY") and shutil.which("xdotool"):
        return "xdotool"
    return None


def type_text(text):
    """Type a literal string.

    uinput has no unicode path at all - it carries scancodes, and what they
    produce is the compositor's business - so a helper does the work where
    one exists. See _text_helper() for the order.
    """
    if not text:
        return
    text = str(text)
    helper = _text_helper()
    if helper == "wtype":
        # "--" so a string starting with a dash is not read as an option.
        _run_text_helper(["wtype", "--", text], helper)
        return
    if helper == "xdotool":
        _run_text_helper(
            ["xdotool", "type", "--clearmodifiers", "--", text], helper)
        return
    for keycode, shifted in _ascii_strokes(text):
        _send_key(keycode, MOD_SHIFT if shifted else 0)


def _run_text_helper(argv, helper):
    """Run wtype/xdotool and wait, because macro order depends on it.

    Waiting blocks the daemon's single thread, hence the timeout: typing a
    line takes milliseconds, and anything that hangs is a broken session we
    should not sit in forever.
    """
    try:
        result = subprocess.run(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=5)
    except subprocess.TimeoutExpired:
        raise ActionError(f"{helper} did not finish - giving up on the text")
    except OSError as exc:
        raise ActionError(f"Could not run {helper}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise ActionError(f"{helper} failed: {detail or result.returncode}")


def _ascii_strokes(text):
    """[(keycode, needs_shift)] for a string, or an ActionError naming why.

    The last-resort path. It presses the US-positional key for each
    character, so it is only honest for ASCII, and only on a US layout -
    which is exactly what the error below says.
    """
    strokes = []
    for char in text:
        if char in ("\n", "\r"):
            strokes.append((KEY_CODES["return"], False))
            continue
        if char == "\t":
            strokes.append((KEY_CODES["tab"], False))
            continue
        if char == " ":
            strokes.append((KEY_CODES["space"], False))
            continue
        if char.isupper() and char.lower() in KEY_CODES:
            strokes.append((KEY_CODES[char.lower()], True))
            continue
        if char in _SHIFTED:
            strokes.append((KEY_CODES[_SHIFTED[char]], True))
            continue
        if char in KEY_CODES:
            strokes.append((KEY_CODES[char], False))
            continue
        raise ActionError(
            f"Cannot type {char!r} through uinput: it carries key positions, "
            "not characters. Install wtype (Wayland) or xdotool (X11) for "
            "text bindings that are not plain ASCII.")
    return strokes


def _send_key(keycode, flags):
    """One keystroke by code, skipping the chord parser."""
    device = _uinput()
    held = [code for mask, code in MODIFIER_KEYS if flags & mask]
    for code in held:
        device.write(evdev.ecodes.EV_KEY, code, 1)
    device.write(evdev.ecodes.EV_KEY, keycode, 1)
    device.syn()
    time.sleep(PRESS_HOLD)
    device.write(evdev.ecodes.EV_KEY, keycode, 0)
    for code in reversed(held):
        device.write(evdev.ecodes.EV_KEY, code, 0)
    device.syn()


# Children we started and will never wait on. Popen objects that are simply
# dropped leave zombies, and the daemon runs for weeks, so each spawn clears
# out the ones that have since exited. setting SIGCHLD to SIG_IGN would be
# shorter but would break subprocess.run() elsewhere in the app.
_children = []


def _reap():
    _children[:] = [child for child in _children if child.poll() is None]


def _spawn(argv, shell=False):
    """Start something detached: its own session, no pipes, no waiting."""
    _reap()
    try:
        child = subprocess.Popen(
            argv, shell=shell, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
    except OSError as exc:
        raise ActionError(f"Could not run {argv!r}: {exc}")
    _children.append(child)
    return child


def run_shell(command):
    _spawn(command, shell=True)


def _desktop_file(app):
    """Full path of a .desktop file for an id, or None.

    Resolved here rather than handed straight to gtk-launch so that a name
    which is not an installed application falls back to being run as a
    command instead of failing silently.
    """
    name = app if app.endswith(".desktop") else app + ".desktop"
    roots = [os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "applications")]
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    roots += [os.path.join(part, "applications")
              for part in dirs.split(":") if part]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _looks_like_desktop_id(app):
    """"org.kde.konsole" or "firefox.desktop" - not "make -j4" or "/bin/ls"."""
    return bool(app) and not any(c.isspace() for c in app) and "/" not in app


def launch_app(name):
    """Launch a desktop application by .desktop id, or run it as a command.

    A .desktop entry is worth going through where one exists: it carries the
    right Exec line, environment and startup notification, which "just run
    the name" does not.
    """
    app = str(name).strip()
    if not app:
        raise ActionError("An 'app' binding needs a name")
    if _looks_like_desktop_id(app):
        path = _desktop_file(app)
        if path:
            if shutil.which("gio"):
                _spawn(["gio", "launch", path])
                return
            if shutil.which("gtk-launch"):
                _spawn(["gtk-launch", os.path.basename(path)])
                return
    run_shell(app)


def play_macro(steps):
    """Replay a recorded sequence of chords with their original timing."""
    for step in steps:
        delay = float(step.get("delay", 0) or 0)
        if delay > 0:
            time.sleep(min(delay, 2.0))      # cap, so a pause cannot hang
        chord = step.get("keys")
        if chord:
            send_chord(chord)


def dispatch(binding, macros=None):
    """Run one binding dict, e.g. {"type": "keys", "keys": "cmd+c"}."""
    if not binding:
        return
    if not isinstance(binding, dict):
        raise ActionError(f"A binding must be an object, not {binding!r}")
    kind = binding.get("type")
    if kind in NEEDS_ACCESSIBILITY and not can_post_events():
        # A 'text' binding is exempt when wtype or xdotool is there: those
        # never touch uinput, so refusing them would be a lie.
        if not (kind == "text" and _text_helper()):
            raise PermissionError_(
                f"a {kind!r} binding needs access to {UINPUT_PATH} - "
                "run: g510 permissions")
    if kind == "keys":
        send_chord(_required(binding, "keys"))
    elif kind == "text":
        type_text(_required(binding, "text"))
    elif kind == "shell":
        run_shell(_required(binding, "command"))
    elif kind == "app":
        launch_app(_required(binding, "name"))
    elif kind == "macro":
        name = binding.get("name")
        steps = (macros or {}).get(name)
        if not steps:
            raise ActionError(f"No recorded macro called {name!r}")
        play_macro(steps)
    else:
        raise ActionError(f"Unknown binding type {kind!r}")


def _required(binding, field):
    """Pull a binding's field, as an ActionError rather than a KeyError."""
    value = binding.get(field)
    if value is None or value == "":
        raise ActionError(
            f"A {binding.get('type')!r} binding needs a {field!r} field")
    return value


def describe(binding):
    """Short human-readable summary of a binding, for menus and listings.

    Runs against whatever is in the config, which is meant to be hand-edited,
    so every shape has to produce a string. This renders every menu and every
    listing; raising here blanks the whole UI.
    """
    if not binding:
        return "-"
    if not isinstance(binding, dict):
        return str(binding)[:26]
    kind = binding.get("type")
    if kind == "keys":
        return _as_text(binding.get("keys")) or "?"
    if kind == "text":
        return f'type "{_clip(_as_text(binding.get("text")), 18)}"'
    if kind == "shell":
        return f"$ {_clip(_as_text(binding.get('command')), 22)}"
    if kind == "app":
        return f"open {_as_text(binding.get('name')) or '?'}"
    if kind == "macro":
        return f"macro {_as_text(binding.get('name')) or '?'}"
    return str(kind)


def _as_text(value):
    """Whatever the config holds, as a string. None becomes empty."""
    return "" if value is None else str(value)


def _clip(text, limit):
    return text[:limit] + ("..." if len(text) > limit else "")
