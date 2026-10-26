"""Command line front end for the G510 tools."""
import collections
import os
import subprocess
import sys
import time

import actions
import alerts
import autoscreen
import config
import control
import device
import ipc
import link
import recorder
import screens
import timers

LABEL = "com.g510.agent"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def bundled():
    """Whether this is running from a packaged .app rather than the sources."""
    return getattr(sys, "frozen", None) == "macosx_app"


def agent_command():
    """What launchd should run. The bundle has one executable for everything."""
    if bundled():
        return [os.path.join(bundle_root(), "Contents", "MacOS", "G510"),
                "--daemon"]
    return [os.path.join(APP_DIR, "venv", "bin", "python"),
            os.path.join(APP_DIR, "daemon.py")]


def bundle_root():
    """The .app directory this is running out of."""
    path = os.path.abspath(sys.executable)
    while path != "/" and not path.endswith(".app"):
        path = os.path.dirname(path)
    return path


PYTHON = os.path.join(APP_DIR, "venv", "bin", "python")

USAGE = """g510 - control a Logitech G510 keyboard on macOS

  g510 info                     device, interfaces, current state
  g510 color [colour]           show or set the backlight (#rrggbb or a name)
  g510 brightness [0-100]       show or set backlight brightness
  g510 poweron <colour>         colour the keyboard powers on with
  g510 mkeys [m1 m2 m3 mr]      light M-key LEDs (no args clears them)
  g510 watch [--raw]            print G-key presses live

  g510 lcd <screen>             draw a screen once: clock status gkeys timers
  g510 lcd text "a" "b" "c"     draw up to 3 lines of text
  g510 lcd clear                blank the display
  g510 next                     step to the next screen in the cycle
  g510 menu [open|close]        the settings menu on the panel
  g510 menu key <name>          press a key in it: up down left right enter esc
  g510 note [text|clear]        a sticky note on the panel (3x press of L5)

  g510 autoscreen               what the rules see, and what they would pick
  g510 autoscreen on|off        switch screens by activity, or stop doing it
  g510 autoscreen freeze|unfreeze   hold the screen where it is, or let go
  g510 autoscreen rules         the rules, in the order they are tried
  g510 autoscreen add <name> <screen> key=value...   add a rule
  g510 autoscreen del <name>    drop one; move <name> up|down reorders it
  g510 autoscreen games [...]   which apps count as games (and nogames ...)
  g510 autoscreen set <key> <value>   idle_seconds hold_seconds fallback

  g510 trigger                 the causes you wrote yourself
  g510 trigger on|off          watch for them, or stop watching
  g510 trigger add <name> <screen> [seconds=N] key=value...   add one
  g510 trigger del <name>      drop one
  g510 trigger fire <name>     set one off now (a script can too)
  g510 trigger set <key> <val> check_seconds sound

  g510 timer                   what is counting down, and the presets
  g510 timer start <10m|name>  start one (a preset name or a length of time)
  g510 timer cancel <n|all>    stop one
  g510 timer preset <name> <d> teach a name like "pomodoro" a length of time
  g510 timer set <key> <val>   show_seconds ack_seconds sound
  g510 timer on|off            let keys start timers at all

  g510 discord                 read the channel the chat app has open
  g510 discord on|off          show new messages on the display, or not
  g510 discord apps            which apps are looked for
  g510 discord set <key> <val> poll_seconds show_seconds sound apps
                               banners banners_seconds
  g510 discord channels        what the Discord client has (Vencord plugin)
  g510 discord history [n]     the last n lines of the open channel
  g510 discord older [n]       fetch n more lines of history
  g510 discord open <name>     switch the client: a channel, a DM, or
                               "friends" for the DM list itself
  g510 discord say <text>      send it in the channel the client has open
  g510 discord reply <#ref|last> <text>
                               send it as a reply to one message; the
                               #ref is what history prints beside it

  g510 bank [1|2|3]             show or switch the binding bank (M1/M2/M3)
  g510 switch [on|off <action>] what the joystick switch does
  g510 printer [host|off]       3D printer the display reads from
  g510 claude [rows...]         which usage windows the Claude screen shows
  g510 bindings                 list G-key bindings for the active bank
  g510 bind G1 app Safari       bind a key: app | keys | shell | text | timer
  g510 record G5 [name]         record a keystroke macro onto a G-key
  g510 macros [delete <name>]   list or remove recorded macros
  g510 profile "App" G1 ...     per-app binding overrides
  g510 config                   path to the config file
  g510 permissions              check/request the macOS permissions needed
  g510 verify                   check the key mapping against the markings
  g510 iso [on|off]             fix ^/° and <>| being swapped
  g510 leds [which] [what]      use the Num/Scroll Lock LEDs macOS ignores
  g510 gauge add <label> <cmd>  a bar fed by any command printing a number
  g510 install                  put the g510 command on your PATH

  g510 start | stop | status    run the background agent at login
  g510 run                      run the agent in the foreground (ctrl-C stops)
  g510 gui                      menu bar app

colours: """ + ", ".join(sorted(config.NAMED_COLORS))


def _gkey_order(name):
    """Sort G1..G18 numerically, tolerating junk from a hand-edited config."""
    digits = name[1:] if name[:1].upper() == "G" else ""
    return (0, int(digits)) if digits.isdigit() else (1, name)


def save_config(settings):
    """Persist, or stop with the reason. A refused save is silent otherwise."""
    if not config.save(settings):
        sys.exit(config.last_error or f"Could not write {config.CONFIG_PATH}")


def guard(work):
    """Run a control-layer call, turning failures into clean CLI errors."""
    try:
        return work()
    except control.ControlError as exc:
        sys.exit(str(exc))


# -- commands --------------------------------------------------------------

def cmd_info(_args):
    import hid
    manufacturer, product = guard(control.device_info)
    print(f"  {manufacturer} {product}")
    print(f"  backlight:  {config.format_color(guard(control.get_backlight))}")
    for pid in device.PRODUCT_IDS:
        for found in hid.enumerate(device.VENDOR_ID, pid):
            mark = " <- control" if found["usage_page"] == 0xFF00 else ""
            print(f"  interface {found['interface_number']}  "
                  f"usage_page=0x{found['usage_page']:04x}{mark}")
    if not device.NON_EXCLUSIVE:
        print("  warning:    could not open non-exclusively; the media and "
              "volume keys may not work while the agent runs")
    print(f"  config:     {config.CONFIG_PATH}")
    print(f"  agent:      {'running' if control.daemon_running() else 'not running'}")


def cmd_color(args):
    if not args:
        print(config.format_color(guard(control.get_backlight)))
        return
    try:
        rgb = config.parse_color(args[0])
    except config.ColorError as exc:
        sys.exit(str(exc))
    settings = config.load()
    settings["backlight"] = config.format_color(rgb)
    save_config(settings)
    guard(lambda: control.set_backlight(config.effective_color(settings)))
    reload_agent()
    brightness = settings.get("brightness", 100)
    suffix = "" if brightness == 100 else f" at {brightness}% brightness"
    print(f"backlight -> {config.format_color(rgb)}{suffix}")


def cmd_poweron(args):
    if not args:
        sys.exit("Give a colour, e.g. g510 poweron blue")
    try:
        rgb = config.parse_color(args[0])
    except config.ColorError as exc:
        sys.exit(str(exc))
    guard(lambda: control.set_power_on(rgb))
    print(f"power-on colour -> {config.format_color(rgb)}")


def cmd_mkeys(args):
    for name in args:
        if name.lower() not in device.MKEY_BITS and name.lower() != "off":
            sys.exit(f"Unknown M-key {name!r}. Use: m1 m2 m3 mr")
    names = [a for a in args if a.lower() != "off"]
    guard(lambda: control.set_mkeys(names))
    print(f"M-key LEDs -> {' '.join(args) if args else 'off'}")


def cmd_watch(args):
    raw = "--raw" in args
    settings = config.load()
    via = "agent" if control.daemon_running() else "direct"
    print(f"Watching for G-key presses ({via}). Ctrl-C to stop.")
    try:
        for message in control.watch_gkeys():
            event = message.get("event")
            if event == "ping":
                continue
            name = message.get("key", "?")
            if event == "press":
                binding = settings.get("bindings", {}).get(name)
                print(f"  press   {name:4} -> {actions.describe(binding)}")
                if raw and message.get("raw"):
                    print(f"          raw: {message['raw']}")
            elif event == "release":
                print(f"  release {name}")
    except control.ControlError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        print("\nstopped")


def reload_agent():
    """Tell a running agent to re-read the config file, if one is there."""
    try:
        if control.daemon_running():
            ipc.request({"cmd": "reload"})
    except OSError:
        pass


