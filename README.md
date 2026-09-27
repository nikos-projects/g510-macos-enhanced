# g510

Control a Logitech G510 gaming keyboard on macOS.

Logitech never shipped a driver for this keyboard that runs on Apple Silicon:
the last Mac build of Logitech Gaming Software is 8.53.130 (June 2014, Intel
only, OS X 10.8/10.9, kernel extension), the G510 was not on its Mac device
list, and G HUB dropped the keyboard entirely. Logitech's G510 support page now
offers no downloads at all.

This talks to the keyboard's vendor HID interface (usage page 0xff00) from
userspace instead. No kernel extension, nothing to whitelist. Report layouts
follow the Linux `hid-lg-g15` driver and `libg15`.

## What works

- RGB backlight, with brightness, a power-on colour, and per-app colours
- M1/M2/M3/MR LEDs
- All 18 G-keys, bound to apps, shell commands, key chords, typed text or
  recorded macros, with per-app profiles layered on top
- The five keys around the display, walking the screen cycle
- The 160x43 LCD, drawn with CoreText: status, clock, Claude usage, now
  playing, G-key echo, frontmost app

## 3D printer progress

The `printer` screen reads a Creality printer on the LAN. Creality's firmware
serves a WebSocket on port 9999 whose **first frame is a complete status
snapshot** - 77 fields including progress, time remaining, layer, temperatures
and the job name - and everything after that is deltas. Reading one snapshot
and closing costs about 30ms, so `printer.py` polls on a cache rather than
holding a connection open.

    g510 printer 192.168.1.50     point it at a printer and enable the screen
    g510 printer                  current setting and live status
    g510 printer off              disable it

Moonraker (port 7125) is not exposed on this firmware, which is why it uses
the vendor WebSocket. The `state` codes are observed on a K1C rather than
documented - 0 idle, 1 printing, 2 paused, 3 stopped - so anything else is
shown as its raw number instead of being guessed at.

## Gauges

Anything that prints a number can be a bar on the display:

    g510 gauge add Disk "df -h / | awk 'NR==2{print \$5}'"
    g510 gauge add GPU  "..."

Up to three, refreshed off-thread. This exists because most tools keep their
quota server-side with nothing local to read - Claude Code caching its
utilization on disk is the exception, not the rule - so a command someone
writes against what they actually use beats a reader guessed at on their
behalf.

## Printers

`auto` tries each backend in turn; `g510 printer kind <name>` pins one.

    creality    K1/K1C/K2 stock firmware, over its WebSocket
    moonraker   Klipper - Voron, RatRig, most custom builds, rooted Creality
    prusalink   MK4, XL, Mini; API key from the printer's own screen
    octoprint   most Marlin printers behind a Pi; API key from its settings

Only the Creality backend has been tested against real hardware. The other
three are written from their documented APIs.

Bambu Lab is deliberately absent: it speaks MQTT over TLS and needs the
printer's serial and access code, which is a dependency and a credential
handling problem rather than another HTTP call, and not something to ship
untested.

## Where the Claude usage numbers come from

The `claude` screen shows how much of the usage limits is consumed. Two files
carry those numbers and they go stale at very different rates:

- `~/.claude.json` -> `cachedUsageUtilization` is Claude Code's own cache. It
  only refreshes now and then; it has been observed 40+ minutes behind.
- `~/.claude/runcat-usage.json` is the statusline dump, rewritten on every
  render, so in practice it is seconds old. It also carries context usage.

Neither is authoritative, so `claude_utilization()` reads both and takes
whichever was written most recently. Preferring one by fixed priority is what
made the screen show 53% while the real figure was 96%. The result is cached
against both files' mtimes rather than a timer, so the screen follows whichever
source last changed instead of lagging behind both. A `~` in the header means
the freshest reading is still over ten minutes old.

Which windows it shows is configurable - any two of `five_hour`, `seven_day`
and `context`:

    g510 claude five_hour context

`claude.stale_after_seconds` controls when the `~` marker appears.

`claude_usage()` still reads raw token totals out of today's JSONL transcripts,
if a token-count screen is ever wanted again.

## The mode keys and the joystick switch

M1/M2/M3, MR and the game-mode switch all ride in the high bits of the same
24-bit field as the G-keys, above the 18 the G-keys use. Mapped from this
keyboard:

    bit 18  joystick (game-mode) switch     bit 21  M2
    bit 20  M1                              bit 22  M3
                                            bit 23  MR

- **M1/M2/M3** pick one of three independent binding banks, the way Logitech's
  software did, and light the matching LED. Per-app profiles layer on top: the
  bank chooses the base set, the frontmost app overrides individual keys.
- **MR** records. Press it, press a G-key, type the sequence, press MR again
  (or Escape). The display shows progress throughout. The macro is saved as
  `<key>-m<bank>` and bound to that key in the current bank.
- **The joystick switch** is Logitech's game-mode switch. It is not purely a
  firmware key lock - it reports over USB, so `game_switch` in the config can
  make each position do something: `bank:N`, `screen:<name>`, `color:<colour>`,
  or a binding object. It defaults to bank 3 engaged, bank 1 released.

## The keys around the display

