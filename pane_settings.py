"""The Settings pane: everything the command line reaches, in one place.

The other four tabs answer "what is my keyboard doing". This one is the rest
of the configuration - the settings that used to need `g510 ...` or the
config file, and the two lists of rules.

Every ordinary setting is one row of `FIELDS`: a kind, a config path, and
where it sits in its card. The cards are built by walking that table rather
than by hand, so a setting cannot be described and then forgotten, and
`tests/test_pane_settings.py` asserts the table reaches exactly the paths the
LCD menu does - minus the ones the other four tabs already own. The two rule
lists are built separately because they are lists: a table of rules, and
underneath it the selected rule with its conditions.

Layout, refresh and the bodies of this pane's actions, as plain functions
taking the window controller. The controller keeps the selectors - AppKit
needs one object to send them to - and calls in here.

The whole pane hangs off one selector, `settingsChanged:`. Every control it
builds carries an identifier of the form `kind:path`, so one dispatcher can
turn a checkbox, a number, a popup or a "+ Rule" button into a change without
a selector per control. That is what keeps the list of actions in window.py
short enough to read.
"""
from AppKit import (NSBezelStyleRounded, NSButton, NSFont, NSMakeRect,
                    NSPopUpButton, NSScrollView, NSSwitchButton, NSTextField,
                    NSView)
from Foundation import NSIndexSet, NSTimer

import cli
import config
import control
import menu
import screens
import timers
from widgets import CARD_WIDTH, SCREEN_LABELS, make_table, place, text

# The two blocks the rule editors work on: config key, card key, card title.
RULE_BLOCKS = (("autoscreen", "rules-auto", "AUTO SWITCH RULES"),
               ("triggers", "triggers", "TRIGGERS"))

# How far apart the rows of a rebuilt list are.
ROW_H = 26

# A number written as "2" is an int and "2.5" a float: the config holds
# seconds either way, and this keeps `idle_seconds: 60` from becoming 60.0
# every time a field is touched.
LIMITS = {
    "seconds": (1, 86400), "check_seconds": (0.25, 60),
    "poll_seconds": (0.5, 60), "show_seconds": (0, 300),
    "ack_seconds": (0, 60), "idle_seconds": (0, 86400),
    "hold_seconds": (0, 60), "manual_seconds": (0, 86400),
    "refresh_seconds": (0.1, 600), "stale_after_seconds": (0, 86400),
    "port": (1, 65535),
    # The beat dip: how far down as a percentage, and how long it takes
    # to climb back - the same two ranges the CLI refuses to go outside.
    "amount": (0, 100), "decay": (0.05, 5),
    # How eager the detector is: the one number on the row that is not
    # about the shape of the dip but about what counts as a hit.
    "sensitivity": (0, 100),
}

# The kinds a FIELDS row can name. `_SCALAR` is the ones that write a config
# path on their own; the test compares that set against the LCD menu's.
_SCALAR = ("bool", "iso", "num", "text", "csv", "screens", "choice", "screen")
_KINDS = _SCALAR + ("boolish", "rows", "rules", "act")

# Tabs that already own these paths. `Settings` must not build a second
# control for something the Display tab already offers, so the parity test
# subtracts them from what the LCD menu knows.
OWNED_ELSEWHERE = frozenset({
    "lcd/screen", "lcd/enabled", "lcd/font",           # Display
    "brightness", "backlight",                          # Keyboard
    "game_switch/on", "game_switch/off",                # Keyboard
    "active_bank",                                      # Keys
    "claude/rows",                                      # Display
    "printer/enabled", "printer/host",                  # Display
    "lcd_keys/L2", "lcd_keys/L3", "lcd_keys/L4", "lcd_keys/L5",
})

