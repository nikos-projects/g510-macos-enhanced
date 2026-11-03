"""Arrow keys for the menu: an evdev grab of the keyboard, and a fake one.

The panel has no arrow keys of its own, so the menu borrows the
keyboard's - and on Linux that is a two-part problem the macOS tap does
not have. Reading `/dev/input/event*` is enough to *see* a key, but the
compositor has read it too by then, and a key the menu takes must never
reach the game underneath. So the G510's typing keyboard is grabbed
(`EVIOCGRAB`, exclusive) and everything the panel does not want is
written straight back out through a uinput virtual keyboard: one
thread, one decision per key, and the desktop sees a single unbroken
stream of keys either way.

Three rules, the same ones the macOS tap works by, because the daemon's
policy (`wants_menu_key`) was written against them:

  * A key is only ever swallowed when `decide` says so. A letter typed
    while the menu is merely showing a list goes to the app as usual.
  * Ctrl, Alt or Super held makes the key part of a shortcut, and
    shortcuts belong to the app, never to the menu. (The macOS tap
    watches Cmd/Control/Option; Shift is not one of them there either,
    so a shifted menu key is still the menu's.)
  * Swallowing is decided on the key down and honoured on the matching
    key up, or an application sees half of every key.

The grab only exists while the tap is running - the first panel that
wants keys starts it - and the kernel releases it the moment this
process goes away, so a dead agent cannot leave a dead keyboard behind.
What it does cost is stated plainly in the README: while a panel has
been opened, a program reading the *physical* node directly (Steam
Input, an evdev game) sees the virtual keyboard instead.

Nothing here knows what a menu is: `feed` is a plain function over
(key, code, character, blocked) and is where the tests live, exactly as
on macOS.
"""
import collections
import ctypes
import locale
import os
import select
import threading

try:
    import evdev
except ImportError:                  # the agent still runs; menus cannot
    evdev = None

# The keys that mean something to the menu, by Linux keycode. These are
# the kernel's input-event-codes.h values and never change - they are a
# stable ABI, which is why they are written out rather than read from
# evdev: the module has to import with the bindings absent, since the
# tests feed keys by hand.
UP, DOWN, LEFT, RIGHT = 103, 108, 105, 106
RETURN, PAD_ENTER, ESCAPE, BACKSPACE = 28, 96, 1, 14

KEY_NAMES = {UP: "up", DOWN: "down", LEFT: "left", RIGHT: "right",
             RETURN: "enter", PAD_ENTER: "enter", ESCAPE: "esc",
             BACKSPACE: "backspace"}

# What the menu takes while it is open: these always mean something to it.
NAV_KEYS = ("up", "down", "left", "right", "enter", "esc", "backspace")

# Holding any of these makes a key part of a shortcut (Ctrl-C, Alt-Tab),
# and shortcuts belong to the app, never to the menu. Super is macOS's
# Cmd here; Shift is deliberately absent, as it is there.
MODIFIER_KEYS = (29, 97,           # left/right ctrl
                 56, 100,          # left/right alt
                 125, 126)         # left/right meta (Super)

VIRTUAL_NAME = "g510 virtual keyboard"
_XKB_KEYCODE_OFFSET = 8            # xkb keycodes are evdev codes plus eight


def key_for(code, character):
    """(name, character) for a keycode and what it typed, or None.

    The name is one of NAV_KEYS for a key the menu navigates with, and
    the character itself for anything printable. A key that is neither -
    Tab, a function key, half a composed accent - is None, and passes
    through. The macOS twin of this function asks the same question
    with macOS keycodes; this is the same answer with kernel ones.
    """
    name = KEY_NAMES.get(code)
    if name:
        return (name, None)
    if character and len(character) == 1 and character.isprintable():
        return (character, character)
    return None


