"""Low-level access to a Logitech G510's vendor HID interface.

Logitech ships no macOS driver for this keyboard, so everything here talks to
the vendor interface (usage page 0xff00) directly. Report layouts follow the
Linux hid-lg-g15 driver and libg15.
"""
import ctypes

import hid

VENDOR_ID = 0x046D
PRODUCT_IDS = (0xC22D, 0xC22E)   # G510, G510 with onboard audio
VENDOR_USAGE_PAGE = 0xFF00

# Writing this report with one zero byte per G-key stops the keyboard also
# emitting F1-F12 and 1-6 when a G-key is pressed. Without it every G-key
# types a character as well as firing its binding. Same as the Linux
# hid-lg-g15 driver does at probe time.
FEATURE_GKEYS_MODE = 0x01

FEATURE_BACKLIGHT_RGB = 0x05
FEATURE_POWER_ON_RGB = 0x06
FEATURE_MKEY_LEDS = 0x04
INPUT_MACRO_KEYS = 0x03

# Report 0x01 carries what the user is actually typing. On this interface it
# is the vendor-page report rather than the boot-keyboard report macOS types
# from, but it holds the same thing: byte 1 is the modifier mask (0x02 =
# shift) and the next two are HID keyboard usages - 0x2c space, 0x28 enter,
# 0x4f/0x50 arrows - verified by watching a typing session arrive. It must
# never be logged, broadcast or stored. 0x02 is the consumer report (media
# keys), filtered alongside it.
KEYSTROKE_REPORTS = frozenset({0x01, 0x02})

# The standard HID keyboard LED report: one byte, one bit each. macOS drives
# Caps Lock and nothing else - it has no Num Lock at all, that key is Clear
# and the keypad always types digits - so Num Lock and Scroll Lock are free
# to say something useful.
LED_NUM_LOCK = 0x01
LED_CAPS_LOCK = 0x02
LED_SCROLL_LOCK = 0x04

LCD_REPORT = 0x03
LCD_WIDTH, LCD_HEIGHT, LCD_PAGES = 160, 43, 6
LCD_FRAME_LEN = 992          # 32-byte header + 160 columns * 6 pages
LCD_HEADER_LEN = 32

MKEY_BITS = {"m1": 0x80, "m2": 0x40, "m3": 0x20, "mr": 0x10}

GKEY_COUNT = 18

# The mode keys and the game-mode switch ride in the high bits of the same
# 24-bit field as the G-keys, above the 18 the G-keys occupy. Mapped from this
# keyboard: M1 is bit 20, and the joystick switch on the top left is bit 18.
MODE_KEY_BITS = {
    "GAME": 1 << 18,
    "M1": 1 << 20,
    "M2": 1 << 21,
    "M3": 1 << 22,
    "MR": 1 << 23,
}

# The keys around the display, in byte 4 of the macro report. L1 is the
# display/menu key; L2-L5 are the four soft keys under the screen, left to
# right. Mapped from this keyboard, and matching libg15's layout.
LCD_KEY_BITS = {"L1": 0x01, "L2": 0x02, "L3": 0x04, "L4": 0x08, "L5": 0x10}


# The G510 reports CountryCode 0 - it declares no locale - so macOS cannot
# tell an ISO board from an ANSI one and assumes ANSI. On an ISO keyboard that
# swaps the only two keys whose positions differ between the two layouts: the
# one left of "1" and the one beside the left shift. On a German board those
# are ^/° and <>|. These are their HID usages.
HID_GRAVE = 0x700000035          # left of "1" on ANSI
HID_NON_US_BACKSLASH = 0x700000064   # the extra key ISO boards have


def _open_non_exclusively():
    """Stop hidapi from seizing the device when we open it.

    On macOS hidapi takes a HID device exclusively by default. The G510's
    vendor interface and its consumer-control interface (the media and volume
    keys) are the same IOHIDDevice, so seizing it makes macOS stop seeing the
    media keys entirely. hidapi ships a darwin-specific switch for this; the
    Python binding does not expose it, but the symbol is exported from the
    extension, so call it directly.
    """
    try:
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_darwin_set_open_exclusive(ctypes.c_int(0))
        return bool(lib.hid_darwin_get_open_exclusive() == 0)
    except (OSError, AttributeError):
        return False