def agent_reply(command, **payload):
    """Ask the running agent about state the config file does not hold.

    The freeze and the sticky note live in the agent - they are things the
    panel is doing, not settings - so `None` here means there is nobody to
    ask, which is also what a not-running agent looks like.
    """
    try:
        if not control.daemon_running():
            return None
        reply = ipc.request(dict(payload, cmd=command))
        return reply if isinstance(reply, dict) else None
    except (OSError, ValueError):
        return None


def cmd_lcd(args):
    """Change what the LCD shows, and make the change stick.

    The agent redraws the display every second, so a one-shot draw would be
    overwritten immediately. Picking a screen updates the config instead, and
    explicit text or a clear switches the auto-refresh off so it stays put.
    """
    if not args:
        sys.exit("Give a screen name, 'text', or 'clear'. See g510 --help")
    settings = config.load()
    lcd = settings.setdefault("lcd", {})
    if args[0] == "clear":
        lcd["enabled"] = False
        save_config(settings)
        reload_agent()
        guard(control.clear_lcd)
        print("LCD cleared (auto-refresh off; re-enable with g510 lcd status)")
    elif args[0] == "text":
        if len(args) < 2:
            sys.exit('Give up to 3 lines, e.g. g510 lcd text "hello" "there"')
        lcd["enabled"] = False
        save_config(settings)
        reload_agent()
        guard(lambda: control.show_text(list(args[1:4])))
        print("LCD updated (auto-refresh off; re-enable with g510 lcd status)")
    elif args[0] in screens.SCREENS:
        lcd["screen"] = args[0]
        lcd["enabled"] = True
        save_config(settings)
        reload_agent()
        guard(lambda: control.show_screen(args[0]))
        print(f"LCD -> {args[0]}")
    else:
        sys.exit(f"Unknown screen {args[0]!r}. "
                 f"Try: {', '.join(screens.SCREENS)}, text, clear")


def cmd_bindings(_args):
    settings = config.load()
    bindings = settings.get("bindings", {})
    for index in range(1, device.GKEY_COUNT + 1):
        name = f"G{index}"
        print(f"  {name:4} {actions.describe(bindings.get(name))}")


def cmd_bind(args):
    if len(args) < 2:
        sys.exit('Usage: g510 bind G1 app Safari | keys cmd+c | '
                 'shell "say hi" | text "hello" | timer 10m | none')
    key = args[0].upper()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[0]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    kind = args[1].lower()
    settings = config.load()
    bindings = settings.setdefault("bindings", {})
    if kind == "none":
        bindings.pop(key, None)
        save_config(settings)
        print(f"{key} cleared")
        return
    value = " ".join(args[2:])
    if not value:
        sys.exit(f"Give something to bind, e.g. g510 bind {key} app Safari")
    shapes = {"app": ("name", value), "keys": ("keys", value),
              "shell": ("command", value), "text": ("text", value),
              "timer": ("seconds", value)}
    if kind not in shapes:
        sys.exit(f"Unknown type {kind!r}. "
                 "Use: app, keys, shell, text, timer, none")
    if kind == "keys":
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            sys.exit(str(exc))
    if kind == "timer":
        presets = settings.get("timers", {}).get("presets") or {}
        try:
            seconds = timers.resolve(value, presets)
        except timers.DurationError as exc:
            sys.exit(str(exc))
    field, content = shapes[kind]
    bindings[key] = {"type": kind, field: content}
    save_config(settings)
    reload_agent()
    print(f"{key} -> {actions.describe(bindings[key])}"
          + (f"  ({timers.left(seconds)} long)" if kind == "timer" else ""))


def cmd_brightness(args):
    """Show or set backlight brightness.

    The G510 has no brightness register, so this scales the chosen colour.
    """
    settings = config.load()
    if not args:
        print(f"{settings.get('brightness', 100)}%")
        return
    raw = args[0].rstrip("%")
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"Brightness must be a number from 0 to 100, not {args[0]!r}")
    if not 0 <= value <= 100:
        sys.exit("Brightness must be between 0 and 100")
    settings["brightness"] = value
    save_config(settings)
    guard(lambda: control.set_backlight(config.effective_color(settings)))
    reload_agent()
    print(f"brightness -> {value}%  "
          f"({config.format_color(config.effective_color(settings))})")


def cmd_record(args):
    """Record a keystroke sequence and bind it to a G-key."""
    if not args:
        sys.exit("Usage: g510 record G5 [macro name]")
    key = args[0].upper()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[0]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    if not actions.can_post_events():
        sys.exit("Recording needs Accessibility permission - run: g510 permissions")
    name = " ".join(args[1:]) or key.lower()

    print(f"Recording a macro for {key}.")
    print("  Type the sequence, then press Escape to finish.")
    print("  Keys still reach whatever app is focused, so switch away first")
    print("  if you do not want them landing somewhere.\n")
    try:
        steps = recorder.record(
            on_step=lambda chord, n: print(f"  {n:2}. {chord}"))
    except recorder.RecordingError as exc:
        sys.exit(str(exc))
    if not steps:
        print("\nNothing recorded; binding unchanged.")
        return
    settings = config.load()
    settings.setdefault("macros", {})[name] = steps
    settings.setdefault("bindings", {})[key] = {"type": "macro", "name": name}
    save_config(settings)
    reload_agent()
    print(f"\n{key} -> macro {name!r} ({len(steps)} steps)")


def cmd_macros(args):
    settings = config.load()
    macros = settings.get("macros") or {}
    if args and args[0] == "delete":
        if len(args) < 2:
            sys.exit("Usage: g510 macros delete <name>")
        name = " ".join(args[1:])
        if macros.pop(name, None) is None:
            sys.exit(f"No macro called {name!r}")
        for key, binding in list(settings.get("bindings", {}).items()):
            if binding.get("type") == "macro" and binding.get("name") == name:
                settings["bindings"].pop(key)
        save_config(settings)
        reload_agent()
        print(f"deleted macro {name!r}")
        return
    if not macros:
        print("  no macros recorded - try: g510 record G5")
        return
    for name, steps in macros.items():
        print(f"  {name:16} {recorder.summarise(steps)}")


def cmd_profile(args):
    """Per-app binding overrides: g510 profile "Final Cut Pro" G1 keys cmd+z"""
    settings = config.load()
    profiles = settings.setdefault("app_profiles", {})
    if not args:
        if not profiles:
            print("  no per-app profiles - try: "
                  'g510 profile "Safari" G1 keys cmd+t')
            return
        for app, bindings in profiles.items():
            print(f"  {app}")
            for key in sorted(bindings, key=_gkey_order):
                print(f"    {key:4} {actions.describe(bindings[key])}")
        return
    if len(args) < 3:
        sys.exit('Usage: g510 profile "App Name" G1 app|keys|shell|text|none <value>')
    app, key, kind = args[0], args[1].upper(), args[2].lower()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[1]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    bindings = profiles.setdefault(app, {})
    if kind == "none":
        bindings.pop(key, None)
        if not bindings:
            profiles.pop(app, None)
        save_config(settings)
        reload_agent()
        print(f"{app}: {key} cleared")
        return
    value = " ".join(args[3:])
    shapes = {"app": "name", "keys": "keys", "shell": "command", "text": "text"}
    if kind not in shapes:
        sys.exit(f"Unknown type {kind!r}. Use: app, keys, shell, text, none")
    if not value:
        sys.exit(f"Give something to bind for {key}")
    if kind == "keys":
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            sys.exit(str(exc))
    bindings[key] = {"type": kind, shapes[kind]: value}
    save_config(settings)
    reload_agent()
    print(f"{app}: {key} -> {actions.describe(bindings[key])}")


def cmd_bank(args):
    """Show or switch the active binding bank (M1, M2, M3)."""
    settings = config.load()
    if not args:
        active = settings.get("active_bank", "1")
        for name in config.BANKS:
            count = len(settings.get("banks", {}).get(name, {}))
            mark = " <- active" if name == active else ""
            print(f"  M{name}  {count} binding" + ("s" if count != 1 else "")
                  + mark)
        return
    name = args[0].lstrip("mM")
    if name not in config.BANKS:
        sys.exit(f"Bank must be 1, 2 or 3, not {args[0]!r}")
    if control.daemon_running():
        try:
            reply = ipc.request({"cmd": "set_bank", "bank": name})
        except OSError as exc:
            sys.exit(f"The agent is not responding ({exc}). Try: g510 start")
        if not reply.get("ok"):
            sys.exit(reply.get("error", "agent refused"))
    else:
        config.set_bank(settings, name)
        save_config(settings)
        guard(lambda: control.set_mkeys([f"m{name}"]))
    print(f"active bank -> M{name}")


