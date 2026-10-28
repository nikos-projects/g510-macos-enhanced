"""The menu on the LCD: every setting, without ever leaving your app.

The display has three text rows, so the menu is a tree you walk rather than
a page you read - a breadcrumb, the item you are on, and what comes after it.
Nothing in here draws: `view()` returns plain text and a small renderer in
`screens.py` puts it on the LCD, so the whole menu can be exercised without
a keyboard, a display or an agent.

An entry is a dict:

    {"label": "Display", "kind": "sub",   "children": [...]}
    {"label": "On",      "kind": "bool",  "path": "lcd/enabled"}
    {"label": "Screen",  "kind": "choice", "path": "lcd/screen",
     "options": ("status", "clock")}
    {"label": "Refresh", "kind": "num",   "path": "lcd/refresh_seconds",
     "step": 0.5, "min": 0.1, "max": 60}
    {"label": "Font",    "kind": "text",  "path": "lcd/font"}
    {"label": "Next",    "kind": "action", "action": "next"}
    {"label": "no rules", "kind": "info"}

Paths use "/" rather than "." because a config key can be a bundle id
("app_colors/com.apple.Terminal") and a path has to survive that.

`children` may be a callable taking (host, entry), so a page shows what the
config holds today - trigger names, the rules, the gauges - instead of a
fixed set. Values are read out of `host.settings` when they are drawn and
never cached, so a config that changed underneath the menu is simply what
the menu shows.

Two kinds of entry write somewhere other than their own path: an `action`
sends (name, arg) to the host - starting a timer, firing a trigger, picking
a power-on colour - and a choice with an `action` but also a `path` shows
the path and acts on commit (switching banks has to repoint `bindings`, so
writing `active_bank` alone would be a lie).
"""
import os

import alerts
import config
import screens
import timers

# Three text rows: one for the breadcrumb, two for whatever is going on.
MAX_ROWS = 3

# The separator inside menu paths. "." is a legal character in a bundle id,
# which is exactly the sort of key `app_colors` holds.
SEP = "/"

# Conditions a rule can carry. Kept as data rather than imported so that
# menu.py stays importable on its own; tests/test_menu.py asserts this list
# is exactly autoscreen.CONDITIONS, so the two cannot drift apart.
CONDITIONS = ("game", "playing", "idle", "active", "app", "not_app",
              "running", "not_running", "fullscreen", "command")

# Conditions whose value is a yes/no rather than something you type.
BOOL_CONDITIONS = ("game", "playing", "fullscreen")
# Conditions whose value is a list of names rather than one word.
LIST_CONDITIONS = ("app", "not_app", "running", "not_running")

# The lock LEDs macOS leaves dark. Same list cli.INDICATORS (asserted).
INDICATORS = ("off", "printing", "claude", "recording")

# What a G-key's `type` can be, and the field each one stores its value in.
BINDING_TYPES = ("none", "app", "keys", "shell", "text", "timer")
VALUE_FIELDS = {"app": "name", "keys": "keys", "shell": "command",
                "text": "text", "timer": "seconds"}
VALUE_LABELS = {"app": "App", "keys": "Keys", "shell": "Command",
                "text": "Text", "timer": "Length"}
VALUE_PARSE = {"keys": "chord", "timer": "duration"}

# What an optional field gets when you ask for it from the menu.
DEFAULT_FIELDS = {"seconds": 10}

# The hint under the item you are on, when there is no next item to show.
_HINTS = {"sub": "enter: open", "bool": "enter: toggle",
          "choice": "enter: change", "num": "enter: adjust",
          "text": "enter: type", "action": "enter: run", "info": "esc: back"}

_EDIT_HINTS = {"choice": "up/down: choose   enter: ok",
               "num": "up/down: change   enter: ok",
               "text": "type it, enter: ok, esc: cancel"}


# -- config paths ---------------------------------------------------------

def get(settings, path, default=None):
    """Read a "/"-separated path out of the config."""
    node = settings
    for part in path.split(SEP):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return default
        if node is None:
            return default
    return node


def put(settings, path, value):
    """Write a "/"-separated path, making the dicts it passes through.

    A path may run through a list - "autoscreen/rules/0/name" - which is how
    the menu reaches one rule without giving it an identity of its own.
    """
    parts = path.split(SEP)
    node = settings
    for part in parts[:-1]:
        if isinstance(node, list):
            if not (part.isdigit() and int(part) < len(node)):
                raise IndexError(f"no item {part} in {path}")
            node = node[int(part)]
            continue
        child = node.get(part)
        if isinstance(child, (dict, list)):
            node = child
            continue
        child = {}
        node[part] = child
        node = child
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value
    return settings