# -- the table ------------------------------------------------------------
#
# kind   what the control is and what it writes
# card   which card builds it
# top    pixels below the top of that card; x/w are its geometry
# label  the caption to the left of a field (fields only - a checkbox is its
#        own caption)
# title  the checkbox's own words
# options()  the values of a popup, computed at build time
# labels how to title those values: "screen" (Now Playing), "lcdkey"
#        (Next/screen:clock), "poweron" (a prompt that fires and resets)
FIELDS = {
    # -- AUTO SWITCH: the timings behind the rules
    "autoscreen/enabled": dict(
        kind="bool", card="auto", top=10, x=16, w=340,
        title="Switch screens by what the Mac is doing"),
    "autoscreen/idle_seconds": dict(
        kind="num", card="auto", top=44, x=100, w=64, label="Idle after"),
    "autoscreen/hold_seconds": dict(
        kind="num", card="auto", top=44, x=252, w=64, label="Hold for",
        lx=180),
    "autoscreen/check_seconds": dict(
        kind="num", card="auto", top=76, x=100, w=64, label="Check every"),
    "autoscreen/manual_seconds": dict(
        kind="num", card="auto", top=76, x=252, w=64, label="Hands-off",
        lx=180),
    "autoscreen/fallback": dict(
        kind="screen", card="auto", top=140, x=100, w=150, label="Fallback",
        options=lambda: ("",) + tuple(screens.SCREENS) + ("off",)),
    "autoscreen/games": dict(
        kind="csv", card="auto", top=172, x=100, w=364, label="Games",
        placeholder="Minecraft*, com.mojang.* - whatever counts as a game"),
    "autoscreen/not_games": dict(
        kind="csv", card="auto", top=200, x=100, w=364, label="Never games",
        placeholder="win over everything else, e.g. Steam"),

    # -- COUNTDOWNS
    "timers/enabled": dict(
        kind="bool", card="timers", top=10, x=16, w=220,
        title="Countdown timers"),
    "timers/show_seconds": dict(
        kind="num", card="timers", top=44, x=76, w=60, label="Show (s)"),
    "timers/ack_seconds": dict(
        kind="num", card="timers", top=44, x=214, w=60, label="Ack (s)",
        lx=144),
    "timers/sound": dict(
        kind="choice", card="timers", top=44, x=336, w=128, label="Sound",
        lx=276, options=menu._sounds),

    # -- TRIGGERS: the switch and the cadence, above the list of rules
    "triggers/enabled": dict(
        kind="bool", card="triggers", top=8, x=16, w=140,
        title="Triggers are on"),
    "triggers/check_seconds": dict(
        kind="num", card="triggers", top=8, x=264, w=64, label="Check every",
        lx=164),

    # -- CHAT MESSAGES
    "notifications/enabled": dict(
        kind="bool", card="chat", top=10, x=16, w=300,
        title="Read chat messages"),
    "notifications/poll_seconds": dict(
        kind="num", card="chat", top=44, x=76, w=60, label="Poll (s)"),
    "notifications/show_seconds": dict(
        kind="num", card="chat", top=44, x=214, w=60, label="Show (s)",
        lx=144),
    "notifications/sound": dict(
        kind="choice", card="chat", top=44, x=336, w=128, label="Sound",
        lx=276, options=menu._sounds),
    "notifications/apps": dict(
        kind="csv", card="chat", top=78, x=100, w=364, label="Apps",
        placeholder="Discord, Vesktop, com.hnc.Discord - names or bundles"),
    "notifications/banners": dict(
        kind="bool", card="chat", top=112, x=16, w=200,
        title="Banners from other apps"),
    "notifications/banners_seconds": dict(
        kind="num", card="chat", top=112, x=290, w=60, label="Show (s)",
        lx=224),
    "vencord/enabled": dict(
        kind="bool", card="chat", top=146, x=16, w=420,
        title="Discord screen - the Vencord plugin (Vesktop)"),

    # -- PANEL & INDICATORS
    "indicators/numlock": dict(
        kind="choice", card="panel", top=10, x=116, w=140,
        label="Num Lock", options=lambda: menu.INDICATORS),
    "indicators/scrolllock": dict(
        kind="choice", card="panel", top=42, x=116, w=140,
        label="Scroll Lock", options=lambda: menu.INDICATORS),
    "restore_backlight_on_start": dict(
        kind="bool", card="panel", top=76, x=16, w=360,
        title="Restore the backlight colour when the agent starts"),
    "iso_keyboard": dict(
        kind="iso", card="panel", top=104, x=16, w=360,
        title="Swap ^/° and <>| back for an ISO keyboard"),
    "lcd_keys/L1": dict(
        kind="choice", card="panel", top=136, x=116, w=150, label="L1 key",
        options=menu._lcd_keys, labels="lcdkey"),
    "lcd/refresh_seconds": dict(
        kind="num", card="panel", top=170, x=116, w=64, label="Refresh (s)"),
    "claude/stale_after_seconds": dict(
        kind="num", card="panel", top=170, x=340, w=70,
        label="Claude stale (s)", lx=190),
    "lcd/cycle": dict(
        kind="screens", card="panel", top=204, x=116, w=348, label="Cycle",
        placeholder="status, clock, media - what the cycle walks"),

    # -- APP API
    # Not a setting so much as an offer: this is the switch that puts
    # the keyboard within reach of every other program on the Mac, so
    # it is worded as what it does rather than as a port number.
    "api/enabled": dict(
        kind="bool", card="api", top=10, x=16, w=420,
        title="Let other programs drive the keyboard (http://127.0.0.1)"),
    "api/port": dict(
        kind="num", card="api", top=42, x=116, w=70, label="Port"),

    # -- MORE BACKLIGHT
    "poweron": dict(
        kind="act", card="backlight", top=10, x=146, w=170,
        label="Power on with", labels="poweron",
        options=lambda: ("",) + tuple(sorted(config.NAMED_COLORS))),
    # The beat dip: the switch first, then the two numbers the pulse is
    # drawn from. They sit under the app-colour list, which is where the
    # card has to grow to reach them.
    "beat/enabled": dict(
        kind="bool", card="backlight", top=214, x=16, w=420,
        title="Dip the backlight to the beat of the music"),
    "beat/amount": dict(
        kind="num", card="backlight", top=242, x=76, w=60,
        label="Dip to %"),
    "beat/decay": dict(
        kind="num", card="backlight", top=242, x=230, w=60,
        label="Back in (s)", lx=150),
    # Third on the same row: the other two say what the dip looks like,
    # this one says how easily a beat is counted as one.
    "beat/sensitivity": dict(
        kind="num", card="backlight", top=242, x=384, w=60,
        label="Sens", lx=308),

    # -- GAUGES and the two other lists
    "gauges": dict(kind="rows", card="gauges"),
    "timers/presets": dict(kind="rows", card="timers"),
    "app_colors": dict(kind="rows", card="backlight"),
    "autoscreen/rules": dict(kind="rules", card="rules-auto"),
    "triggers/rules": dict(kind="rules", card="triggers"),

    # -- 3D PRINTER
    "printer/port": dict(
        kind="num", card="printer", top=10, x=76, w=80, label="Port"),
    "printer/refresh_seconds": dict(
        kind="num", card="printer", top=10, x=300, w=70, label="Refresh (s)",
        lx=200),
    "printer/kind": dict(
        kind="choice", card="printer", top=44, x=76, w=170, label="Backend",
        options=lambda: _backends()),
    "printer/api_key": dict(
        kind="text", card="printer", top=78, x=76, w=300, label="API key",
        placeholder="only some backends need one"),
}