def cmd_switch(args):
    """Show or set what the joystick (game-mode) switch does."""
    settings = config.load()
    current = settings.get("game_switch") or {}
    if not args:
        print(f"  engaged     {current.get('on') or 'nothing'}")
        print(f"  released    {current.get('off') or 'nothing'}")
        print("\n  set with: g510 switch on bank:3   |   off bank:1")
        print("  values:   bank:1-3, screen:<name>, color:<colour>, none")
        return
    if len(args) < 2:
        sys.exit("Usage: g510 switch on|off <action>")
    position = args[0].lower()
    if position not in ("on", "off"):
        sys.exit("First argument must be 'on' or 'off'")
    value = args[1]
    if value.lower() == "none":
        current.pop(position, None)
    else:
        kind, _, rest = value.partition(":")
        if kind == "bank" and rest not in config.BANKS:
            sys.exit(f"Bank must be one of {', '.join(config.BANKS)}")
        if kind == "screen" and rest not in screens.SCREENS:
            sys.exit(f"Unknown screen {rest!r}. Try: {', '.join(screens.SCREENS)}")
        if kind == "color":
            try:
                config.parse_color(rest)
            except config.ColorError as exc:
                sys.exit(str(exc))
        if kind not in ("bank", "screen", "color"):
            sys.exit("Action must be bank:N, screen:<name>, color:<colour> or none")
        current[position] = value
    settings["game_switch"] = current
    save_config(settings)
    reload_agent()
    print(f"switch {position} -> {current.get(position) or 'nothing'}")


def cmd_printer(args):
    """Show or set the 3D printer the display reads from."""
    import printer

    settings = config.load()
    block = settings.setdefault("printer", {})
    if not args:
        host = block.get("host") or "not set"
        print(f"  host     {host}:{block.get('port', 9999)}")
        print(f"  enabled  {'yes' if block.get('enabled') else 'no'}")
        reading = printer.status(force=True)
        if reading:
            print(f"  state    {printer.describe_state(reading)}"
                  f"  {reading['progress']}%")
            if reading["file"]:
                print(f"  file     {reading['file']}")
            print(f"  temps    nozzle {reading['nozzle']:.0f}"
                  f"  bed {reading['bed']:.0f}")
        elif printer.last_error():
            print(f"  error    {printer.last_error()}")
        print(f"  kind     {block.get('kind', 'auto')}"
              " (auto tries Creality, then Moonraker, then OctoPrint)")
        print("\n  set with: g510 printer 192.168.1.50   |   g510 printer off")
        print("            g510 printer kind moonraker")
        print("            g510 printer key <octoprint api key>")
        return
    if args[0].lower() == "kind":
        if len(args) < 2 or args[1].lower() not in printer.BACKENDS:
            sys.exit("Usage: g510 printer kind " + "|".join(printer.BACKENDS))
        block["kind"] = args[1].lower()
        save_config(settings)
        reload_agent()
        print(f"printer kind -> {block['kind']}")
        return
    if args[0].lower() == "key":
        if len(args) < 2:
            sys.exit("Usage: g510 printer key <octoprint api key>")
        block["api_key"] = args[1]
        save_config(settings)
        reload_agent()
        print("OctoPrint API key saved")
        return
    if args[0].lower() in ("off", "none", "disable"):
        block["enabled"] = False
        save_config(settings)
        reload_agent()
        print("printer screen disabled")
        return
    host, _, port = args[0].partition(":")
    block["host"] = host
    block["enabled"] = True
    if port.isdigit():
        block["port"] = int(port)
    save_config(settings)
    reload_agent()
    reading = printer.status(force=True)
    if reading:
        print(f"printer -> {host}  ({reading['model']}, "
              f"{printer.describe_state(reading)})")
    else:
        print(f"printer -> {host}, but it did not answer: "
              f"{printer.last_error()}")


def cmd_claude(args):
    """Choose which usage windows the Claude screen shows."""
    settings = config.load()
    block = settings.setdefault("claude", {})
    if not args:
        print(f"  rows   {', '.join(block.get('rows') or [])}")
        print(f"  stale  after {block.get('stale_after_seconds', 600)}s")
        print("\n  choose any two: " + ", ".join(screens.CLAUDE_ROWS))
        print("  e.g. g510 claude five_hour context")
        return
    wanted = [a for a in args if a in screens.CLAUDE_ROWS]
    unknown = [a for a in args if a not in screens.CLAUDE_ROWS]
    if unknown:
        sys.exit(f"Unknown row(s) {', '.join(unknown)}. "
                 f"Choose from: {', '.join(screens.CLAUDE_ROWS)}")
    block["rows"] = wanted[:2]
    save_config(settings)
    reload_agent()
    print(f"claude screen -> {', '.join(block['rows'])}")


def cmd_next(_args):
    """Advance the LCD to the next screen in the cycle."""
    if control.daemon_running():
        try:
            reply = ipc.request({"cmd": "next_screen"})
        except OSError as exc:
            sys.exit(f"The agent is not responding ({exc}). Try: g510 start")
        if reply.get("ok"):
            print(f"LCD -> {reply.get('screen')}")
            return
        sys.exit(reply.get("error", "agent refused"))
    settings = config.load()
    lcd = settings.setdefault("lcd", {})
    cycle = [n for n in lcd.get("cycle") or [] if n in screens.SCREENS] or list(screens.SCREENS)
    try:
        index = cycle.index(lcd.get("screen", cycle[0]))
    except ValueError:
        index = -1
    choice = cycle[(index + 1) % len(cycle)]
    lcd["screen"], lcd["enabled"] = choice, True
    save_config(settings)
    guard(lambda: control.show_screen(choice))
    print(f"LCD -> {choice}")


def cmd_menu(args):
    """The settings menu on the panel, reached over the socket.

    The panel has no arrow keys of its own, so the menu borrows the
    keyboard's, which needs Input Monitoring. This is the same menu - opened
    by holding the second display key down - driven from here, so it can be
    walked without a gesture and without touching the config.
    """
    head = args[0].lower() if args else "show"
    if head in ("key", "press", "k"):
        if len(args) < 2:
            sys.exit("Usage: g510 menu key "
                     "<up|down|left|right|enter|esc|backspace|character>")
        action, name = "key", args[1]
    elif head in ("open", "on"):
        action, name = "open", None
    elif head in ("close", "off", "quit"):
        action, name = "close", None
    elif head == "show":
        action, name = "show", None
    else:
        sys.exit(f"Unknown menu command {args[0]!r}. Try: g510 menu open")
    payload = {"cmd": "menu", "action": action}
    if name is not None:
        payload["name"] = name
    try:
        reply = ipc.request(payload)
    except OSError as exc:
        sys.exit(f"The agent is not responding ({exc}). Try: g510 start")
    if not reply.get("ok"):
        sys.exit(reply.get("error", "agent refused"))
    if not reply.get("open"):
        print("  the menu is closed - g510 menu open")
        return
    view = reply.get("view") or {}
    print(f"  {view.get('title') or 'Menu'}")
    for line in view.get("main") or []:
        print(f"  {line}")
    if view.get("sub"):
        print(f"  {view['sub']}")


def cmd_note(args):
    """Pin a note on the panel, or take it down.

    The same note a triple press of L5 writes, for when the words are
    easier to type at a shell than on a 160x43 display. The note lives in
    the agent - it is what the panel is showing, not a setting - so every
    one of these needs the agent to be running.
    """
    if not args:
        reply = agent_reply("note")
        if reply is None:
            sys.exit("No agent running - the note lives in the agent. "
                     "Try: g510 start")
        if not reply.get("ok"):
            sys.exit(reply.get("error", "agent refused"))
        print(f"  {reply.get('note') or '(nothing pinned)'}")
        if reply.get("typing"):
            print("  (a note is being written on the panel - "
                  "enter pins it, esc drops it)")
        return
    if args[0].lower() in ("clear", "off", "none", "-", "delete", "rm"):
        reply = agent_reply("note", action="clear")
        if reply is None:
            sys.exit("No agent running - the note lives in the agent.")
        print("note cleared")
        return
    reply = agent_reply("note", action="set", text=" ".join(args))
    if reply is None:
        sys.exit("No agent running - the note lives in the agent, so there "
                 "would be nowhere to show it. Try: g510 start")
    if not reply.get("ok"):
        sys.exit(reply.get("error", "agent refused"))
    print(f"note pinned: {reply.get('note')}")


SETTING_KEYS = ("idle_seconds", "hold_seconds", "manual_seconds",
                "check_seconds", "fallback")