def drop(settings, path):
    """Remove the last key of a path. Used when a value means "unset"."""
    parts = path.split(SEP)
    node = settings
    for part in parts[:-1]:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return False
        if not isinstance(node, (dict, list)):
            return False
    last = parts[-1]
    if isinstance(node, list):
        if not (last.isdigit() and int(last) < len(node)):
            return False
        del node[int(last)]
        return True
    if last in node:
        del node[last]
        return True
    return False


# -- helpers the agent calls when an action names a config edit -----------

def add_field(settings, path):
    """Give an optional field (a trigger's `seconds`) its value."""
    key = path.split(SEP)[-1]
    put(settings, path, DEFAULT_FIELDS.get(key, ""))
    return path


def del_field(settings, path):
    """Take an optional field away again, back to "not set"."""
    return drop(settings, path)


def add_rule(settings, path):
    """Append a rule with nothing but a name and a screen, ready to fill in.

    The conditions are what make a rule mean anything, and they are the
    part a person has to think about, so a new rule is deliberately empty:
    it goes at the end, where it can do no harm, and the menu walks you
    straight into its Conditions page.
    """
    rules = get(settings, path)
    if not isinstance(rules, list):
        rules = []
        put(settings, path, rules)
    rules.append({"name": f"rule {len(rules) + 1}", "screen": "status",
                  "when": {}})
    return len(rules) - 1


def add_when(settings, dotted):
    """Add a condition to a rule's `when`, with a sensible first value."""
    key = dotted.split(SEP)[-1]
    when = get(settings, dotted.rsplit(SEP, 1)[0])
    if not isinstance(when, dict):
        when = {}
        put(settings, dotted.rsplit(SEP, 1)[0], when)
    when[key] = "" if key not in BOOL_CONDITIONS else False
    return when[key]


def del_when(settings, dotted):
    """Remove one condition from a rule's `when`."""
    parent, key = dotted.rsplit(SEP, 1)
    when = get(settings, parent)
    if isinstance(when, dict) and key in when:
        del when[key]
        return True
    return False


# -- entry constructors ---------------------------------------------------

def _label_of(path):
    return path.split(SEP)[-1].replace("_", " ").capitalize()


def _sub(label, children):
    return {"label": label, "kind": "sub", "children": children}


def _bool(path, label=None, default=False):
    return {"label": label or _label_of(path), "kind": "bool", "path": path,
            "default": default}


def _choice(path, label, options, action=None, omit=None, default=None,
            unset=None):
    entry = {"label": label, "kind": "choice", "path": path,
             "options": tuple(options), "default": default}
    if action:
        entry["action"] = action
    if omit is not None:
        entry["omit"] = omit
        entry["unset"] = unset or path
    return entry


def _num(path, label, step=1, lo=0, hi=None, default=0):
    return {"label": label, "kind": "num", "path": path, "step": step,
            "min": lo, "max": hi, "default": default}


def _text(path, label=None, parse=None, default="", action=None):
    entry = {"label": label or (_label_of(path) if path else ""),
             "kind": "text", "path": path, "parse": parse,
             "default": default}
    if action:
        entry["action"] = action
    return entry


def _act(label, action, arg=None):
    return {"label": label, "kind": "action", "action": action, "arg": arg}


def _info(label):
    return {"label": label, "kind": "info"}


# -- option lists ---------------------------------------------------------

def _screens(with_off=False):
    names = tuple(screens.SCREENS)
    return names + ("off",) if with_off else names


def _colors():
    return tuple(sorted(config.NAMED_COLORS))


def _sounds():
    try:
        names = sorted(name[:-len(".aiff")]
                       for name in os.listdir(alerts.SOUND_DIR)
                       if name.endswith(".aiff"))
    except OSError:
        names = ["Glass"]
    return tuple(names) or ("Glass",)


def _lcd_keys():
    return ("next", "prev", "off") + tuple(
        f"screen:{name}" for name in screens.SCREENS)