def _backends():
    try:
        from printer import BACKENDS
        return tuple(BACKENDS)
    except Exception:                     # pragma: no cover
        return ("auto", "creality", "moonraker", "octoprint", "prusalink")


# -- controls --------------------------------------------------------------

def _y(height, top, h=22):
    """Where a control `top` pixels below the top of a card sits."""
    return height - top - h


def _wire(win, ident, control, register):
    control.setIdentifier_(ident)
    control.setTarget_(win)
    control.setAction_(b"settingsChanged:")
    if register:
        win.s_ctrls[ident] = control
    return control


def caption(parent, height, top, x, width, message, size=11):
    parent.addSubview_(place(text(message, size, secondary=True),
                             x, _y(height, top, 15), width, 15))


def check(win, parent, height, top, x, width, ident, title, register=True):
    box = NSButton.alloc().initWithFrame_(
        NSMakeRect(x, _y(height, top, 20), width, 20))
    box.setButtonType_(NSSwitchButton)
    box.setTitle_(title)
    box.setFont_(NSFont.systemFontOfSize_(11))
    parent.addSubview_(box)
    return _wire(win, ident, box, register)


def field(win, parent, height, top, x, width, ident, placeholder=None,
          register=True):
    box = NSTextField.alloc().initWithFrame_(NSMakeRect(x, _y(height, top),
                                                        width, 22))
    if placeholder:
        box.setPlaceholderString_(placeholder)
    box.setFont_(NSFont.systemFontOfSize_(11))
    parent.addSubview_(box)
    return _wire(win, ident, box, register)


def pick(win, parent, height, top, x, width, ident, options, labels=None,
         placeholder=None, register=True):
    box = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(x, _y(height, top, 24), width, 24), False)
    box.setFont_(NSFont.systemFontOfSize_(11))
    for title in _titles(list(options), labels, placeholder):
        box.addItemWithTitle_(title)
    parent.addSubview_(box)
    win.s_options[ident] = tuple(options)
    return _wire(win, ident, box, register)


def button(win, parent, height, top, x, width, title, ident, register=False):
    """A button. Never registered for refresh: it has no value to show."""
    box = NSButton.alloc().initWithFrame_(
        NSMakeRect(x, _y(height, top, 22), width, 22))
    box.setTitle_(title)
    box.setBezelStyle_(NSBezelStyleRounded)
    box.setFont_(NSFont.systemFontOfSize_(11))
    parent.addSubview_(box)
    return _wire(win, ident, box, register)


def _titles(values, labels, placeholder=None):
    """Popup titles for a list of config values."""
    out = []
    for index, value in enumerate(values):
        if index == 0 and value == "" and placeholder:
            out.append(placeholder)
        elif labels == "screen":
            out.append(SCREEN_LABELS.get(value, value.title()) if value
                       else "None")
        elif labels == "lcdkey":
            out.append(_lcd_key_title(value))
        elif labels == "poweron":
            out.append("Choose…" if value == "" else value)
        else:
            out.append(value or "None")
    return out


def _lcd_key_title(value):
    if value.startswith("screen:"):
        name = value.split(":", 1)[1]
        return SCREEN_LABELS.get(name, name.title())
    return {"next": "Next", "prev": "Previous", "off": "Off"}.get(
        value, value.title())


def clear(container):
    for view in list(container.subviews()):
        view.removeFromSuperview()


def resize(container, rows):
    """Give a list's container the height its rows need, keeping its width."""
    frame = container.frame()
    container.setFrame_(NSMakeRect(0, 0, frame.size.width,
                                   max(3, rows) * ROW_H))
    return int(frame.size.width)


# -- cards -----------------------------------------------------------------

