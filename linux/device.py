"""Low-level access to a Logitech G510 on Linux.

The macOS build has to drive the vendor HID interface itself because Apple
ships no driver for this keyboard. Linux does: hid-lg-g15 is in the tree, it
binds the G510 at probe time and it already exposes everything we used raw HID
for - the backlight and the M-key LEDs as LED class devices, the G-keys, the
M-keys and the display keys as an ordinary input device.

So this port deliberately does *not* open the keyboard with hidapi. The vendor
interface and the consumer-control interface (the media and volume keys) are
the same USB interface; claiming it would take the media keys away from the
desktop, exactly the trap the macOS side works around with
hid_darwin_set_open_exclusive. Going through the kernel avoids the fight
altogether and costs nothing - the kernel does the same work.

The one thing the kernel driver does not offer is the LCD, so frames still go
out over /dev/hidrawN, which is a write-only user of a device the driver keeps
owning.

Everything below keeps the macOS module's API: same names, same report layout
out of read_raw(), so daemon.py and control.py import it unchanged.
"""
import os
import select

VENDOR_ID = 0x046D
PRODUCT_IDS = (0xC22D, 0xC22E)   # G510, G510 with onboard audio
VENDOR_USAGE_PAGE = 0xFF00

# Report 0x01 is the standard boot-keyboard report: it carries the characters
# the user is actually typing. It must never be logged, broadcast or stored.
# Nothing here ever produces one - the kernel keeps typing to itself - but the
# check stays so the shared daemon's guard keeps meaning something.
KEYSTROKE_REPORTS = frozenset({0x01, 0x02})

INPUT_MACRO_KEYS = 0x03

LCD_REPORT = 0x03
LCD_WIDTH, LCD_HEIGHT, LCD_PAGES = 160, 43, 6
LCD_FRAME_LEN = 992          # 32-byte header + 160 columns * 6 pages
LCD_HEADER_LEN = 32

# Only the names survive from the macOS bitmask: the M-key LEDs are separate
# LED class devices here. cli.py validates names against this.
MKEY_BITS = {"m1": 0x80, "m2": 0x40, "m3": 0x20, "mr": 0x10}

GKEY_COUNT = 18

# The mode keys and the game-mode switch ride in the high bits of the same
# 24-bit field as the G-keys, above the 18 the G-keys occupy. Kept bit for bit
# as the macOS side has them, because read_raw() below synthesises that exact
# field and the shared decoders parse it.
#
# GAME is never set on Linux: hid-lg-g15 does not report the joystick switch
# on the top left at all, and there is no other route to it. The name stays so
# bindings and configs written on macOS still load.
MODE_KEY_BITS = {
    "GAME": 1 << 18,
    "M1": 1 << 20,
    "M2": 1 << 21,
    "M3": 1 << 22,
    "MR": 1 << 23,
}

# The keys around the display, in byte 4 of the macro report. L1 is the
# display/menu key; L2-L5 are the four soft keys under the screen, left to
# right.
LCD_KEY_BITS = {"L1": 0x01, "L2": 0x02, "L3": 0x04, "L4": 0x08, "L5": 0x10}

# Referenced by control.iso_swap(), which is a hidutil call and so macOS only.
# Linux needs no equivalent: the kernel takes the keyboard's country code from
# the USB descriptor and the desktop's own layout decides the rest.
HID_GRAVE = 0x700000035          # left of "1" on ANSI
HID_NON_US_BACKSLASH = 0x700000064   # the extra key ISO boards have

# cli.py warns when the device had to be opened exclusively. Nothing here
# opens it exclusively - we never claim the interface - so there is nothing to
# warn about.
NON_EXCLUSIVE = True

SYSFS_LEDS = "/sys/class/leds"
SYSFS_HIDRAW = "/sys/class/hidraw"

BACKLIGHT_LED = "g15::kbd_backlight"
POWER_ON_LED = "g15::power_on_backlight_val"
MKEY_LEDS = {
    "m1": "g15::macro_preset1",
    "m2": "g15::macro_preset2",
    "m3": "g15::macro_preset3",
    "mr": "g15::macro_record",
}

