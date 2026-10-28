"""The G510 window, laid out the way a Mac settings window is.

Four panes behind a segmented control, each a column of grouped cards. Panes
size themselves from the cards they hold, so adding a control cannot silently
push another one off the bottom.

What lives where
----------------
This file is the window itself: the chrome, the pane registry, the refresh
timer, and the selectors. Everything a pane does - building its cards, reading
its controls back, the work behind its buttons - lives in that pane's module:

    widgets.py          AppKit primitives and the shared geometry
    pane_keyboard.py    device, backlight, joystick switch
    pane_display.py     LCD screen, soft keys, Claude rows, printer
    pane_keys.py        G-key bindings, per-app overrides, the editor sheet
    pane_agent.py       background agent, permissions, the config file
    pane_settings.py    everything else the config holds, and the two rule lists

Why one class and five helper modules, rather than five classes
---------------------------------------------------------------
Every control here sends its action to a target, and AppKit holds that target
weakly. One controller as the target for all of them keeps the object graph
exactly as simple as it looks: gui.py's AppDelegate retains this controller,
the repeating NSTimer retains it too, and nothing else needs an owner. Give
each pane its own NSObject and every one of them becomes something that has to
be kept alive by hand for as long as its buttons are on screen - a pane whose
owner is collected leaves live controls pointing at freed memory.

Splitting the class itself across modules is worse still: PyObjC will not
build one class from several NSObject bases, and an objc.Category cannot add
the instance attributes the panes store. So the panes are plain functions
taking this controller, and the selectors stay here, on one object, in one
readable list.

The Settings pane carries most of its controls in one dispatcher rather than
one selector each: every control it builds is identified `kind:path`, and
`settingsChanged:` turns that into a change. See pane_settings.py.

What a pane may use from the controller passed to it:

    win.config              the settings dict, reloaded once per refresh
    win.mutate(apply)       re-read, change, write back, tell the agent
    win.refresh()           refresh every pane
    win.complain(message)   a modal error
    win.window              the NSWindow, for miniaturise and first responder
"""
import time

