"""py2app build for the G510 app.

Produces a self-contained G510.app: the interpreter, the Python modules and
the compiled extensions all live inside the bundle, so it runs on a Mac with
no Homebrew and no virtualenv. One executable serves the window, the
background agent and the command line - see main.py.
"""
from setuptools import setup

# Named explicitly rather than left to modulegraph. py2app would find most
# of these by following imports, but the agent and the command line are
# reached through main.py by argument rather than by import, so at least some
# of the list is load-bearing and the rest is kept for consistency.
MODULES = [
    "actions", "alerts", "autoscreen", "cli", "config", "control", "daemon",
    "device", "gui", "ipc", "keytap", "lcd", "main", "menu", "notify",
    "printer", "recorder",
    "refresh", "screens", "timers", "triggers", "widgets", "window",
    "pane_keyboard",
    "pane_display", "pane_keys", "pane_agent", "pane_settings",
]

setup(
    app=["main.py"],
    options={
        "py2app": {
            "argv_emulation": False,
            "includes": MODULES + ["hid", "websocket"],
            "packages": ["objc", "Foundation", "AppKit", "Quartz",
                         "CoreFoundation", "websocket"],
            "plist": {
                "CFBundleName": "G510",
                "CFBundleDisplayName": "G510",
                "CFBundleIdentifier": "com.g510.app",
                "CFBundleVersion": "1.0",
                "CFBundleShortVersionString": "1.0",
                # Menu bar only: the window is opened from the menu.
                "LSUIElement": True,
                "LSMinimumSystemVersion": "13.0",
                "NSHumanReadableCopyright":
                    "Local utility for the Logitech G510.",
            },
        }
    },
    setup_requires=["py2app"],
)