# The vendor interface. Interface 0 is the boot keyboard: it has its own
# hidraw node, but no LCD behind it, and writing frames there does nothing.
LCD_INTERFACE = 1

# The third input device hid-lg-g15 creates, alongside the boot keyboard and
# the consumer-control device. Matched by name because its event number moves
# around between boots and re-plugs.
GAMING_KEYS_NAME = "Logitech Gaming Keyboard Gaming Keys"

# From include/uapi/linux/input-event-codes.h. Spelled out rather than taken
# from evdev.ecodes so the tables below can be built without importing evdev,
# which is only needed once something actually reads keys.
KEY_MACRO1 = 0x290               # ..0x2a1 = G1..G18
KEY_MACRO_RECORD_START = 0x2B0   # MR
KEY_MACRO_PRESET1 = 0x2B3        # ..0x2b5 = M1..M3
KEY_KBD_LCD_MENU1 = 0x2B8        # ..0x2bc = L1..L5
EV_KEY = 0x01

# evdev keycode -> bit in the 24-bit G-key/mode field.
KEYCODE_BITS = {KEY_MACRO1 + i: 1 << i for i in range(GKEY_COUNT)}
KEYCODE_BITS[KEY_MACRO_PRESET1] = MODE_KEY_BITS["M1"]
KEYCODE_BITS[KEY_MACRO_PRESET1 + 1] = MODE_KEY_BITS["M2"]
KEYCODE_BITS[KEY_MACRO_PRESET1 + 2] = MODE_KEY_BITS["M3"]
KEYCODE_BITS[KEY_MACRO_RECORD_START] = MODE_KEY_BITS["MR"]

# evdev keycode -> bit in byte 4.
KEYCODE_LCD_BITS = {KEY_KBD_LCD_MENU1 + i: LCD_KEY_BITS[f"L{i + 1}"]
                    for i in range(5)}

UDEV_HINT = ("The kernel hands these out to root by default; the udev rule in "
             "linux/ gives the logged-in user access.")


class DeviceNotFound(Exception):
    pass


def _read_sysfs(path):
    with open(path) as handle:
        return handle.read().strip()


def _write_sysfs(path, text):
    """Write one sysfs attribute, blaming permissions clearly when it fails."""
    try:
        with open(path, "w") as handle:
            handle.write(text)
    except PermissionError:
        raise PermissionError(f"Cannot write {path}. {UDEV_HINT}")


def _hid_ids():
    """The HID_ID strings the kernel prints for this keyboard."""
    return {f"0003:{VENDOR_ID:08X}:{pid:08X}" for pid in PRODUCT_IDS}


def _uevent(path):
    values = {}
    try:
        for line in _read_sysfs(path).splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    except OSError:
        pass
    return values


def find_path():
    """Path of the G510's LCD hidraw node, or None if it isn't plugged in.

    Both of the keyboard's USB interfaces get a hidraw node and both report the
    same HID id, so the id alone is not enough: the LCD lives behind interface
    1. The hidraw device's parent directory *is* the USB interface, which is
    where bInterfaceNumber comes from.
    """
    wanted = _hid_ids()
    try:
        nodes = sorted(os.listdir(SYSFS_HIDRAW))
    except OSError:
        return None
    for node in nodes:
        device = os.path.realpath(os.path.join(SYSFS_HIDRAW, node, "device"))
        if _uevent(os.path.join(device, "uevent")).get("HID_ID") not in wanted:
            continue
        interface = os.path.dirname(device)
        try:
            number = int(_read_sysfs(os.path.join(interface,
                                                  "bInterfaceNumber")), 16)
        except (OSError, ValueError):
            continue
        if number == LCD_INTERFACE:
            return f"/dev/{node}"
    return None