def _value(text):
    """One CLI argument as JSON: a boolean, a number, a list, or text.

    Commas separate a list, so `app=Safari,Terminal` is two patterns and
    `idle=60` is a number - which is what makes `add` able to write a rule
    without quoting anything.
    """
    text = text.strip()
    if "," in text:
        return [_value(part) for part in text.split(",") if part.strip()]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _seconds(value, key):
    """A duration from the command line, or stop with the reason."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        sys.exit(f"{key} must be a number of seconds, not {value!r}")
    if number < 0:
        sys.exit(f"{key} cannot be negative")
    return number


def _condition_line(rule):
    when = rule.get("when")
    if not isinstance(when, dict) or not when:
        return "always"
    return "  ".join(f"{key}={_show_value(value)}" for key, value in when.items())


def _show_value(value):
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _autoscreen_rules(block, heading=True):
    rules = block.get("rules") or []
    if heading:
        print()
    if not rules:
        print("  no rules - nothing will ever switch")
        return
    for index, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "?")
        screen = str(rule.get("screen") or "?")
        print(f"  {index:2}  {name:14} {screen:8} {_condition_line(rule)}")
    if heading:
        print("\n  rules are tried in order; the first whose conditions all "
              "hold wins.")
        print("  conditions: " + ", ".join(sorted(autoscreen.CONDITIONS)))
        print("  e.g. g510 autoscreen add cinema media running=com.apple.TV*")


def _autoscreen_status(settings, block):
    """What the rules can see, and what they would do with it."""
    signals, rule, wanted = autoscreen.preview(settings)
    lcd = settings.get("lcd") or {}
    showing = str(lcd.get("screen", "status"))
    if not lcd.get("enabled", True):
        showing += " (display off)"
    print(f"  enabled    {'yes' if block.get('enabled', True) else 'no'}")
    state = agent_reply("autoscreen")
    if state is None:
        print("  frozen     ? - the agent is not running")
    elif state.get("frozen"):
        print("  frozen     yes - holding the screen: "
              "g510 autoscreen unfreeze")
    else:
        print("  frozen     no")
    print(f"  showing    {showing}")
    picked = wanted or "nothing - leave the screen alone"
    if rule:
        picked += f"  (rule: {autoscreen.describe(rule)})"
    print(f"  rules pick {picked}")
    app = signals.get("app") or "?"
    bundle = signals.get("bundle")
    if bundle:
        app += f" ({bundle})"
    print(f"  frontmost  {app}")
    if signals.get("game"):
        print(f"  game       yes ({signals.get('game_reason')})")
    else:
        reason = signals.get("game_reason")
        print(f"  game       no" + (f" ({reason})" if reason else ""))
    print(f"  idle for   {signals.get('idle', 0.0):.0f}s")
    track = signals.get("track")
    if track and signals.get("playing"):
        artist = track.get("artist") or ""
        print(f"  playing    {track.get('title')}"
              + (f" - {artist}" if artist else ""))
    elif track:
        print(f"  paused     {track.get('title')}")
    else:
        print("  playing    nothing")
    print(f"  hold       {block.get('hold_seconds', 3)}s a rule must win "
          "before it shows")
    print(f"  hands off  {block.get('manual_seconds', 600)}s after you pick "
          "a screen yourself")
    if block.get("fallback"):
        print(f"  fallback   {block['fallback']}")
    _autoscreen_rules(block)


def _autoscreen_add(settings, block, args):
    if len(args) < 2:
        sys.exit("Usage: g510 autoscreen add <name> <screen> [key=value ...]\n"
                 "  e.g. g510 autoscreen add cinema media running=com.apple.TV*\n"
                 "       g510 autoscreen add afk clock idle=true playing=false")
    name, screen = args[0], args[1]
    if not autoscreen.is_screen(screen):
        sys.exit(f"Unknown screen {screen!r}. Try: "
                 f"{', '.join(screens.SCREENS)}, off")
    rules = block.setdefault("rules", [])
    if any(isinstance(rule, dict) and rule.get("name") == name
           for rule in rules):
        sys.exit(f"There is a rule called {name!r} already. Remove it first: "
                 f"g510 autoscreen del {name}")
    when = {}
    rule = {"name": name, "screen": screen, "when": when}
    for item in args[2:]:
        key, sep, value = item.partition("=")
        if not sep:
            sys.exit(f"{item!r} is not a condition. Conditions are key=value, "
                     f"e.g. playing=true. Try: {', '.join(sorted(autoscreen.CONDITIONS))}")
        key = key.strip()
        if key == "hold":
            rule["hold_seconds"] = _seconds(value, "hold")
        elif key in autoscreen.CONDITIONS:
            when[key] = _value(value)
        else:
            sys.exit(f"Unknown condition {key!r}. Try: "
                     + ", ".join(sorted(autoscreen.CONDITIONS)))
    if not when:
        sys.exit("A rule needs at least one condition. To catch everything "
                 "the rules above miss, use: g510 autoscreen set fallback "
                 "<screen>")
    rules.append(rule)
    save_config(settings)
    reload_agent()
    print(f"rule {name!r} -> {screen}  ({_condition_line(rule)})")


def _rule_index(rules, target):
    """A rule's position, by name or by its number in the list."""
    for index, rule in enumerate(rules):
        if isinstance(rule, dict) and str(rule.get("name")) == target:
            return index
    if target.isdigit() and 1 <= int(target) <= len(rules):
        return int(target) - 1
    sys.exit(f"No rule called {target!r}. See: g510 autoscreen rules")


def _autoscreen_del(settings, block, args):
    if len(args) != 1:
        sys.exit("Usage: g510 autoscreen del <name|number>")
    rules = block.get("rules") or []
    removed = rules.pop(_rule_index(rules, args[0]))
    save_config(settings)
    reload_agent()
    print(f"rule {removed.get('name')!r} removed")


def _autoscreen_move(settings, block, args):
    """Priority is order, so this is how a rule is made to count more."""
    if len(args) != 2:
        sys.exit("Usage: g510 autoscreen move <name> up|down|first|last|<n>")
    rules = block.get("rules") or []
    index = _rule_index(rules, args[0])
    rule = rules.pop(index)
    where = args[1].lower()
    if where == "up":
        index = max(0, index - 1)
    elif where == "down":
        index = min(len(rules), index + 1)
    elif where == "first":
        index = 0
    elif where == "last":
        index = len(rules)
    elif where.isdigit() and 1 <= int(where) <= len(rules) + 1:
        index = int(where) - 1
    else:
        sys.exit("Say up, down, first, last, or a position from 1")
    rules.insert(index, rule)
    save_config(settings)
    reload_agent()
    print(f"rule {rule.get('name')!r} is now number {index + 1}")
    _autoscreen_rules(block, heading=False)


def _autoscreen_patterns(settings, block, which, args):
    """`games` and `nogames`: the only part of game detection that is sure."""
    field = "games" if which == "games" else "not_games"
    patterns = list(block.get(field) or [])
    if not args:
        print(f"\n  counted as games:      "
              + (", ".join(block.get("games") or []) or "the built-in list only"))
        print(f"  never counted as games: "
              + (", ".join(block.get("not_games") or []) or "nothing"))
        print(f"\n  list one with: g510 autoscreen {which} "
              "<name or bundle id, globs fine>")
        print(f"  take one back:  g510 autoscreen {which} remove <pattern>")
        return
    if args[0].lower() in ("remove", "del", "delete"):
        wanted = args[1:]
        if not wanted:
            sys.exit(f"Say which pattern to remove: g510 autoscreen {which} "
                     "remove <pattern>")
        missing = [item for item in wanted if item not in patterns]
        if missing:
            sys.exit(f"Not in {field}: {', '.join(missing)}")
        block[field] = [item for item in patterns if item not in wanted]
        save_config(settings)
        reload_agent()
        print(f"{field} -> {', '.join(block[field]) or 'nothing'}")
        return
    wanted = args[1:] if args[0].lower() in ("add", "new") else args
    if not wanted:
        sys.exit(f"Say which pattern to add: g510 autoscreen {which} "
                 "<name or bundle id>")
    added = [item for item in wanted if item not in patterns]
    block[field] = patterns + added
    save_config(settings)
    reload_agent()
    print(f"{field} -> {', '.join(block[field])}")


def _autoscreen_set(settings, block, args):
    if len(args) != 2 or args[0] not in SETTING_KEYS:
        sys.exit("Usage: g510 autoscreen set " + "|".join(SETTING_KEYS)
                 + " <value>")
    key, value = args[0], args[1].strip()
    if key == "fallback":
        if value.lower() in ("", "none", "keep", "-"):
            block[key] = ""
        elif value == "off" or value in screens.SCREENS:
            block[key] = value
        else:
            sys.exit(f"Fallback must be a screen, 'off', or '' to leave the "
                     f"screen alone. Try: {', '.join(screens.SCREENS)}")
    else:
        block[key] = _seconds(value, key)
    save_config(settings)
    reload_agent()
    print(f"autoscreen {key} -> {block[key] if block[key] != '' else '(keep)'}")