def _switch_options():
    return ("none", "bank:1", "bank:2", "bank:3") + tuple(
        f"screen:{name}" for name in screens.SCREENS) + tuple(
        f"color:{name}" for name in _colors())


def _presets(settings):
    block = (settings.get("timers") or {}).get("presets") or {}
    return [str(name) for name in block]


# -- dynamic pages --------------------------------------------------------

def _gauges(host, _entry):
    gauges = get(host.settings, "gauges") or []
    rows = [_info(f"{entry.get('label', '?')}: {entry.get('command', '')}")
            for entry in gauges if isinstance(entry, dict)]
    if gauges:
        rows.append(_act("Clear every gauge", "gauge_clear"))
    rows.append(_text(None, "Add a gauge", action="gauge_add"))
    return rows


def _presets_page(host, _entry):
    return [_text(f"timers/presets/{name}", name, parse="duration")
            for name in _presets(host.settings)]


def _start_page(host, _entry):
    return [_act(name, "timer_start", name)
            for name in _presets(host.settings)] or [_info("no presets")]


def _fire_page(host, _entry):
    rules = get(host.settings, "triggers/rules") or []
    rows = [_act(str(rule.get("name") or f"rule {index + 1}"),
                 "trigger_fire", rule.get("name") or str(index))
            for index, rule in enumerate(rules) if isinstance(rule, dict)]
    return rows or [_info("no triggers - g510 trigger add")]


def _rules_page(path, trigger):
    def children(host, _entry):
        rules = get(host.settings, path) or []
        rows = [_rule(path + SEP + str(index), index, trigger)
                for index in range(len(rules) if isinstance(rules, list) else 0)]
        rows.append(_act("Add a rule", "rule_add", path))
        return rows
    return children


def _rule(path, index, trigger):
    def children(host, _entry):
        rule = get(host.settings, path)
        if not isinstance(rule, dict):
            rule = {}
        rows = [_text(path + SEP + "name", "Name"),
                _choice(path + SEP + "screen", "Screen",
                        _screens(with_off=True))]
        if trigger:
            if "seconds" in rule:
                rows.append(_num(path + SEP + "seconds", "Length s",
                                 step=1, lo=1, hi=86400))
                rows.append(_act("State rule (no length)", "field_del",
                                 path + SEP + "seconds"))
            else:
                rows.append(_act("Give it a length (10s)", "field_add",
                                 path + SEP + "seconds"))
        rows.append(_sub("Conditions", _when_page(path + SEP + "when")))
        rows.append(_act("Delete rule", "rule_del", path))
        return rows
    return _sub(f"Rule {index + 1}", children)


def _when_page(path):
    def children(host, _entry):
        when = get(host.settings, path)
        if not isinstance(when, dict):
            when = {}
        rows = [_condition(path, key) for key in when]
        absent = [key for key in CONDITIONS if key not in when]
        if absent:
            rows.append(_sub("Add a condition",
                             [_act(key, "when_add", path + SEP + key)
                              for key in absent]))
        if when:
            rows.append(_sub("Remove one",
                             [_act(key, "when_del", path + SEP + key)
                              for key in when]))
        if not when:
            rows.insert(0, _info("no conditions - holds all the time"))
        return rows
    return children


def _condition(path, key):
    full = path + SEP + key
    if key in BOOL_CONDITIONS:
        return _bool(full, key)
    parse = "csv" if key in LIST_CONDITIONS else None
    return _text(full, key, parse=parse)


def _binding_page(path):
    def children(host, _entry):
        binding = get(host.settings, path)
        if not isinstance(binding, dict):
            binding = {}
        kind = str(binding.get("type") or "none")
        # Choosing "none" means the key is not bound at all, so it is the
        # whole binding that goes - which is what `g510 bind G1 none` does.
        rows = [_choice(path + SEP + "type", "Action", BINDING_TYPES,
                        omit="none", default="none", unset=path)]
        if kind in VALUE_LABELS:
            rows.append(_text(path + SEP + VALUE_FIELDS[kind],
                              VALUE_LABELS[kind],
                              parse=VALUE_PARSE.get(kind)))
        else:
            rows.append(_info("not bound to anything"))
        return rows
    return _sub(_label_of(path), children)


def _gkeys(_host, _entry):
    import device
    return [_binding_page(f"bindings{SEP}G{index}")
            for index in range(1, device.GKEY_COUNT + 1)]


