"""The Keys pane: G-key bindings per bank, per-app overrides, the editor sheet.

Layout, refresh, both table data sources and the bodies of this pane's actions,
as plain functions taking the window controller. The controller keeps the
selectors - AppKit needs one object to send them to - and calls in here.

Three pieces of controller state belong to this pane: `editing_key` and
`editing_app` say what the shared editor sheet is pointed at, and `edit_field`
is the sheet's text field, which the type popup re-hints while the sheet is up.
"""
from AppKit import (NSAlert, NSBezelStyleRounded, NSButton, NSFont, NSMakeRect,
                    NSPopUpButton, NSScrollView, NSSegmentedControl,
                    NSTextField, NSView)

import actions
import config
import control
import device
import ipc
import recorder
import screens
from widgets import CARD_WIDTH, make_table, place, text

BINDING_TYPES = ["app", "keys", "shell", "text", "macro", "timer", "none"]
TYPE_HINTS = {
    "app": "Application name, e.g. Safari",
    "keys": "Key chord, e.g. cmd+shift+4",
    "shell": "Shell command, e.g. open -a Terminal",
    "text": "Text to type",
    "macro": "Name of a recorded macro",
    "timer": "Preset name or length of time, e.g. pomodoro or 10m",
    "none": "Nothing - clears the binding",
}


def gkey_order(name):
    """Sort G1..G18 numerically, tolerating junk from a hand-edited config."""
    digits = name[1:] if name[:1].upper() == "G" else ""
    return (0, int(digits)) if digits.isdigit() else (1, name)


# -- layout ----------------------------------------------------------------

def fill_bindings(win, content, height):
    win.bank_picker = NSSegmentedControl.alloc().initWithFrame_(
        NSMakeRect(CARD_WIDTH - 152, height - 26, 138, 22))
    win.bank_picker.setSegmentCount_(3)
    for index, name in enumerate(config.BANKS):
        win.bank_picker.setLabel_forSegment_(f"M{name}", index)
        win.bank_picker.setWidth_forSegment_(46, index)
    win.bank_picker.setTarget_(win)
    win.bank_picker.setAction_(b"bankChanged:")
    content.addSubview_(win.bank_picker)

    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(1, 30, CARD_WIDTH - 2, height - 61))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)
    win.table = make_table(win, ("Key", 56), ("Action", 392))
    win.table.setDoubleAction_(b"editBinding:")
    scroll.setDocumentView_(win.table)
    content.addSubview_(scroll)

    content.addSubview_(place(
        text("Double-click a row to change it", 11, secondary=True),
        14, 8, 200, 15))
    record = NSButton.alloc().initWithFrame_(
        NSMakeRect(CARD_WIDTH - 268, 5, 116, 22))
    record.setTitle_("Record macro…")
    record.setBezelStyle_(NSBezelStyleRounded)
    record.setFont_(NSFont.systemFontOfSize_(11))
    record.setTarget_(win)
    record.setAction_(b"recordMacro:")
    content.addSubview_(record)
    edit = NSButton.alloc().initWithFrame_(
        NSMakeRect(CARD_WIDTH - 148, 5, 134, 22))
    edit.setTitle_("Edit JSON…")
    edit.setBezelStyle_(NSBezelStyleRounded)
    edit.setFont_(NSFont.systemFontOfSize_(11))
    edit.setTarget_(win)
    edit.setAction_(b"editConfig:")
    content.addSubview_(edit)