def cmd_autoscreen(args):
    """Switch the display by what the Mac is doing - the whole policy is
    config, and this is the part of it worth typing."""
    settings = config.load()
    block = settings.setdefault("autoscreen", {})
    if not args:
        _autoscreen_status(settings, block)
        return
    head, rest = args[0].lower(), args[1:]
    if head in ("status", "show", "preview"):
        _autoscreen_status(settings, block)
        return
    if head in ("freeze", "frozen", "unfreeze", "thaw"):
        # Held or released by the agent: this is runtime state, so there is
        # nothing to write and nothing to reload.
        action = "unfreeze" if head in ("unfreeze", "thaw") else "freeze"
        reply = agent_reply("autoscreen", action=action)
        if reply is None:
            sys.exit("No agent running - the freeze lives in the agent, so "
                     "there is nothing to hold or release.")
        print(f"autoscreen {'unfrozen' if action == 'unfreeze' else 'frozen'}"
              + ("" if reply.get("enabled") else " (autoscreen is off anyway)"))
        return
    if head in ("on", "off", "enable", "disable"):
        block["enabled"] = head in ("on", "enable")
        save_config(settings)
        reload_agent()
        if block["enabled"]:
            # Turning it on means wanting it to work, so a freeze left over
            # from earlier is released rather than silently defeating it.
            agent_reply("autoscreen", action="unfreeze")
        print(f"autoscreen -> {'on' if block['enabled'] else 'off'}")
        if block["enabled"] and not (block.get("rules") or []):
            print("  (there are no rules, so it will not switch anything)")
        return
    if head in ("rules", "list"):
        _autoscreen_rules(block)
        return
    if head == "add":
        _autoscreen_add(settings, block, rest)
        return
    if head in ("del", "delete", "remove"):
        _autoscreen_del(settings, block, rest)
        return
    if head == "move":
        _autoscreen_move(settings, block, rest)
        return
    if head in ("games", "nogames"):
        _autoscreen_patterns(settings, block, head, rest)
        return
    if head == "set":
        _autoscreen_set(settings, block, rest)
        return
    sys.exit(f"Unknown autoscreen command {args[0]!r}. Try: g510 --help")


# -- triggers --------------------------------------------------------------

TRIGGER_SET = ("check_seconds", "sound")


def _trigger_seconds(rule):
    """How long a rule shows for, or 0 when it holds instead."""
    try:
        return float(rule.get("seconds") or 0)
    except (TypeError, ValueError):
        return 0.0


def _trigger_rules(block):
    return [rule for rule in (block.get("rules") or [])
            if isinstance(rule, dict)]


def _trigger_list(block, heading=True):
    rules = _trigger_rules(block)
    if heading:
        print()
    if not rules:
        print("  no triggers - nothing of your own is being watched")
        return
    for index, rule in enumerate(rules, 1):
        name = str(rule.get("name") or "?")
        screen = str(rule.get("screen") or "?")
        seconds = _trigger_seconds(rule)
        kind = f"{seconds:g}s" if seconds > 0 else "holds"
        print(f"  {index:2}  {name:14} {screen:8} {kind:7} "
              f"{_condition_line(rule)}")
    if heading:
        print("\n  'holds' keeps its screen up for as long as the condition "
              "holds.")
        print("  a number of seconds fires once, the moment it becomes true, "
              "then lets go.")
        print("  conditions: " + ", ".join(sorted(autoscreen.CONDITIONS)))
        print("  e.g. g510 trigger add build status seconds=8 "
              "command='test -f /tmp/build.ok'")


def _trigger_status(settings, block):
    rules = _trigger_rules(block)
    held = [rule for rule in rules if not _trigger_seconds(rule)]
    events = [rule for rule in rules if _trigger_seconds(rule)]
    print(f"  enabled    {'yes' if block.get('enabled', True) else 'no'}")
    print(f"  checking   every {block.get('check_seconds', 2)}s")
    print(f"  watching   {len(events)} firing, {len(held)} holding"
          + ("" if rules else " - nothing"))
    if block.get("sound"):
        print(f"  rings      {block['sound']} when one fires")
    _trigger_list(block)


def _trigger_index(rules, target):
    """A trigger's position, by name or by its number in the list."""
    for index, rule in enumerate(rules):
        if isinstance(rule, dict) and str(rule.get("name")) == target:
            return index
    kept = [rule for rule in rules if isinstance(rule, dict)]
    if target.isdigit() and 1 <= int(target) <= len(kept):
        wanted = kept[int(target) - 1]
        for index, rule in enumerate(rules):
            if rule is wanted:
                return index
    sys.exit(f"No trigger called {target!r}. See: g510 trigger list")


def _trigger_add(settings, block, args):
    if len(args) < 2:
        sys.exit("Usage: g510 trigger add <name> <screen> [seconds=N] "
                 "key=value...\n"
                 "  e.g. g510 trigger add build status seconds=8 "
                 "command='test -f /tmp/build.ok'\n"
                 "       g510 trigger add afk clock idle=300\n"
                 "  leave seconds out (or 0) to hold the screen instead of "
                 "firing once")
    name, screen = args[0], args[1]
    if not autoscreen.is_screen(screen):
        sys.exit(f"Unknown screen {screen!r}. Try: "
                 f"{', '.join(screens.SCREENS)}, off")
    rules = block.setdefault("rules", [])
    if any(str(rule.get("name")) == name for rule in _trigger_rules(block)):
        sys.exit(f"There is a trigger called {name!r} already. Remove it "
                 f"first: g510 trigger del {name}")
    when = {}
    rule = {"name": name, "screen": screen, "when": when}
    for item in args[2:]:
        key, sep, value = item.partition("=")
        if not sep:
            sys.exit(f"{item!r} is not a condition. Conditions are key=value, "
                     f"e.g. idle=300. Try: "
                     + ", ".join(sorted(autoscreen.CONDITIONS)))
        key = key.strip()
        if key == "seconds":
            rule["seconds"] = _seconds(value, "seconds")
        elif key == "hold":
            rule["hold_seconds"] = _seconds(value, "hold")
        elif key == "sound":
            rule["sound"] = value
        elif key in autoscreen.CONDITIONS:
            when[key] = _value(value)
        else:
            sys.exit(f"Unknown condition {key!r}. Try: "
                     + ", ".join(sorted(autoscreen.CONDITIONS)))
    if not when:
        sys.exit("A trigger needs at least one condition to watch for.")
    rules.append(rule)
    save_config(settings)
    reload_agent()
    seconds = _trigger_seconds(rule)
    print(f"trigger {name!r} -> {screen}  ({_condition_line(rule)})"
          + (f"  fires once, for {seconds:g}s" if seconds > 0
             else "  holds the screen while true"))


def _trigger_del(settings, block, args):
    if len(args) != 1:
        sys.exit("Usage: g510 trigger del <name|number>")
    rules = block.get("rules") or []
    removed = rules.pop(_trigger_index(rules, args[0]))
    save_config(settings)
    reload_agent()
    print(f"trigger {removed.get('name')!r} removed")


def _trigger_set(settings, block, args):
    if len(args) != 2 or args[0] not in TRIGGER_SET:
        sys.exit("Usage: g510 trigger set " + "|".join(TRIGGER_SET)
                 + " <value>")
    key, value = args[0], args[1].strip()
    if key == "check_seconds":
        block[key] = _seconds(value, key)
    else:
        if not alerts.sound_path(value):
            sys.exit(f"No system sound {value!r}. Try one of: "
                     + ", ".join(sorted(os.listdir(alerts.SOUND_DIR)))[:200])
        block[key] = value
    save_config(settings)
    reload_agent()
    print(f"triggers {key} -> {block[key]}")


def cmd_trigger(args):
    """Causes of the user's own: a screen for something they care about.

    What a trigger watches is config, and so is how long it shows; the agent
    is only needed to fire one by hand.
    """
    settings = config.load()
    block = settings.setdefault("triggers", {})
    head = args[0].lower() if args else "list"
    rest = args[1:]
    if head in ("list", "rules", "status", "show"):
        _trigger_status(settings, block)
        return
    if head in ("on", "off", "enable", "disable"):
        block["enabled"] = head in ("on", "enable")
        save_config(settings)
        reload_agent()
        print(f"triggers -> {'on' if block['enabled'] else 'off'}")
        if block["enabled"] and not _trigger_rules(block):
            print("  (there are no triggers, so nothing is being watched)")
        return
    if head == "add":
        _trigger_add(settings, block, rest)
        return
    if head in ("del", "delete", "remove"):
        _trigger_del(settings, block, rest)
        return
    if head == "set":
        _trigger_set(settings, block, rest)
        return
    if head in ("fire", "run", "now"):
        if not rest:
            sys.exit("Usage: g510 trigger fire <name>")
        reply = _ask_agent({"cmd": "trigger", "action": "fire",
                            "name": rest[0]})
        print(f"{reply.get('fired')} -> {reply.get('screen')} for "
              f"{float(reply.get('seconds') or 0):g}s")
        return
    sys.exit(f"Unknown trigger command {args[0]!r}. Try: g510 --help")


