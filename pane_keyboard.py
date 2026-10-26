"""The Keyboard pane: what is plugged in, the backlight, the joystick switch.

Layout, refresh and the bodies of this pane's actions, as plain functions
taking the window controller. The controller keeps the selectors - AppKit
needs one object to send them to - and calls in here.
"""
from AppKit import (NSButton, NSColor, NSColorWell, NSImageView, NSMakeRect,
                    NSPopUpButton, NSSlider)

import config
import control
import device
from widgets import SCREEN_LABELS, place, swatch_image, symbol, text

SWATCHES = ["white", "red", "orange", "yellow", "green",
            "cyan", "blue", "purple", "pink", "off"]

SWITCH_ACTIONS = ["nothing", "bank:1", "bank:2", "bank:3"] + \
    [f"screen:{name}" for name in SCREEN_LABELS]


def switch_label(value):
    """'screen:claude' -> 'Claude Usage', for the switch popups."""
    if not value or value == "nothing":
        return "Nothing"
    if value.startswith("bank:"):
        return f"Bank M{value.split(':', 1)[1]}"
    if value.startswith("screen:"):
        return SCREEN_LABELS.get(value.split(':', 1)[1], value)
    return str(value)


# -- layout ----------------------------------------------------------------

def fill_device(win, content, height):
    icon = NSImageView.alloc().init()
    icon.setImage_(symbol("keyboard", 26))
    icon.setContentTintColor_(NSColor.secondaryLabelColor())
    content.addSubview_(place(icon, 16, height - 42, 30, 26))
    win.device_title = text("G510 Gaming Keyboard", 13, bold=True)
    content.addSubview_(place(win.device_title, 56, height - 36, 380, 18))
    win.device_detail = text("", 11, secondary=True)
    content.addSubview_(place(win.device_detail, 56, height - 54, 380, 15))


def fill_backlight(win, content, height):
    win.color_well = NSColorWell.alloc().initWithFrame_(
        NSMakeRect(16, height - 44, 56, 28))
    win.color_well.setTarget_(win)
    win.color_well.setAction_(b"colorChanged:")
    content.addSubview_(win.color_well)
    win.color_label = text("", 12)
    content.addSubview_(place(win.color_label, 84, height - 38, 120, 17))

    win.brightness_slider = NSSlider.alloc().initWithFrame_(
        NSMakeRect(84, height - 62, 250, 20))
    win.brightness_slider.setMinValue_(0)
    win.brightness_slider.setMaxValue_(100)
    win.brightness_slider.setTarget_(win)
    win.brightness_slider.setAction_(b"brightnessChanged:")
    win.brightness_slider.setContinuous_(True)
    content.addSubview_(win.brightness_slider)
    win.brightness_label = text("", 11, secondary=True)
    content.addSubview_(place(win.brightness_label, 342, height - 60, 90, 15))

    x = 16
    for name in SWATCHES:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, 14, 28, 28))
        button.setImage_(swatch_image(config.NAMED_COLORS[name]))
        button.setBordered_(False)
        button.setToolTip_(name.title())
        button.setTarget_(win)
        button.setAction_(b"presetClicked:")
        button.setIdentifier_(name)
        content.addSubview_(button)
        x += 34


def fill_switch(win, content, height):
    content.addSubview_(place(
        text("The switch above the F-keys, which locks the ⌘ key in "
             "hardware.", 11, secondary=True), 16, height - 26, 460, 15))
    win.switch_popups = {}
    for index, (key, label) in enumerate((("on", "Engaged"),
                                          ("off", "Released"))):
        x = 16 + index * 236
        content.addSubview_(place(text(label, 11, secondary=True),
                                  x, height - 44, 100, 15))
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, 14, 210, 26), False)
        for value in SWITCH_ACTIONS:
            popup.addItemWithTitle_(switch_label(value))
        popup.setTarget_(win)
        popup.setAction_(b"switchChanged:")
        popup.setIdentifier_(key)
        content.addSubview_(popup)
        win.switch_popups[key] = popup


# -- state -----------------------------------------------------------------

def refresh(win):
    running = control.daemon_running()
    connected = running or device.find_path() is not None
    win.device_title.setStringValue_(
        "G510 Gaming Keyboard" if connected else "No G510 found")
    if connected:
        detail = "Connected"
        if running:
            detail += " · agent holds the device"
        win.device_detail.setTextColor_(NSColor.secondaryLabelColor())
    else:
        detail = "Plug the keyboard in, or check the dock"
        win.device_detail.setTextColor_(NSColor.systemRedColor())
    win.device_detail.setStringValue_(detail)

    brightness = int(win.config.get("brightness", 100))
    win.brightness_slider.setIntValue_(brightness)
    win.brightness_label.setStringValue_(f"{brightness}% brightness")
    try:
        rgb = config.parse_color(win.config.get("backlight", "#ffffff"))
        win.color_well.setColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0))
        win.color_label.setStringValue_(config.format_color(rgb))
    except config.ColorError:
        win.color_label.setStringValue_("-")

    switch = win.config.get("game_switch") or {}
    for key, popup in win.switch_popups.items():
        popup.selectItemWithTitle_(switch_label(switch.get(key)))


# -- actions ---------------------------------------------------------------

def apply_color(win, rgb):
    brightness = win.config.get("brightness", 100)
    try:
        control.set_backlight(config.apply_brightness(rgb, brightness))
    except control.ControlError:
        pass
    win.mutate(lambda s: s.update(backlight=config.format_color(rgb)))
    win.refresh()


def preset_clicked(win, sender):
    apply_color(win, config.NAMED_COLORS[str(sender.identifier())])


def color_changed(win, sender):
    colour = sender.color().colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
    if colour is None:
        return
    apply_color(win, (int(colour.redComponent() * 255),
                      int(colour.greenComponent() * 255),
                      int(colour.blueComponent() * 255)))


def brightness_changed(win, sender):
    value = int(sender.intValue())
    win.brightness_label.setStringValue_(f"{value}% brightness")
    fresh = win.mutate(lambda s: s.update(brightness=value))
    try:
        control.set_backlight(config.effective_color(fresh))
    except control.ControlError:
        pass


def switch_changed(win, sender):
    key = str(sender.identifier())
    index = int(sender.indexOfSelectedItem())
    value = SWITCH_ACTIONS[index] if 0 <= index < len(SWITCH_ACTIONS) else "nothing"

    def apply(settings):
        block = settings.setdefault("game_switch", {})
        if value == "nothing":
            block.pop(key, None)
        else:
            block[key] = value

    win.mutate(apply)
