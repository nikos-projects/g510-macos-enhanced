# g510 on Linux

The Linux port of the G510 tools. Where the macOS build has to talk to the
keyboard for everything, here the kernel has already done half the work, and
this project is what sits on top of it.

## What the kernel already does

Debian 13 ships `hid-lg-g15`, an in-tree driver for this keyboard. Load it (it
binds automatically) and you get:

    /sys/class/leds/g15::kbd_backlight/          the RGB backlight
    /sys/class/leds/g15::macro_preset1..3/       the M1/M2/M3 LEDs
    /sys/class/leds/g15::macro_record/           the MR LED

and a second input device, named **Logitech Gaming Keyboard Gaming Keys**,
which is where the extra keys arrive. They come through as ordinary key codes:

    KEY_MACRO1 .. KEY_MACRO18          the eighteen G-keys
    KEY_MACRO_PRESET1 .. PRESET3       M1, M2, M3
    KEY_MACRO_RECORD_START             MR
    KEY_KBD_LCD_MENU1 .. MENU5         the five keys under the display

That is worth saying plainly: the G-keys need no special access at all. They
are a keyboard, on an input node, and anything that can read `/dev/input` can
read them. The macOS build had to ask for Input Monitoring and run a tap to
get the same thing.

### The RGB attribute is not the same on every kernel

The backlight colour is written to a sysfs attribute whose name changed when
the driver moved onto the kernel's multicolor LED class:

    color             Debian 13's 6.12 kernel, driver-specific, "R G B"
    multi_intensity   mainline, with multi_index naming the channel order

Both exist in the wild and neither is wrong. The code tries them in turn, and
the udev rule grants access to whichever of them is actually present rather
than picking one and failing quietly on the other kernel.

## What this adds

Everything the driver does not expose:

- **The 160x43 LCD.** There is no kernel interface for it, so it is driven
  directly: HID output report `0x03` on the keyboard's hidraw node, one frame
  of packed monochrome pixels per write. The screens - clock, status, now
  playing, G-key echo, printer progress, gauges - are drawn with Pillow.
- **Bindings for the G-keys.** The kernel turns the G-keys into key codes but
  has no opinion about what they should do. This maps them to applications,
  shell commands, key chords, typed text and recorded macros, in three banks
  switched by M1/M2/M3.
- **Macro record and replay.** MR starts a recording, a G-key stores it, and
  replay goes back out through `/dev/uinput`.
- **The keys under the display**, which walk the screen cycle.

## Installing

### From the .deb

    packaging/build-deb.sh
    sudo apt install ../g510-linux_0.1.0_all.deb

`apt` rather than `dpkg -i`, so the dependencies are pulled in with it. That
puts the entry point at `/usr/bin/g510`, the modules under
`/usr/share/g510-linux/`, the udev rule in `/lib/udev/rules.d/` and the user
unit in `/usr/lib/systemd/user/`.

Then, as yourself and not as root:

    systemctl --user enable --now g510.service
    journalctl --user -u g510 -f

### From a checkout

    linux/install.sh

That checks for the packages it needs (and prints the `apt` line for you to
approve rather than running it behind your back), installs the udev rule, and
writes a user unit pointing at the checkout, so the agent that runs is the
code you are editing. A unit in `~/.config/systemd/user` shadows the packaged
one, which is the right way round while developing.

## Why the agent is a user service

It types into your session, launches your applications and reads your media
player. All of that belongs to a logged-in user. Running it as root would mean
reaching across the session boundary to do things the session can do by
itself, so the unit is `WantedBy=default.target` in the user manager and the
permissions problem is solved at the device instead.

`g510 start`, `g510 stop` and `g510 status` are the macOS side of this: they
write a launchd plist. On Linux, systemd owns that job, and `systemctl --user`
is the equivalent. What the unit runs is `g510 run`, which is the same daemon
either way.

It also starts happily with no keyboard attached - a missing device is an
ordinary state, and it picks the keyboard up when it appears. The unit
deliberately has no `.device` dependency, which would otherwise leave it
not-started after every boot with the keyboard unplugged.

## Permissions

Three things need access, and `packaging/udev/99-g510.rules` grants all three:

| What | Where | How |
| --- | --- | --- |
| LCD, backlight over HID | the `hidraw` node | `uaccess`, plus `plugdev` as a fallback |
| backlight, M-key LEDs | `/sys/class/leds/g15::*` | `chgrp plugdev` + `chmod g+w` on the attributes |
| typing keys and text | `/dev/uinput` | group `plugdev` |

`TAG+="uaccess"` is the modern answer for the hidraw node: logind hands the
device to whoever is sitting at the seat. The `plugdev` group is there for
sessions logind does not treat as active - an ssh login, a bare tty - where
uaccess grants nothing.

`/dev/uinput` is the one to think about before granting. A process that can
open it can create a virtual keyboard and type into whatever has focus, and
nothing downstream can tell those events from a real keypress. That is the
price of bindings that send key chords or text. If you do not want to pay it,
leave yourself out of `plugdev` and drop that rule: the display, the
backlight, the LEDs and bindings that launch applications or run commands all
keep working.

Group changes only take effect at the next login.

## Per-application profiles are not available on Linux

On macOS, bindings and backlight colours can be layered per frontmost
application. That feature is not here, and it is not a matter of it being
unfinished.

Wayland gives an unprivileged client no way to ask which window has focus.
There is no equivalent of `NSWorkspace.frontmostApplication`: a client knows
about its own surfaces and nothing else, by design, because "what is the user
looking at" is exactly the question the isolation is there to refuse. KWin can
answer it through its scripting interface, but only to something the user has
loaded into the compositor, which is a different shape of program than a
background agent.

So the parts of the configuration that key off the frontmost application - the
per-app profiles, and the `app` screen - do nothing here. The banks (M1/M2/M3)
cover most of what per-app profiles were used for, switched deliberately
rather than inferred.

## Layout

    linux/                  the Linux half: hardware, and this README
    linux/_path.py          the import model - read this first
    config.py daemon.py     shared with the macOS build
    ipc.py control.py       ...
    packaging/g510          the entry point, /usr/bin/g510 once installed
    packaging/debian/       the source package
    packaging/udev/         the rules file
    packaging/systemd/      the user unit

`linux/` goes ahead of the repo root on `sys.path`, so a bare `import device`
inside the shared daemon finds the Linux one while `import config` falls
through to the shared one. `linux/_path.py` explains it properly; the entry
point sets up the same two paths in the same order.

The macOS-only modules - the menu bar app, the window and its panes - are not
shipped by the .deb, and neither is `setup.py` or `make-dmg.sh`.
