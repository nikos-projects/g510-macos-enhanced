"""A menu bar app for the G510, built on AppKit.

Deliberately a status item rather than a window: the things you change often
(backlight colour, LCD screen, whether the agent runs) are one click each, and
anything deeper opens the JSON config.
"""
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSColor,
                    NSColorPanel, NSImage, NSMenu, NSMenuItem, NSStatusBar,
                    NSVariableStatusItemLength)
from Foundation import NSObject

import actions
import config
import control
import device
import ipc
import screens
from window import swatch_image

MENU_COLORS = ["white", "red", "orange", "yellow", "green",
               "cyan", "blue", "purple", "pink", "off"]

SCREEN_NAMES = {
    "status": "Status",
    "clock": "Clock",
    "claude": "Claude Usage",
    "media": "Now Playing",
    "printer": "3D Printer",
    "gkeys": "G-key Echo",
    "app": "Active App",
}


class G510Menu(NSObject):

    def init(self):
        self = objc.super(G510Menu, self).init()
        if self is None:
            return None
        self.config = config.load()
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        # Without a name of its own an item is handed a generic "Item-N" slot,
        # shared with whatever else happens to be running under the same
        # preference domain - which for a bundle that runs an outside
        # interpreter is org.python.python, not this app. Name it first: the
        # remembered position and visible flag are keyed on the name, so
        # setting it afterwards would re-read them over anything set here.
        self.status_item.setAutosaveName_("G510StatusItem")
        # macOS remembers a status item as hidden across launches, so say so
        # explicitly rather than trusting the default.
        self.status_item.setVisible_(True)
        self.set_icon()
        self.rebuild()
        return self

    @objc.python_method
    def set_icon(self, connected=True):
        """A keyboard glyph reads better in a crowded menu bar than text."""
        button = self.status_item.button()
        # Symbol availability varies by macOS release, so fall through to
        # whatever this machine actually has, and to text as a last resort.
        candidates = (["keyboard"] if connected
                      else ["keyboard.badge.exclamationmark", "keyboard"])
        for symbol in candidates:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol, "G510")
            if image is not None:
                image.setTemplate_(True)
                button.setImage_(image)
                button.setTitle_("" if connected else " ?")
                break
        else:
            button.setImage_(None)
            button.setTitle_("G510" if connected else "G510?")
        button.setToolTip_("G510 keyboard"
                           if connected else "G510 keyboard not found")

    # -- helpers -----------------------------------------------------------

    @objc.python_method
    def item(self, title, selector, target=None, state=False, enabled=True):
        entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, selector, "")
        entry.setTarget_(target or self)
        entry.setState_(1 if state else 0)
        entry.setEnabled_(enabled)
        return entry

    @objc.python_method
    def attempt(self, work):
        """Run a control-layer call, flagging the status item if it fails."""
        try:
            work()
            self.set_icon()
            return True
        except control.ControlError:
            self.set_icon(connected=False)
            return False

    @objc.python_method
    def mutate(self, apply):
        """Re-read, change, write back.

        The menu's cached config is only refreshed when the menu is rebuilt,
        so it can be minutes old. Writing it wholesale would revert a bank the
        keyboard switched or a macro MR recorded in the meantime.
        """
        fresh = config.load()
        apply(fresh)
        if not config.save(fresh):
            return fresh
        self.config = fresh
        if ipc.is_running():
            try:
                ipc.request({"cmd": "reload"})
            except OSError:
                pass
        return fresh

    # -- menu construction -------------------------------------------------

    @objc.python_method
    def rebuild(self):
        """Build the menu.

        This is the glanceable half of the UI: what the keyboard is doing and
        the handful of things worth changing without opening a window. Setup
        lives in Configuration, so nothing here needs explaining.
        """
        self.config = config.load()
        self.pending_submenus = []
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        connected = control.present()
        menu.addItem_(self.item(
            "G510 connected" if connected else "G510 not found",
            None, enabled=False))
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self.backlight_item())
        menu.addItem_(self.display_item())
        menu.addItem_(self.gkeys_item())

        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self.item("Configuration", b"showWindow:"))
        menu.addItem_(self.item("Quit", b"quit:"))
        for submenu, parent in self.pending_submenus:
            menu.setSubmenu_forItem_(submenu, parent)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def backlight_item(self):
        """Colours as swatches. The hex is noise at a glance; the colour is not."""
        try:
            rgb = config.parse_color(self.config.get("backlight", "#ffffff"))
        except config.ColorError:
            rgb = (255, 255, 255)
        current = config.format_color(rgb)

        submenu = NSMenu.alloc().init()
        submenu.setAutoenablesItems_(False)
        for name in MENU_COLORS:
            swatch = config.NAMED_COLORS[name]
            entry = self.item(name.title(), b"pickColor:",
                              state=(config.format_color(swatch) == current))
            entry.setImage_(swatch_image(swatch))
            entry.setRepresentedObject_(name)
            submenu.addItem_(entry)
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(self.item("Custom Colour…", b"openColorPanel:"))

        brightness = int(self.config.get("brightness", 100))
        title = "Backlight" if brightness == 100 else f"Backlight  {brightness}%"
        parent = self.item(title, None)
        parent.setImage_(swatch_image(rgb))
        self.pending_submenus.append((submenu, parent))
        return parent

    @objc.python_method
    def display_item(self):
        lcd = self.config.get("lcd", {})
        enabled = lcd.get("enabled", True)
        active = lcd.get("screen", "status")

        submenu = NSMenu.alloc().init()
        submenu.setAutoenablesItems_(False)
        for name in screens.SCREENS:
            entry = self.item(SCREEN_NAMES.get(name, name.title()),
                              b"pickScreen:",
                              state=(enabled and name == active))
            entry.setRepresentedObject_(name)
            submenu.addItem_(entry)
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(self.item("Turn Off", b"disableLcd:", state=not enabled))

        showing = SCREEN_NAMES.get(active, active.title()) if enabled else "Off"
        parent = self.item(f"Display  {showing}", None)
        self.pending_submenus.append((submenu, parent))
        return parent

    @objc.python_method
    def gkeys_item(self):
        bindings = self.config.get("bindings", {})
        bank = self.config.get("active_bank", "1")

        submenu = NSMenu.alloc().init()
        submenu.setAutoenablesItems_(False)
        bound = [(index, bindings[f"G{index}"])
                 for index in range(1, device.GKEY_COUNT + 1)
                 if bindings.get(f"G{index}")]
        if bound:
            for index, binding in bound:
                submenu.addItem_(self.item(
                    f"G{index}   {actions.describe(binding)}", b"showWindow:"))
        else:
            submenu.addItem_(self.item("Nothing bound yet", None, enabled=False))
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(self.item("Edit in Configuration…", b"showWindow:"))

        parent = self.item(
            f"G-keys  M{bank} · {len(bound)}" if bound else f"G-keys  M{bank}",
            None)
        self.pending_submenus.append((submenu, parent))
        return parent

    # -- actions -----------------------------------------------------------

    @objc.IBAction
    def pickColor_(self, sender):
        name = sender.representedObject()
        rgb = config.NAMED_COLORS[name]
        self.apply_color(rgb)

    @objc.IBAction
    def openColorPanel_(self, _sender):
        panel = NSColorPanel.sharedColorPanel()
        panel.setTarget_(self)
        panel.setAction_(b"colorPanelChanged:")
        panel.setContinuous_(True)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)

    @objc.IBAction
    def colorPanelChanged_(self, sender):
        color = sender.color().colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        if color is None:
            return
        rgb = (int(color.redComponent() * 255),
               int(color.greenComponent() * 255),
               int(color.blueComponent() * 255))
        self.apply_color(rgb)

    @objc.python_method
    def apply_color(self, rgb):
        brightness = self.config.get("brightness", 100)
        self.attempt(lambda: control.set_backlight(
            config.apply_brightness(rgb, brightness)))
        self.mutate(lambda s: s.update(backlight=config.format_color(rgb)))
        self.rebuild()

    @objc.IBAction
    def pickScreen_(self, sender):
        name = sender.representedObject()
        self.mutate(lambda s: s.setdefault("lcd", {}).update(
            screen=name, enabled=True))
        self.attempt(lambda: control.show_screen(name))
        self.rebuild()

    @objc.IBAction
    def disableLcd_(self, _sender):
        self.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
        self.attempt(control.clear_lcd)
        self.rebuild()

    @objc.IBAction
    def showWindow_(self, _sender):
        if getattr(self, "window_controller", None) is not None:
            self.window_controller.show()

    @objc.IBAction
    def quit_(self, _sender):
        NSApplication.sharedApplication().terminate_(None)


