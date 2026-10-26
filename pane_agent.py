"""The Agent pane: the background agent, macOS permissions, the config file.

Layout, refresh and the bodies of this pane's actions, as plain functions
taking the window controller. The controller keeps the selectors - AppKit
needs one object to send them to - and calls in here.
"""
import os
import subprocess

from AppKit import (NSAlert, NSBezelStyleRounded, NSButton, NSColor, NSFont,
                    NSMakeRect)

import actions
import cli
import config
from widgets import CARD_WIDTH, place, text


# -- layout ----------------------------------------------------------------

def fill_agent(win, content, height):
    win.agent_button = NSButton.alloc().initWithFrame_(
        NSMakeRect(16, height - 46, 130, 30))
    win.agent_button.setBezelStyle_(NSBezelStyleRounded)
    win.agent_button.setTarget_(win)
    win.agent_button.setAction_(b"toggleAgent:")
    content.addSubview_(win.agent_button)
    win.agent_label = text("", 11, secondary=True)
    content.addSubview_(place(win.agent_label, 156, height - 40, 330, 15))
    win.permission_label = text("", 11, secondary=True)
    content.addSubview_(place(win.permission_label, 16, 16, 340, 15))
    win.permission_button = NSButton.alloc().initWithFrame_(
        NSMakeRect(CARD_WIDTH - 148, 12, 134, 22))
    win.permission_button.setTitle_("Permissions…")
    win.permission_button.setBezelStyle_(NSBezelStyleRounded)
    win.permission_button.setFont_(NSFont.systemFontOfSize_(11))
    win.permission_button.setTarget_(win)
    win.permission_button.setAction_(b"askPermissions:")
    content.addSubview_(win.permission_button)


def fill_configfile(win, content, height):
    content.addSubview_(place(text(config.CONFIG_PATH, 11, secondary=True),
                              16, height - 30, 460, 15))
    open_button = NSButton.alloc().initWithFrame_(
        NSMakeRect(16, 14, 160, 26))
    open_button.setTitle_("Open in editor")
    open_button.setBezelStyle_(NSBezelStyleRounded)
    open_button.setTarget_(win)
    open_button.setAction_(b"editConfig:")
    content.addSubview_(open_button)
    win.cli_button = NSButton.alloc().initWithFrame_(
        NSMakeRect(356, 14, 130, 26))
    win.cli_button.setTitle_("Install g510")
    win.cli_button.setBezelStyle_(NSBezelStyleRounded)
    win.cli_button.setToolTip_("Put the g510 command on your PATH")
    win.cli_button.setTarget_(win)
    win.cli_button.setAction_(b"installCli:")
    content.addSubview_(win.cli_button)
    win.log_button = NSButton.alloc().initWithFrame_(
        NSMakeRect(186, 14, 160, 26))
    win.log_button.setTitle_("Open agent log")
    win.log_button.setBezelStyle_(NSBezelStyleRounded)
    win.log_button.setTarget_(win)
    win.log_button.setAction_(b"openLog:")
    content.addSubview_(win.log_button)


# -- state -----------------------------------------------------------------

def refresh(win):
    loaded = win.agent_loaded_cached()
    win.agent_button.setTitle_("Stop" if loaded else "Start")
    win.agent_label.setStringValue_(
        "Running, and starts at login" if loaded
        else "Not running - G-keys will not do anything")
    posting = actions.can_post_events()
    reading_ok = actions.can_read_input()
    if posting and reading_ok:
        win.permission_label.setTextColor_(NSColor.secondaryLabelColor())
        win.permission_label.setStringValue_(
            "Input Monitoring and Accessibility granted")
        win.permission_button.setHidden_(True)
    else:
        missing = ([] if reading_ok else ["Input Monitoring"]) + \
                  ([] if posting else ["Accessibility"])
        win.permission_label.setTextColor_(NSColor.systemOrangeColor())
        win.permission_label.setStringValue_(f"Needed: {', '.join(missing)}")
        win.permission_button.setHidden_(False)


# -- actions ---------------------------------------------------------------

def toggle_agent(win):
    win.invalidate_agent_cache()
    if cli.agent_loaded():
        subprocess.run(["launchctl", "unload", "-w", cli.PLIST_PATH],
                       capture_output=True)
    else:
        try:
            cli.cmd_start([])
        except SystemExit as exc:
            win.complain(str(exc))
    win.invalidate_agent_cache()
    win.refresh()


def ask_permissions(win):
    if not actions.can_post_events():
        actions.request_post_access()
    subprocess.run(["open", "x-apple.systempreferences:com.apple."
                    "preference.security?Privacy_Accessibility"])
    win.refresh()


def install_cli(win):
    try:
        path = cli.install_cli_tool()
    except OSError as exc:
        win.complain(str(exc))
        return
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Command line tool installed")
    on_path = os.path.dirname(path) in os.environ.get("PATH", "").split(":")
    alert.setInformativeText_(
        f"{path}\n\n" + ("Try: g510 --help" if on_path else
                         "That folder is not on your PATH yet. Add it to "
                         "~/.zshrc:\n\n"
                         'export PATH="$HOME/.local/bin:$PATH"'))
    alert.addButtonWithTitle_("OK")
    alert.runModal()