L1 is the display/menu key, L2-L5 the four soft keys under the screen, left to
right. They arrive in byte 4 of the macro report (bits 0x01, 0x02, 0x04, 0x08,
0x10), mapped from this keyboard. `lcd_keys` in the config says what each does:
`next` and `prev` walk the cycle, `screen:<name>` jumps to one, `off` blanks
the display, or give it a binding object to run anything a G-key can.

## Installing

Download the latest `G510.dmg` from
[Releases](https://github.com/paulloth1/g510-macos/releases/latest), drag the
app to Applications, and open it.

Because it is ad-hoc signed rather than notarised, macOS refuses it on first
open. Right-click the app and choose Open, or clear the quarantine flag:

    xattr -d com.apple.quarantine /Applications/G510.app
 It lives in the
menu bar; there is no Dock icon. Configuration is on its menu.

The bundle is self-contained - the interpreter, the Python modules and the
compiled extensions are all inside it - so the machine it lands on needs
neither Homebrew nor a virtualenv. The signature is ad-hoc, which is fine for
a Mac you control; distributing it more widely would want a Developer ID.

To build the image from a checkout:

    ./make-dmg.sh          -> dist/G510.dmg

To work on the sources instead, `./install.sh` sets up a virtualenv and puts
the `g510` command on your PATH. Run the UI with `venv/bin/python gui.py`.

Two macOS permissions are needed, and the app asks for both:

    Input Monitoring   to read the G-keys
    Accessibility      only for bindings that type keystrokes

## Layout

    device.py    vendor HID interface: backlight, M-keys, G-keys, LCD framing
    lcd.py       CoreGraphics canvas -> 1-bit 160x43 bitmap
    screens.py   what the LCD shows (clock, status, gkeys, app)
    actions.py   bindings -> keystrokes (CGEvent), apps, shell
    config.py    ~/.config/g510/config.json
    ipc.py       control socket
    control.py   one way in, whether or not the daemon holds the device
    daemon.py    background agent
    gui.py       menu bar app
    cli.py       command line

## Exclusive opens, and why the media keys stopped working

hidapi seizes a HID device on macOS by default. That matters more than it
sounds: the vendor interface (usage page 0xff00) and the consumer-control
interface that carries the media and volume keys (0x000c) are the *same*
IOHIDDevice, with the same device path. Holding it exclusively stopped macOS
from ever seeing the media keys.

`device._open_non_exclusively()` fixes it by calling hidapi's darwin-specific
`hid_darwin_set_open_exclusive(0)`. The Python binding does not expose that
function, but the symbol is exported from the compiled extension, so it is
reached through ctypes at import time. `device.NON_EXCLUSIVE` reports whether
it took effect.

## Why the control socket

It predates the fix above: while the agent held the device exclusively, nothing
else could open it. It is no longer load-bearing, but it stays because a single
owner for shared state and a stream of G-key events is still the right shape.
The CLI and GUI send requests to the agent and fall back to opening the device
directly when no agent is running.

## The GUI and multiple displays

`G510.app` opens a normal window with a Dock icon. It used to be a menu bar
item only, which was a mistake on this machine: the main screen is an external
display well off to the right, so both the status item and a centred window
landed on a monitor that was not being looked at. The window now re-centres on
the active screen every time it is shown. The menu bar item is still there as a
shortcut, and `--menu-only` brings back the old behaviour.

Because the bundle execs an interpreter that lives outside it, macOS would
otherwise call the app "Python"; `name_the_app()` patches the main bundle's
info dictionary before NSApplication starts.

## Permissions

- **Input Monitoring** - needed to read G-key presses.
- **Accessibility** - needed only for `keys` and `text` bindings, which
  synthesise keystrokes. `app` and `shell` bindings work without it.

`g510 permissions` reports both and raises the prompt.

## Config

`~/.config/g510/config.json`:

    {
      "backlight": "#00ffff",
      "lcd": { "enabled": true, "screen": "status", "refresh_seconds": 1.0 },
      "app_colors": { "Final Cut Pro": "red", "Terminal": "green" },
      "bindings": {
        "G1": { "type": "app",   "name": "Safari" },
        "G2": { "type": "keys",  "keys": "cmd+shift+4" },
        "G3": { "type": "shell", "command": "say hello" },
        "G4": { "type": "text",  "text": "signature block" }
      }
    }

`app_colors` recolours the backlight when that application comes to the front.

## Install layout

    ~/.local/bin/g510                          command line entry point
    ~/.local/share/g510/                       modules and virtualenv
    ~/Applications/G510.app                    menu bar app
    ~/Library/LaunchAgents/com.g510.agent.plist      agent, starts at login
    ~/Library/Logs/g510.log                    agent log

Remove with `g510 stop`, then delete those paths.

## Licence

GNU General Public License v3.0 — see `LICENSE`.

    Copyright (C) 2026 Paul Loth

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the
    Free Software Foundation, either version 3 of the License, or (at your
    option) any later version.

    This program is distributed in the hope that it will be useful, but
    WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
    Public License for more details.

The HID report layouts here were worked out from the Linux `hid-lg-g15`
driver's documented behaviour and from libg15, and the rest was mapped off the
hardware directly by pressing keys and watching what arrived. No code was
copied from either.