NON_EXCLUSIVE = _open_non_exclusively()


class DeviceNotFound(Exception):
    pass


def find_path():
    """Path of the G510's vendor interface, or None if it isn't plugged in."""
    for pid in PRODUCT_IDS:
        for dev in hid.enumerate(VENDOR_ID, pid):
            if dev["usage_page"] == VENDOR_USAGE_PAGE:
                return dev["path"]
    return None


def keyboard_led_path():
    """The boot-keyboard interface, which carries the lock LEDs."""
    for pid in PRODUCT_IDS:
        for dev in hid.enumerate(VENDOR_ID, pid):
            if dev["usage_page"] == 1 and dev["usage"] == 6:
                return dev["path"]
    return None


class LockLeds:
    """The Num Lock and Scroll Lock LEDs, which macOS leaves dark.

    Caps Lock is read back from the system rather than assumed, so driving the
    other two does not switch it off underneath the user.
    """

    def __init__(self):
        path = keyboard_led_path()
        if path is None:
            raise DeviceNotFound("No G510 keyboard interface found")
        self._dev = hid.device()
        self._dev.open_path(path)
        self._last = None

    def set(self, num=False, scroll=False):
        state = (LED_NUM_LOCK if num else 0) | (LED_SCROLL_LOCK if scroll else 0)
        if _caps_lock_on():
            state |= LED_CAPS_LOCK
        if state == self._last:
            return False
        self._dev.write(bytes([0x00, state]))
        self._last = state
        return True

    def close(self):
        try:
            self._dev.write(bytes([0x00, LED_CAPS_LOCK if _caps_lock_on() else 0]))
        except Exception:
            pass
        self._dev.close()


def _caps_lock_on():
    try:
        import Quartz
        return bool(Quartz.CGEventSourceFlagsState(1)
                    & Quartz.kCGEventFlagMaskAlphaShift)
    except Exception:
        return False


class G510:
    """An open handle on the keyboard. Usable as a context manager."""

    def __init__(self):
        path = find_path()
        if path is None:
            raise DeviceNotFound(
                "No G510 found. Check it is plugged in:\n"
                "  system_profiler SPUSBDataType | grep -i g510")
        self._dev = hid.device()
        self._dev.open_path(path)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._dev.close()

    def silence_gkey_scancodes(self):
        """Stop the G-keys doubling as ordinary keyboard keys."""
        self._dev.send_feature_report(
            bytes([FEATURE_GKEYS_MODE]) + bytes(GKEY_COUNT))

    # -- backlight ---------------------------------------------------------

    def get_backlight(self):
        """Current backlight as an (r, g, b) tuple."""
        report = self._dev.get_feature_report(FEATURE_BACKLIGHT_RGB, 4)
        return (report[1], report[2], report[3])

    def set_backlight(self, rgb):
        r, g, b = rgb
        self._dev.send_feature_report(bytes([FEATURE_BACKLIGHT_RGB, r, g, b]))

    def set_power_on_backlight(self, rgb):
        """Colour the keyboard comes up in at power-on."""
        r, g, b = rgb
        self._dev.send_feature_report(bytes([FEATURE_POWER_ON_RGB, r, g, b]))

    # -- M-key LEDs --------------------------------------------------------

    def set_mkeys(self, names):
        mask = 0
        for name in names:
            mask |= MKEY_BITS.get(name.lower(), 0)
        self._dev.send_feature_report(bytes([FEATURE_MKEY_LEDS, mask]))

    # -- G-keys ------------------------------------------------------------

    def set_nonblocking(self, flag=True):
        self._dev.set_nonblocking(1 if flag else 0)

    def read_raw(self, size=64):
        """One pending input report, or None."""
        data = self._dev.read(size)
        return bytes(data) if data else None

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