TIMER_SET = ("show_seconds", "ack_seconds", "sound")
NOTE_SET = ("poll_seconds", "show_seconds", "banners_seconds", "sound",
            "apps", "banners")


def _ask_agent(payload):
    """Send one request to the agent, or stop with the reason."""
    if not ipc.is_running():
        sys.exit("The agent keeps the countdowns, and it is not running.\n"
                 "  start it with: g510 start")
    try:
        reply = ipc.request(payload)
    except OSError as exc:
        sys.exit(f"Could not reach the agent ({exc}). Try: g510 start")
    if not reply.get("ok"):
        sys.exit(reply.get("error") or "the agent refused that")
    return reply


def _running_timers():
    """The countdowns the agent holds, or None when there is no agent."""
    if not ipc.is_running():
        return None
    try:
        reply = ipc.request({"cmd": "timer", "action": "list"})
    except OSError:
        return None
    return reply.get("timers") if reply.get("ok") else None


def _timer_status(settings, block):
    enabled = block.get("enabled", True)
    print(f"  enabled    {'yes' if enabled else 'no'}")
    print(f"  rings      {block.get('sound') or '(nothing)'} - "
          f"{block.get('show_seconds', 10)}s when one ends, "
          f"{block.get('ack_seconds', 2.5)}s when one starts")
    presets = block.get("presets") or {}
    if presets:
        print("  presets")
        for name, value in presets.items():
            try:
                span = timers.left(timers.parse_duration(value))
            except timers.DurationError as exc:
                span = f"(broken: {exc})"
            print(f"    {name:10} {value:8} {span}")
    else:
        print("  presets    none are set up - g510 timer preset pomodoro 25m")
    running = _running_timers()
    if running is None:
        print("  running    (the agent is not running, so nothing is counting)")
    elif not running:
        print("  running    nothing")
    else:
        print("  running")
        for index, item in enumerate(running, 1):
            label = item.get("label") or item.get("name") or "timer"
            print(f"    {index}  {label:16} "
                  f"{timers.left(item.get('left') or 0)}")
    print("\n  bind a key to one: g510 bind G5 timer pomodoro")


def cmd_timer(args):
    """Countdowns the display rings out. Presets live in the config; what is
    counting down right now lives in the agent, because only it can tick."""
    settings = config.load()
    block = settings.setdefault("timers", {})
    head = args[0].lower() if args else "list"
    rest = args[1:]
    if head in ("list", "status", "show"):
        _timer_status(settings, block)
        return
    if head in ("on", "off", "enable", "disable"):
        block["enabled"] = head in ("on", "enable")
        save_config(settings)
        reload_agent()
        print(f"timers -> {'on' if block['enabled'] else 'off'}")
        return
    if head == "start":
        if not rest:
            sys.exit("Usage: g510 timer start <10m|pomodoro> [label]\n"
                     "  a preset name or a length of time - both work the same")
        reply = _ask_agent({"cmd": "timer", "action": "start",
                            "value": rest[0], "label": " ".join(rest[1:])})
        item = reply.get("timer") or {}
        label = item.get("label") or item.get("name") or rest[0]
        until = item.get("until")
        ends = time.strftime("%H:%M", time.localtime(until)) if until else "?"
        print(f"{label}: {timers.left(item.get('seconds') or 0)}, "
              f"ends {ends}")
        return
    if head == "cancel":
        if not rest:
            sys.exit("Usage: g510 timer cancel <number|all>")
        reply = _ask_agent({"cmd": "timer", "action": "cancel",
                            "value": rest[0]})
        gone = reply.get("cancelled") or []
        for item in gone:
            label = item.get("label") or item.get("name") or "timer"
            print(f"cancelled {label}")
        return
    if head == "preset":
        if len(rest) < 2:
            sys.exit("Usage: g510 timer preset <name> <10m>\n"
                     "  e.g. g510 timer preset tea 3m")
        name, value = rest[0], " ".join(rest[1:])
        try:
            span = timers.parse_duration(value)
        except timers.DurationError as exc:
            sys.exit(str(exc))
        block.setdefault("presets", {})[name] = value
        save_config(settings)
        reload_agent()
        print(f"preset {name} -> {value} ({timers.left(span)})")
        return
    if head == "set":
        if len(rest) != 2 or rest[0] not in TIMER_SET:
            sys.exit("Usage: g510 timer set " + "|".join(TIMER_SET)
                     + " <value>")
        key, value = rest[0], rest[1].strip()
        if key == "sound":
            if not alerts.sound_path(value):
                sys.exit(f"No system sound {value!r}. Try one of: "
                         + ", ".join(sorted(os.listdir(alerts.SOUND_DIR)
                                            ))[:200])
            block[key] = value
        else:
            block[key] = _seconds(value, key)
        save_config(settings)
        reload_agent()
        print(f"timers {key} -> {block[key]}")
        return
    sys.exit(f"Unknown timer command {args[0]!r}. Try: g510 --help")


def _discord_read(block, count=3):
    """One read of the channel the chat app has open, printed."""
    import notify
    found = notify.probe(block, count=count)
    trusted = found.get("trusted")
    print(f"  access     {'granted' if trusted else 'missing'}")
    if not trusted:
        print("             the agent needs it to read a window - "
              "run: g510 permissions")
    app = found.get("app")
    if app:
        print(f"  app        {app}")
        print(f"  channel    {found.get('channel') or '(nothing open)'}")
        print(f"  read in    {found.get('waited', 0.0):.2f}s")
    else:
        print(f"  app        {found.get('error') or 'not running'}")
        print("             install and open Discord or Vesktop, or add "
              "another app with: g510 discord set apps <id,name>")
        return
    messages = found.get("messages") or []
    if messages:
        print("  latest")
        for message in messages:
            author = message.get("author") or "?"
            stamp = message.get("time") or ""
            print(f"    {author:16} {stamp:6} {message.get('text') or ''}")
    else:
        print("  (the window gave up no messages - is a channel open in it?)")


def _banners_status(block):
    """What the other source - everything macOS was told - is doing.

    There is no window to probe here: the store either opens or it does
    not, and it is the agent that reads it. So this says the setting, and
    asks the store once for whether it can be read at all.
    """
    import notify
    if not block.get("enabled", True):
        # One switch stands for the whole channel: both sources are read by
        # the same watcher, so saying "banners on" here would be a lie.
        print("  banners    (off with everything else - g510 discord on)")
        return
    reader = notify.Notifications(block)
    reader.poll()                      # the first poll only draws its line
    found = reader.status()
    print(f"  banners    {'on' if found['wanted'] else 'off'}"
          f" (show {found['seconds']:g}s)")
    if found["error"]:
        print(f"             {found['error']}")
    elif found["wanted"]:
        print("             from the store macOS keeps - about five "
              "seconds behind, in one batch")


def _client(action, **payload):
    """Ask the agent to talk to the Vencord plugin, or stop with why not."""
    reply = agent_reply("vencord", action=action, **payload)
    if reply is None:
        sys.exit("the agent is not running - it is what holds the "
                 "connection to the plugin")
    if not reply.get("ok"):
        detail = reply.get("error") or "the plugin did not take it"
        waiting = int(reply.get("status", {}).get("waiting") or 0)
        if waiting:
            detail += f" ({waiting} command waiting for the plugin)"
        sys.exit(detail)
    return reply


def _client_line(reply):
    """One line saying where the plugin side stands."""
    state = reply.get("state") or {}
    if reply.get("connected"):
        channel = state.get("channel") or {}
        where = str(channel.get("name") or "(nothing open)")
        count = len(state.get("channels") or [])
        print(f"  client     connected - {where}, {count} channels")
        return
    seen = reply.get("seen")
    how = f"last push {int(seen)}s ago" if seen else "never connected"
    print(f"  client     not connected ({how})")
    if reply.get("error"):
        print(f"             {reply['error']}")