class _Layout:
    """Keycode to character, through xkbcommon - or nothing at all.

    evdev hands over positions, not characters: `code 30` is the key
    marked A on one keyboard and Z on another, and which one it types is
    a question only a keymap can answer. libxkbcommon answers it with
    the same keymap the compositor uses, which is what makes a German
    layout type German here. It is loaded lazily and treated as a
    bonus: without the library (a minimal install) navigation still
    works, because arrows do not need a keymap - only typing does.
    """

    def __init__(self):
        self.state = None
        self._lib = None

    def character(self, code):
        """What this keycode types, or None when it cannot be said."""
        if self.state is None and not self._load():
            return None
        try:
            symbol = self._lib.xkb_state_key_get_utf32(self.state,
                                                       code + _XKB_KEYCODE_OFFSET)
        except Exception:
            return None
        if not symbol:
            return None                # a dead key, or no symbol at all
        try:
            return chr(symbol)
        except ValueError:
            return None

    def _load(self):
        if self._lib is not None:
            return self.state is not None
        try:
            lib = ctypes.CDLL("libxkbcommon.so.0")
        except OSError:
            self._lib = False          # remembered, so it is tried once
            return False

        class Names(ctypes.Structure):
            _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                        ("layout", ctypes.c_char_p),
                        ("variant", ctypes.c_char_p),
                        ("options", ctypes.c_char_p)]

        lib.xkb_context_new.argtypes = [ctypes.c_int]
        lib.xkb_context_new.restype = ctypes.c_void_p
        lib.xkb_keymap_new_from_names.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(Names), ctypes.c_uint]
        lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
        lib.xkb_state_new.argtypes = [ctypes.c_void_p]
        lib.xkb_state_new.restype = ctypes.c_void_p
        lib.xkb_state_key_get_utf32.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        lib.xkb_state_key_get_utf32.restype = ctypes.c_uint
        context = lib.xkb_context_new(0)
        if not context:
            self._lib = False
            return False
        self._lib = lib
        layout, variant = _system_layout()
        names = Names(None, None,
                      layout.encode() if layout else None,
                      variant.encode() if variant else None, None)
        keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        if not keymap:
            # A layout name the library does not know: the built-in
            # default (us) beats no characters at all.
            keymap = lib.xkb_keymap_new_from_names(
                context, ctypes.byref(Names(None, None, None, None, None)), 0)
        if not keymap:
            self._lib = False
            return False
        # One context, one keymap, one state for the life of the tap.
        # Not unreferenced on purpose: they are needed until the process
        # ends, and the tap is stopped exactly then.
        self.state = lib.xkb_state_new(keymap)
        return self.state is not None


def _keyboard_file(path="/etc/default/keyboard"):
    """(layout, variant) the installer wrote down, or (None, None).

    Split out of _system_layout so it can be pointed at a file the tests
    make: this is the source that is right on exactly the systems the port
    targets, and a quoting mistake here would silently type the wrong
    characters in a menu rather than fail.
    """
    layout = variant = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                key = key.strip().upper()
                if key == "XKBBLAYOUT" and value.strip():
                    layout = value.strip().strip('"')
                elif key == "XKBVARIANT" and value.strip():
                    variant = value.strip().strip('"')
    except OSError:
        return (None, None)
    return (layout, variant)


def _system_layout(path="/etc/default/keyboard"):
    """(layout, variant) the machine is configured with, best effort.

    Asked in the order the answer is most likely to be right in: what a
    session manager exports, what Debian-family systems write down for
    the installer, then the locale. A guess that is wrong types the
    wrong characters in a menu, so every source that could be wrong is
    one the user can override with XKB_DEFAULT_LAYOUT.

    `path` exists so the tests can point this at a file they made; nothing
    in the program passes it.
    """
    layout = os.environ.get("XKB_DEFAULT_LAYOUT")
    variant = os.environ.get("XKB_DEFAULT_VARIANT")
    if layout:
        return (layout, variant)
    layout, variant = _keyboard_file(path)
    if layout:
        # Only a layout carries the answer. A variant on its own names half
        # of a pair, and the locale is a better guess than half.
        return (layout, variant)
    try:
        locale_code = locale.getlocale()[0] or ""
    except ValueError:
        locale_code = ""
    if "_" in locale_code:
        return (locale_code.split("_")[0], None)
    return (None, None)