def fill_fields(win, content, height, card):
    """Every FIELDS row that belongs to `card`, in table order.

    The list rows and the rule lists carry no geometry - they build
    themselves - so they are skipped here and built by their own fill.
    """
    for path, spec in FIELDS.items():
        if spec.get("card") != card or "top" not in spec:
            continue
        kind = spec["kind"]
        top, x, width = spec["top"], spec["x"], spec["w"]
        label = spec.get("label")
        if label:
            # The caption sits just left of its control; two controls on one
            # row each keep their own caption rather than one long one.
            left = spec.get("lx", 16)
            caption(content, height, top + 3, left, x - left - 8, label)
        ident = f"{kind}:{path}"
        register = kind in _SCALAR
        if kind in ("bool", "iso"):
            check(win, content, height, top, x, width, ident, spec["title"],
                  register=register)
        elif kind in ("choice", "screen") or "options" in spec:
            labels = spec.get("labels") or (
                "screen" if kind == "screen" else None)
            pick(win, content, height, top, x, width, ident,
                 spec["options"](), labels, spec.get("placeholder"),
                 register=register)
        else:
            field(win, content, height, top, x, width, ident,
                  spec.get("placeholder"), register=register)


def fill_auto(win, content, height):
    fill_fields(win, content, height, "auto")
    caption(content, height, 108, 16, 440,
            "Hands-off is how long autoscreen stays out after you pick a "
            "screen yourself.")


def fill_timers(win, content, height):
    fill_fields(win, content, height, "timers")
    caption(content, height, 80, 16, 440,
            "Presets a key can start - a name and a length, as in 5m or 1h")
    win.rows_view["presets"] = _scroll_rows(
        win, content, _y(height, 100, 78), 14, CARD_WIDTH - 28, 78,
        "presets")
    _add_row(win, content, height, 184, "presets")


def fill_chat(win, content, height):
    fill_fields(win, content, height, "chat")
    caption(content, height, 142, 16, 440,
            "Only the channel left open in the app can be read at all.")
    caption(content, height, 160, 16, 440,
            "Banners are read from macOS, about five seconds after they "
            "arrive.")


def fill_panel(win, content, height):
    fill_fields(win, content, height, "panel")
    caption(content, height, 141, 276, 188, "hold L2 to open the menu")


def fill_api(win, content, height):
    fill_fields(win, content, height, "api")
    caption(content, height, 74, 16, 448,
            "Anything on this Mac can read your settings, drive the "
            "keyboard and add screens of its own - see http://127.0.0.1:"
            f"{win.config.get('api', {}).get('port', 7420)}/v1")


def fill_backlight(win, content, height):
    fill_fields(win, content, height, "backlight")
    caption(content, height, 48, 16, 56, "M-keys")
    for index, name in enumerate(("m1", "m2", "m3", "mr")):
        button(win, content, height, 44, 80 + index * 62, 56,
               name.upper(), f"mkeys:{name}")
    button(win, content, height, 44, 334, 74, "All off", "mkeys:off")
    caption(content, height, 80, 16, 440,
            "One colour per app, as it comes up in front.")
    win.rows_view["appcolors"] = _scroll_rows(
        win, content, _y(height, 100, 78), 14, CARD_WIDTH - 28, 78,
        "appcolors")
    _add_row(win, content, height, 184, "appcolors")


def fill_gauges(win, content, height):
    caption(content, height, 10, 16, 440,
            "Up to three bars, each fed by a command that prints a number.")
    win.rows_view["gauges"] = _scroll_rows(
        win, content, _y(height, 32, 78), 14, CARD_WIDTH - 28, 78, "gauges")
    _add_row(win, content, height, 114, "gauges")


def fill_printer(win, content, height):
    fill_fields(win, content, height, "printer")
    caption(content, height, 108, 16, 440,
            "Enabled, address and the Test button are on the Display tab.")


# -- the rule lists --------------------------------------------------------
#
# Two of everything, told apart by config block: a table of the rules, and
# underneath it the selected rule - name, screen, and its conditions. The
# editor half is rebuilt whenever the rules or the selection change, which is
# why its controls are not registered for the ordinary refresh pass.

def fill_auto_rules(win, content, height):
    fill_rules(win, content, height, "autoscreen", "rules-auto")


def fill_trigger_rules(win, content, height):
    fill_rules(win, content, height, "triggers", "triggers")


def fill_rules(win, content, height, block, card):
    fill_fields(win, content, height, card)
    if block == "autoscreen":
        caption(content, height, 8, 16, 440,
                "First rule whose conditions hold wins; the order is the "
                "priority.")

    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(1, _y(height, 34, 92), CARD_WIDTH - 2, 92))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)
    table = make_table(win, ("Name", 130), ("Screen", 90), ("When", 228))
    scroll.setDocumentView_(table)
    content.addSubview_(scroll)
    win.rule_table[block] = table

    caption(content, height, 134, 16, 230, "Pick a row to edit it below")
    button(win, content, height, 130, 250, 106, "+ Rule", f"rule-add:{block}")
    button(win, content, height, 130, 362, 104, "- Rule", f"rule-del:{block}")

    body = NSView.alloc().initWithFrame_(
        NSMakeRect(0, _y(height, 158, 144), CARD_WIDTH, 144))
    content.addSubview_(body)
    win.rule_body[block] = body