def _wait_for_ack(sent, seconds=2.5):
    """What the plugin said about the command, once it has run it.

    Commands are handed over and executed on the plugin's own schedule, so
    "accepted" is not "done". The plugin reports back in its next push of
    state, which arrives about once a second - so waiting for that one line
    is what turns a fire-and-forget into an answer.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = agent_reply("vencord", action="state")
        ack = ((status or {}).get("state") or {}).get("ack") or {}
        if ack.get("n") == sent.get("n"):
            return ack
        time.sleep(0.25)
    return {}


def _discord_client(head, rest):
    """The Discord client behind the screen: read it, or tell it something.

    Everything here goes through the agent, because the agent is what the
    plugin talks to - the CLI never reaches into Discord itself, and never
    has to: what the plugin found is already in the agent's hands.
    """
    if head == "channels":
        reply = _client("state")
        _client_line(reply)
        channels = (reply.get("state") or {}).get("channels") or []
        if not channels:
            print("  (no channels - open Vesktop and give it a second)")
            return
        for row in channels:
            if row.get("current"):
                mark = ">"
            elif row.get("mentions"):
                mark = "@"
            elif row.get("unread"):
                mark = "*"
            else:
                mark = " "
            extra = (f"  x{row['mentions']}" if row.get("mentions") else "")
            print(f"    {mark} {row.get('name')}{extra}")
        print("  > here now, * unread, @ mentions")
        return
    if head == "history":
        limit = 10
        if rest:
            try:
                limit = max(1, int(rest[0]))
            except ValueError:
                sys.exit(f"g510 discord history [n], not {rest[0]!r}")
        reply = _client("state")
        state = reply.get("state") or {}
        channel = state.get("channel") or {}
        messages = state.get("messages") or []
        _client_line(reply)
        if not messages:
            print("  (nothing read yet - pick one: g510 discord open <name>)")
            return
        for message in messages[-limit:]:
            text = str(message.get("text") or "").replace("\n", " ")
            if message.get("reply_to"):
                text = f"(re: {message['reply_to']}) {text}"
            # The reference is what `reply` is given afterwards: the tail
            # of the id, so nobody has to read a nineteen digit number off
            # a terminal to answer one particular message.
            print(f"    {link.reference(message)} {message.get('time')}  "
                  f"{str(message.get('author'))[:15]:15} {text}")
        print(f"  reply to one: g510 discord reply "
              f"{link.reference(messages[-1])} <text>")
        if state.get("has_more"):
            print(f"  ({len(messages)} held; more before these: "
                  "g510 discord older)")
        print(f"  {channel.get('name') or '?'}")
        return

    # The four that change something in Discord.
    if head == "older":
        limit = int(rest[0]) if rest and str(rest[0]).isdigit() else 30
        sent = _client("older", limit=limit)
    elif head == "open":
        if not rest:
            sys.exit("Usage: g510 discord open <channel>")
        sent = _client("open", name=" ".join(rest))
    elif head == "say":
        if not rest:
            sys.exit('Usage: g510 discord say "hello there"')
        sent = _client("say", text=" ".join(rest))
    else:                                   # reply
        if len(rest) < 2:
            sys.exit("Usage: g510 discord reply <#ref|last> <text>")
        sent = _client("reply", id=rest[0], text=" ".join(rest[1:]))
    ack = _wait_for_ack(sent)
    if ack and not ack.get("ok"):
        sys.exit(f"the plugin could not: {ack.get('error')}")
    if not ack:
        print("handed over - the plugin has not run it yet")
        return
    if head == "open":
        print(f"opening: {sent.get('channel', {}).get('name')}")
    elif head == "older":
        print(f"asked for {limit} older messages - "
              "g510 discord history in a second")
    else:
        print("sent")


def cmd_discord(args):
    """Reading chat off the panel - Discord keeps its messages in its own
    window rather than telling macOS, so this reads the window itself.

    Only the channel left open in the app can be read: that is a limit of
    what Discord draws, not of this. Everything else the machine has to say
    arrives through macOS instead, off the store macOS keeps, which is the
    `banners` setting below.

    `channels`, `history`, `older`, `open`, `say` and `reply` are the other
    half: a Vencord plugin inside Vesktop reads the whole client and hands
    it to the agent, which is what the Discord screen draws.
    """
    settings = config.load()
    block = settings.setdefault("notifications", {})
    head = args[0].lower() if args else "show"
    rest = args[1:]
    if head in ("channels", "history", "older", "open", "say", "reply"):
        return _discord_client(head, rest)
    if head in ("on", "off", "enable", "disable"):
        block["enabled"] = head in ("on", "enable")
        save_config(settings)
        reload_agent()
        print(f"chat reading -> {'on' if block['enabled'] else 'off'}")
        return
    if head == "set":
        if len(rest) != 2 or rest[0] not in NOTE_SET:
            sys.exit("Usage: g510 discord set " + "|".join(NOTE_SET)
                     + " <value>")
        key, value = rest[0], rest[1].strip()
        if key in ("poll_seconds", "show_seconds", "banners_seconds"):
            block[key] = _seconds(value, key)
        elif key == "apps":
            block[key] = [item.strip() for item in value.split(",")
                          if item.strip()]
        elif key == "banners":
            parsed = _value(value)
            if not isinstance(parsed, bool):
                sys.exit(f"banners must be on or off, not {value!r}")
            block[key] = parsed
        else:
            block[key] = value
        save_config(settings)
        reload_agent()
        print(f"notifications {key} -> {block[key]}")
        return
    if head == "apps":
        print("  looking for:")
        for name in block.get("apps") or []:
            print(f"    {name}")
        import notify
        pid, name = notify.find_app(block.get("apps") or notify.DEFAULT_APPS)
        print(f"  running:   {name if name else 'none of them'}")
        return
    if head in ("show", "status", "read", "probe", "test"):
        if not block.get("enabled", True):
            print("  (reading is off - turn it on: g510 discord on)")
        _discord_read(block)
        _banners_status(block)
        return
    sys.exit(f"Unknown discord command {args[0]!r}. Try: g510 --help")


BIN_PATH = os.path.expanduser("~/.local/bin/g510")

def install_cli_tool():
    """Put a `g510` wrapper on PATH, pointing at however this was installed."""
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    if bundled():
        target = os.path.join(bundle_root(), "Contents", "MacOS", "G510")
        body = f'#!/bin/sh\nexec "{target}" --cli "$@"\n'
    else:
        body = (f'#!/bin/sh\nexec "{PYTHON}" "{APP_DIR}/cli.py" "$@"\n')
    with open(BIN_PATH, "w") as handle:
        handle.write(body)
    os.chmod(BIN_PATH, 0o755)
    return BIN_PATH


def cmd_install(_args):
    path = install_cli_tool()
    print(f"installed {path}")
    if os.path.dirname(path) not in os.environ.get("PATH", "").split(":"):
        print(f"  {os.path.dirname(path)} is not on your PATH; add it with:")
        print(f'  echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc')


VERIFY_GKEYS = ["G1", "G6", "G7", "G12", "G13", "G18"]
VERIFY_MODE = [("M1", "M1"), ("M2", "M2"), ("M3", "M3"), ("MR", "MR")]
VERIFY_LCD = [
    ("L2", "the FIRST button under the display, leftmost"),
    ("L3", "the SECOND button under the display"),
    ("L4", "the THIRD button under the display"),
    ("L5", "the FOURTH button under the display, rightmost"),
]


def _await_press(stream, expected, timeout=15.0):
    """Wait for one key press, returning what was decoded, or None."""
    deadline = time.time() + timeout
    for message in stream:
        if time.time() > deadline:
            return None
        if message.get("event") != "press":
            continue
        return message.get("key")
    return None


INDICATORS = ("off", "printing", "claude", "recording")


def cmd_gauge(args):
    """Bars on the display fed by a command that prints one number.

    For anything this tool has no reader for - another assistant's quota, a
    GPU temperature, a build queue. Most tools keep their quota server-side,
    so a command you write beats a reader I guessed at.
    """
    settings = config.load()
    gauges = settings.setdefault("gauges", [])
    if not args or args[0] == "list":
        if not gauges:
            print("  none configured")
            print("\n  e.g. g510 gauge add Disk \"df -h / | awk 'NR==2{print $5}'\"")
        for entry in gauges:
            print(f"  {entry.get('label', '?'):10} {entry.get('command', '')}")
        return
    if args[0] == "clear":
        settings["gauges"] = []
        save_config(settings)
        reload_agent()
        print("gauges cleared")
        return
    if args[0] == "add":
        if len(args) < 3:
            sys.exit('Usage: g510 gauge add <label> "<command printing a number>"')
        if len(gauges) >= 3:
            sys.exit("Three gauges is all the display has room for; "
                     "clear them first")
        gauges.append({"label": args[1], "command": " ".join(args[2:])})
        save_config(settings)
        reload_agent()
        print(f"gauge {args[1]!r} added")
        return
    sys.exit("Usage: g510 gauge [list|add <label> <command>|clear]")


def cmd_leds(args):
    """Use the Num Lock and Scroll Lock LEDs, which macOS leaves dark."""
    settings = config.load()
    block = settings.setdefault("indicators", {})
    if not args:
        print(f"  num lock     {block.get('numlock', 'off')}")
        print(f"  scroll lock  {block.get('scrolllock', 'off')}")
        print("\n  macOS has no Num Lock and never drives either of these, so")
        print("  they are free. Each can show: " + ", ".join(INDICATORS))
        print("\n  e.g. g510 leds numlock printing")
        print("       g510 leds scrolllock claude")
        return
    if len(args) < 2:
        sys.exit("Usage: g510 leds numlock|scrolllock " + "|".join(INDICATORS))
    which = args[0].lower().replace("-", "")
    if which not in ("numlock", "scrolllock"):
        sys.exit("First argument must be numlock or scrolllock")
    what = args[1].lower()
    if what not in INDICATORS:
        sys.exit(f"Unknown indicator {what!r}. Choose from: "
                 + ", ".join(INDICATORS))
    block[which] = what
    save_config(settings)
    reload_agent()
    print(f"{which} -> {what}")


def cmd_iso(args):
    """Fix ^/° and <>| being swapped, which macOS does on this keyboard."""
    settings = config.load()
    if not args:
        state = settings.get("iso_keyboard")
        print(f"  ISO key swap: {'on' if state else 'off'}")
        print("\n  The G510 declares no locale, so macOS assumes it is an ANSI")
        print("  keyboard and swaps the key left of 1 with the one beside the")
        print("  left shift - on a German board, ^/° and <>|.")
        print("\n  Try System Settings > Keyboard > Change Keyboard Type first;")
        print("  that is the proper fix. If it will not stick: g510 iso on")
        return
    wanted = args[0].lower() in ("on", "yes", "true", "1")
    settings["iso_keyboard"] = wanted
    save_config(settings)
    try:
        control.iso_swap(wanted)
    except control.ControlError as exc:
        sys.exit(str(exc))
    reload_agent()
    print(f"ISO key swap {'applied' if wanted else 'removed'}"
          + (" - ^ and <> should be the right way round now" if wanted else ""))


def cmd_verify(_args):
    """Check the decoded key names against the keyboard's own markings.

    Most of the bit positions were worked out from the order keys were pressed
    during mapping, not from their labels - so this asks for specific keys by
    the text printed on them and reports anything that disagrees.
    """
    was_running = agent_loaded()
    if was_running:
        print("Stopping the agent so pressing keys does not fire bindings.\n")
        subprocess.run(["launchctl", "unload", "-w", PLIST_PATH],
                       capture_output=True)
        time.sleep(1.0)
    problems = []
    try:
        stream = control.watch_gkeys()
        print("Press each key as asked. Ctrl-C to stop.\n")

        for label in VERIFY_GKEYS:
            print(f"  press the key marked {label} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        for label, marking in VERIFY_MODE:
            print(f"  press the key marked {marking} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        for label, description in VERIFY_LCD:
            print(f"  press {description} ... ", end="", flush=True)
            got = _await_press(stream, label)
            print(_result(label, got, problems))

        print("\n  flip the joystick switch (top left) ... ", end="", flush=True)
        got = _await_press(stream, "GAME")
        print(_result("GAME", got, problems))
    except control.ControlError as exc:
        print(f"\n{exc}")
        return
    except KeyboardInterrupt:
        print("\n  stopped early")
    finally:
        _verify_leds(problems)
        if was_running:
            subprocess.run(["launchctl", "load", "-w", PLIST_PATH],
                           capture_output=True)
            print("\nAgent restarted.")

    print()
    if problems:
        print(f"{len(problems)} mismatch(es) - the mapping does not match the "
              "keyboard:")
        for line in problems:
            print(f"  {line}")
    else:
        print("Everything checked matches the markings on the keyboard.")


def _result(expected, got, problems):
    if got is None:
        return "no press seen (skipped)"
    if got == expected:
        return f"read as {got}  ok"
    problems.append(f"{expected} on the keyboard reads as {got} in software")
    return f"read as {got}  MISMATCH"


def _verify_leds(problems):
    """Light each M-key LED alone and ask which one actually lit."""
    print("\n  Now the M-key LEDs.")
    for name in ("m1", "m2", "m3", "mr"):
        try:
            control.set_mkeys([name])
        except control.ControlError as exc:
            print(f"    {exc}")
            return
        answer = input(f"    lighting {name.upper()} - which one lit up? "
                       "(m1/m2/m3/mr, or blank to skip) ").strip().lower()
        if not answer:
            continue
        if answer != name:
            problems.append(f"LED {name.upper()} lights the one marked "
                            f"{answer.upper()}")
    try:
        control.set_mkeys([])
    except control.ControlError:
        pass


def cmd_permissions(_args):
    """Report and request the macOS permissions the bindings need."""
    reading = actions.can_read_input()
    posting = actions.can_post_events()
    print(f"  Input Monitoring  {'granted' if reading else 'NOT granted'}"
          "   (reading G-key presses)")
    print(f"  Accessibility     {'granted' if posting else 'NOT granted'}"
          "   (keys/text bindings)")
    if posting and reading:
        print("\nBoth granted - every binding type will work.")
        return
    if not posting:
        print("\nWithout Accessibility, 'keys' and 'text' bindings do nothing.")
        print("'app' and 'shell' bindings work regardless.")
        print("\nAsking macOS now - approve the prompt, then add this binary")
        print("under System Settings > Privacy & Security > Accessibility:")
        print(f"  {PYTHON}")
        actions.request_post_access()
        print("\nAfter approving, restart the agent:  g510 stop && g510 start")


def cmd_config(_args):
    created = config.ensure_exists()
    print(config.CONFIG_PATH + ("  (created)" if created else ""))


# -- launch agent ----------------------------------------------------------

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{arguments}  </array>
  <key>WorkingDirectory</key><string>{app_dir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

LOG_PATH = os.path.expanduser("~/Library/Logs/g510.log")


def agent_loaded():
    result = subprocess.run(["launchctl", "list", LABEL],
                            capture_output=True, text=True)
    return result.returncode == 0


def cmd_start(_args):
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    arguments = "".join(f"    <string>{part}</string>\n"
                        for part in agent_command())
    with open(PLIST_PATH, "w") as handle:
        handle.write(PLIST.format(label=LABEL, arguments=arguments,
                                  app_dir=APP_DIR, log=LOG_PATH))
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", PLIST_PATH],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"launchctl load failed: {result.stderr.strip()}")
    print(f"agent loaded; it will start at login\n  log: {LOG_PATH}")


def cmd_stop(_args):
    if not os.path.exists(PLIST_PATH):
        sys.exit("Agent was never installed (nothing to stop)")
    subprocess.run(["launchctl", "unload", "-w", PLIST_PATH],
                   capture_output=True)
    print("agent stopped and disabled")


def cmd_status(_args):
    print(f"  agent:  {'running' if agent_loaded() else 'not loaded'}")
    print(f"  plist:  {PLIST_PATH}")
    print(f"  log:    {LOG_PATH}")
    if os.path.exists(LOG_PATH):
        print("  recent:")
        with open(LOG_PATH) as handle:
            for line in collections.deque(handle, maxlen=6):
                print("    " + line.rstrip())


def cmd_run(_args):
    import daemon
    daemon.main()


def cmd_gui(_args):
    import gui
    gui.main()


COMMANDS = {
    "info": cmd_info, "color": cmd_color, "colour": cmd_color,
    "poweron": cmd_poweron, "mkeys": cmd_mkeys, "watch": cmd_watch,
    "lcd": cmd_lcd, "bindings": cmd_bindings, "bind": cmd_bind,
    "config": cmd_config, "permissions": cmd_permissions,
    "brightness": cmd_brightness, "record": cmd_record, "macros": cmd_macros,
    "profile": cmd_profile, "profiles": cmd_profile, "next": cmd_next,
    "bank": cmd_bank, "switch": cmd_switch,
    "printer": cmd_printer, "claude": cmd_claude, "install": cmd_install,
    "verify": cmd_verify, "iso": cmd_iso, "leds": cmd_leds,
    "gauge": cmd_gauge, "start": cmd_start, "stop": cmd_stop,
    "status": cmd_status, "run": cmd_run, "gui": cmd_gui,
    "autoscreen": cmd_autoscreen, "auto": cmd_autoscreen,
    "trigger": cmd_trigger, "triggers": cmd_trigger,
    "timer": cmd_timer, "timers": cmd_timer,
    "discord": cmd_discord, "chat": cmd_discord, "notifications": cmd_discord,
    "menu": cmd_menu, "note": cmd_note,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command = COMMANDS.get(sys.argv[1])
    if not command:
        sys.exit(f"Unknown command {sys.argv[1]!r}. Try: g510 --help")
    command(sys.argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
