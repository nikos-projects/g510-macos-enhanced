"""One way in to the keyboard, whether or not the daemon holds it.

Every caller goes through here: if the daemon is running it owns the only HID
handle, so requests are forwarded to it; otherwise we open the device directly.
"""
import time

import device
import ipc
import screens
from lcd import Canvas


class ControlError(Exception):
    pass


def _direct(work):
    try:
        with device.G510() as keyboard:
            return work(keyboard)
    except device.DeviceNotFound as exc:
        raise ControlError(str(exc))
    except OSError as exc:
        raise ControlError(
            f"Could not open the keyboard ({exc}). Another process may hold it; "
            "try: g510 stop")


def _via_daemon(payload):
    """Ask the agent to act. The agent may vanish between check and call."""
    try:
        reply = ipc.request(payload)
    except OSError as exc:
        raise ControlError(f"Could not reach the agent ({exc}). "
                           "It may have stopped; try: g510 start")
    if not reply.get("ok"):
        raise ControlError(reply.get("error", "daemon refused the request"))
    return reply


def present():
    return ipc.is_running() or device.find_path() is not None


def daemon_running():
    return ipc.is_running()


# -- operations ------------------------------------------------------------

def get_backlight():
    if ipc.is_running():
        return tuple(_via_daemon({"cmd": "get_backlight"})["rgb"])
    return _direct(lambda k: k.get_backlight())


def set_backlight(rgb):
    if ipc.is_running():
        _via_daemon({"cmd": "set_backlight", "rgb": list(rgb)})
        return
    _direct(lambda k: k.set_backlight(rgb))


def set_power_on(rgb):
    if ipc.is_running():
        _via_daemon({"cmd": "set_power_on", "rgb": list(rgb)})
        return
    _direct(lambda k: k.set_power_on_backlight(rgb))


def set_mkeys(names):
    if ipc.is_running():
        _via_daemon({"cmd": "set_mkeys", "names": list(names)})
        return
    _direct(lambda k: k.set_mkeys(names))


def show_screen(name, state=None):
    pixels = screens.render(name, state or {})
    _send_lcd(pixels)


def show_text(lines):
    canvas = Canvas()
    for index, line in enumerate(lines[:3]):
        canvas.text(line, 2, 1 + index * 14, 12)
    _send_lcd(canvas.pixels())


def silence_gkeys():
    if ipc.is_running():
        _via_daemon({"cmd": "silence_gkeys"})
        return
    _direct(lambda k: k.silence_gkey_scancodes())


def clear_lcd():
    if ipc.is_running():
        _via_daemon({"cmd": "clear_lcd"})
        return
    _direct(lambda k: k.clear_lcd())


def _send_lcd(pixels):
    if ipc.is_running():
        packed = bytes(device.pack_lcd(pixels))
        _via_daemon({"cmd": "send_lcd", "frame": packed.hex()})
        return
    _direct(lambda k: k.send_lcd(pixels))


def iso_swap(enable):
    """Swap the two keys macOS gets wrong when it assumes an ANSI keyboard.

    A hidutil mapping scoped to this keyboard, so nothing else on the machine
    is affected. It does not survive a reboot on its own, which is why the
    agent re-applies it whenever it connects.
    """
    import json
    import subprocess

    matching = json.dumps({"ProductID": device.PRODUCT_IDS[0],
                           "VendorID": device.VENDOR_ID})
    if enable:
        pairs = [
            {"HIDKeyboardModifierMappingSrc": device.HID_GRAVE,
             "HIDKeyboardModifierMappingDst": device.HID_NON_US_BACKSLASH},
            {"HIDKeyboardModifierMappingSrc": device.HID_NON_US_BACKSLASH,
             "HIDKeyboardModifierMappingDst": device.HID_GRAVE},
        ]
    else:
        pairs = []
    setting = json.dumps({"UserKeyMapping": pairs})
    result = subprocess.run(
        ["hidutil", "property", "--matching", matching, "--set", setting],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise ControlError(f"hidutil refused the mapping: {result.stderr.strip()}")
    return bool(pairs)


def device_info():
    """(manufacturer, product) as reported by the keyboard."""
    if ipc.is_running():
        reply = _via_daemon({"cmd": "info"})
        return reply["manufacturer"], reply["product"]
    return _direct(lambda k: (k._dev.get_manufacturer_string(),
                              k._dev.get_product_string()))


def watch_gkeys():
    """Yield {'event': 'press'|'release', 'key': 'G1'} until interrupted."""
    if ipc.is_running():
        for message in ipc.request({"cmd": "watch"}, stream=True):
            yield message
        return
    try:
        keyboard = device.G510()
    except device.DeviceNotFound as exc:
        raise ControlError(str(exc))
    except OSError as exc:
        raise ControlError(f"Could not open the keyboard ({exc})")
    keyboard.set_nonblocking(True)
    # Every group the macro report carries, not just the G-keys: the mode keys
    # and the keys around the display arrive in the same report, and a caller
    # watching for a key press means any of them.
    decoders = (device.G510.decode_gkeys,
                device.G510.decode_mode_keys,
                device.G510.decode_lcd_keys)
    pressed = [set() for _ in decoders]
    try:
        while True:
            data = keyboard.read_raw()
            if data:
                if device.G510.carries_keystrokes(data):
                    continue          # never surface what the user typed
                for index, decode in enumerate(decoders):
                    keys = decode(data)
                    if keys is None:
                        continue
                    for name in sorted(keys - pressed[index]):
                        yield {"event": "press", "key": name,
                               "raw": data.hex()}
                    for name in sorted(pressed[index] - keys):
                        yield {"event": "release", "key": name}
                    pressed[index] = keys
            time.sleep(0.004)
    finally:
        keyboard.close()