def _rebuild_rule(win, block, rules, index):
    """The editor half of a rules card: the selected rule, then its `when`."""
    body = win.rule_body[block]
    clear(body)
    height = 144
    if not rules:
        caption(body, height, 8, 16, 420,
                "No rules yet - press + Rule to write one.")
        return
    rule = rules[index] if isinstance(rules[index], dict) else {}
    base = f"{block}/rules/{index}"
    field(win, body, height, 0, 16, 150, f"text:{base}/name",
          placeholder="Name", register=False)
    pick(win, body, height, 0, 172, 130, f"screen:{base}/screen",
         tuple(screens.SCREENS) + ("off",), "screen", register=False)
    if block == "triggers":
        fires = check(win, body, height, 2, 314, 96,
                      f"present:{base}/seconds", "Fires once",
                      register=False)
        fires.setState_(1 if "seconds" in rule else 0)
        length = field(win, body, height, 0, 416, 56, f"num:{base}/seconds",
                       register=False)
        length.setEnabled_(1 if "seconds" in rule else 0)

    when = rule.get("when") if isinstance(rule.get("when"), dict) else {}
    caption(body, height, 30, 16, 240, "Conditions - all of them must hold")
    absent = tuple(key for key in menu.CONDITIONS if key not in when)
    add = pick(win, body, height, 26, 270, 194, f"cond-add:{block}",
               ("",) + absent, placeholder="Add a condition…",
               register=False)
    add.setEnabled_(bool(absent))

    container = _scroll_rows(win, body, _y(height, 54, 90), 16, 436, 90,
                             f"conditions:{block}")
    keys = list(when)
    inner = resize(container, len(keys))
    if not keys:
        caption(container, inner, 6, 0, 420,
                "No conditions - this rule holds all the time.")
    for row, key in enumerate(keys):
        top = row * ROW_H
        path = f"{base}/when/{key}"
        if key in menu.BOOL_CONDITIONS:
            box = check(win, container, inner, top, 0, 300,
                        f"bool:{path}", key, register=False)
            box.setState_(1 if when[key] else 0)
        else:
            caption(container, inner, top + 4, 0, 100, key)
            kind = ("csv" if key in menu.LIST_CONDITIONS
                    else "boolish" if key in ("idle", "active") else "text")
            field(win, container, inner, top, 104, 296, f"{kind}:{path}",
                  placeholder=_help(key), register=False)
        button(win, container, inner, top, 410, 36, "-",
               f"when-del:{block}:{key}", register=False)


def _help(key):
    try:
        import autoscreen
        return autoscreen.CONDITION_HELP.get(key, "")
    except Exception:                     # pragma: no cover
        return ""


# -- the other lists -------------------------------------------------------
#
# Presets, gauges and app colours are rows of their own, rebuilt the same way
# the conditions are: one container, cleared and refilled when the data under
# it changes.

def _scroll_rows(win, parent, y, x, width, height, name):
    """A scrolling container, returned so a rebuild can fill it."""
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setDocumentView_(container)
    parent.addSubview_(scroll)
    return container


def _add_row(win, content, height, top, name):
    """The row under a list: what you type, and the button that adds it."""
    refs = {}
    if name == "presets":
        refs["first"] = field(win, content, height, top, 16, 160,
                              "preset-name:", placeholder="name",
                              register=False)
        refs["second"] = field(win, content, height, top, 184, 100,
                               "preset-len:", placeholder="25m",
                               register=False)
        button(win, content, height, top, 294, 44, "+", "preset-add:",
               register=False)
    elif name == "gauges":
        refs["first"] = field(win, content, height, top, 16, 110,
                              "gauge-label:", placeholder="Label",
                              register=False)
        refs["second"] = field(win, content, height, top, 134, 272,
                               "gauge-cmd:", placeholder="df -h /",
                               register=False)
        button(win, content, height, top, 414, 44, "+", "gauge-add:",
               register=False)
    else:
        refs["first"] = field(win, content, height, top, 16, 200,
                              "color-app:", placeholder="Application name",
                              register=False)
        refs["second"] = pick(win, content, height, top, 224, 130,
                              "color-pick:", _colours(), register=False)
        button(win, content, height, top, 364, 44, "+", "appcolor-add:",
               register=False)
    win.add_refs[name] = refs


def _colours():
    return tuple(sorted(config.NAMED_COLORS))


def _rebuild_rows(win, name, entries):
    """Fill a list's container with one row per entry."""
    container = win.rows_view[name]
    clear(container)
    inner = resize(container, len(entries))
    for row, entry in enumerate(entries):
        top = row * ROW_H
        if name == "presets":
            label, length = entry
            field(win, container, inner, top, 0, 160,
                  f"preset-name:{row}", register=False).setStringValue_(label)
            field(win, container, inner, top, 170, 100,
                  f"preset-len:{row}", register=False).setStringValue_(length)
            button(win, container, inner, top, 280, 36, "-",
                   f"preset-del:{row}", register=False)
        elif name == "gauges":
            field(win, container, inner, top, 0, 110,
                  f"gauge-label:{row}",
                  register=False).setStringValue_(str(entry.get("label", "")))
            field(win, container, inner, top, 120, 300,
                  f"gauge-cmd:{row}",
                  register=False).setStringValue_(str(entry.get("command", "")))
            button(win, container, inner, top, 410, 36, "-",
                   f"gauge-del:{row}", register=False)
        else:
            app, colour = entry
            field(win, container, inner, top, 0, 220,
                  f"color-name:{row}",
                  register=False).setStringValue_(app)
            pick(win, container, inner, top, 230, 130,
                 f"color-pick:{row}", _colours(),
                 register=False).selectItemWithTitle_(colour)
            button(win, container, inner, top, 370, 36, "-",
                   f"color-del:{row}", register=False)