import objc
from AppKit import (NSAlert, NSApplication, NSBackingStoreBuffered, NSMakePoint,
                    NSMakeRect, NSMakeSize, NSScreen, NSScrollView,
                    NSSegmentedControl, NSWindow, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSObject, NSTimer

import cli
import config
import pane_agent
import pane_display
import pane_keyboard
import pane_keys
import pane_settings
from widgets import (CARD_WIDTH, MARGIN, MAX_HEIGHT, MIN_HEIGHT,
                     NSViewHeightSizable, NSViewWidthSizable, WIDTH,
                     build_pane, place)
# gui.py draws the menu's colour swatches with this, and imports it from here.
from widgets import swatch_image  # noqa: F401  (re-exported for gui.py)

PANES = ("Keyboard", "Display", "Keys", "Agent", "Settings")


class G510Window(NSObject):

    # Methods AppKit reaches by selector - init, the timer callback, the table
    # data source, every IBAction - must stay undecorated on this class.
    # Everything else is @objc.python_method: called only from Python, and in
    # several cases returning or taking things no selector could carry.

    def initWithMenu_(self, menu):
        self = objc.super(G510Window, self).init()
        if self is None:
            return None
        self.menu = menu
        self.config = config.load()
        self.editing_key = None
        self.editing_app = None
        self.panes = {}
        # The Settings pane's own state: every control it built, keyed by
        # its `kind:path` identifier; the lists it rebuilds; and which rule
        # each of the two tables is editing.
        self.s_ctrls = {}
        self.s_options = {}
        self.rows_view = {}
        self.rows_sig = {}
        self.add_refs = {}
        self.rule_table = {}
        self.rule_body = {}
        self.rule_selected = {"autoscreen": 0, "triggers": 0}
        self.rules_sig = {}
        self.build()
        self.start_timer()
        return self

    # -- construction ------------------------------------------------------

    @objc.python_method
    def build(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, 640), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("G510")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSMakeSize(WIDTH, MIN_HEIGHT))
        self.window.setContentMaxSize_(NSMakeSize(WIDTH, MAX_HEIGHT))
        root = self.window.contentView()

        self.picker = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(MARGIN, 0, CARD_WIDTH, 24))
        self.picker.setSegmentCount_(len(PANES))
        for index, name in enumerate(PANES):
            self.picker.setLabel_forSegment_(name, index)
            self.picker.setWidth_forSegment_(CARD_WIDTH / len(PANES), index)
        self.picker.setSelectedSegment_(0)
        self.picker.setTarget_(self)
        self.picker.setAction_(b"paneChanged:")
        root.addSubview_(self.picker)

        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 100))
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(0)
        self.scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        root.addSubview_(self.scroll)

        self.panes["Keyboard"] = build_pane(self, [
            (62, None, pane_keyboard.fill_device),
            (118, "BACKLIGHT", pane_keyboard.fill_backlight),
            (86, "JOYSTICK SWITCH", pane_keyboard.fill_switch),
        ])
        self.panes["Display"] = build_pane(self, [
            (250, "SCREEN", pane_display.fill_screen),
            (62, "AUTO SWITCH", pane_display.fill_autoscreen),
            (62, "CLAUDE USAGE", pane_display.fill_claude),
            (104, "3D PRINTER", pane_display.fill_printer),
        ])
        self.panes["Keys"] = build_pane(self, [
            (254, "G-KEY BINDINGS", pane_keys.fill_bindings),
            (170, "PER-APP OVERRIDES", pane_keys.fill_profiles),
        ])
        self.panes["Agent"] = build_pane(self, [
            (92, "BACKGROUND AGENT", pane_agent.fill_agent),
            (76, "CONFIGURATION FILE", pane_agent.fill_configfile),
        ])
        self.panes["Settings"] = build_pane(self, [
            (230, "AUTO SWITCH", pane_settings.fill_auto),
            (310, "RULES FOR AUTO SWITCH", pane_settings.fill_auto_rules),
            (218, "COUNTDOWNS", pane_settings.fill_timers),
            (310, "TRIGGERS", pane_settings.fill_trigger_rules),
            (178, "CHAT MESSAGES", pane_settings.fill_chat),
            (234, "PANEL & INDICATORS", pane_settings.fill_panel),
            (272, "MORE BACKLIGHT", pane_settings.fill_backlight),
            (148, "GAUGES", pane_settings.fill_gauges),
            (132, "3D PRINTER", pane_settings.fill_printer),
        ])

        self.show_pane("Keyboard")
        self.refresh()

    @objc.python_method
    def show_pane(self, name):
        """Swap the pane in and size the window to it."""
        pane = self.panes[name]
        height = pane.frame().size.height
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        usable = int(screen.visibleFrame().size.height) - 120
        visible = max(MIN_HEIGHT, min(height, MAX_HEIGHT, usable))

        frame = self.window.frame()
        self.window.setContentSize_(NSMakeSize(WIDTH, visible + 40))
        self.window.setFrameTopLeftPoint_(NSMakePoint(frame.origin.x,
                                                      frame.origin.y + frame.size.height))
        root = self.window.contentView()
        top = root.frame().size.height
        place(self.picker, MARGIN, top - 32, CARD_WIDTH, 24)
        place(self.scroll, 0, 0, WIDTH, top - 40)
        self.scroll.setDocumentView_(pane)
        clip = self.scroll.contentView()
        clip.scrollToPoint_(NSMakePoint(0, height - clip.bounds().size.height))
        self.scroll.reflectScrolledClipView_(clip)

    @objc.python_method
    def show(self):
        self.center_on_active_screen()
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.refresh()

    @objc.python_method
    def center_on_active_screen(self):
        """Put the window on the screen being used, not a secondary display."""
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        frame = screen.visibleFrame()
        height = self.window.frame().size.height
        self.window.setFrameOrigin_((
            frame.origin.x + (frame.size.width - WIDTH) / 2,
            frame.origin.y + (frame.size.height - height) / 2))

    # -- state -------------------------------------------------------------

    @objc.python_method
    def start_timer(self):
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, b"tick:", None, True)

    def tick_(self, _timer):
        """Keep the window honest: the keyboard is driven from elsewhere too."""
        if not self.window.isVisible():
            return
        try:
            self.refresh()
        except Exception:
            try:
                pane_display.refresh_preview(self)
            except Exception:
                pass

    def refreshSoon_(self, _timer):
        """The refresh after an action from the Settings pane.

        Deferred by a turn of the run loop because a change there can
        rebuild the very view that sent the action.
        """
        try:
            self.refresh()
        except Exception:
            pass

    @objc.python_method
    def refresh(self):
        """Re-read the config once, then let each pane read itself back.

        The order is the order the panes are in, so a failure part-way leaves
        the same panes stale as it always did.
        """
        self.config = config.load()
        pane_keyboard.refresh(self)
        pane_display.refresh(self)
        pane_keys.refresh(self)
        pane_agent.refresh(self)
        pane_settings.refresh(self)

    @objc.python_method
    def agent_loaded_cached(self, max_age=5.0):
        """launchctl costs ~8ms and the answer rarely changes; cache it."""
        now = time.time()
        cached = getattr(self, "_agent_cache", None)
        if cached and now - cached[0] < max_age:
            return cached[1]
        value = cli.agent_loaded()
        self._agent_cache = (now, value)
        return value

    @objc.python_method
    def invalidate_agent_cache(self):
        self._agent_cache = None

    @objc.python_method
    def mutate(self, apply):
        """Re-read, change, write back.

        The agent and the CLI write this file too, and the window holds its
        copy for up to a second between refreshes. Saving that copy wholesale
        would revert anything they changed in the meantime.
        """
        fresh = config.load()
        apply(fresh)
        if not config.save(fresh):
            self.complain(config.last_error or "the config could not be saved")
            return fresh
        self.config = fresh
        cli.reload_agent()
        if self.menu is not None:
            self.menu.rebuild()
        return fresh

    @objc.python_method
    def complain(self, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("That did not work")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    # -- tables ------------------------------------------------------------
    #
    # Both tables belong to the Keys pane, but AppKit needs one data source
    # object, so the selectors live here and the answers come from there.

    def numberOfRowsInTableView_(self, table):
        if pane_settings.owns(table, self):
            return pane_settings.row_count(self, table)
        return pane_keys.row_count(self, table)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if pane_settings.owns(table, self):
            return pane_settings.cell_value(self, table, column, row)
        return pane_keys.cell_value(self, table, column, row)

    def tableViewSelectionDidChange_(self, notification):
        # Both rules tables edit in place, so a click is what selects the
        # rule shown underneath. The Keys tables ignore this.
        pane_settings.selection_changed(self, notification.object())

    # -- actions -----------------------------------------------------------
    #
    # The whole of what this window can do, in one list. Each one hands off to
    # the pane that owns the controls it touches.

    @objc.IBAction
    def paneChanged_(self, sender):
        self.show_pane(PANES[int(sender.selectedSegment())])

    # Settings pane - one selector for every control in it, told apart by
    # the `kind:path` identifier each one carries. See pane_settings.py.

    @objc.IBAction
    def settingsChanged_(self, sender):
        pane_settings.changed(self, sender)

    # Keyboard pane

    @objc.IBAction
    def presetClicked_(self, sender):
        pane_keyboard.preset_clicked(self, sender)

    @objc.IBAction
    def colorChanged_(self, sender):
        pane_keyboard.color_changed(self, sender)

    @objc.IBAction
    def brightnessChanged_(self, sender):
        pane_keyboard.brightness_changed(self, sender)

    @objc.IBAction
    def switchChanged_(self, sender):
        pane_keyboard.switch_changed(self, sender)

    # Display pane

    @objc.IBAction
    def screenChanged_(self, sender):
        pane_display.screen_changed(self, sender)

    @objc.IBAction
    def lcdKeyChanged_(self, sender):
        pane_display.lcd_key_changed(self, sender)

    @objc.IBAction
    def fontChanged_(self, sender):
        pane_display.font_changed(self, sender)

    @objc.IBAction
    def autoscreenToggled_(self, sender):
        pane_display.autoscreen_toggled(self, sender)

    @objc.IBAction
    def claudeRowChanged_(self, sender):
        pane_display.claude_row_changed(self, sender)

    @objc.IBAction
    def printerToggled_(self, sender):
        pane_display.printer_toggled(self, sender)

    @objc.IBAction
    def printerHostChanged_(self, sender):
        pane_display.printer_host_changed(self, sender)

    @objc.IBAction
    def printerTest_(self, _sender):
        pane_display.printer_test(self)

    # Keys pane

    @objc.IBAction
    def bankChanged_(self, sender):
        pane_keys.bank_changed(self, sender)

    @objc.IBAction
    def profileAppChanged_(self, sender):
        pane_keys.profile_app_changed(self, sender)

    @objc.IBAction
    def addProfileBinding_(self, _sender):
        pane_keys.add_profile_binding(self)

    @objc.IBAction
    def editProfileBinding_(self, _sender):
        pane_keys.edit_profile_binding(self)

    @objc.IBAction
    def editBinding_(self, _sender):
        pane_keys.edit_binding(self)

    @objc.IBAction
    def bindingTypeChanged_(self, sender):
        pane_keys.binding_type_changed(self, sender)

    @objc.IBAction
    def recordMacro_(self, _sender):
        pane_keys.record_macro(self)

    # Agent pane

    @objc.IBAction
    def toggleAgent_(self, _sender):
        pane_agent.toggle_agent(self)

    @objc.IBAction
    def askPermissions_(self, _sender):
        pane_agent.ask_permissions(self)

    @objc.IBAction
    def installCli_(self, _sender):
        pane_agent.install_cli(self)

    @objc.IBAction
    def openLog_(self, _sender):
        NSWorkspace.sharedWorkspace().openFile_(cli.LOG_PATH)

    # Both the Keys pane and the Agent pane offer this, so it stays here
    # rather than belonging to either of them.

    @objc.IBAction
    def editConfig_(self, _sender):
        config.ensure_exists()
        NSWorkspace.sharedWorkspace().openFile_(config.CONFIG_PATH)