def _app_colors(host, _entry):
    colours = get(host.settings, "app_colors") or {}
    rows = [_choice(f"app_colors{SEP}{name}", name, _colors())
            for name in list(colours)]
    rows.append(_text(None, "Add (app, colour)", action="app_color_add"))
    if not rows:
        rows.insert(0, _info("one colour per app, as it comes up"))
    return rows


# -- the tree -------------------------------------------------------------

def build():
    """The whole menu, built fresh so names the config holds today are in it."""
    return [
        _sub("Screen", [
            _choice("lcd/screen", "Screen", _screens()),
            _bool("lcd/enabled", "Display"),
            _num("lcd/refresh_seconds", "Refresh s", step=0.5, lo=0.1, hi=60),
            _text("lcd/cycle", "Cycle",
                  parse="list:" + "|".join(_screens(with_off=True))),
            _sub("Gauges", _gauges),
            _act("Next screen", "next"),
        ]),

        _sub("Timers", [
            _bool("timers/enabled", "Timers"),
            _sub("Start", _start_page),
            _num("timers/show_seconds", "Show s", step=1, lo=0, hi=300),
            _num("timers/ack_seconds", "Ack s", step=0.5, lo=0, hi=60),
            _choice("timers/sound", "Sound", _sounds()),
            _sub("Presets", _presets_page),
        ]),

        _sub("Triggers", [
            _bool("triggers/enabled", "Triggers"),
            _num("triggers/check_seconds", "Check s", step=0.5, lo=0.25, hi=60),
            _sub("Fire now", _fire_page),
            _sub("Rules", _rules_page(f"triggers{SEP}rules", True)),
        ]),

        _sub("Bindings", [
            _choice("active_bank", "Bank", config.BANKS, action="bank"),
            _sub("G-keys", _gkeys),
        ]),

        _sub("Settings", [
            _sub("Display", [
                _text("lcd/font", "Font"),
                _bool("restore_backlight_on_start", "Restore on start"),
                _choice("indicators/numlock", "Num lock", INDICATORS),
                _choice("indicators/scrolllock", "Scroll lock", INDICATORS),
                _sub("Keys by the screen",
                     [_choice(f"lcd_keys{SEP}{name}", name, _lcd_keys())
                      for name in ("L1", "L2", "L3", "L4", "L5")]),
            ]),

            _sub("Backlight", [
                _num("brightness", "Brightness", step=5, lo=0, hi=100),
                _choice("backlight", "Colour", _colors()),
                _bool("beat/enabled", "Beat pulse"),
                _num("beat/amount", "Dip %", step=5, lo=0, hi=100),
                _num("beat/decay", "Back in s", step=0.05, lo=0.05, hi=5),
                _num("beat/sensitivity", "Sens", step=5, lo=0, hi=100),
                _sub("Power-on colour",
                     [_act(name, "poweron", name) for name in _colors()]),
                _sub("M-keys",
                     [_act(name.upper(), "mkeys", name)
                      for name in ("m1", "m2", "m3", "mr")]
                     + [_act("All off", "mkeys", "off")]),
                _sub("App colours", _app_colors),
            ]),

            _sub("Auto switch", [
                _bool("autoscreen/enabled", "Auto switch"),
                _num("autoscreen/check_seconds", "Check s",
                     step=0.5, lo=0.25, hi=60),
                _num("autoscreen/idle_seconds", "Idle s",
                     step=5, lo=0, hi=86400),
                _num("autoscreen/hold_seconds", "Hold s",
                     step=0.5, lo=0, hi=60),
                _num("autoscreen/manual_seconds", "Manual s",
                     step=30, lo=0, hi=86400),
                _choice("autoscreen/fallback", "Fallback",
                        ("",) + _screens(with_off=True)),
                _text("autoscreen/games", "Games", parse="csv"),
                _text("autoscreen/not_games", "Not games", parse="csv"),
                _sub("Rules", _rules_page(f"autoscreen{SEP}rules", False)),
            ]),

            _sub("Chat", [
                _bool("notifications/enabled", "Chat reader"),
                _bool("notifications/banners", "Banners"),
                _num("notifications/poll_seconds", "Poll s",
                     step=0.5, lo=0.5, hi=60),
                _num("notifications/show_seconds", "Show s",
                     step=1, lo=0, hi=300),
                _num("notifications/banners_seconds", "Banner s",
                     step=1, lo=0, hi=300),
                _choice("notifications/sound", "Sound", _sounds()),
                _text("notifications/apps", "Apps", parse="csv"),
                _bool("vencord/enabled", "Discord screen"),
            ]),

            _sub("Keyboard", [
                _bool("iso_keyboard", "ISO key swap"),
                _choice("game_switch/on", "Switch engaged", _switch_options(),
                        omit="none"),
                _choice("game_switch/off", "Switch released",
                        _switch_options(), omit="none"),
            ]),

            _sub("Claude", [
                _text("claude/rows", "Rows",
                      parse="list:five_hour|seven_day|context"),
                _num("claude/stale_after_seconds", "Stale s",
                     step=60, lo=0, hi=86400),
            ]),

            _sub("Printer", [
                _bool("printer/enabled", "Printer"),
                _text("printer/host", "Host"),
                _num("printer/port", "Port", step=1, lo=1, hi=65535),
                _num("printer/refresh_seconds", "Refresh s",
                     step=1, lo=1, hi=600),
                _choice("printer/kind", "Backend", _backends()),
                _text("printer/api_key", "API key"),
            ]),

            _sub("Agent", [
                _act("Reload the config", "reload"),
                _act("Restore factory backlight", "restore_backlight"),
                _bool("api/enabled", "App API"),
                _num("api/port", "API port", step=1, lo=1, hi=65535),
                _info("API: http://127.0.0.1:<port>/v1"),
                _info("config: ~/.config/g510/config.json"),
            ]),
        ]),

        _act("Leave", "leave"),
    ]