def _rows_data(win, name):
    if name == "presets":
        block = win.config.get("timers") or {}
        return list((block.get("presets") or {}).items())
    if name == "gauges":
        return [entry for entry in (win.config.get("gauges") or [])
                if isinstance(entry, dict)]
    return list((win.config.get("app_colors") or {}).items())


# -- refresh ---------------------------------------------------------------

def refresh(win):
    for ident, control in win.s_ctrls.items():
        kind, _, path = ident.partition(":")
        spec = FIELDS.get(path)
        if not spec or spec["kind"] not in _SCALAR:
            continue
        _show(control, kind, spec, menu.get(win.config, path))
    refresh_rules(win)
    for name in ("presets", "gauges", "appcolors"):
        entries = _rows_data(win, name)
        if win.rows_sig.get(name) != repr(entries):
            win.rows_sig[name] = repr(entries)
            _rebuild_rows(win, name, entries)


def _show(control, kind, spec, value):
    if kind in ("bool", "iso"):
        control.setState_(1 if value else 0)
        return
    if kind in ("choice", "screen") or "options" in spec:
        title = _title_of(spec, value)
        if title:
            control.selectItemWithTitle_(title)
        return
    # A field somebody is typing into keeps what they typed.
    if control.currentEditor() is not None:
        return
    if kind == "num":
        control.setStringValue_(_number(value))
    elif kind in ("csv", "screens"):
        parts = value if isinstance(value, list) else []
        control.setStringValue_(", ".join(str(part) for part in parts))
    else:
        control.setStringValue_("" if value is None else str(value))


def _number(value):
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


def _title_of(spec, value):
    options = list(spec["options"]())
    labels = spec.get("labels") or (
        "screen" if spec["kind"] == "screen" else None)
    titles = _titles(options, labels, spec.get("placeholder"))
    try:
        return titles[options.index(value)]
    except ValueError:
        return None


def refresh_rules(win):
    for block, _card, _title in RULE_BLOCKS:
        table = win.rule_table[block]
        rules = _rules(win, block)
        index = win.rule_selected.get(block, 0)
        if index > len(rules) - 1:
            index = max(0, len(rules) - 1)
        win.rule_selected[block] = index
        signature = (repr(rules), index)
        if win.rules_sig.get(block) != signature:
            win.rules_sig[block] = signature
            _rebuild_rule(win, block, rules, index)
            if rules and table.selectedRow() != index:
                table.selectRowIndexes_byExtendingSelection_(
                    NSIndexSet.indexSetWithIndex_(index), False)
        table.reloadData()


def _rules(win, block):
    block_ = win.config.get(block) or {}
    rules = block_.get("rules")
    return rules if isinstance(rules, list) else []


def owns(table, win):
    return table in win.rule_table.values()


def selection_changed(win, table):
    """A row clicked in either rules table becomes the rule being edited."""
    block = next((name for name, value in win.rule_table.items()
                  if value is table), None)
    if block is None:
        return
    row = table.selectedRow()
    if row < 0 or row == win.rule_selected.get(block):
        return
    win.rule_selected[block] = row
    rules = _rules(win, block)
    signature = (repr(rules), row)
    if win.rules_sig.get(block) == signature:
        return
    win.rules_sig[block] = signature
    _rebuild_rule(win, block, rules, row)


def row_count(win, table):
    # AppKit asks the moment the table is attached, before anything is built.
    block = next((name for name, value in win.rule_table.items()
                  if value is table), None)
    return len(_rules(win, block)) if block else 0


def cell_value(win, table, column, row):
    block = next((name for name, value in win.rule_table.items()
                  if value is table), None)
    rules = _rules(win, block) if block else []
    if row >= len(rules):
        return ""
    rule = rules[row] if isinstance(rules[row], dict) else {}
    which = column.identifier()
    if which == "name":
        return str(rule.get("name") or f"rule {row + 1}")
    if which == "screen":
        name = str(rule.get("screen") or "")
        return SCREEN_LABELS.get(name, name.title()) if name else "—"
    line = cli._condition_line(rule)
    seconds = rule.get("seconds")
    if block == "triggers" and seconds:
        line = f"{line} · {int(seconds)}s"
    return line


# -- one selector for the whole pane ---------------------------------------

HANDLERS = {}


def _handles(kind):
    def register(function):
        HANDLERS[kind] = function
        return function
    return register


