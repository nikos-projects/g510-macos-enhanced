# Running g510 on Linux

The Linux port lives in `linux/` and shares the agent with the macOS build:
`daemon.py`, `config.py`, `cli.py`, `ipc.py`, `control.py`, `screens.py`,
`alerts.py` and the rest are one set of files used by both, and `linux/` holds
only what genuinely differs - the hardware underneath, plus the two mechanisms
Linux needs for things macOS gives away (`linux/_path.py` explains the import
model in detail).

Debian 13 and Arch are the targets. CachyOS, SteamOS and Linux Mint are all
covered by one of those two.

## What the kernel already does

Debian 13 and current Arch ship `hid-lg-g15`, an in-tree driver for this
keyboard. It exposes the RGB backlight, the M1/M2/M3 LEDs, the MR LED, and the
G-keys / M-keys / display keys as an ordinary input device named **Logitech
Gaming Keyboard Gaming Keys**. Those keys need no special access at all - the
macOS build had to ask for Input Monitoring and run a tap to get the same
thing.

Everything the driver does not do is what this project adds: the 160x43 LCD
over HID, G-key bindings, macro record and replay, and the agent that drives
all of it. `linux/README.md` has the detail, including the two different RGB
attribute names you will meet depending on the kernel.

## Installing

### Debian, Ubuntu, Mint

    ./packaging/build-deb.sh
    sudo apt install ../g510-linux_0.1.1_all.deb

`apt` rather than `dpkg -i`, so `python3-evdev`, `python3-pil` and the rest
come with it.

### Arch, CachyOS, SteamOS

    cd packaging/arch
    makepkg -si

`-s` pulls the dependencies (`python-evdev`, `python-pillow`). Both packages
install the same tree: `/usr/bin/g510`, the modules under
`/usr/share/g510-linux/`, a udev rule and a systemd user unit.

### From a checkout

    linux/install.sh

That checks for the packages it needs, installs the udev rule, and writes a
user unit pointing at the checkout, so the agent that runs is the code you
are editing.

### Then, as yourself and not as root

    systemctl --user daemon-reload
    systemctl --user enable --now g510.service
    journalctl --user -u g510 -f

The agent is a **user** service on purpose: it types into your session,
launches your applications and reads your media player, all of which belong to
a logged-in user. Running it as root would mean reaching across the session
boundary for things the session can do by itself. The permissions problem is
solved at the device instead - `linux/README.md` has the table, including the
one rule (`/dev/uinput`) you may reasonably want to leave out.

It starts happily with no keyboard attached and picks the device up when it
appears, so the unit deliberately has no `.device` dependency.

## The two things Linux does differently

### The beat detector listens through PulseAudio

macOS hands the mix over through a CoreAudio process tap. Linux has no free
equivalent, but every PulseAudio *or* PipeWire system exposes the same idea as
a monitor source: the bytes the default sink is about to play. `parec` reads
one, and `linux/beat.py` turns it into the same two numbers the CoreAudio
listener produces - through the shared `beat.envelope()`, so a kick means the
same thing on both platforms and the sensitivity knob means the same thing
too.

Needs `pulseaudio-utils` (Debian) or `libpulse` (Arch). Without it the agent
logs one line naming the package and the backlight simply does not pulse; the
rest of the agent is unaffected.

### The menu takes the keyboard, and gives it back

The panel has no arrow keys, so the menu borrows the keyboard's - and reading
`/dev/input/event*` is not enough, because the compositor has read the key too
by then. `linux/keytap.py` grabs the G510's typing keyboard exclusively
(`EVIOCGRAB`) while a panel is open and writes everything the menu does not
want straight back out through a uinput virtual keyboard. The grab only exists
while a panel wants keys, and the kernel releases it the moment the process
goes away, so a dead agent cannot leave a dead keyboard behind.

**What this costs, stated plainly:** while a panel has been opened, a program
reading the *physical* node directly - Steam Input, an evdev game - sees the
virtual keyboard instead of the real one. The daemon is restarted (or a panel
closed) to release it. Bindings that send key chords or typed text need write
access to `/dev/uinput`, which is a real privilege: anything in the `plugdev`
group can create a virtual keyboard and type into whatever has focus, and
nothing downstream can tell those events from a real keypress.

## Feature parity

| Feature | Linux | Note |
| --- | --- | --- |
| LCD, screens, screen cycle | yes | including timers, note, menu, message and Discord screens |
| G-key / M-key / display-key bindings | yes | app, shell, keys, text, timer, macro |
| Macro record and replay | yes | replay through `/dev/uinput` |
| Beat pulse on the music | yes | through a PulseAudio/PipeWire monitor |
| API (`http://127.0.0.1:7420/v1`) | yes | |
| Link (the remote control port) | yes | |
| Timers, triggers, autoscreen | yes | |
| Discord screen (Vencord plugin) | yes | |
| Settings menu on the panel | yes | through the evdev grab above |
| On-screen note, message banners | yes | |
| Alert sounds | yes | the freedesktop sound theme rather than macOS's |
| Per-app profiles, `app` screen | **no** | Wayland gives an unprivileged client no way to read the focused window - see below |
| Notification banners from other apps | **no** | macOS reads its own notification store; there is no Linux equivalent yet, so the watcher is deliberately not started |
| Joystick switch, Num/Scroll Lock LEDs | **no** | not reported by `hid-lg-g15`; the settings are kept in the schema and drive nothing |
| ISO/ANSI key swap | n/a | XKB owns the layout; `g510 iso` says so |

**Per-app profiles** are not a matter of the port being unfinished. Wayland
deliberately gives an unprivileged client no way to ask which window has
focus: a client knows about its own surfaces and nothing else, by design,
because "what is the user looking at" is exactly the question the isolation
is there to refuse. The banks (M1/M2/M3) cover most of what per-app profiles
were used for, switched deliberately rather than inferred.

**Notification banners** are the one gap that is genuinely unfinished rather
than impossible: D-Bus has `org.freedesktop.Notifications`, so a watcher can
be written, it just has not been yet. Until it is, `sync_watcher` is skipped
rather than started - calling it would start a reader that finds nothing,
forever.

## Checking it works

    g510 info          device, interfaces, current state
    g510 permissions   access to /dev/uinput and /dev/input
    g510 watch         print G-key presses live
    g510 beat          the beat pulse's switch, amount, decay and sensitivity

`g510 permissions` is the one to run first: it says which of the three things
the agent needs (hidraw for the LCD, the LED class for the backlight, uinput
for chords and text) you can actually get at, and what to do about the ones
you cannot.

## Tests

    python3 -m unittest discover -t . -s linux/tests        # the Linux suite
    ./venv/bin/python3 -m unittest discover -s tests -q     # the macOS suite

Nothing in either suite opens the keyboard, writes to `/sys` or `/dev`,
creates a uinput device or needs `python3-evdev`; the Linux suite also runs
the platform-neutral root tests (`test_screens`, `test_printer`,
`test_control`) against the Linux modules, which is how a change to a shared
file that only breaks one platform gets caught.

## Where to look next

    linux/README.md          the kernel driver, permissions, import model
    linux/_path.py           why `linux/` sits ahead of the repo root
    linux/_shared.py         why shared modules are amended, not forked
    packaging/               the .deb, the Arch package, udev, systemd