def fill_profiles(win, content, height):
    content.addSubview_(place(
        text("Bindings that apply only while an app is frontmost", 11,
             secondary=True), 16, height - 26, 400, 15))
    win.profile_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(14, height - 56, 240, 24), False)
    win.profile_popup.setTarget_(win)
    win.profile_popup.setAction_(b"profileAppChanged:")
    win.profile_popup.setFont_(NSFont.systemFontOfSize_(11))
    content.addSubview_(win.profile_popup)

    add = NSButton.alloc().initWithFrame_(
        NSMakeRect(CARD_WIDTH - 148, height - 56, 134, 24))
    add.setTitle_("Add override…")
    add.setBezelStyle_(NSBezelStyleRounded)
    add.setFont_(NSFont.systemFontOfSize_(11))
    add.setTarget_(win)
    add.setAction_(b"addProfileBinding:")
    content.addSubview_(add)

    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(1, 8, CARD_WIDTH - 2, height - 72))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)
    win.profile_table = make_table(win, ("Key", 56), ("Override", 392))
    win.profile_table.setDoubleAction_(b"editProfileBinding:")
    scroll.setDocumentView_(win.profile_table)
    content.addSubview_(scroll)


# -- state -----------------------------------------------------------------

def profile_rows(win):
    profiles = (win.config.get("app_profiles") or {}).get(win.editing_app) or {}
    return sorted(profiles.items(), key=lambda pair: gkey_order(pair[0]))


def refresh_profiles(win):
    """Keep the app popup listing every app with overrides, plus this one."""
    profiles = win.config.get("app_profiles") or {}
    front = screens.frontmost_app()
    names = sorted(profiles)
    if front and front not in names:
        names.insert(0, front)
    chosen = win.editing_app if win.editing_app in names else (
        names[0] if names else None)
    win.editing_app = chosen
    titles = [str(win.profile_popup.itemTitleAtIndex_(i))
              for i in range(win.profile_popup.numberOfItems())]
    if titles != names:
        win.profile_popup.removeAllItems()
        for name in names:
            win.profile_popup.addItemWithTitle_(name)
    if chosen:
        win.profile_popup.selectItemWithTitle_(chosen)


def refresh(win):
    bank = str(win.config.get("active_bank", "1"))
    if bank in config.BANKS:
        win.bank_picker.setSelectedSegment_(config.BANKS.index(bank))
    refresh_profiles(win)
    win.table.reloadData()
    win.profile_table.reloadData()


# -- table data source -----------------------------------------------------

def row_count(win, table):
    # AppKit queries the data source the moment it is attached, which is
    # while the panes are still being built.
    if table is getattr(win, "profile_table", None):
        return len(profile_rows(win))
    return device.GKEY_COUNT


def cell_value(win, table, column, row):
    if table is getattr(win, "profile_table", None):
        rows = profile_rows(win)
        if row >= len(rows):
            return ""
        name, binding = rows[row]
        return name if column.identifier() == "key" else actions.describe(binding)
    name = f"G{row + 1}"
    if column.identifier() == "key":
        return name
    binding = win.config.get("bindings", {}).get(name)
    return actions.describe(binding) if binding else "—"


# -- actions ---------------------------------------------------------------

def bank_changed(win, sender):
    name = config.BANKS[int(sender.selectedSegment())]
    if control.daemon_running():
        try:
            ipc.request({"cmd": "set_bank", "bank": name})
        except OSError:
            pass
    else:
        settings = config.load()
        config.set_bank(settings, name)
        config.save(settings)
    win.refresh()


def profile_app_changed(win, sender):
    win.editing_app = str(sender.titleOfSelectedItem())
    win.profile_table.reloadData()


def add_profile_binding(win):
    if not win.editing_app:
        win.complain("No application to add an override for.")
        return
    prompt_binding(win, "G1", {}, app=win.editing_app)


def edit_profile_binding(win):
    row = win.profile_table.clickedRow()
    rows = profile_rows(win)
    if row < 0 or row >= len(rows):
        return
    name, binding = rows[row]
    prompt_binding(win, name, binding, app=win.editing_app)


def edit_binding(win):
    row = win.table.clickedRow()
    if row < 0:
        return
    name = f"G{row + 1}"
    prompt_binding(win, name, win.config.get("bindings", {}).get(name) or {})