def name_the_app():
    """Make the Dock and menu say G510 rather than Python.

    The bundle execs an interpreter that lives outside it, so macOS identifies
    the process as Python. Patching the main bundle's info dictionary before
    NSApplication starts is the standard fix for a PyObjC app.
    """
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "G510"
            info["CFBundleDisplayName"] = "G510"
    except Exception:
        pass


class AppDelegate(NSObject):
    """Owns the UI, and builds it only once the app has finished launching.

    A status item created before the run loop starts is accepted but never
    placed on the menu bar, which is why this waits for the launch callback
    rather than constructing anything in main().
    """

    def initWithWindow_(self, wants_window):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.wants_window = wants_window
        self.menu = None
        self.controller = None
        return self

    def applicationDidFinishLaunching_(self, _notification):
        self.menu = G510Menu.alloc().init()
        if not self.wants_window:
            return
        try:
            import window
            self.controller = window.G510Window.alloc().initWithMenu_(self.menu)
            self.menu.window_controller = self.controller
            self.controller.show()
        except Exception:
            # An exception here is swallowed by AppKit, which would leave the
            # menu bar item up and the window silently absent.
            import traceback
            traceback.print_exc()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, visible):
        if not visible and self.controller is not None:
            self.controller.show()
        return True


def main():
    name_the_app()
    app = NSApplication.sharedApplication()
    # Accessory: a menu bar item and no Dock icon. The window is still there,
    # opened from the menu or on launch.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().initWithWindow_("--no-window" not in sys.argv)
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
