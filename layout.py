"""Resolve key names against the keyboard layout actually in use.

macOS virtual keycodes are positional, not alphabetic: keycode 6 is whatever
key sits where "Z" is on a US board, which on a German QWERTZ types "y". A
hard-coded US table therefore types the wrong character on any other layout,
and has no name at all for keys a US board does not have - the ISO key beside
the left shift, or umlauts.

So instead of guessing, ask macOS what each key produces under the current
layout, via the Text Input Source API.
"""
import ctypes
import ctypes.util

_CARBON = "/System/Library/Frameworks/Carbon.framework/Carbon"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

KEYCODE_LIMIT = 128
_SHIFT_STATE = 0x02          # shiftKey >> 8
_ACTION_DISPLAY = 3          # kUCKeyActionDisplay
_NO_DEAD_KEYS = 1            # kUCKeyTranslateNoDeadKeysBit


def _load():
    carbon = ctypes.cdll.LoadLibrary(_CARBON)
    core = ctypes.cdll.LoadLibrary(_CF)
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    core.CFDataGetBytePtr.restype = ctypes.c_void_p
    core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    return carbon, core


def _layout_data(carbon, core):
    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        return None
    key = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
    data = carbon.TISGetInputSourceProperty(source, key)
    if not data:
        return None
    return core.CFDataGetBytePtr(ctypes.c_void_p(data))


def characters():
    """{keycode: (unshifted, shifted)} for the layout in use, or {} if unknown."""
    try:
        carbon, core = _load()
        layout = _layout_data(carbon, core)
        if not layout:
            return {}
        translate = carbon.UCKeyTranslate
        translate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint16)]
        kbd_type = carbon.LMGetKbdType()
    except (OSError, AttributeError, ValueError):
        return {}

    result = {}
    for code in range(KEYCODE_LIMIT):
        pair = []
        for state in (0, _SHIFT_STATE):
            dead = ctypes.c_uint32(0)
            length = ctypes.c_ulong(0)
            buffer = (ctypes.c_uint16 * 8)()
            status = translate(
                ctypes.c_void_p(layout), code, _ACTION_DISPLAY, state,
                kbd_type, _NO_DEAD_KEYS, ctypes.byref(dead), 8,
                ctypes.byref(length), buffer)
            if status != 0 or length.value == 0:
                pair.append(None)
                continue
            pair.append("".join(chr(buffer[i]) for i in range(length.value)))
        if any(pair):
            result[code] = tuple(pair)
    return result


# The numeric keypad types the same digits and operators as the main rows, so
# it has to come second or "cmd+shift+4" resolves to keypad 4 - which is a
# different key as far as every shortcut is concerned.
KEYPAD = frozenset({65, 67, 69, 71, 75, 76, 78, 81,
                    82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92})


def name_to_keycode():
    """{character: (keycode, needs_shift)} for the layout in use.

    Unshifted spellings win over shifted ones, and the main rows win over the
    keypad, so each character resolves to the key a person would reach for.
    """
    chars = characters()
    mapping = {}

    def add(code, char, needs_shift):
        if not char or not char.isprintable() or char == " ":
            return
        mapping.setdefault(char, (code, needs_shift))
        mapping.setdefault(char.lower(), (code, needs_shift))

    for keypad in (False, True):
        codes = [c for c in sorted(chars) if (c in KEYPAD) is keypad]
        for code in codes:
            add(code, chars[code][0], False)
        for code in codes:
            add(code, chars[code][1], True)
    return mapping