def prompt_binding(win, key, binding, app=None):
    """The shared editor for a binding, whether base or per-app."""
    win.editing_key = key
    alert = NSAlert.alloc().init()
    alert.setMessageText_(f"{key} in {app}" if app else f"Binding for {key}")
    alert.setInformativeText_(
        f"Applies only while {app} is frontmost." if app
        else "Choose what this key should do.")
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")

    accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 110))
    keys_popup = None
    if app:
        keys_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 78, 150, 26), False)
        for index in range(1, device.GKEY_COUNT + 1):
            keys_popup.addItemWithTitle_(f"G{index}")
        keys_popup.selectItemWithTitle_(key)
        accessory.addSubview_(keys_popup)

    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(0, 46, 150, 26), False)
    for name in BINDING_TYPES:
        popup.addItemWithTitle_(name)
    popup.selectItemWithTitle_(binding.get("type", "app"))
    popup.setTarget_(win)
    popup.setAction_(b"bindingTypeChanged:")
    accessory.addSubview_(popup)

    field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 14, 320, 24))
    current = ""
    for name in ("name", "keys", "command", "text", "seconds", "preset"):
        if name in binding:
            current = binding[name]
    field.setStringValue_(current)
    field.setPlaceholderString_(TYPE_HINTS.get(binding.get("type", "app"), ""))
    accessory.addSubview_(field)

    win.edit_field = field
    alert.setAccessoryView_(accessory)
    alert.window().setInitialFirstResponder_(field)
    if alert.runModal() != 1000:
        return
    if keys_popup is not None:
        win.editing_key = str(keys_popup.titleOfSelectedItem())
    save_binding(win, str(popup.titleOfSelectedItem()),
                 str(field.stringValue()).strip(), app=app)


def binding_type_changed(win, sender):
    win.edit_field.setPlaceholderString_(
        TYPE_HINTS.get(str(sender.titleOfSelectedItem()), ""))


def save_binding(win, kind, value, app=None):
    key = win.editing_key
    if kind != "none" and value and kind == "keys":
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            win.complain(str(exc))
            return

    def apply(settings):
        if app:
            profiles = settings.setdefault("app_profiles", {})
            target = profiles.setdefault(app, {})
        else:
            target = settings.setdefault("bindings", {})
        if kind == "none" or not value:
            target.pop(key, None)
            if app and not target:
                settings["app_profiles"].pop(app, None)
        else:
            field = {"app": "name", "keys": "keys", "shell": "command",
                     "text": "text", "macro": "name", "timer": "seconds"}[kind]
            target[key] = {"type": kind, field: value}

    win.mutate(apply)
    win.refresh()


def record_macro(win):
    row = win.table.selectedRow()
    if row < 0:
        win.complain("Select a G-key row first, then record.")
        return
    key = f"G{row + 1}"
    if not actions.can_post_events():
        win.complain("Recording needs Accessibility permission. "
                     "Use the Permissions button in the Agent tab.")
        return
    notice = NSAlert.alloc().init()
    notice.setMessageText_(f"Record a macro for {key}")
    notice.setInformativeText_(
        "After you click Start, type the sequence you want, then press "
        "Escape to finish.\n\nKeys still reach whatever app is focused, "
        "so switch to a safe window first if that matters.")
    notice.addButtonWithTitle_("Start")
    notice.addButtonWithTitle_("Cancel")
    if notice.runModal() != 1000:
        return
    win.window.miniaturize_(None)
    try:
        steps = recorder.record(timeout=120.0)
    except recorder.RecordingError as exc:
        win.window.deminiaturize_(None)
        win.complain(str(exc))
        return
    win.window.deminiaturize_(None)
    win.show()
    if not steps:
        win.complain("Nothing was recorded, so the binding is unchanged.")
        return
    name = key.lower()

    def apply(settings):
        settings.setdefault("macros", {})[name] = steps
        settings.setdefault("bindings", {})[key] = {
            "type": "macro", "name": name}

    win.mutate(apply)
    win.refresh()
    done = NSAlert.alloc().init()
    done.setMessageText_(f"{key} now plays a {len(steps)}-step macro")
    done.setInformativeText_(recorder.summarise(steps, limit=8))
    done.addButtonWithTitle_("OK")
    done.runModal()