def _usb_strings(hidraw_path):
    """(manufacturer, product) for the USB device behind a hidraw node."""
    node = os.path.basename(hidraw_path)
    device = os.path.realpath(os.path.join(SYSFS_HIDRAW, node, "device"))
    usb = os.path.dirname(os.path.dirname(device))   # interface -> device
    out = []
    for name in ("manufacturer", "product"):
        try:
            out.append(_read_sysfs(os.path.join(usb, name)))
        except OSError:
            out.append("")
    return out[0], out[1]


class LockLeds:
    """The Num Lock and Scroll Lock LEDs. A no-op on Linux, on purpose.

    On macOS these are free to say something useful, because the system drives
    only Caps Lock and has no Num Lock at all. On Linux they belong to the
    kernel's input core, and the desktop resets them on every layout change,
    VT switch and resume. Repurposing them means writing over the desktop in a
    loop and losing the race half the time, so the indicators feature simply
    does nothing here rather than making the LEDs flicker.

    The class stays so daemon.py's indicator path needs no Linux branch.
    """

    def __init__(self):
        pass

    def set(self, num=False, scroll=False):
        return False          # nothing changed, so nothing to report

    def close(self):
        pass


class _RgbLed:
    """One of the driver's colour LEDs, whichever way this kernel exposes it.

    Debian's 6.12 lg-g15 carries a driver-private `color` attribute taking
    "#rrggbb". Mainline moved the same LED to the multicolour LED class, where
    the colour is `multi_intensity` ("R G B", channel order in `multi_index`)
    and `brightness` scales it. Older or stripped builds have neither, leaving
    plain monochrome `brightness`. Probe once, at open time.
    """

    def __init__(self, name):
        self.path = os.path.join(SYSFS_LEDS, name)
        self.mode = None
        if os.path.exists(os.path.join(self.path, "color")):
            self.mode = "color"
        elif os.path.exists(os.path.join(self.path, "multi_intensity")):
            self.mode = "multi"
        elif os.path.exists(os.path.join(self.path, "brightness")):
            self.mode = "mono"

    def _channels(self):
        """Channel order of multi_intensity, as multi_index gives it."""
        try:
            order = _read_sysfs(os.path.join(self.path, "multi_index")).split()
        except OSError:
            order = []
        return order or ["red", "green", "blue"]

    def get(self):
        if self.mode == "color":
            value = _read_sysfs(os.path.join(self.path, "color")).lstrip("#")
            return (int(value[0:2], 16), int(value[2:4], 16),
                    int(value[4:6], 16))
        if self.mode == "multi":
            values = _read_sysfs(
                os.path.join(self.path, "multi_intensity")).split()
            found = dict(zip(self._channels(), values))
            return tuple(int(found.get(c, 0)) for c in ("red", "green", "blue"))
        if self.mode == "mono":
            level = int(_read_sysfs(os.path.join(self.path, "brightness")))
            return (level, level, level)
        raise OSError(f"{self.path} exposes no backlight interface")

    def set(self, rgb):
        red, green, blue = (max(0, min(255, int(v))) for v in rgb)
        if self.mode == "color":
            _write_sysfs(os.path.join(self.path, "color"),
                         "#%02x%02x%02x" % (red, green, blue))
            return
        if self.mode == "multi":
            by_name = {"red": red, "green": green, "blue": blue}
            _write_sysfs(
                os.path.join(self.path, "multi_intensity"),
                " ".join(str(by_name.get(c, 0)) for c in self._channels()))
            # The multicolour class only pushes an intensity change to the
            # hardware on the next brightness write, so write the brightness
            # the LED already has straight back. Harmless, and it means a
            # colour change lands immediately as it does on macOS.
            brightness = os.path.join(self.path, "brightness")
            _write_sysfs(brightness, _read_sysfs(brightness))
            return
        if self.mode == "mono":
            # No colour to give it, so the brightest channel sets the level.
            _write_sysfs(os.path.join(self.path, "brightness"),
                         str(max(red, green, blue)))
            return
        raise OSError(f"{self.path} exposes no backlight interface")