class KeyTap:
    """A grab that runs while `decide` wants keys, queueing what it takes.

    `decide(name, character)` is called from the tap's own thread and must
    be quick and safe to call there - the daemon keeps it to reading two
    flags. What it takes lands in `queue`, which the daemon drains from
    its main loop, so the menu itself is only ever touched by one thread.
    """

    def __init__(self, decide, log=None):
        self.decide = decide
        self.log = log or (lambda _message: None)
        self.queue = collections.deque()
        self.error = None
        self.tap = None               # the grabbed device, once running
        self._swallowed = set()       # keycodes whose keyUp still has to go
        self._mods = set()            # modifiers currently held
        self._thread = None
        self._ready = threading.Event()
        self._running = False
        self._layout = _Layout()
        self._ui = None
        self._dev = None

    # -- life cycle -----------------------------------------------------

    def start(self, timeout=5.0):
        """Start the grab. False (with `error`) when it cannot be taken."""
        if self._thread is not None:
            return self.tap is not None
        if evdev is None:
            self.error = ("no evdev bindings - apt install python3-evdev "
                          "/ pacman -S python-evdev")
            self.log(f"menu cannot take keys: {self.error}")
            return False
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="g510-keytap")
        self._thread.start()
        self._ready.wait(timeout)
        if self.tap is None and not self.error:
            self.error = "the keyboard did not come up"
        if self.tap is None:
            self.log(f"menu cannot take keys: {self.error}")
        return self.tap is not None

    def stop(self):
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._running = False
        # The thread ungrabs and closes on its way out; two seconds is
        # for a select in progress, not for anything slow.
        thread.join(timeout=2.0)
        self.tap = None

    def _run(self):
        dev = ui = None
        try:
            dev = find_keyboard()
            if dev is None:
                self.error = ("no keyboard to take - is the G510 connected, "
                              "and is /dev/input readable? (g510 permissions)")
                self._ready.set()
                return
            ui = evdev.UInput.from_device(dev, name=VIRTUAL_NAME)
            try:
                dev.grab()
            except OSError as exc:
                self.error = (f"the keyboard would not be grabbed: {exc} "
                              "(is keyd or another remapper holding it?)")
                raise
        except Exception as exc:
            if not self.error:
                self.error = f"{type(exc).__name__}: {exc}"
            for opened in (ui, dev):
                if opened is not None:
                    try:
                        opened.close()
                    except Exception:
                        pass
            self._ready.set()
            return
        self._dev, self._ui = dev, ui
        self.tap = dev
        self._ready.set()
        while self._running:
            try:
                ready, _, _ = select.select([dev.fd], [], [], 0.5)
                if not ready:
                    continue
                events = list(dev.read())
            except BlockingIOError:
                continue
            except OSError:
                break                   # unplugged, or let go underneath us
            for event in events:
                self._forward(event)
        # Ungrab before closing: the grab dies with the fd either way,
        # but an explicit ungrab means a keyboard that comes back the
        # instant this thread is done rather than at process exit.
        if dev is not None:
            try:
                dev.ungrab()
            except Exception:
                pass
        for opened in (ui, dev):
            if opened is not None:
                try:
                    opened.close()
                except Exception:
                    pass
        self.tap = None
        if self._running:
            # The loop only ends early when the device went away.
            self.log("menu keys: the keyboard went away")
        self._running = False

    # -- the tap --------------------------------------------------------

    def _forward(self, event):
        """One evdev event: decide, swallow or write it back out."""
        if event.type == evdev.ecodes.EV_KEY:
            code, value = event.code, event.value
            if code in MODIFIER_KEYS:
                # Recorded before deciding: the modifier's own press is
                # never the menu's (key_for says no), but the key that
                # follows it has to see the flag already raised.
                if value:
                    self._mods.add(code)
                else:
                    self._mods.discard(code)
            kind = "up" if value == 0 else "down"
            character = None
            if kind == "down" and key_for(code, None) is None:
                # Only worth asking the keymap for a key that might be
                # printable - nav keys are answered without it.
                character = self._layout.character(code)
            swallowed = self.feed(kind, code, character,
                                  blocked=bool(self._mods))
            if swallowed:
                return
        try:
            self._ui.write_event(event)
        except (OSError, ValueError):
            pass                        # the desktop lost a key; nothing
            # here may raise into the reader thread - the grab would go
            # on holding a keyboard nobody is forwarding for.

    # -- the part that can be tested without a keyboard -----------------

    def feed(self, kind, code, character, blocked=False):
        """One key. True means the event is swallowed and never delivered.

        `blocked` is the shortcut case: with Ctrl, Alt or Super down,
        whatever the key is, it belongs to the application. The logic is
        the macOS tap's, deliberately: the daemon's policy answers the
        same question on both platforms, so the machinery underneath it
        had better not differ.
        """
        if kind == "up":
            if code in self._swallowed:
                self._swallowed.discard(code)
                return True
            return False
        if blocked:
            return False
        found = key_for(code, character)
        if found is None:
            return False
        name, text = found
        try:
            wanted = bool(self.decide(name, text))
        except Exception as exc:                 # a policy that throws is
            self.log(f"menu key filter failed: {type(exc).__name__}: {exc}")
            wanted = False
        if not wanted:
            return False
        self._swallowed.add(code)
        self.queue.append((name, text))
        return True

    def drain(self):
        """Everything the menu has been handed since the last call."""
        out = list(self.queue)
        self.queue.clear()
        return out


def find_keyboard():
    """The G510's typing keyboard as an open evdev device, or None.

    Matched on the USB ids first (046d:c22d and c22e, the same pair the
    udev rule matches), because the product string is the firmware's to
    change; the name is only a fallback for a kernel that does not hand
    the ids along. Two nodes are always refused whatever they claim:
    the macro-keys node (the agent's own `device.py` reads that one,
    and grabbing it would take the G-keys away from the daemon), and
    the virtual keyboard this file creates, so a restarted tap never
    grabs its own predecessor.
    """
    if evdev is None:
        return None
    candidates = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue                    # not readable, or gone already
        try:
            keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
            typing = (evdev.ecodes.KEY_A in keys
                      and evdev.ecodes.KEY_UP in keys)
            ours = dev.name != VIRTUAL_NAME and not dev.name.endswith(
                "Gaming Keys")
        except Exception:
            typing, ours = False, False
        if typing and ours:
            candidates.append(dev)
        else:
            dev.close()
    chosen = None
    for dev in candidates:
        try:
            info = dev.info
            if info.vendor == 0x046d and info.product in (0xc22d, 0xc22e):
                chosen = dev
                break
        except Exception:
            continue
    if chosen is None:
        for dev in candidates:
            if "g510" in dev.name.lower():
                chosen = dev
                break
    for dev in candidates:
        if dev is not chosen:
            dev.close()
    return chosen