def changed(win, sender):
    """Turn one control into a change.

    Everything this pane builds carries `kind:path[:arg]`, so a checkbox, a
    number, a popup and a "+ Rule" button all reach the same dispatcher.
    The refresh is deferred to the next turn of the run loop rather than
    done here: a structural change rebuilds the very view the action came
    from, and removing a view while it is still delivering its action is
    asking AppKit for trouble.
    """
    ident = str(sender.identifier() or "")
    kind, _, rest = ident.partition(":")
    path, _, arg = rest.partition(":")
    function = HANDLERS.get(kind)
    if function is None:
        return
    try:
        function(win, sender, path, arg)
    except (ValueError, TypeError, config.ColorError, timers.DurationError,
            control.ControlError) as exc:
        win.complain(str(exc))
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.0, win, b"refreshSoon:", None, False)


def _picked(win, sender, ident):
    """The config value behind a popup, from the options it was built with."""
    options = win.s_options.get(ident) or ()
    index = int(sender.indexOfSelectedItem())
    if index < 0 or index >= len(options):
        return None
    return options[index]


def _set(win, path, value):
    win.mutate(lambda settings: menu.put(settings, path, value))


@_handles("bool")
def _bool(win, sender, path, _arg):
    _set(win, path, bool(sender.state()))


@_handles("iso")
def _iso(win, sender, path, _arg):
    """`iso_keyboard` is a config value and a change to the keyboard."""
    wanted = bool(sender.state())
    _set(win, path, wanted)
    control.iso_swap(wanted)


@_handles("num")
def _num(win, sender, path, _arg):
    raw = str(sender.stringValue()).strip()
    if not raw:
        raise ValueError("a number is needed")
    value = float(raw)
    low, high = LIMITS.get(path.rsplit("/", 1)[-1], (None, None))
    if low is not None and value < low:
        raise ValueError(f"at least {low:g}")
    if high is not None and value > high:
        raise ValueError(f"no more than {high:g}")
    _set(win, path, int(value) if float(value).is_integer() and "." not in raw
         else value)


@_handles("text")
def _text(win, sender, path, _arg):
    _set(win, path, str(sender.stringValue()).strip())


@_handles("csv")
def _csv(win, sender, path, _arg):
    raw = str(sender.stringValue())
    _set(win, path, [part.strip() for part in raw.split(",") if part.strip()])


@_handles("screens")
def _screens(win, sender, path, _arg):
    wanted = [part.strip() for part in str(sender.stringValue()).split(",")
              if part.strip()]
    unknown = [name for name in wanted
               if name != "off" and name not in screens.SCREENS]
    if unknown:
        raise ValueError(f"no screen called {unknown[0]!r} - "
                         + ", ".join(screens.SCREENS))
    _set(win, path, wanted)


@_handles("choice")
@_handles("screen")
def _choice(win, sender, path, _arg):
    value = _picked(win, sender, str(sender.identifier() or ""))
    if value is not None:
        _set(win, path, value)


@_handles("boolish")
def _boolish(win, sender, path, _arg):
    """`idle` and `active` take true/false or a number of seconds."""
    raw = str(sender.stringValue()).strip()
    low = raw.lower()
    if low in ("true", "yes", "on"):
        value = True
    elif low in ("false", "no", "off"):
        value = False
    elif raw:
        value = float(raw)
    else:
        value = ""
    _set(win, path, value)


@_handles("act")
def _poweron(win, sender, _path, _arg):
    """The power-on colour lives on the keyboard, not in the config."""
    colour = _picked(win, sender, str(sender.identifier() or ""))
    sender.selectItemAtIndex_(0)
    if colour:
        control.set_power_on(config.parse_color(colour))


@_handles("mkeys")
def _mkeys(win, _sender, path, _arg):
    control.set_mkeys([] if path == "off" else [path])


@_handles("present")
def _present(win, sender, path, _arg):
    """"Fires once": the field exists or it does not, rather than being 0."""
    if sender.state():
        _set(win, path, 10)
        return
    win.mutate(lambda settings: menu.drop(settings, path))


@_handles("cond-add")
def _cond_add(win, sender, block, _arg):
    key = _picked(win, sender, str(sender.identifier() or ""))
    sender.selectItemAtIndex_(0)
    if not key:
        return
    dotted = f"{block}/rules/{win.rule_selected[block]}/when/{key}"
    win.mutate(lambda settings: menu.add_when(settings, dotted))


@_handles("when-del")
def _when_del(win, _sender, block, key):
    dotted = f"{block}/rules/{win.rule_selected[block]}/when/{key}"
    win.mutate(lambda settings: menu.del_when(settings, dotted))


@_handles("rule-add")
def _rule_add(win, _sender, block, _arg):
    path = f"{block}/rules"

    def apply(settings):
        menu.add_rule(settings, path)
    win.mutate(apply)
    win.rule_selected[block] = len(_rules(win, block)) - 1


@_handles("rule-del")
def _rule_del(win, _sender, block, _arg):
    index = win.rule_selected[block]
    win.mutate(lambda settings: menu.drop(settings, f"{block}/rules/{index}"))
    win.rule_selected[block] = max(0, index - 1)


# -- the lists -------------------------------------------------------------
#
# Presets, gauges and app colours are edited row by row: the row's index is
# in its identifier, and every change goes through `mutate` like the rest.

def _preset_block(settings):
    timers_block = settings.setdefault("timers", {})
    return timers_block.setdefault("presets", {})