def _backends():
    try:
        from printer import BACKENDS
        return BACKENDS
    except Exception:                     # pragma: no cover - printer is stdlib-free
        return ("auto", "creality", "moonraker", "octoprint", "prusalink")


# -- the host -------------------------------------------------------------

class Host:
    """What the menu needs from the agent: the config, and two ways out.

    `settings` is where the menu reads from - the agent's live config.
    `save(apply)` is how a change gets written: it is handed the change
    itself, a function taking a freshly read config, because the CLI and
    the GUI write the same file and a menu that saved its own copy would
    roll their work back. `act` is everything that is not a config value -
    starting a timer, firing a trigger, choosing a power-on colour - and
    returns either None or a short string, which the menu shows on the row
    under the item.
    """

    def __init__(self, settings=None, save=None, act=None):
        self.settings = settings if settings is not None else {}
        self._save = save
        self._act = act

    def save(self, apply=None):
        if self._save:
            return self._save(apply)

    def act(self, name, arg=None):
        if self._act:
            return self._act(name, arg)


# -- the menu -------------------------------------------------------------

class Menu:
    """The walkable tree. `press` takes a key name and `view` says what to draw.

    Key names are "up", "down", "left", "right", "enter", "esc",
    "backspace", or a single printable character (which only means anything
    while a text field is being edited). Anything else is ignored.

    `press` returns "redraw" when the display needs a new frame, "closed"
    when the menu went away, and "ignored" when the key was none of its
    business - which is how the event tap knows to let a key through to the
    app underneath.
    """

    def __init__(self, host=None):
        self.host = host if host is not None else Host()
        self.closed = True
        self.stack = []          # [(entry, selected index), ...]
        self.sel = 0
        self.edit = None         # the entry being edited, or None
        self.message = ""        # one line of feedback, cleared by the next key

    # -- state ----------------------------------------------------------

    def open(self):
        self.stack = [(_sub("", build()), 0)]
        self.sel = 0
        self.edit = None
        self.message = ""
        self.closed = False
        return "redraw"

    def close(self):
        if self.closed:
            return "ignored"
        self.closed = True
        self.edit = None
        self.stack = []
        self.sel = 0
        return "closed"

    @property
    def is_open(self):
        return not self.closed

    def _entries(self):
        if not self.stack:
            return []
        children = self.stack[-1][0].get("children")
        if callable(children):
            children = children(self.host, self.stack[-1][0])
        return list(children or [])

    def _save_sel(self):
        entry, _ = self.stack[-1]
        self.stack[-1] = (entry, self.sel)

    def _push(self, entry):
        self._save_sel()
        self.stack.append((entry, 0))
        self.sel = 0

    def _pop(self):
        if len(self.stack) <= 1:
            return False
        self.stack.pop()
        self.sel = self.stack[-1][1]
        return True

    # -- keys -----------------------------------------------------------

    def press(self, name):
        if self.closed:
            return "ignored"
        if self.edit:
            if name == "esc":
                self.edit = None
                self.message = ""
                return "redraw"
            return self._press_edit(name)
        self.message = ""
        if name in ("up", "down"):
            entries = self._entries()
            if entries:
                step = -1 if name == "up" else 1
                self.sel = (self.sel + step) % len(entries)
            return "redraw"
        if name == "left":
            return self._back()
        if name in ("right", "enter"):
            return self._select()
        if name == "esc":
            # Esc walks back out, and closes the menu at the top level.
            if not self._pop():
                return self.close()
            self.message = ""
            return "redraw"
        return "ignored"

    def _back(self):
        if not self._pop():
            return "ignored"
        self.message = ""
        return "redraw"

    def _select(self):
        entries = self._entries()
        if not entries or not 0 <= self.sel < len(entries):
            return "ignored"
        entry = entries[self.sel]
        kind = entry.get("kind")
        if kind == "sub":
            self._push(entry)
            return "redraw"
        if kind == "action":
            if entry.get("action") == "leave":
                self.host.act("leave", entry.get("arg"))
                return self.close()
            result = self.host.act(entry.get("action"), entry.get("arg"))
            self.message = result if isinstance(result, str) else ""
            return "redraw"
        if kind == "bool":
            # The row itself says what it became, so there is nothing to
            # add underneath it - that line belongs to the actions.
            self._apply(entry, not bool(self._value(entry, False)))
            return "redraw"
        if kind in ("choice", "num", "text"):
            self._begin(entry)
            return "redraw"
        return "ignored"

    def _press_edit(self, name):
        edit = self.edit
        kind = edit["kind"]
        if kind == "choice":
            options = edit["options"]
            if not options:
                return "ignored"
            # Down the list is a bigger index, the way it is drawn.
            if name in ("up", "left"):
                edit["index"] = (edit["index"] - 1) % len(options)
            elif name in ("down", "right"):
                edit["index"] = (edit["index"] + 1) % len(options)
            elif name == "enter":
                return self._commit()
            else:
                return "ignored"
            edit["value"] = options[edit["index"]]
            return "redraw"
        if kind == "num":
            if name in ("up", "right"):
                delta = edit["step"]
            elif name in ("down", "left"):
                delta = -edit["step"]
            elif name == "enter":
                return self._commit()
            else:
                return "ignored"
            value = round(edit["value"] + delta, 4)
            if edit["min"] is not None:
                value = max(edit["min"], value)
            if edit["max"] is not None:
                value = min(edit["max"], value)
            edit["value"] = int(round(value)) if edit["is_int"] else float(value)
            return "redraw"
        # text
        if name == "enter":
            return self._commit()
        if name == "backspace":
            edit["value"] = edit["value"][:-1]
            return "redraw"
        if len(name) == 1 and name.isprintable():
            edit["value"] += name
            return "redraw"
        return "ignored"

    # -- editing --------------------------------------------------------

    def _begin(self, entry):
        kind = entry["kind"]
        label = entry.get("label") or ""
        if kind == "choice":
            options = tuple(entry.get("options") or ())
            current = self._value(entry)
            if current is not None and current not in options:
                # The config holds something the menu has no name for - a
                # hand-written colour, say. Show it as it is rather than
                # pretending the first option is what is set.
                options = (current,) + options
            index = options.index(current) if current in options else 0
            self.edit = {"entry": entry, "kind": "choice", "label": label,
                         "options": options, "index": index,
                         "value": options[index] if options else ""}
            return
        if kind == "num":
            value = self._value(entry, entry.get("default", 0))
            if value is None or isinstance(value, bool):
                value = entry.get("default", 0) or 0
            is_int = isinstance(value, int) and not isinstance(value, bool)
            self.edit = {"entry": entry, "kind": "num", "label": label,
                         "value": float(value), "is_int": is_int,
                         "step": float(entry.get("step", 1) or 1),
                         "min": entry.get("min"), "max": entry.get("max")}
            return
        value = self._value(entry, entry.get("default", ""))
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        self.edit = {"entry": entry, "kind": "text", "label": label,
                     "value": "" if value is None else str(value)}

    def _commit(self):
        edit = self.edit
        entry = edit["entry"]
        try:
            value = self._parse(entry, edit["value"])
        except Exception as exc:            # a bad value is a message, not a crash
            self.message = str(exc) or type(exc).__name__
            return "redraw"
        if entry.get("action"):
            result = self.host.act(entry["action"], value)
            self.edit = None
            self.message = result if isinstance(result, str) else ""
            return "redraw"
        self._apply(entry, value)
        self.edit = None
        return "redraw"

    def _parse(self, entry, raw):
        kind = entry.get("parse")
        if not kind:
            return raw
        if kind == "csv":
            return [part.strip() for part in str(raw).split(",")
                    if part.strip()]
        if kind.startswith("list:"):
            allowed = [part for part in kind[len("list:"):].split("|")]
            parts = [part.strip() for part in str(raw).split(",")
                     if part.strip()]
            bad = [part for part in parts if part not in allowed]
            if not parts:
                raise ValueError("give at least one")
            if bad:
                raise ValueError(f"not one of: {', '.join(allowed[:5])}")
            return parts
        if kind == "chord":
            import actions
            actions.parse_chord(str(raw))   # raises with a plain message
            return str(raw)
        if kind == "duration":
            timers.parse_duration(str(raw))
            return str(raw)
        return raw

    def _apply(self, entry, value):
        """Hand the change to the host, as a change rather than a result.

        The menu never writes the config file itself: it says what should
        be different, and the host applies that to a freshly read copy, so
        a change the CLI or GUI made while the menu was open survives.
        """
        path = entry.get("path")
        if not path:
            return
        omit = entry.get("omit")
        if omit is not None and value == omit:
            target = entry.get("unset", path)
            self.host.save(lambda fresh: drop(fresh, target))
        else:
            self.host.save(lambda fresh: put(fresh, path, value))

    def _value(self, entry, default=None):
        path = entry.get("path")
        if not path:
            return default
        found = get(self.host.settings, path, _MISSING)
        if found is _MISSING:
            return default if default is not None else entry.get("default")
        return found

    # -- drawing --------------------------------------------------------

    def view(self):
        """{closed, title, main, sub, editing} - the whole frame in words."""
        if self.closed:
            return {"closed": True, "title": "", "main": [], "sub": None,
                    "editing": None}
        entries = self._entries()
        if entries:
            self.sel = max(0, min(self.sel, len(entries) - 1))
        entry = entries[self.sel] if entries else None
        title = self._crumb()
        if self.edit:
            return {"closed": False, "title": title,
                    "main": [self._edit_row()],
                    "sub": self.message or _EDIT_HINTS.get(self.edit["kind"], ""),
                    "editing": dict(self.edit)}
        if not entries:
            main, sub = ["(nothing here)"], "esc: back"
        elif self.message:
            main, sub = [self._row(entry, True)], self.message
        else:
            main = [self._row(entry, True)]
            following = (entries[self.sel + 1]
                         if self.sel + 1 < len(entries) else None)
            if following is not None:
                main.append(self._row(following, False))
                sub = None
            else:
                sub = _HINTS.get(entry.get("kind"), "") or "esc: back"
        if len(main) > MAX_ROWS - 1:
            main = main[:MAX_ROWS - 1]
        if sub and len(main) >= MAX_ROWS - 1:
            sub = None
        return {"closed": False, "title": title, "main": main, "sub": sub,
                "editing": None}

    def _crumb(self):
        parts = [str(entry.get("label") or "")
                 for entry, _ in self.stack[1:] if entry.get("label")]
        return " > ".join(parts) or "Menu"

    def _row(self, entry, selected):
        return (">" if selected else " ") + self._decorate(entry)

    def _decorate(self, entry):
        kind = entry.get("kind")
        label = str(entry.get("label") or "")
        if kind in ("info", "action"):
            return label
        if kind == "sub":
            return f"{label} >"
        value = self._value(entry, entry.get("default"))
        if value is None:
            return label
        if kind == "bool":
            shown = "on" if value else "off"
        else:
            shown = _show(value)
        sep = "" if label.endswith(":") else ": "
        return f"{label}{sep}{shown}"

    def _edit_row(self):
        edit = self.edit
        if edit["kind"] == "text":
            body = edit["value"] + "|"
        else:
            body = _show(edit["value"])
        return f"{edit['label']}: {body}"


class _Missing:
    def __repr__(self):
        return "<unset>"


_MISSING = _Missing()


def _show(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
