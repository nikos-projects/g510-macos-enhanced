# g510 (enhanced fork)

Control a Logitech G510 gaming keyboard on macOS.

This is a fork of [paulloth1/g510-macos](https://github.com/paulloth1/g510-macos)
with three additions:

- **Auto screen** - the LCD follows what the Mac is actually doing instead of
  sitting on whichever screen was picked last.
- **Timers** - a G-key starts a countdown, the display counts it down, and it
  rings out on the panel with a sound.
- **Chat messages** - what Discord just said, read out of the chat window and
  shown on the LCD for ten seconds.
- **Banners** - every other app's notifications, read back off the store
  macOS keeps, with no permission to ask for.

Everything else, including how the keyboard itself is driven, is unchanged.
There is no Linux port here (yet): the upstream `linux/` tree and the Debian
packaging are not part of this fork (yet).

Logitech never shipped a driver for this keyboard that runs on Apple Silicon:
the last Mac build of Logitech Gaming Software is 8.53.130 (June 2014, Intel
only, OS X 10.8/10.9, kernel extension), the G510 was not on its Mac device
list, and G HUB dropped the keyboard entirely. Logitech's G510 support page now
offers no downloads at all.

This fork was ironically tested on a Hackintosh - macOS Sequoia 15.8.1

This talks to the keyboard's vendor HID interface (usage page 0xff00) from
userspace instead. No kernel extension, nothing to whitelist. Report layouts
follow the Linux `hid-lg-g15` driver and `libg15`.

## What works

- RGB backlight, with brightness, a power-on colour, and per-app colours
- **An API other programs can hook into** - `http://127.0.0.1:7420/v1`,
  over plain HTTP: read every setting, change any of them, send the
  keyboard a command, subscribe to what it is doing as it happens, and
  put screens of your own on the LCD for as long as your program is
  running. `curl http://127.0.0.1:7420/v1` says what there is
- The same backlight dipping to the beat of whatever is playing, and
  climbing back over a third of a second - off until `g510 beat on`, since
  it listens to the Mac's audio to do it. How hard it takes to be counted
  as a hit is a knob of its own, 0 to 100
- M1/M2/M3/MR LEDs
- All 18 G-keys, bound to apps, shell commands, key chords, typed text or
  recorded macros, with per-app profiles layered on top
- The five keys around the display, walking the screen cycle
- **The menu on the panel** - hold L2 and the whole configuration is on
  the LCD, walked with the arrow keys, so no setting needs the command
  line or the window
- **The Settings tab** - the window's fifth tab holds the rest: the
  timings, the lists, the two rule editors, and everything that used to be
  command-line-only
- **Freeze and sticky notes** - double-press a soft key to hold the screen
  where it is until you double-press it again, and triple-press L5 to pin a
  note on the panel that stays until the same three presses take it down
- The 160x43 LCD, drawn with CoreText: status, clock, Claude usage, now
  playing, G-key echo, frontmost app, running timers, and Discord
- **The LCD switching itself** - a game brings up status, music brings up the
  track, an idle machine the clock, and whatever is left the frontmost app,
  with every one of those rules yours to write
- **Countdowns** - `g510 bind G5 timer 10m` starts one from a key, the panel
  counts it down, and it rings out with a system sound
- **Triggers** - causes of your own: a screen held while something is true,
  or one fired the moment it becomes true, from any condition autoscreen
  reads - `g510 trigger fire <name>` sets one off from a shell
- **Chat messages** - a new Discord message takes the LCD for ten seconds and
  then hands back whatever was on it
- **Banners from everything else** - what any other app has to say, off the
  store macOS writes it into: no permission, no window to read, about five
  seconds behind and never in pieces
- **The Discord screen** - three presses of L3 shows the conversation
  itself and hands over the keys: the channel you are in, its newest
  message, unread and mention marks over the channel list, pages of
  history behind it - and with the arrows you can pick another channel,
  answer a message, or ping a person or a role, all without leaving the
  game. `g510 discord say|reply` writes in from a shell too. Read from
  inside the client by our own Vencord plugin, so Vesktop (or another
  Vencord build) is what this one asks for
- **The audio mixer** - hold L4 and the panel shows every application
  that is making sound, one bar each, with the volume keys moving the
  system's or Music's

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

Hold L2 down for half a second and it opens the menu below instead of
stepping on - no pattern to aim at, only the key kept down.

The keys are held as well as counted, because two, three and *staying down*
each mean something else. A lone press waits half a second to see whether
another is coming, and a key still down by then is on its way to a hold
rather than a tap, so nothing fires on the way to a gesture:

    one press     what the key is set to - a screen, the cycle, off
    two presses   L2-L5: freeze the screen where it is
    held down     L2: the menu below, and holding it again shuts it
    held down     L3: the Discord screen with the keys to drive it
    held down     L4: the audio mixer - what is playing, and how loud
    three         L5: a sticky note
    three         L3: the Discord screen, and the keys to drive it

A **frozen** screen is autoscreen standing still: the rules go on reading the
machine, they just get no say until the same double press lets go. It is what
the hands-off window is when you mean it rather than when it times out, and
`g510 autoscreen` reports it:

    g510 autoscreen               says `frozen yes` while it holds
    g510 autoscreen freeze        hold it from a shell
    g510 autoscreen unfreeze      let go (`autoscreen on` does the same)

The freeze lives in the agent rather than in the config - it is what the
panel is doing, not a setting - so restarting the agent drops it, which is
what "hold this for now" ought to mean.

## The menu on the display

Holding L2 turns the panel into a menu - the whole configuration,
walked with the arrow keys, without ever leaving what is in front of you:

    Menu                    Settings > Display
    >Screen >               >Font: Menlo-Bold
     Timers >                Restore on start: on

Three rows: where you are, the item you are on, and the item after it (or,
at the end of a page, a hint about the key you are about to press).
`enter` or `right` opens a page or starts editing, `left` walks back out,
`esc` walks back out and closes at the top - and so does holding L2 again,
or half a minute with nothing pressed. What is in it:

    Screen      pick a screen, turn the panel off, the cycle, gauges
    Timers      start a preset, cancel one, how long each one shows
    Triggers    fire one now; every rule and its conditions
    Bindings    pick a bank, then G1-G18: app, keys, shell, text, timer
    Settings    display, backlight, auto switch, chat, keyboard, Claude,
                printer, agent - every setting the command line reaches
    Leave

A toggle takes effect as you press it. A choice, a number or a line of text
goes into editing instead: `up`/`down` changes it, `enter` keeps it, `esc`
drops it, and `backspace` is `backspace`. A list - the screens in the cycle,
the apps that count as games - is typed as one comma-separated line, and an
unknown name is refused rather than saved.

The keys come from a session event tap, so the agent needs Input
Monitoring (`g510 permissions`). It takes only what the menu needs: the
navigation keys while the menu is open, and printable characters only while
a text field is being edited. Everything else - shortcuts included - still
belongs to the application underneath, because the tap is asked before each
key rather than swallowing the keyboard wholesale.

    g510 menu open           the same thing holding L2 does
    g510 menu                what it is showing right now
    g510 menu key <name>     press one: up down left right enter esc
    g510 menu close

Writing from the menu does what every other writer of that file does: read
it fresh, make the one change, save it, then reload. A change the command
line or the window made while the menu was open is still there when you
come out of it, and the font, the backlight and the chat reader are told
about it the same way they would be for those.

## Sticky notes

Three presses of L5 and the panel asks for something to say:

    NOTE                enter pins  esc cancels
    buy milk before _

Type, hit return, and the note stays on the display until the same three
presses take it down - over whatever the rules, the cycle or the soft keys
would have shown instead. A timer ringing or a message arriving still
borrows the panel for its few seconds and then hands the note back: an alarm
that went unseen is worse than a note that waited.

    g510 note "buy milk"     pin one without touching the keyboard
    g510 note                what is pinned right now
    g510 note clear          take it down

Nothing about it is a setting: the note lives in the agent, so it is dropped
when the agent restarts, and no rule, screen or config key can name it. The
text wraps over the three rows rather than being cut at the right edge, and
an empty note is not pinned at all - enter with nothing typed cancels.

## The Settings tab

The window's fifth tab is the rest of the configuration - everything that
used to need `g510 ...` or the config file, in one column of cards:

    Auto switch        the timings behind the rules, and which apps are games
    Rules              a table of the rules, and underneath: the selected
                       rule's name, its screen, and its conditions
    Countdowns         on/off, how long each one shows, the alarm, presets
    Triggers           on/off, the cadence, and the second list of rules
    Chat messages      which app, how often, what it shows, for how long,
                       and the banners every other app announces
    Panel & indicators num/scroll lock, the power-on colour, ISO swap, L1
    More backlight     per-app colours, the M-keys
    Gauges             up to three bars, each fed by a command
    3D printer         port, refresh, which backend

Two front-ends over one file: what the LCD menu reaches and what this tab
reaches are asserted equal by `tests/test_pane_settings.py`, minus the
settings the other four tabs already own, so neither can silently grow a
hole the other does not have.

The ordinary settings are one row each of a table (`pane_settings.FIELDS`:
a kind, a config path, where it sits in its card), so the cards are built
from that table rather than by hand. The two rule lists are edited in
place - click a row, rename it, pick its screen, and add or remove
conditions one at a time, each with the control its type wants (a checkbox
for the true/false ones, a list for `app`/`running`, seconds or true/false
for `idle`/`active`). Everything writes through the same read-fresh,
change-one, save, reload path as the command line and the menu.

## Auto screen

The display follows what the Mac is doing rather than sitting on whichever
screen was picked last:

    a game in front              -> status
    something playing            -> now playing
    idle, nothing playing        -> clock
    otherwise, at work           -> active app

Those four are only the default rules. The rules are the whole feature, and
they are config:

    "autoscreen": {
      "enabled": true,
      "check_seconds": 2,     how often the signals are re-read
      "idle_seconds": 60,     how long with no input counts as idle
      "hold_seconds": 3,      a rule must win this long before it shows
      "manual_seconds": 600,  hands off after you pick a screen yourself
      "fallback": "",         what to show when no rule holds; "" keeps
                              whatever is on screen
      "games": [],            app names or bundle ids that count as games
      "not_games": [],        ...and that do not, whatever they look like
      "rules": [
        {"name": "game",  "screen": "status", "when": {"game": true}},
        {"name": "music", "screen": "media",  "when": {"playing": true}},
        {"name": "afk",   "screen": "clock",  "when": {"idle": true,
                                                       "playing": false}},
        {"name": "working", "screen": "app",  "when": {"active": true}}
      ]
    }

Rules are tried in the order they are written and the first whose conditions
all hold wins, so **order is priority**. A rule's `when` is an AND: there is
no OR inside one rule, because two rules pointing at the same screen say that
just as well. A rule's `screen` may be `off`, which blanks the display, and a
rule may carry its own `hold_seconds` instead of the shared one.

Every condition any rule can use:

    game          true/false - the frontmost app counts as a game
    playing       true/false - something is playing right now
    idle          true/false, or a number of seconds: no input for that long
    active        true/false, or a number of seconds: input within that long
    app           the frontmost app: a name, a bundle id, or a glob
    not_app       the frontmost app is none of these
    running       one of these is running, frontmost or not
    not_running   none of these is running
    fullscreen    true/false - the frontmost app covers a whole display
    command       a shell command; true when it exits 0

Conditions hold within a rule as an AND, so the `afk` rule above - idle *and*
nothing playing - is what puts the clock up while music is stopped. Wildcards
work wherever a pattern is accepted (`app=com.apple.*`, `running=Music`), and
`command` is there for anything with no reader of its own: it is asked
off-thread at `check_seconds`, and true means exit 0, so
`command=pgrep -x Music` is a condition like any other.

### Telling a game from everything else

Game detection is the one part that cannot be certain, so it is layered, and
each layer can overrule the one below:

1. `not_games` - you said this is not a game, so it is not.
2. `games` - you said this one is.
3. A short built-in list of launchers and emulators (Steam, Epic, GOG,
   Battle.net, Dolphin, OpenEmu, RetroArch, Minecraft).
4. The category the app declares for itself in its `Info.plist`.

Most games declare nothing at all - Roblox and osu! both leave it blank - so
layer 4 catches only some, and `games` is the real tool:

    g510 autoscreen games Roblox "osu!" com.unity.*

Two commands say what an app claims about itself, if you want to check first:

    mdls -name kMDItemAppCategory -raw /Applications/SomeGame.app
    defaults read /Applications/SomeGame.app/Contents/Info.plist LSApplicationCategoryType

Patterns are matched against the app's name and its bundle id,
case-insensitively, as globs. `g510 autoscreen nogames <pattern>` adds to the
first list; `... nogames remove <pattern>` takes one back.

### The command line

    g510 autoscreen                   what it sees now, and what it would pick
    g510 autoscreen on | off          start switching, or stop
    g510 autoscreen rules             the rules, in the order they are tried
    g510 autoscreen add <name> <screen> [key=value ...]
    g510 autoscreen move <name> up|down|first|last|<n>
    g510 autoscreen del <name|number>
    g510 autoscreen games [pattern ...]      count these as games
    g510 autoscreen nogames [pattern ...]    ...except these
    g510 autoscreen set <key> <value>        idle_seconds hold_seconds
                                             manual_seconds check_seconds
                                             fallback

Some examples:

    g510 autoscreen add cinema media fullscreen=true playing=true hold=6
    g510 autoscreen add brb status running=com.mojang.*
    g510 autoscreen add quiet clock idle=true playing=false
    g510 autoscreen move cinema first          # cinema now outranks game
    g510 autoscreen set fallback app           # nothing held -> active app

The Display pane of the app has the on/off switch; the rules themselves are
easier to edit in `~/.config/g510/config.json` than in any form.

### Picking a screen by hand

Every switch - an automatic one, a soft key, `g510 lcd clock`, the popup in
the Display pane - is the same field, `lcd.screen`, and the agent can tell an
automatic switch from somebody else's by which one it made. Pick a screen
yourself and it stands aside for `manual_seconds` (ten minutes by default) so
the rules do not take it back off you; set that to `0` and the rules always
win immediately.

`g510 lcd clear` blanks the display and stops autoscreen with it - while the
display is off there is nothing to keep in step - and MR recording owns the
screen for as long as it is running. Neither needs any permission beyond the
two the driver already asks for.

### Fonts, and anything that is not Latin

The panel draws through CoreText, so it draws whatever the script needs:
Cyrillic, Greek, CJK and emoji all come out as themselves instead of as
question marks (the old path encoded every string as MacRoman, which had none
of them). `lcd.font` picks the face - any family or PostScript name macOS
knows - and anything that font has no glyph for borrows one that does, so a
monospace font stays monospace for Latin while still showing `日本語`.

    "lcd": { "font": "Menlo-Bold" }      the default
    "lcd": { "font": "SF Mono" }         any installed family
    g510 lcd text "Привет из Г510"       draw it once and look

Change the font in the config (or the Display pane) and the agent picks it up
on its next reload; the panel shows it from the next redraw. A font you
install for yourself - `~/Library/Fonts`, or double-clicked in Font Book - is
listed and usable like any system one, no copying into the app needed.

## Triggers

Autoscreen answers "what is the Mac doing" with four conditions the build
chose. A trigger is the same idea with the conditions left open: you say what
you care about, and how the display should answer.

    g510 trigger                 what you are watching, and for what
    g510 trigger on|off          watch for them, or stop watching
    g510 trigger add <name> <screen> [seconds=N] key=value...   add one
    g510 trigger del <name>      drop one
    g510 trigger fire <name>     set one off now - a script can too
    g510 trigger set <key> <val> check_seconds sound

    g510 trigger add build status seconds=8 command='test -f /tmp/build.ok'
    g510 trigger add afk-long clock idle=300
    g510 trigger add raid status seconds=6 app='World of Warcraft*'

One key tells the two halves apart - `seconds`:

**Without it, the trigger holds.** Its screen stays up for as long as the
conditions hold, which makes it an autoscreen rule in everything but name, so
that is exactly what it is: the two are tried as one list, yours first. It
inherits the hold, the hands-off window and the fallback rather than having a
second opinion about them.

**With `seconds`, it fires.** The moment the conditions come true, the screen
goes up for that long and then lets go, and the display underneath - whatever
autoscreen or your hand had picked - is what comes back. Firing is an edge,
not a state: a condition that is already true when the agent starts only arms
it, so a flag file that has been there for a week does not light up the panel
on every restart.

    "triggers": {
      "enabled": true,
      "check_seconds": 2,      how often the conditions are re-read
      "rules": [
        {"name": "afk-long", "screen": "clock", "when": {"idle": 300}},
        {"name": "build", "screen": "status", "seconds": 8,
         "when": {"command": "test -f /tmp/build.ok"}}
      ]
    }

The conditions are autoscreen's own, so both halves of the config read the
same way: `app`, `not_app`, `running`, `idle`, `active`, `playing`, `game`,
`fullscreen`, `command`. A rule may also name a `sound` of its own, and a
hand-written `"data"` dict is passed to the screen that shows.

`g510 trigger fire <name>` is the escape hatch: no condition, no waiting, and
it works whether or not anything is being watched. That is what a build
script, a cron job or a `g510 bind G3 shell 'g510 trigger fire done'` reaches
for.

## Timers

A G-key can start a countdown. The panel counts it down while it runs, and
when it runs out the alarm takes the display for a few seconds and a system
sound rings.

    g510 timer                     what is counting down, and the presets
    g510 timer start 10m           start one: a length of time...
    g510 timer start pomodoro      ...or a preset name - both are the same
    g510 timer cancel 2 | all      stop one, by position, name or label
    g510 timer preset tea 3m       teach a name a length of time
    g510 timer set show_seconds 10 how long the alarm holds the panel
    g510 timer set ack_seconds 2.5 how long "started" holds it
    g510 timer set sound Glass     which system sound rings
    g510 timer on | off            let keys start timers at all

Binding a key to one is the same as binding anything else:

    g510 bind G5 timer pomodoro
    g510 bind G5 timer 10m

The `timer` type reads a preset name or a length of time, so `seconds` and
`preset` mean the same thing in a binding - write whichever reads better. In
the config the whole thing is:

    "timers": {
      "enabled": true,
      "show_seconds": 10,      the alarm borrows the panel for this long
      "ack_seconds": 2.5,      ...and "started" for this long
      "sound": "Glass",        a name from /System/Library/Sounds, or a path
      "presets": {"quick": "90s", "short": "5m", "pomodoro": "25m",
                  "hour": "1h"}
    }

A duration is written `90s`, `5m` or `1h30m`; a bare number means seconds.

The countdown lives in the agent rather than in the config: it has to keep
counting while a game is in front, and it should not outlive a reboot. What is
running right now is on the `timers` screen, which is part of the default
cycle.

The alarm does not switch screens. It borrows the panel for `show_seconds`
while autoscreen goes on choosing underneath, so what you were looking at
simply comes back when it is over. The ring is not tied to the panel either:
if the display is off, or MR is recording into it, the sound still rings.

## Chat messages and banners

Two sources, and they are different in kind.

**Discord never tells macOS about a message.** There is no notification to
show, nothing written to a store, and no API for a background process to hook -
so the only thing there is to read is the window the app draws. That is what
this reads, through the Accessibility API, off the key loop and on its own
thread.

**Everything else goes through macOS**, which writes every notification it is
told about into a store of its own before it draws one. That store is a plain
read-only SQLite file in a group container, so it needs no permission at all -
not Accessibility, not Full Disk Access - and it covers every app on the
machine. What it costs is time: the store commits about five seconds after the
most recent notification, in one batch, so a banner turns up late but never in
pieces. Rows already there when the agent starts are taken as a baseline
rather than announced - they arrived before anybody was listening.

    g510 discord                   read the channel the app has open, now
    g510 discord on | off          both sources onto the panel, or neither
    g510 discord apps              which apps are looked for
    g510 discord set poll_seconds 2 how often the window is asked
    g510 discord set show_seconds 10 how long a message holds the panel
    g510 discord set sound Glass
    g510 discord set apps dev.vencord.vesktop,com.hnc.Discord
    g510 discord set banners on    every other app's notifications too
    g510 discord set banners_seconds 10  how long one of those holds the panel

    "notifications": {
      "enabled": true,
      "apps": ["dev.vencord.vesktop", "com.hnc.Discord",
               "com.hnc.DiscordPTB", "com.hnc.DiscordCanary",
               "Discord", "Vesktop"],
      "poll_seconds": 2,
      "show_seconds": 10,
      "sound": "Glass",
      "banners": true,
      "banners_seconds": 10
    }

A message that arrives takes the panel for `show_seconds` - author, channel
and text - and then the screen underneath is what comes back, which is the
same hand-back the timers use. A poll costs about 70ms of CPU every two
seconds, and only the last stretch of the message list is read at all: the
rest of the channel arrived before anybody asked.

A banner is shown the same shape: what the notification is *titled* goes
where the author goes, what it says is the text, and the time it arrived sits
at the top right - for `banners_seconds` rather than `show_seconds`. Both
sources are read by the same watcher on its own thread, so `g510 discord off`
stops the panel hearing anything at all, and `g510 discord` prints what each
of them is doing.

**Discord is not said twice.** A Discord message both draws in the window and
posts a notification for it, and the window has it a few seconds before the
store does - so while a window is being read, notifications from *that* app
are let through by the window only, or the late copy would land on top of the
live one and read as new. Turn the chat reader off, or point it at a
different app, and Discord's banners arrive by the other route instead.

Two things the banner source does not do:

- **It does not honour Focus.** Rows are written whether or not a banner was
  drawn, and macOS's own "was this presented" flag reads 0 on nearly every row
  including banners that certainly were - so there is nothing there to filter
  on, and with Focus on the panel still shows what arrives.
- **It cannot be made faster.** Five seconds is the store's own batching, not
  a polling interval. What arrives by the window is there in a second or two;
  everything else is five seconds behind it, measured from the *last*
  notification in a burst rather than the first.

**Your own messages are not shown.** Sending one puts it back in front of the
reader a moment later as if it had just arrived, so the account panel at the
bottom of the sidebar is read once for the signed-in name and anything
written under it is dropped. No panel - the window on the Friends page, say -
means nothing is dropped.

Two limits, both of what Discord draws rather than of this:

- **Only the channel left open can be read.** Discord draws one at a time, so
  a message landing in another channel shows up as what the window title
  carries instead: `(3) Discord | ...` becomes "3 unread" on the panel.
- **The window has to be open.** Closed to the menu bar there is no tree to
  read, and `g510 discord` says exactly that.

Reading needs **Accessibility** permission, which `g510 permissions` raises.
The reader takes the app's `AXEnhancedUserInterface` switch while it reads and
hands it back when it stops - unless VoiceOver is running, which wants that
switch for itself.

## The Discord screen

Everything above reads the *window* Discord draws: one channel, only while
that window is open, through Accessibility. This is the other way round -
the whole client, read where Discord actually keeps it, which is only
reachable from inside it.

It therefore needs **Vesktop** (or any Vencord build) installed, because
the reader is a Vencord plugin of ours, `vencord/g510/index.ts`, loaded
into the client. Not a bot account, not a user token, not a
screen-scrape: it reads Discord's own stores over Discord's own API, and
what it finds is what the window is drawing anyway.

Three presses of L3 puts the screen up *and hands it the keyboard*, so
the panel becomes a small Discord browser rather than a notification -
see "Driving it from the keys" below. It is in `SCREENS` so it can be
named - by the gesture, by `g510 lcd discord`, by a rule - but not in the
default cycle, so it never turns up on its own:

    general                         <10:41
    mira
    afternoon, is the build green

The `<` in front of the time means there is history behind these lines.
With nothing loaded yet, the screen is a list instead:

    DISCORD                         10:41
    *general
    @dev notes
     random

`>` is the channel the client is in, `*` is unread, `@` is mentions -
three rows, because that is all the panel has.

    g510 discord channels        what the client has, marked as above
    g510 discord history [n]     the last n lines of the open channel
    g510 discord older [n]       fetch n more lines of history
    g510 discord open <name>     switch the client: a channel, a DM, or
                                 "friends" for the list of them
    g510 discord say <text>      send it in the channel that is open
    g510 discord reply <#ref|last> <text>   send it as a reply
    g510 discord browse [on|off] give the panel the keys, or take them back

`open` matches an id, a whole name or a prefix of one, case not
mattering, and a name fitting more than one channel is refused with the
candidates rather than guessed at. The four that change something in
Discord are handed over and then confirmed by the plugin's next push -
which is what the CLI waits about two seconds for, so "sent" means sent
and a failure says why.

Every line of `history` is labelled with a `#` reference, and that is
what `reply` takes:

    11:21  Win             (re: Izz_vinni2) Chess pieces in B.C
    11:21  Izz_vinni2      Finally done with my 2nd engine
      reply to one: g510 discord reply #678901 <text>

The reference is the tail of the message's id - short enough to read off
a listing, long enough to be one message rather than another - so
answering a *particular* message costs no more than naming it. `last`
(or nothing at all) is the newest one, a full id is accepted as itself,
and a tail fitting more than one message is refused with a count instead
of being guessed at. Anything not held comes back pointing at `history`
and `older`.

**None of this needs Discord in front.** The plugin lives inside the
client and is asked for its state over localhost, so reading history,
switching channels and sending all work from a terminal - or from a
key - while Vesktop sits behind whatever else is on screen.

    "vencord": {
      "enabled": true,
      "port": 47832      must equal HOST in vencord/g510/index.ts
    }

### Driving it from the keys

The screen is not only something to look at. Three presses of L3 puts it
up *and* takes the keyboard, so a channel can be picked, a message
answered and a person or a role pinged with the arrow keys - the game
never loses the front of the screen, and Discord's own window never comes
forward. Hold L3 to take the keys again after letting go of them.

Nothing is written to the config file to make this happen: which pane is
open, where the cursor is and what is being typed all live in the agent
for as long as the browsing does.

Three panes, and the keys move between them:

    channels   the list of channels, three at a time with the one under
               the cursor in the middle. Up and down walk it; left and
               right switch which list it is - the direct messages, then
               each server in sidebar order, without Discord's own view
               moving. Enter opens the channel under the cursor.
    messages   one message at a time, newest first. Up and down move
               through them, and the page behind the oldest is asked for
               as the cursor nears it rather than once it has run out -
               a page that starts at thirty lines and doubles each time,
               up to the hundred Discord answers for in one request, so
               the further back you climb the fewer keys it costs.
               Enter answers the message on show; right writes a new one
               instead; left goes back to the channels.
    compose    the message itself. Enter sends, esc throws the draft
               away. Typing an @ starts a mention: the plugin offers the
               people, the roles, @everyone and @here that fit the word,
               up and down choose between them and enter puts one into
               the text - sent as the code Discord reads, shown on the
               panel as the name it belongs to.

A message longer than the two lines it is given scrolls itself: the
first window sits still long enough to be read, then the text climbs a
line at a time until it has all gone past - and moving to another
message, or a new one arriving under the cursor, starts that one again
from its first line. There is no key spare to spend on scrolling, since
the arrows are what move between messages.

While browsing, the panel only takes the keys its pane has a use for: the
arrows, enter and esc - so letters still reach the game underneath. The
moment a compose starts it takes the lot, the same as writing a note,
because there is no game worth playing at with a sentence half typed.
Cmd and the other shortcut modifiers are never touched at all, which is
how the panel is left again from anywhere. Esc is that key: it hands the
keys back and leaves the screen up as a viewer.

Two things it will not take over, because they own the display more than
it does - a pinned note, and MR while it records. Each says why rather
than doing nothing, and pinning a note while browsing lets the browser go
first. The display moving to another screen, or being switched off,
closes the browser in the same breath: a pane nobody can see must not go
on swallowing arrows from a game.

A notification is a third thing it keeps the pane from. A banner over
the top would cover the very message the browser was opened to read, so
it waits the way it does behind the menu - the sound still rings, only
the sight of it holds off, and it expires in place rather than turning
up over a read that has moved on. The log says
`banner held back - the browser has the keys` once when it happens.

    g510 discord browse            is the keyboard being taken or not
    g510 discord browse on         take it (what three presses of L3 do)
    g510 discord browse off        give it back

A draft nobody goes on with for a minute is dropped, back to the
messages, for the reason an unfinished note is: a pane that is not being
written into must not hold a whole keyboard away from a game.

### How it gets to the panel

Discord holds every conversation in memory in its own stores, and
nothing outside the client can read them - so the plugin reads them and
pushes what it finds to the agent over localhost, once a second and only
when something has changed:

    plugin   POST  /state      ->   agent on 127.0.0.1:47832   what Discord has
    plugin   GET   /cmd?since=N <-  ...held until there is one to run

State is pushed: a push that never arrives costs one second. Commands
are pulled, with the number the plugin has already run, so one sent
while Vesktop is restarting is not lost - and the next push says which
command ran, which retires it, so a plugin coming back cannot send the
same message twice.

The transport is a plain `fetch` to `127.0.0.1`, which Discord's own
content security policy would refuse - except that Vencord patches that
header on the way in and whitelists localhost in `connect-src` already
(`src/main/csp/index.ts`). That, and nothing else, is why this is a
Vencord plugin rather than an injected script: there is no CSP of our
own to write.

The port is the same number in both places by hand, because the plugin
has no way to read the config file.

### Building it

The plugin's source lives in this tree; `vencord/build.sh` copies it
into a Vencord checkout, builds it, and points Vesktop at the result:

    VENCORD=~/Documents/Vencord ./vencord/build.sh

That is the same as the three steps below, plus writing `vencordDir` for
Vesktop and refusing to do that while Vesktop is running - it would
overwrite the file on the way out. Needs node and pnpm; Vencord names
the pnpm it wants, so corepack fetches it.

    cp -R vencord/g510 ~/Documents/Vencord/src/userplugins/
    cd ~/Documents/Vencord && pnpm install && pnpm build

That produces `dist/` with `vencordDesktopMain.js`,
`vencordDesktopRenderer.js` and `vencordDesktopPreload.js` - the three
files Vesktop loads - and Vesktop is told where they are by
`vencordDir` in `~/Library/Application Support/Vesktop/state.json`:

    "vencordDir": "/Users/you/Documents/Vencord/dist"

Vesktop has to be restarted afterwards, and the G510 plugin enabled in
its plugin list. `g510 discord channels` says whether the plugin has
connected: `connected` with a channel and a count, or how long it has
been since the last push.

### What it does not do

- **Only the channel the client is on is followed live.** Messages in
  other channels are what the list's `*` and `@` marks are for; `open` -
  or the arrow keys, once the screen has them - is how you get to them.
- **History is fetched, not watched.** `older`, or up-arrow near the
  oldest message, asks Discord for a page behind what is held; nothing
  behind that is read until it is asked for. The page grows the further
  back you climb - thirty lines, then sixty, then as many as Discord
  will answer for at once - and starts over in each channel.
- **No reactions, edits, attachments or typing state** - the last line is
  the message's text and who said it, at panel width. Sending is plain
  text: a mention is the only markup that goes in.
- **Vesktop has to be open.** The plugin runs inside the client, so a
  closed client is `not connected` on the CLI and "waiting for Vesktop"
  on the screen.

## The mixer on the display

Hold L4 and the panel becomes an audio mixer: which applications are
making the sound, and how loud each of them is, on a display that is
already under your fingers.

    MIXER                            16:29
  > SYSTEM                        ██████ 57%
    Music                         ██ 65%
    Brave                         ---- --

The bar is the level, read from a **process tap per app**: one tap per
application, all of them hanging off a single private aggregate device,
so one callback hands over one buffer per app and the loudest sample of
each is that app's row. Two apps playing at once are two bars moving,
which is the question macOS itself never answers anywhere.

The number on the right is the thing the arrow keys *act on*, not the
level: the system volume on the system row, that app's own volume on
the two apps that keep one of their own (Music and Spotify), and `--`
on everything else. macOS has no per-app output volume - that would
need a virtual device per application - so a number that promised one
would be a lie, and the bar already says how loud it is.

    left / right        the volume of the row you are on
    up / down           pick another row (the list scrolls)
    enter               mute, or unmute, on the system row
    esc                 hand the keys back

A mixer nobody touches for half a minute gives the keys back by
itself, for the reason the menu closes itself: an idle panel must not
go on holding arrows away from the game underneath it. A note, the
Discord browser or the menu takes the panel from it outright, and a
notification waits rather than covering the bars - the sound still
rings, only the sight of it holds off, and the log says
`banner held back - the mixer has the keys`.

    g510 mixer               bring it up (what holding L4 does)
    g510 mixer on            the same thing
    g510 mixer off           let go of it

### What it does not do

- **Nothing is tapped until it has played something.** CoreAudio makes
  an application's object the first time it makes a sound, so there is
  nothing to tap before that. The list is looked for again every couple
  of seconds while the panel is up, which is how an app that starts
  playing turns up on it - and why the panel opens with "listening..."
  for a moment.
- **Only regular applications.** A helper process holding an audio
  object is not something anybody would recognise on a panel.
- **No per-app volume but Music's and Spotify's**, and those two are
  asked with AppleScript rather than a property, because that is the
  only way they will say. An app that will not answer is drawn with
  `--` rather than with a key that does nothing.
- **The bars are peaks, not averages.** What each app did in the last
  tenth of a second, falling back towards zero when it stops rather
  than sitting on its last loud reading.

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
    Accessibility      for bindings that type keystrokes, and for reading a
                       chat window

## Layout

    device.py    vendor HID interface: backlight, M-keys, G-keys, LCD framing
    lcd.py       CoreText canvas -> 1-bit 160x43 bitmap (any script)
    screens.py   what the LCD shows (clock, status, gkeys, app, timers,
                 discord)
    autoscreen.py  what the display should be showing, and when - the rules,
                   the signals behind them, and not fighting a hand that got
                   there first
    triggers.py  causes of your own: hold a screen while something is true,
                 or fire one the moment it becomes true
    actions.py   bindings -> keystrokes (CGEvent), apps, shell
    timers.py    durations, presets, and the countdowns that are running
    alerts.py    the alarm and the message borrowing the panel, and the ring
    notify.py    the chat window, and the store macOS keeps notifications in
    link.py      the localhost server the Vencord plugin talks to, and the
                 last thing it said
    vencord/g510/index.ts  the plugin itself: copied into a Vencord
                 checkout to be built, see "The Discord screen"
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
- **Accessibility** - needed for `keys` and `text` bindings, which synthesise
  keystrokes, and for reading a chat window. `app` and `shell` bindings work
  without it.

Banners ask for neither: the store macOS writes notifications into is a plain
read-only file in a group container, opened the way any process opens a file.

`g510 permissions` reports both and raises the prompt.

## Config

`~/.config/g510/config.json`:

    {
      "backlight": "#00ffff",
      "lcd": { "enabled": true, "screen": "status", "refresh_seconds": 1.0,
               "font": "Menlo-Bold" },
      "app_colors": { "Final Cut Pro": "red", "Terminal": "green" },
      "bindings": {
        "G1": { "type": "app",   "name": "Safari" },
        "G2": { "type": "keys",  "keys": "cmd+shift+4" },
        "G3": { "type": "shell", "command": "say hello" },
        "G4": { "type": "text",  "text": "signature block" },
        "G5": { "type": "timer", "seconds": "pomodoro" }
      }
    }

`app_colors` recolours the backlight when that application comes to the front.

The `autoscreen` block - which screens exist in the rotation, when each one
comes up, and which apps count as games - is documented under **Auto screen**
above, and edited with `g510 autoscreen`.

The `timers` block (presets, how long the alarm holds the panel, which sound)
is documented under **Timers**, and `notifications` - which chat app, how
often it is read, and whether other apps' banners speak too - under the
**Chat messages and banners** section.

The `triggers` block holds the causes you wrote yourself - which condition,
which screen, and whether it holds or fires for a few seconds - and is
documented under **Triggers**, edited with `g510 trigger`.

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