class _HidrawNode:
    """A writable handle on the LCD, shaped like the hidapi object it replaces.

    daemon.py reaches past G510 to keyboard._dev for the pre-packed frame path
    and for the device strings, so keep those three methods answering.
    """

    def __init__(self, path):
        self.path = path
        try:
            self._fd = os.open(path, os.O_WRONLY)
        except PermissionError:
            raise DeviceNotFound(
                f"A G510 is plugged in, but {path} is not writable. "
                f"{UDEV_HINT}")
        self._manufacturer, self._product = _usb_strings(path)

    def write(self, data):
        return os.write(self._fd, bytes(data))

    def get_manufacturer_string(self):
        return self._manufacturer

    def get_product_string(self):
        return self._product

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class G510:
    """An open handle on the keyboard. Usable as a context manager."""

    def __init__(self):
        path = find_path()
        if path is None:
            raise DeviceNotFound(
                "No G510 found. Check it is plugged in:\n"
                "  lsusb | grep -i g510")
        if not os.path.isdir(os.path.join(SYSFS_LEDS, BACKLIGHT_LED)):
            raise DeviceNotFound(
                "Found a G510, but hid-lg-g15 is not driving it, so the "
                "backlight and G-keys are invisible. Try:\n"
                "  sudo modprobe hid-lg-g15")
        self._dev = _HidrawNode(path)
        self._backlight = _RgbLed(BACKLIGHT_LED)
        self._power_on = _RgbLed(POWER_ON_LED)
        self._nonblocking = False
        # The G-key device is opened on first use, not here: backlight and LCD
        # work without python3-evdev installed, and nothing should fail over a
        # dependency it is not about to use.
        self._keys = None
        self._bits = 0
        self._lcd_bits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self._keys is not None:
            try:
                self._keys.close()
            except Exception:
                pass
            self._keys = None
        self._dev.close()

    def silence_gkey_scancodes(self):
        """Stop the G-keys doubling as ordinary keyboard keys.

        Already done: hid-lg-g15 writes the same feature report at probe time,
        which is why the G-keys do not also type F1-F12 and 1-6 here. Kept as
        a no-op so the daemon's connect path stays the same on both systems.
        """

    # -- backlight ---------------------------------------------------------

    def get_backlight(self):
        """Current backlight as an (r, g, b) tuple."""
        return self._backlight.get()

    def set_backlight(self, rgb):
        self._backlight.set(rgb)

    def set_power_on_backlight(self, rgb):
        """Colour the keyboard comes up in at power-on."""
        self._power_on.set(rgb)

    # -- M-key LEDs --------------------------------------------------------

    def set_mkeys(self, names):
        wanted = {name.lower() for name in names}
        for name, led in MKEY_LEDS.items():
            _write_sysfs(os.path.join(SYSFS_LEDS, led, "brightness"),
                         "1" if name in wanted else "0")

    # -- G-keys ------------------------------------------------------------

    def set_nonblocking(self, flag=True):
        self._nonblocking = bool(flag)

    def _gaming_keys(self):
        """The evdev device carrying the G-keys, M-keys and display keys."""
        if self._keys is not None:
            return self._keys
        try:
            import evdev
        except ImportError:
            raise OSError(
                "The G-keys need the evdev bindings: apt install python3-evdev")
        found = None
        for path in evdev.list_devices():
            candidate = evdev.InputDevice(path)
            if candidate.name == GAMING_KEYS_NAME:
                found = candidate
                break
            candidate.close()
        if found is None:
            raise OSError(
                f"No input device named {GAMING_KEYS_NAME!r}. Either "
                f"hid-lg-g15 is not loaded, or /dev/input is not readable. "
                f"{UDEV_HINT}")
        # Never grab: the desktop is welcome to these keys too, and grabbing
        # is how you end up owning the media keys by accident.
        os.set_blocking(found.fd, False)
        self._keys = found
        # A key already held when we open would otherwise go unseen until it
        # is released, which reads as a phantom release.
        for code in found.active_keys():
            self._apply(code, 1)
        return found

    def _apply(self, code, value):
        """Fold one key event into the bitfields. Returns True if they moved."""
        before = (self._bits, self._lcd_bits)
        bit = KEYCODE_BITS.get(code)
        if bit is not None:
            if value:
                self._bits |= bit
            else:
                self._bits &= ~bit
        bit = KEYCODE_LCD_BITS.get(code)
        if bit is not None:
            if value:
                self._lcd_bits |= bit
            else:
                self._lcd_bits &= ~bit
        return (self._bits, self._lcd_bits) != before

    def read_raw(self, size=64):
        """One pending input report, or None.

        There is no report to read here - the kernel decoded it into key
        events long ago - so build one back out of the accumulated key state,
        in the layout the macOS hardware sends. That keeps the poll-and-decode
        contract the shared daemon is written against, and the decode_*
        methods below stay the same code on both platforms.

        Like the hardware, a report only comes out when something changed.
        """
        keys = self._gaming_keys()
        timeout = 0 if self._nonblocking else None
        if not select.select([keys.fd], [], [], timeout)[0]:
            return None
        changed = False
        try:
            for event in keys.read():
                if event.type != EV_KEY:
                    continue
                # value 2 is autorepeat: the key is still down, nothing moved.
                if self._apply(event.code, event.value):
                    changed = True
        except BlockingIOError:
            pass
        if not changed:
            return None
        return bytes([INPUT_MACRO_KEYS,
                      self._bits & 0xFF,
                      (self._bits >> 8) & 0xFF,
                      (self._bits >> 16) & 0xFF,
                      self._lcd_bits])

    @staticmethod
    def carries_keystrokes(data):
        """True if this report contains what the user typed."""
        return bool(data) and data[0] in KEYSTROKE_REPORTS

    @staticmethod
    def decode_gkeys(data):
        """Set of pressed G-key names from a macro-key input report."""
        if not data or data[0] != INPUT_MACRO_KEYS or len(data) < 4:
            return None
        bits = data[1] | (data[2] << 8) | (data[3] << 16)
        return {f"G{i + 1}" for i in range(GKEY_COUNT) if bits & (1 << i)}

    @staticmethod
    def decode_mode_keys(data):
        """Set of M1/M2/M3/MR plus GAME if the joystick switch is engaged."""
        if not data or data[0] != INPUT_MACRO_KEYS or len(data) < 4:
            return None
        bits = data[1] | (data[2] << 8) | (data[3] << 16)
        return {name for name, mask in MODE_KEY_BITS.items() if bits & mask}

    @staticmethod
    def decode_lcd_keys(data):
        """Set of pressed display keys from a macro-key input report."""
        if not data or data[0] != INPUT_MACRO_KEYS or len(data) < 5:
            return None
        return {name for name, bit in LCD_KEY_BITS.items() if data[4] & bit}

    # -- LCD ---------------------------------------------------------------

    # Firmware quirk, worth knowing before chasing a blank screen: the G510
    # drops LCD writes while the backlight is fully off. The frame is accepted
    # and nothing appears. Bring the backlight up before blaming the frame.

    def send_lcd(self, pixels):
        """Push a frame. pixels is a list of LCD_HEIGHT rows of LCD_WIDTH ints."""
        self._dev.write(bytes(pack_lcd(pixels)))

    def clear_lcd(self):
        buf = bytearray(LCD_FRAME_LEN)
        buf[0] = LCD_REPORT
        self._dev.write(bytes(buf))


def pack_lcd(pixels):
    """Pack a 160x43 bitmap into the wire format.

    The panel wants vertical 8-pixel pages: one byte per column per page,
    with the topmost row of the page in the least significant bit.
    """
    buf = bytearray(LCD_FRAME_LEN)
    buf[0] = LCD_REPORT
    for page in range(LCD_PAGES):
        base = LCD_HEADER_LEN + page * LCD_WIDTH
        for x in range(LCD_WIDTH):
            byte = 0
            for bit in range(8):
                y = page * 8 + bit
                if y < LCD_HEIGHT and pixels[y][x]:
                    byte |= 1 << bit
            buf[base + x] = byte
    return buf