def _preset_names(win):
    return list((win.config.get("timers") or {}).get("presets") or {})


def _name_at(win, path):
    names = _preset_names(win)
    index = int(path)
    if index >= len(names):
        raise ValueError("that preset is already gone")
    return names[index]


@_handles("preset-name")
def _preset_name(win, sender, path, _arg):
    old = _name_at(win, path)
    new = str(sender.stringValue()).strip()
    if not new or new == old:
        return
    if new in _preset_names(win):
        raise ValueError(f"there is already a preset called {new!r}")

    def apply(settings):
        block = _preset_block(settings)
        items = list(block.items())
        items[int(path)] = (new, items[int(path)][1])
        block.clear()
        block.update(items)
    win.mutate(apply)


@_handles("preset-len")
def _preset_len(win, sender, path, _arg):
    raw = str(sender.stringValue()).strip()
    timers.parse_duration(raw)             # raises unless it is a length
    name = _name_at(win, path)
    win.mutate(lambda settings: _preset_block(settings).update({name: raw}))


@_handles("preset-del")
def _preset_del(win, _sender, path, _arg):
    name = _name_at(win, path)
    win.mutate(lambda settings: _preset_block(settings).pop(name, None))


@_handles("preset-add")
def _preset_add(win, _sender, _path, _arg):
    refs = win.add_refs["presets"]
    name = str(refs["first"].stringValue()).strip()
    length = str(refs["second"].stringValue()).strip()
    if not name:
        raise ValueError("give the preset a name")
    if name in _preset_names(win):
        raise ValueError(f"there is already a preset called {name!r}")
    timers.parse_duration(length)

    def apply(settings):
        _preset_block(settings)[name] = length
    win.mutate(apply)
    refs["first"].setStringValue_("")
    refs["second"].setStringValue_("")


def _gauge_list(settings):
    return [entry for entry in (settings.get("gauges") or [])
            if isinstance(entry, dict)]


def _write_gauge(settings, index, key, value):
    gauges = _gauge_list(settings)
    if index < len(gauges):
        gauges[index][key] = value


@_handles("gauge-label")
def _gauge_label(win, sender, path, _arg):
    label = str(sender.stringValue()).strip()
    win.mutate(lambda settings: _write_gauge(settings, int(path),
                                             "label", label))


@_handles("gauge-cmd")
def _gauge_cmd(win, sender, path, _arg):
    command = str(sender.stringValue()).strip()
    win.mutate(lambda settings: _write_gauge(settings, int(path),
                                             "command", command))


@_handles("gauge-del")
def _gauge_del(win, _sender, path, _arg):
    def apply(settings):
        gauges = _gauge_list(settings)
        index = int(path)
        if index < len(gauges):
            settings["gauges"].remove(gauges[index])
    win.mutate(apply)


@_handles("gauge-add")
def _gauge_add(win, _sender, _path, _arg):
    refs = win.add_refs["gauges"]
    label = str(refs["first"].stringValue()).strip()
    command = str(refs["second"].stringValue()).strip()
    if not label or not command:
        raise ValueError("a gauge needs a label and a command")

    def apply(settings):
        gauges = settings.setdefault("gauges", [])
        if len(gauges) >= 3:
            raise ValueError("the display only has room for three gauges")
        gauges.append({"label": label, "command": command})
    win.mutate(apply)
    refs["first"].setStringValue_("")
    refs["second"].setStringValue_("")


def _colour_at(win, index):
    colours = list((win.config.get("app_colors") or {}).items())
    if index >= len(colours):
        raise ValueError("that colour is already gone")
    return colours[index][0]


@_handles("color-name")
def _colour_name(win, sender, path, _arg):
    old = _colour_at(win, path)
    new = str(sender.stringValue()).strip()
    if not new or new == old:
        return
    if new in (win.config.get("app_colors") or {}):
        raise ValueError(f"{new} already has a colour")

    def apply(settings):
        colours = settings.setdefault("app_colors", {})
        items = list(colours.items())
        items[int(path)] = (new, items[int(path)][1])
        colours.clear()
        colours.update(items)
    win.mutate(apply)


@_handles("color-pick")
def _colour_pick(win, sender, path, _arg):
    app = _colour_at(win, path)
    colour = _picked(win, sender, str(sender.identifier() or ""))
    if colour:
        win.mutate(lambda settings: settings.setdefault("app_colors", {})
                   .update({app: colour}))


@_handles("color-del")
def _colour_del(win, _sender, path, _arg):
    app = _colour_at(win, path)
    win.mutate(lambda settings: settings.setdefault("app_colors", {})
               .pop(app, None))


@_handles("appcolor-add")
def _colour_add(win, _sender, _path, _arg):
    refs = win.add_refs["appcolors"]
    app = str(refs["first"].stringValue()).strip()
    colour = _picked(win, refs["second"], str(refs["second"].identifier() or ""))
    if not app or not colour:
        raise ValueError("give the application and a colour")
    config.parse_color(colour)

    def apply(settings):
        settings.setdefault("app_colors", {})[app] = colour
    win.mutate(apply)
    refs["first"].setStringValue_("")
