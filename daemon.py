"""Background agent: owns the keyboard, dispatches G-keys, drives the LCD.

hidapi takes the HID device exclusively, so while this runs it holds the only
handle. The CLI and GUI reach the keyboard by asking this process over a unix
socket (see ipc.py and control.py).

The keyboard lives on a dock, so it can vanish and come back at any time. A
missing device is treated as a normal state, not an error.
"""
import queue
import signal
import sys
import queue
import threading
import time

import actions
import alerts
import api
import audio
import autoscreen
import beat
import browse
import extern
import config
import device
import ipc
import keytap
import lcd
import link
import menu
import mixer
import notify
import recorder
import screens
import timers
import triggers

RECONNECT_DELAY = 2.0
POLL_INTERVAL = 0.004
APP_CHECK_INTERVAL = 0.75

# Display keys are pressed in patterns, not just pressed. One press waits to
# see whether more are coming, because three of L5 have to be able to mean a
# note without the single press firing twice on the way there; two of a soft
# key freeze the screen where it is; and a key held down means something a
# tap never could - a held L2 is the settings menu, open or shut. Everything
# else a key is set to do happens when its window closes.
#
# Two windows, and they are deliberately different lengths:
#
#   GESTURE_HOLD  when a press's own action runs - short, so a single press
#                 still answers almost straight away;
#   GESTURE_SPAN  how long the *count* of presses lives. A triple tapped at
#                 an easy pace has gaps longer than the hold, so counting
#                 has to outlive the action it promised, or the second
#                 window was the first one in disguise and every relaxed
#                 triple was really three lone presses.
#
# A hold is not a third window - it is the key still being down, which the
# loop already reads off every report, so it costs a deadline and no counter.
# It takes the press's own action away while it lasts as well: a menu
# arriving on top of the screen change that carried it there would be two
# answers to one gesture, and the second would hide the first.
GESTURE_SPAN = 0.7          # presses closer together than this are one gesture
GESTURE_HOLD = 0.45         # how long one or two presses wait before firing
HOLD_SECONDS = 0.5          # a key down this long is a hold, not a tap
DOUBLE_KEYS = ("L2", "L3", "L4", "L5")     # twice: freeze the screen (E1)
# Held: the settings menu on L2 (E2), the Discord browser on L3 (P6b) and
# the audio mixer on L4 (P7). All three *open* rather than toggle - esc is
# how the browser and the mixer are left, and a second hold of L2 is how
# the menu is, since the triple that used to shut it is gone.
HOLD_KEYS = {"L2": "menu", "L3": "browse", "L4": "mixer"}
# Three times: L5 writes a note (E3), L3 brings up the Discord screen (P6)
# with the keys to drive it.
TRIPLE_KEYS = {"L5": "note", "L3": "discord"}
MENU_IDLE = 30.0            # a menu nobody touches closes itself
NOTE_IDLE = 30.0            # typing a note nobody finishes abandons it
MIXER_IDLE = 30.0           # a mixer nobody touches gives the keys back
NOTE_MAX = 240              # how long a sticky note may be
MIXER_POLL = 0.1            # how often the levels are read into the bars
MIXER_RESCAN = 2.0          # how often the apps are looked for again


def _find_channel(channels, wanted):
    """Which channel a name on a command line means, or why not.

    A name here is whatever the panel could fit - nine characters of a
    channel that may also be carrying its server - so it is matched as an
    id, then exactly, then as a prefix of either the whole name or its last
    part. An ambiguous name is refused with the candidates instead of
    picking one of them.
    """
    if not wanted:
        return "no channel given - try: g510 discord channels"
    word = wanted.lower()

    def keys(entry):
        name = str(entry.get("name") or "").lower()
        return {name, name.rsplit(" ", 1)[-1]}

    def named(entries):
        return ", ".join(str(entry.get("name") or entry.get("id"))
                         for entry in entries[:6])

    exact = [entry for entry in channels
             if str(entry.get("id")) == wanted or word in keys(entry)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return f"{wanted!r} matches more than one channel: {named(exact)}"
    partial = [entry for entry in channels
               if any(key.startswith(word) for key in keys(entry))]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        return f"no channel matches {wanted!r} - try: g510 discord channels"
    return (f"{wanted!r} matches {len(partial)} channels: "
            f"{named(partial)} - give more of the name")


class MenuHost:
    """The agent's side of the on-screen menu.

    The menu reads `settings` - the agent's live config - and hands its
    changes to `save` as a change rather than as a result, so they can be
    applied to a freshly read copy. The CLI and the GUI write the same
    file, and a menu that saved its own copy would roll their work back.
    `act` covers the rest: the things only this process can do, because it
    holds the keyboard and the countdowns.
    """

    def __init__(self, daemon):
        self.daemon = daemon

    @property
    def settings(self):
        return self.daemon.config

    def save(self, apply=None):
        return self.daemon.save_config_change(apply)

    def act(self, name, arg=None):
        return self.daemon.menu_action(name, arg)


class Daemon:
    def __init__(self):
        self.config = config.load()
        lcd.set_font((self.config.get("lcd") or {}).get("font"))
        self.keyboard = None
        self.running = True
        self.state = {}
        self.pressed = set()
        self.lcd_pressed = set()
        self.mode_pressed = set()
        self.leds = None
        self.last_leds = 0.0
        self.record_state = None       # None | "await_key" | "recording"
        self.record_target = None
        self.recorder = None
        self.last_lcd = 0.0
        self.last_app_check = 0.0
        self.current_app = None
        self.autoscreen = autoscreen.AutoScreen()
        # The user's own triggers ride on the same reader, so one read of
        # "what is happening" answers both halves of the policy.
        self.triggers = triggers.Triggers(reader=self.autoscreen.reader)
        self.alerts = alerts.Alerts()
        self.timers = timers.STORE
        self.alerting = False          # whether an alert has the display
        self.held_alert = None         # the banner last kept from the browser
        self.watcher = None            # the chat reader, once it is wanted
        self.link = None               # the Vencord plugin's server, once up
        self.base_color = config.effective_color(self.config)
        # Reentrant: a socket command already holds this when it reaches
        # select_bank, which takes it again via show_bank_led.
        self.lock = threading.RLock()     # guards every device touch
        self.config_lock = threading.Lock()   # serialises load/apply/save
        self.record_lock = threading.Lock()   # guards the recorder handoff
        self.watchers = []
        self.watchers_lock = threading.Lock()
        self.server = ipc.Server(self.handle_request)
        # What a binding can ask for that the agent itself has to do: a
        # timer starts here because only this process holds the countdown.
        self.hooks = {"timer": self.fire_timer}
        # The menu on the panel. Its host reads the config straight off
        # `self`, so what the menu draws is what the agent is running.
        self.menu = menu.Menu(MenuHost(self))
        self.menu_touch = 0.0        # the last key the menu was given
        self.menu_typing = False     # a text field is being typed into
        self.keytap = None           # started the first time the menu opens
        # The menu is moved by the poll loop (keys the tap took) and by the
        # socket (`g510 menu key ...`) at the same time, so one lock owns
        # every mutation of it. Reentrant: an action can close the menu
        # from inside the press that asked for it.
        self.menu_lock = threading.RLock()
        # One entry per display key: how many presses, when, and what the
        # gesture is currently promising to do when its window closes.
        # `[count, last, due, pending]` - see note_gesture.
        self._gestures = {}
        # A sticky note (E3): `note` is what is pinned, `note_draft` what
        # is being typed. Runtime state on purpose - it is a thing on the
        # screen, not a setting, so nothing in the config knows about it.
        self.note = None
        self.note_draft = None
        self.note_touch = 0.0
        # The Discord screen with keys (P6b): which pane, where the cursor
        # is, what is being written. Runtime state again - browsing is a
        # thing happening on the screen, not a setting, so the config file
        # never learns about it either.
        self.browse = browse.Browser()
        # The audio mixer, opened by holding L4 (P7). Runtime state like
        # the browser above it: what the panel is showing is not a
        # setting, so the config file never learns about it either. The
        # levels are a CoreAudio tap of their own, opened and closed with
        # the panel - and opened on a worker thread, because making an
        # IOProc has been seen to take eleven seconds on this machine
        # (P5) and this loop holds the only handle to the keyboard.
        self.mixer = mixer.Mixer()
        self.mixer_levels = None     # audio.Levels, once the taps are up
        self.mixer_building = False  # whether a worker is opening them
        self.mixer_generation = 0    # bumped on open and close, so a
        # worker that finished after esc can tell and drop what it made
        self.mixer_tapped = ()       # the pids the last attempt was for
        self.mixer_seen = 0.0        # the last time the apps were looked for
        self.mixer_poll = 0.0        # the last time the levels were read
        self.mixer_jobs = None       # the queue of app-volume writes, made
        # The backlight dipping on the beat of the music (P5). The tap is
        # runtime state - opened and closed by `beat.enabled` - and so is
        # the pulse it triggers: the config says whether to dip and how
        # far, never which beat is happening now.
        self.beat = None                # the listener, once one is wanted
        self.beat_detector = beat.Detector()
        self.pulse_start = 0.0
        self.pulse_decay = 0.0
        self.pulse_amount = 0.0
        self.pulse_end = 0.0            # when the dip has climbed back
        self.pulse_at = 0.0             # last write, to pace the climb
        # The API any program can hook into (P8): a server on 127.0.0.1
        # over the same dispatcher the control socket uses, so the two
        # can never disagree about what a command means. Started and
        # stopped by the config, exactly like the Discord link.
        self.api = None

    # -- lifecycle ---------------------------------------------------------

    def stop(self, *_args):
        self.running = False

    def connect(self):
        try:
            keyboard = device.G510()
            keyboard.set_nonblocking(True)
        except (device.DeviceNotFound, OSError):
            return False
        # Publish and set up under the lock, so a socket thread cannot reach
        # a half-configured handle.
        with self.lock:
            self.keyboard = keyboard
            try:
                keyboard.silence_gkey_scancodes()
            except OSError as exc:
                log(f"could not silence G-key scancodes: {exc}")
            if self.config.get("restore_backlight_on_start", True):
                keyboard.set_backlight(self.base_color)
            self.show_bank_led()
        self.state = {}
        log("connected to G510")
        return True

    def drop(self, reason):
        """Let go of the keyboard. Holds the lock: a socket thread may be
        part way through a call on the handle we are about to close."""
        log(f"lost keyboard ({reason}); waiting for it to come back")
        with self.lock:
            keyboard, self.keyboard = self.keyboard, None
            try:
                if keyboard:
                    keyboard.close()
            except Exception:
                pass
        self.pressed = set()
        self.lcd_pressed = set()
        self.mode_pressed = set()
        self.leds = None
        self.last_leds = 0.0

    # -- control socket ----------------------------------------------------

    def handle_request(self, payload):
        """Yield reply dicts for one client request."""
        command = payload.get("cmd")
        if command == "watch":
            yield from self.stream_gkeys()
            return
        try:
            reply = self.run_command(command, payload)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            yield {"ok": False, "error": str(exc)}
            return
        # Commands that touch the config file run outside the device lock,
        # so a save cannot stall G-key polling.
        deferred = reply.pop("deferred", None)
        try:
            if deferred == "reload":
                self.reload_config()
            elif deferred == "next_screen":
                reply["screen"] = self.advance_screen(1)
            elif deferred == "set_bank":
                reply["bank"] = self.select_bank(reply["bank"])
        except (OSError, ValueError, KeyError) as exc:
            yield {"ok": False, "error": str(exc)}
            return
        yield reply

    def reload_config(self):
        """Re-read the config and re-apply everything it drives."""
        self.config = config.load()
        lcd.set_font((self.config.get("lcd") or {}).get("font"))
        self.base_color = config.effective_color(self.config)
        if self.config.get("restore_backlight_on_start", True):
            with self.lock:
                if self.keyboard is not None:
                    self.keyboard.set_backlight(self.active_color())
        self.show_bank_led()
        self.sync_watcher()
        self.sync_link()
        self.sync_beat()
        self.sync_api()

    def sync_watcher(self):
        """The readers follow `notifications.enabled`, and the config.

        Both of them live in this one watcher - the window reader and the
        store reader - so the switch is the whole alerts channel's, not one
        source's.
        """
        block = self.config.get("notifications") or {}
        wanted = bool(block.get("enabled", True))
        if wanted and self.watcher is None:
            self.watcher = notify.Watcher(block, log=log)
            self.watcher.start()
        elif not wanted and self.watcher is not None:
            watcher, self.watcher = self.watcher, None
            watcher.stop()
        elif self.watcher is not None:
            # The config came back as a new dict on every reload, so the
            # watcher is pointed at the current one rather than restarted -
            # restarting would drop whatever it had already learned.
            self.watcher.block = block
            self.watcher.reader.block = block
            self.watcher.banners.block = block

    def sync_link(self):
        """The plugin's server follows `vencord.enabled`, and its port.

        Only a port that actually changed restarts it - repointing the block
        is enough for everything else - and a port that is already taken is
        a state rather than a reason to stop the agent: `Link.start` records
        it and everything else carries on without it.
        """
        block = self.config.get("vencord") or {}
        if not block.get("enabled", True):
            if self.link is not None:
                self.link.stop()
                self.link = None
            return
        try:
            port = int(block.get("port", link.DEFAULT_PORT) or
                       link.DEFAULT_PORT)
        except (TypeError, ValueError):
            port = link.DEFAULT_PORT
        if self.link is not None and self.link.port == port:
            self.link.block = block
            return
        if self.link is not None:
            self.link.stop()
        self.link = link.Link(block, log=log).start()

    def sync_api(self):
        """The public API follows `api.enabled`, and its port.

        The same shape as `sync_link`: a port that changed restarts it,
        everything else is just repointing the block. A port that is
        already taken is a state rather than a reason to stop the agent
        - `Api.start` records it and the keyboard carries on being a
        keyboard.
        """
        block = self.config.get("api") or {}
        if not block.get("enabled", True):
            if self.api is not None:
                self.api.stop()
                self.api = None
            return
        try:
            port = int(block.get("port", api.DEFAULT_PORT) or
                       api.DEFAULT_PORT)
        except (TypeError, ValueError):
            port = api.DEFAULT_PORT
        if self.api is not None and self.api.port == port:
            self.api.block = block
            return
        if self.api is not None:
            self.api.stop()
        self.api = api.Api(self, block, log=log).start()

    def publish(self, kind, data=None):
        """Tell everyone watching the API. Nothing may ever raise here.

        This is called from the poll loop, from key handling and from
        the socket threads - all places holding, or about to hold, the
        only handle to the keyboard. A subscriber with a broken pipe is
        that subscriber's problem and must never become the agent's.
        """
        hub = self.api.hub if self.api is not None else None
        if hub is None:
            return
        try:
            hub.publish(kind, data)
        except Exception:
            pass

    def sweep_screens(self, now):
        """Take down outside screens whose program has stopped pushing.

        Run from the poll loop rather than from a timer thread because
        it is the loop that already owns the display: a screen whose
        program has gone is only worth catching on the way to the next
        redraw, and `extern.TTL` is thirty seconds - the throttling
        here is about not calling `time.time()` into a dict walk a
        hundred times a second, not about the sweep being expensive.
        """
        if now - getattr(self, "_swept", 0.0) < extern.TTL / 3.0:
            return
        self._swept = now
        for name in extern.sweep(now):
            log(f"screen {name!r} went away - its program stopped pushing")
            self.leave_screen(name)

    def leave_screen(self, name):
        """The panel is showing a screen that has just gone away.

        An outside program's screen is only there while the program is
        (see `extern.py`), so this is the moment it stops being. The
        display moves on rather than sitting on a fallback the user did
        not ask for, and the config is corrected too - otherwise the
        next reload would put it straight back.
        """
        lcd = self.config.get("lcd") or {}
        if lcd.get("screen") != name:
            return
        cycle = [n for n in lcd.get("cycle") or [] if n in screens.SCREENS
                 and n != name]
        target = cycle[0] if cycle else "status"

        def apply(settings):
            entry = settings.setdefault("lcd", {})
            if entry.get("screen") == name:
                entry["screen"] = target

        self.mutate(apply)
        self.last_lcd = 0.0
        log(f"LCD -> {target} (a screen went away)")
        self.publish("screen", {"screen": target})

    def sync_beat(self):
        """The beat listener follows `beat.enabled`.

        A tap is a system-wide audio tap plus an aggregate device, so it
        is opened here, on a config reload, and never in the poll loop -
        and a tap that will not open leaves the switch where it is,
        because `refresh_beat` does nothing without a listener and the
        reason is already in the log. Restarting on every reload would
        also throw away the levels the detector has already averaged.
        """
        block = self.config.get("beat") or {}
        wanted = bool(block.get("enabled", False))
        if wanted and self.beat is None:
            listener = beat.Listener(log=log)
            if listener.start():
                self.beat = listener
                self.beat_detector = beat.Detector(
                    beat.sensitivity_of(block))
        elif not wanted and self.beat is not None:
            listener, self.beat = self.beat, None
            listener.stop()
            log("beat tap off")
        # The sensitivity is pushed on every reload, tap open or not, so
        # a spinner on the panel takes effect at once. It is applied to
        # the running detector rather than by replacing it: the averages
        # are what the last few seconds of music actually sounded like,
        # and starting them over would make the next hit a miss.
        self.beat_detector.set_sensitivity(beat.sensitivity_of(block))

    def run_command(self, command, payload):
        if command == "timer":
            # Counting down needs the agent, not the keyboard: a timer can be
            # started, listed or cancelled with the keyboard unplugged.
            return self.timer_command(payload)
        if command == "trigger":
            # Firing one borrows the display, which also needs no keyboard.
            return self.trigger_command(payload)
        if command == "menu":
            # The menu is the panel's own: it opens and moves whether or
            # not the keyboard is plugged in.
            return self.menu_command(payload)
        if command == "note":
            # So is a sticky note - it is a thing on the screen, and the
            # screen is what this process owns.
            return self.note_command(payload)
        if command == "mixer":
            # Like the note: a thing on the screen, so it lives here and
            # not in the config file.
            return self.mixer_command(payload)
        if command == "vencord":
            # The Discord screen is fed by this process's own localhost
            # server, which no keyboard and no config file has a say in.
            return self.vencord_command(payload)
        if command == "autoscreen":
            # The freeze lives here, not in the config file, so this is the
            # only place that can answer for it.
            return self.autoscreen_command(payload)
        with self.lock:
            if self.keyboard is None:
                return {"ok": False, "error": "keyboard not connected"}
            if command == "get_backlight":
                return {"ok": True, "rgb": list(self.keyboard.get_backlight())}
            if command == "set_backlight":
                rgb = tuple(payload["rgb"])
                self.keyboard.set_backlight(rgb)
                self.base_color = rgb
                return {"ok": True}
            if command == "get_config":
                return {"ok": True, "active_bank":
                        str(self.config.get("active_bank", "1"))}
            if command == "set_power_on":
                self.keyboard.set_power_on_backlight(tuple(payload["rgb"]))
                return {"ok": True}
            if command == "set_mkeys":
                self.keyboard.set_mkeys(payload.get("names", []))
                return {"ok": True}
            if command == "send_lcd":
                frame = bytes.fromhex(payload["frame"])
                if len(frame) != device.LCD_FRAME_LEN:
                    return {"ok": False,
                            "error": f"frame must be {device.LCD_FRAME_LEN} bytes"}
                self.keyboard._dev.write(frame)
                return {"ok": True}
            if command == "silence_gkeys":
                self.keyboard.silence_gkey_scancodes()
                return {"ok": True}
            if command == "clear_lcd":
                self.keyboard.clear_lcd()
                return {"ok": True}
            if command == "info":
                return {"ok": True,
                        "manufacturer": self.keyboard._dev.get_manufacturer_string(),
                        "product": self.keyboard._dev.get_product_string()}
            if command == "reload":
                return {"ok": True, "deferred": "reload"}
            if command == "next_screen":
                return {"ok": True, "deferred": "next_screen"}
            if command == "set_bank":
                return {"ok": True, "deferred": "set_bank",
                        "bank": str(payload["bank"])}
            return {"ok": False, "error": f"unknown command {command!r}"}

    def timer_command(self, payload):
        """What `g510 timer ...` asks the agent to do."""
        action = str(payload.get("action") or "list")
        block = self.config.get("timers") or {}
        if action == "list":
            return {"ok": True, "timers": self.timers.running(),
                    "presets": dict(block.get("presets") or {}),
                    "enabled": bool(block.get("enabled", True)),
                    "show_seconds": block.get("show_seconds", 10),
                    "ack_seconds": block.get("ack_seconds", 2.5),
                    "sound": block.get("sound", "")}
        if action == "start":
            if not block.get("enabled", True):
                return {"ok": False,
                        "error": "timers are off - turn them on: g510 timer on"}
            try:
                seconds = timers.resolve(payload.get("value"),
                                         block.get("presets"))
            except (timers.DurationError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            item = self.start_timer(seconds,
                                    label=str(payload.get("label") or ""),
                                    name=str(payload.get("name") or ""))
            return {"ok": True, "timer": item,
                    "timers": self.timers.running()}
        if action == "cancel":
            gone = self.timers.cancel(payload.get("value"))
            if not gone:
                return {"ok": False,
                        "error": f"no timer {payload.get('value')!r} is running"}
            return {"ok": True, "cancelled": gone,
                    "timers": self.timers.running()}
        return {"ok": False, "error": f"unknown timer action {action!r}"}

    def start_timer(self, seconds, label="", name=""):
        """Count down from `seconds`, and say so on the display straight
        away - a key pressed with a game in front needs an answer now."""
        block = self.config.get("timers") or {}
        item = self.timers.start(seconds, label=label, name=name)
        self.alerts.push("timeralert", self._timer_data("started", item),
                         float(block.get("ack_seconds", 2.5) or 0))
        log(f"timer {timers.left(seconds)} started"
            + (f" for {label}" if label else ""))
        return item

    def fire_timer(self, binding):
        """A `timer` binding on a G-key, a soft key or the game switch."""
        block = self.config.get("timers") or {}
        if not block.get("enabled", True):
            log("timer ignored - timers are off (g510 timer on)")
            return
        try:
            seconds = timers.duration_for(binding, block.get("presets"))
        except (timers.DurationError, ValueError) as exc:
            log(f"timer: {exc}")
            return
        label = str(binding.get("label") or "")
        name = str(binding.get("preset") or "")
        self.start_timer(seconds, label=label, name=name)

    def _timer_data(self, phase, item):
        return {"phase": phase, "label": item.get("label") or "",
                "name": item.get("name") or "",
                "seconds": float(item.get("seconds") or 0),
                "until": float(item.get("until") or 0)}

    def _message_data(self, event):
        """An alert from the chat reader, in the shape the screen draws."""
        if event.get("kind") == "unread":
            count = int(event.get("count") or 0)
            return {"app": event.get("app") or "Discord",
                    "channel": event.get("channel") or "",
                    "author": "", "text": f"{count} unread",
                    "title": event.get("title") or "", "time": ""}
        return {"app": event.get("app") or "Discord",
                "channel": event.get("channel") or "",
                "author": event.get("author") or "",
                "text": event.get("text") or "",
                "title": "", "time": event.get("time") or ""}

    def stream_gkeys(self):
        """Feed G-key events to one watching client until it disconnects."""
        events = queue.Queue(maxsize=256)
        with self.watchers_lock:
            self.watchers.append(events)
        try:
            while self.running:
                try:
                    yield events.get(timeout=1.0)
                except queue.Empty:
                    yield {"event": "ping"}
        finally:
            with self.watchers_lock:
                if events in self.watchers:
                    self.watchers.remove(events)

    def broadcast(self, message):
        """Fan out to watchers, dropping for any client that stopped reading."""
        with self.watchers_lock:
            for events in self.watchers:
                try:
                    events.put_nowait(message)
                except queue.Full:
                    pass
        # The API's subscribers get the same events from the same places,
        # so there is one list of "things that happened" rather than two
        # that drift. Key presses and releases are what `broadcast` is
        # called with today; `publish` below is for the wider set - the
        # screen moving, an app coming forward, a timer finishing - that
        # has no business being on the raw key stream.
        self.publish("key", message)

    # -- work --------------------------------------------------------------

    # How many input reports one pass may chew through. The queue is drained
    # rather than sampled: reading one report per pass lets a busy keyboard
    # - somebody typing while a triple press is landing - build a backlog,
    # and the OS drops from the front of its queue once it is full, which is
    # how a key press goes missing without a trace. The bound keeps a device
    # that never stops sending from starving the rest of the loop.
    REPORT_BURST = 64

    def handle_gkeys(self):
        with self.lock:
            reports = []
            while len(reports) < self.REPORT_BURST:
                data = self.keyboard.read_raw()
                if data is None:
                    break
                reports.append(data)
        for report in reports:
            self.handle_report(report)

    def handle_report(self, data):
        # Raw reports go out so unknown buttons can be mapped, but never the
        # boot-keyboard report - that is the user's actual typing.
        if not device.G510.carries_keystrokes(data):
            self.broadcast({"event": "raw", "raw": data.hex()})
        keys = device.G510.decode_gkeys(data)
        if keys is None:
            return
        for name in sorted(keys - self.pressed):
            if self.record_state == "await_key":
                self.start_recording(name)
            else:
                self.fire(name)
            self.broadcast({"event": "press", "key": name, "raw": data.hex()})
        for name in sorted(self.pressed - keys):
            self.broadcast({"event": "release", "key": name})
        self.pressed = keys

        mode_keys = device.G510.decode_mode_keys(data)
        if mode_keys is not None:
            for name in sorted(mode_keys - self.mode_pressed):
                self.fire_mode_key(name)
                self.broadcast({"event": "press", "key": name})
            for name in sorted(self.mode_pressed - mode_keys):
                self.broadcast({"event": "release", "key": name})
            for name in sorted(self.mode_pressed - mode_keys):
                if name == "GAME":
                    self.fire_game_switch(False)
            self.mode_pressed = mode_keys

        lcd_keys = device.G510.decode_lcd_keys(data)
        if lcd_keys is not None:
            for name in sorted(lcd_keys - self.lcd_pressed):
                # Every display key is counted rather than fired: one press
                # waits for a possible second or third, so nothing runs on
                # the way to a gesture. See note_gesture.
                self.note_gesture(name)
                self.broadcast({"event": "press", "key": name})
            self.lcd_pressed = lcd_keys

    def note_gesture(self, name, now=None):
        """Count a possible double or triple press of a display key.

        One press waits `GESTURE_HOLD` before it does anything, so a second
        or a third can still change what the gesture means:

            1   do what the key is set to (a screen, the cycle, off)
            2   of a soft key: freeze the screen where it is   (E1)
            3   L5: a sticky note (E3); L3: the Discord screen (P6)

        A key held down instead of tapped is read in `refresh_gestures`,
        where the loop can see that it is still down: that is the menu
        (E2), and it needs no count at all - which is the point of it, a
        hold being something the hand does rather than something it has to
        aim at.

        Nothing fires on the way to a gesture *within* the hold: a triple
        in a hurry runs neither the single press nor the freeze on the way
        there. The count itself lives longer, for `GESTURE_SPAN`, because a
        triple tapped at an easy pace has gaps wider than the hold - so a
        press that arrives late finds the count still standing, and the lone
        press it follows has already done its job. That is the trade: a
        gesture you can actually hit, at the price of a stray screen change
        along the way.
        """
        now = time.time() if now is None else now
        state = self._gestures.get(name)
        if state is None or now - state[1] > GESTURE_SPAN:
            state = [0, 0.0, 0.0, None]
        state[0] += 1
        state[1] = now
        if state[0] == 1:
            state[2], state[3] = now + GESTURE_HOLD, "single"
        elif state[0] == 2:
            # Two of a soft key promise the freeze; two of anything else
            # are still just a press that has not finished arriving.
            state[2] = now + GESTURE_HOLD
            state[3] = "freeze" if name in DOUBLE_KEYS else "single"
        else:
            action = TRIPLE_KEYS.get(name)
            if action is None:
                # No triple on this key: what the double promised still
                # stands - or has already run - and the count is kept only
                # so the next press knows the shape of the gesture so far.
                self._gestures[name] = state
                return
            self._gestures[name] = [0, now, 0.0, None]
            if action == "menu":
                if self.menu.closed:
                    self.open_menu()
                else:
                    self.close_menu()
            elif action == "discord":
                self.show_discord()
            else:
                self.toggle_note()
            return
        self._gestures[name] = state

    def refresh_gestures(self, now):
        """A display key whose gesture window has closed does its job.

        Two windows end here, at different times: the action a press
        promised runs `GESTURE_HOLD` after it, while the count of presses is
        kept until `GESTURE_SPAN` has passed with no more of them. So the
        state is not thrown away when the action runs - only the promise is
        - which is what lets a slower triple still be told apart from three
        lonely presses.

        A key held down is checked first and answered differently: while it
        is down the promise it made waits (a tap that turns into a hold must
        not also change the screen), and at `HOLD_SECONDS` the hold fires
        and takes the press with it, so a finger that stays where it is
        cannot open the menu and shut it again - or start a browser twice.
        """
        for name, state in list(self._gestures.items()):
            if name in HOLD_KEYS and name in self.lcd_pressed:
                if now - state[1] >= HOLD_SECONDS:
                    # The hold owns the key now: drop the press, or the
                    # next tick would find it owing a second answer.
                    self._gestures.pop(name, None)
                    self.hold_key(name)
                continue
            if not state[2]:
                if now - state[1] > GESTURE_SPAN:
                    # Nothing owed, and the pressing has stopped: forget it.
                    self._gestures.pop(name, None)
                continue
            if now < state[2]:
                continue
            pending, state[3], state[2] = state[3], None, 0.0
            # Nothing underneath the menu to act on: the key would only
            # change a screen nobody can see.
            if not self.menu.closed:
                continue
            if pending == "freeze":
                self.toggle_freeze(name)
            elif pending:
                self.fire_lcd_key(name)

    def hold_key(self, name):
        """What a key *held* down means: the menu on L2, the browser on L3,
        the mixer on L4.

        A hold is a deliberate hand, so all three answers are "come up"
        rather than "go away": leaving is a tap of esc (the browser and
        the mixer) or a second hold (the menu), which is written on the
        panel for the menu and in the README for this.
        """
        action = HOLD_KEYS.get(name)
        if action == "menu":
            if self.menu.closed:
                self.open_menu()
            else:
                self.close_menu()
        elif action == "browse":
            self.start_browse()
        elif action == "mixer":
            self.start_mixer()

    def toggle_freeze(self, name=None):
        """Double press of a soft key: hold the screen where it is (E1).

        The rules get no say until it is asked for again; `g510 autoscreen`
        reports it and `g510 autoscreen unfreeze` lets go of it.
        """
        frozen = self.autoscreen.toggle_freeze()
        self.last_lcd = 0.0
        log("autoscreen frozen" if frozen else "autoscreen unfrozen"
            + (f" ({name} twice)" if name else ""))
        return frozen

    @property
    def bindings(self):
        """Base bindings, overlaid with any profile for the frontmost app."""
        merged = dict(self.config.get("bindings", {}))
        profile = (self.config.get("app_profiles") or {}).get(self.current_app)
        if isinstance(profile, dict):
            for key, value in profile.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
        return merged

    def active_color(self):
        """Backlight colour for the frontmost app, brightness applied."""
        wanted = (self.config.get("app_colors") or {}).get(self.current_app)
        if not wanted:
            return self.base_color
        try:
            return config.apply_brightness(
                config.parse_color(wanted), self.config.get("brightness", 100))
        except config.ColorError:
            return self.base_color

    def mutate(self, apply):
        """Re-read the config, apply a change, write it back.

        The CLI and GUI write this file too. Saving our own long-held copy
        would silently revert whatever they changed since we last read it.
        """
        with self.config_lock:
            fresh = config.load()
            apply(fresh)
            if not config.save(fresh):
                log(f"config not saved: {config.last_error}")
            self.config = fresh
            return fresh

    def select_bank(self, name):
        """Switch the active binding bank and light its M-key."""
        self.mutate(lambda settings: config.set_bank(settings, name))
        self.show_bank_led()
        log(f"bank -> M{name}")
        return str(name)

    def show_bank_led(self):
        active = str(self.config.get("active_bank", "1"))
        with self.lock:
            if self.keyboard is not None:
                self.keyboard.set_mkeys([f"m{active}"])

    def advance_screen(self, step=1):
        """Move to the next screen in the configured cycle."""
        lcd = self.config.setdefault("lcd", {})
        cycle = [name for name in lcd.get("cycle") or [] if name in screens.SCREENS]
        if not cycle:
            cycle = list(screens.SCREENS)
        if not lcd.get("enabled", True):
            lcd["enabled"] = True
            current = cycle[0]
        else:
            try:
                index = cycle.index(lcd.get("screen", cycle[0]))
            except ValueError:
                index = -1
            current = cycle[(index + step) % len(cycle)]
        def apply(settings):
            target = settings.setdefault("lcd", {})
            target["screen"], target["enabled"] = current, True

        self.mutate(apply)
        self.last_lcd = 0.0
        log(f"LCD -> {current}")
        self.publish("screen", {"screen": current})
        return current

    def fire_mode_key(self, name):
        """M1/M2/M3 pick a bank, MR drives recording, GAME is the switch."""
        try:
            self._run_mode_key(name)
        except OSError:
            raise
        except Exception as exc:
            log(f"{name} failed: {type(exc).__name__}: {exc}")

    def _run_mode_key(self, name):
        if name in ("M1", "M2", "M3"):
            if self.record_state:
                return
            self.select_bank(name[1])
        elif name == "MR":
            self.toggle_recording()
        elif name == "GAME":
            self.fire_game_switch(True)

    def fire_game_switch(self, engaged):
        """Run whatever the joystick switch is configured to do."""
        log(f"game switch {'on' if engaged else 'off'}")
        try:
            self._run_game_switch(engaged)
        except OSError:
            raise
        except Exception as exc:
            log(f"  game switch failed: {type(exc).__name__}: {exc}")

    def _run_game_switch(self, engaged):
        action = (self.config.get("game_switch") or {}).get(
            "on" if engaged else "off")
        if not action:
            return
        if isinstance(action, dict):
            actions.dispatch(action, self.config.get("macros"),
                             hooks=self.hooks)
            return
        if not isinstance(action, str):
            log(f"  {action!r} is not a valid action")
            return
        if action.startswith("bank:"):
            self.select_bank(action.split(":", 1)[1])
        elif action.startswith("screen:"):
            wanted = action.split(":", 1)[1]
            if wanted in screens.SCREENS:
                self.mutate(lambda s: s.setdefault("lcd", {}).update(
                    screen=wanted, enabled=True))
                self.last_lcd = 0.0
        elif action.startswith("color:"):
            try:
                rgb = config.apply_brightness(
                    config.parse_color(action.split(":", 1)[1]),
                    self.config.get("brightness", 100))
                with self.lock:
                    self.keyboard.set_backlight(rgb)
            except config.ColorError:
                pass

    def toggle_recording(self):
        """MR: arm, then record onto the next G-key, then finish."""
        if self.record_state is None:
            if not actions.can_post_events():
                log("MR pressed but Accessibility is not granted")
                return
            with self.record_lock:
                self.record_state = "await_key"
                self.record_target = None
            self.last_lcd = 0.0
            log("MR -> waiting for a G-key")
        elif self.record_state == "await_key":
            with self.record_lock:
                self.record_state = None
            self.last_lcd = 0.0
            log("MR -> recording cancelled")
        else:
            self.finish_recording()

    def start_recording(self, key):
        active = recorder.Recorder()
        with self.record_lock:
            self.record_target = key
            self.record_state = "recording"
            self.recorder = active
        self.last_lcd = 0.0
        log(f"recording macro onto {key}")
        thread = threading.Thread(target=self._record_thread, args=(active,),
                                  daemon=True)
        thread.start()

    def _record_thread(self, active):
        """Run the event tap. `active` is passed in rather than read off self,
        which the main loop may have cleared by the time this runs."""
        try:
            active.run(timeout=300.0)
        except recorder.RecordingError as exc:
            log(f"recording failed: {exc}")
        except Exception as exc:
            log(f"recording failed: {type(exc).__name__}: {exc}")
        finally:
            self.finish_recording()

    def finish_recording(self):
        """Save whatever was captured. Safe to call from either thread, and
        from both: the second call finds nothing to do."""
        with self.record_lock:
            if self.record_state != "recording":
                return
            active, self.recorder = self.recorder, None
            key, self.record_target = self.record_target, None
            self.record_state = None
        steps = list(active.steps) if active else []
        if active:
            active.stop()
        self.last_lcd = 0.0
        if not steps or not key:
            log("recording discarded (nothing captured)")
            return
        name = f"{key.lower()}-m{self.config.get('active_bank', '1')}"

        def apply(settings):
            settings.setdefault("macros", {})[name] = steps
            settings.setdefault("bindings", {})[key] = {
                "type": "macro", "name": name}

        self.mutate(apply)
        log(f"{key} -> macro {name!r} ({len(steps)} steps)")

    def fire_lcd_key(self, name):
        """Run whatever the display key is configured to do."""
        try:
            self._run_lcd_key(name)
        except OSError:
            raise                 # device trouble belongs to the main loop
        except Exception as exc:
            log(f"{name} failed: {type(exc).__name__}: {exc}")

    def _run_lcd_key(self, name):
        action = (self.config.get("lcd_keys") or {}).get(name)
        if not action:
            return
        if isinstance(action, dict):
            actions.dispatch(action, self.config.get("macros"),
                             hooks=self.hooks)
            return
        if not isinstance(action, str):
            log(f"{name}: {action!r} is not a valid action")
            return
        if action == "next":
            self.advance_screen(1)
        elif action == "prev":
            self.advance_screen(-1)
        elif action == "off":
            self.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
            with self.lock:
                self.keyboard.clear_lcd()
            log(f"{name} -> LCD off")
        elif action.startswith("screen:"):
            wanted = action.split(":", 1)[1]
            if wanted in screens.SCREENS:
                self.mutate(lambda s: s.setdefault("lcd", {}).update(
                    screen=wanted, enabled=True))
                self.last_lcd = 0.0
                log(f"{name} -> LCD {wanted}")

    def fire(self, name):
        """Run a G-key's binding.

        The config is meant to be hand-edited, so a binding can be any shape
        at all - a bare string, a dict missing its field, a macro that is a
        list of strings. None of those may kill the agent, so everything from
        describing the binding onwards is guarded.
        """
        self.state["last_gkey"] = name
        try:
            binding = self.bindings.get(name)
            self.state["last_action"] = actions.describe(binding)
            if not binding:
                log(f"{name} pressed (unbound)")
                return
            log(f"{name} -> {actions.describe(binding)}")
            actions.dispatch(binding, self.config.get("macros"),
                             hooks=self.hooks)
        except Exception as exc:
            log(f"  {name} failed: {type(exc).__name__}: {exc}")

    def _policy(self):
        """The config as autoscreen reads it, with the user's triggers in it.

        A trigger with no `seconds` is a rule that holds a screen while its
        conditions hold - so it is handed to autoscreen rather than judged a
        second time: one engine, one hold, one hands-off window, one fallback.
        It goes in first, so a cause the user wrote by hand is not shadowed
        by a broad rule sitting above it.

        Only the `autoscreen` block is rebuilt; the rest of the config is
        handed over as it is, and nothing is written anywhere.
        """
        held = triggers.state_rules(self.config)
        if not held:
            return self.config
        settings = dict(self.config)
        block = dict(self.config.get("autoscreen") or {})
        block["rules"] = list(held) + list(block.get("rules") or [])
        settings["autoscreen"] = block
        return settings

    def refresh_autoscreen(self, now):
        """Let the rules pick the screen, if one of them has an opinion.

        The answer is written straight into `lcd.screen`, like any other
        switch - which is also what makes a switch by somebody else visible:
        the screen no longer being the one autoscreen put up is how it knows
        a hand, a soft key or the CLI got there first.
        """
        current = (self.config.get("lcd") or {}).get("screen", "status")
        wanted = self.autoscreen.update(now, self._policy(), current)
        if not wanted:
            return
        if wanted == "off":
            self.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
            self.autoscreen.rest()
            with self.lock:
                self.keyboard.clear_lcd()
            log("autoscreen -> off")
            return
        self.mutate(lambda s: s.setdefault("lcd", {}).update(screen=wanted,
                                                             enabled=True))
        self.last_lcd = 0.0
        log(f"autoscreen -> {wanted} ({self.autoscreen.rule})")

    def refresh_alerts(self, now):
        """Turn what just happened into something the display can show."""
        block = self.config.get("timers") or {}
        for item in self.timers.due(now):
            self.alerts.push("timeralert", self._timer_data("done", item),
                             float(block.get("show_seconds", 10) or 0),
                             block.get("sound"))
            log(f"timer {timers.left(item.get('seconds') or 0)} is up"
                + (f" ({item.get('label')})" if item.get("label") else ""))
            # Published where it happens rather than where it was set:
            # "a countdown finished" is the thing worth reacting to, and
            # it has no key attached to it.
            self.publish("timer", self._timer_data("done", item))
        if self.watcher is None:
            return
        note = self.config.get("notifications") or {}
        if not note.get("enabled", True):
            return
        for event in self.watcher.drain():
            data = self._message_data(event)
            # A source may say how long it wants the panel for. The store's
            # banners do: they are a system-wide announcement rather than a
            # line in a conversation, and they arrive seconds late already,
            # so they are given their own number rather than borrowing the
            # chat reader's.
            seconds = event.get("seconds")
            if seconds is None:
                seconds = note.get("show_seconds", 10)
            self.alerts.push("message", data, float(seconds or 0),
                             note.get("sound"))
            log(f"{data['app']}: "
                + (f"{data['author']}: {data['text']}" if data["author"]
                   else data["text"]))

    def show_trigger(self, event):
        """Put a trigger's screen up for as long as it asked for.

        An alert borrows the display rather than switching it, so what was
        underneath goes on being chosen and is what comes back - which is
        also how a hand-picked screen survives being interrupted.
        """
        self.alerts.push(event["screen"], event.get("data") or {},
                         event["seconds"], event.get("sound") or None)
        log(f"trigger {event['name']} -> {event['screen']} "
            f"for {event['seconds']:g}s")

    def refresh_triggers(self, now):
        """Anything whose condition has just come true, shown now."""
        for event in self.triggers.update(now, self.config):
            self.show_trigger(event)

    def trigger_command(self, payload):
        """What `g510 trigger ...` asks the agent to do."""
        action = str(payload.get("action") or "list")
        block = self.config.get("triggers") or {}
        if action == "list":
            return {"ok": True,
                    "enabled": bool(block.get("enabled", True)),
                    "rules": [rule for rule in (block.get("rules") or [])
                              if isinstance(rule, dict)]}
        if action == "fire":
            name = str(payload.get("name") or "")
            try:
                event = self.triggers.fire(self.config, name)
            except KeyError:
                return {"ok": False,
                        "error": f"no trigger called {name!r} - "
                                 "see: g510 trigger list"}
            self.show_trigger(event)
            return {"ok": True, "fired": event["name"],
                    "screen": event["screen"], "seconds": event["seconds"]}
        return {"ok": False, "error": f"unknown trigger action {action!r}"}

    # -- the menu on the panel -------------------------------------------

    def ensure_tap(self):
        """The event tap the menu navigates with, started the first time."""
        if self.keytap is not None and self.keytap.tap is not None:
            return True
        if self.keytap is None:
            self.keytap = keytap.KeyTap(decide=self.wants_menu_key, log=log)
        if not self.keytap.start():
            log(f"menu cannot take keys: {self.keytap.error}")
            return False
        return True

    def wants_menu_key(self, name, character):
        """Whether a key belongs to the panel right now.

        Called on the tap's own thread, so this reads flags and nothing
        else. The menu is modal while it is open: the navigation keys are
        always its, and a printable character only while a text field is
        being typed into - a letter pressed at a list still goes to the app
        underneath. Writing a note is modal the other way round: the note
        takes everything until enter or esc, because there is no list to be
        sitting at. The Discord screen and the mixer are narrower than
        either: only the keys their rows have a use for, so a game can
        carry on being played at with the letters while the arrows pick a
        channel or a volume - until a compose starts, which is a note's
        rule and takes the lot.
        """
        if not self.menu.closed:
            if name in keytap.NAV_KEYS:
                return True
            return bool(character) and self.menu_typing
        if self.note_draft is not None:
            return name in ("enter", "esc", "backspace") or bool(character)
        if self.browse.active:
            return self.browse.wants(name, character)
        return self.mixer.wants(name, character)

    def open_menu(self):
        with self.menu_lock:
            if not self.menu.closed:
                return True
            if not self.ensure_tap():
                return False
            self.menu.open()
            self.menu_touch = time.time()
            self.menu_typing = False
            self.last_lcd = 0.0
            log("menu open")
            return True

    def close_menu(self):
        with self.menu_lock:
            if self.menu.closed:
                return
            self.menu.close()
            self.menu_typing = False
            self.last_lcd = 0.0
            log("menu closed")

    def press_menu(self, name):
        """One key for the menu, and everything that changes about the frame."""
        with self.menu_lock:
            was_open = not self.menu.closed
            self.menu.press(name)
            self.menu_touch = time.time()
            view = self.menu.view()
            editing = view.get("editing") or {}
            self.menu_typing = bool(editing) and editing.get("kind") == "text"
            if was_open and self.menu.closed:
                log("menu closed")
            # The menu answers a line at a time and should answer as fast
            # as a key does, whatever `lcd.refresh_seconds` is set to.
            self.last_lcd = 0.0

    def refresh_menu(self, now):
        """Keys the tap took: the menu's while it is open, the note's while
        one is being written, the Discord screen's while it is being
        driven, the mixer's while it is up, and nobody's otherwise."""
        with self.menu_lock:
            if self.keytap is not None:
                keys = self.keytap.drain()
                if not self.menu.closed:
                    for name, _character in keys:
                        self.press_menu(name)
                        if self.menu.closed:
                            break
                elif self.note_draft is not None:
                    for name, character in keys:
                        self.feed_note(name, character)
                elif self.browse.active:
                    for name, character in keys:
                        self.feed_browse(name, character)
                        if not self.browse.active:
                            break                   # esc let the keys go
                elif self.mixer.active:
                    for name, character in keys:
                        self.feed_mixer(name, character)
                        if not self.mixer.active:
                            break                   # esc let the keys go
            if not self.menu.closed and now - self.menu_touch >= MENU_IDLE:
                self.close_menu()
            if self.note_draft is not None and now - self.note_touch >= NOTE_IDLE:
                self.note_draft = None
                self.last_lcd = 0.0
                log("note entry timed out")
            if self.mixer.active and now - self.mixer.idle >= MIXER_IDLE:
                # A mixer nobody is at must not go on holding the
                # navigation keys away from the game underneath it, for
                # the same reason the menu closes itself.
                self.close_mixer("nobody touched it")
            if self.browse.typing and now - self.browse.touch >= browse.IDLE_COMPOSE:
                # Nobody has gone on with the message: back to the
                # messages it was answering, keys and all. The draft is
                # dropped for the reason the note's is - a pane that is
                # not being written into must not hold a whole keyboard
                # away from a game.
                self.browse.abandon()
                self.last_lcd = 0.0
                log("discord compose timed out")

    # -- the sticky note (E3) ---------------------------------------------

    @property
    def note_active(self):
        """A note is up: either pinned, or waiting to be finished."""
        return self.note is not None or self.note_draft is not None

    def toggle_note(self):
        """Three presses of L5: write a note, or take down the one that is.

        Taking it down is what hands the display back to the four screens
        the soft keys pick, so nothing else has to be undone with it.
        """
        with self.menu_lock:
            if self.note_active:
                self.note = None
                self.note_draft = None
                log("note cleared")
            else:
                if not self.ensure_tap():
                    log("note cannot take keys: "
                        + (self.keytap.error if self.keytap else "no event tap"))
                    return False
                if not self.menu.closed:
                    self.close_menu()
                if self.browse.active:
                    # The note takes the panel, and it cannot be shown
                    # over a screen whose keys it does not have: the
                    # browser lets go first, the way the menu does.
                    self.close_browse("a note took the panel")
                if self.mixer.active:
                    # And so does the mixer, for the same reason - a row
                    # nobody can see would be a row nobody could pick.
                    self.close_mixer("a note took the panel")
                self.note_draft = ""
                self.note_touch = time.time()
                log("note: type it, enter pins it, esc cancels")
            self.last_lcd = 0.0
        return True

    def pick_discord_screen(self):
        """Put the Discord screen up, the way picking a screen by hand is.

        It is a screen like any other in that `lcd.screen` names it, so
        picking it by hand is a pick: autoscreen leaves it alone for
        `manual_seconds` afterwards, exactly as `g510 lcd discord` would.
        """
        def apply(settings):
            target = settings.setdefault("lcd", {})
            target["screen"], target["enabled"] = "discord", True

        self.mutate(apply)
        self.last_lcd = 0.0
        log("LCD -> discord")
        return "discord"

    def show_discord(self):
        """Three presses of L3: the Discord screen, and the keys to drive it.

        The screen on its own would still be the notification P6 shipped.
        The point of the gesture now is that the browser is ready when the
        finger comes off, so a channel can be picked and a message
        answered without ever going near Discord's window - which is the
        only reason to have it on a panel this size.
        """
        with self.menu_lock:
            self.pick_discord_screen()
            self.start_browse()
        return "discord"

    def start_browse(self):
        """Take the keyboard for the Discord screen.

        The screen comes up first if it is not up already: keys going to
        a pane nobody can see would be keys going nowhere. Two things are
        refused rather than taken over invisibly - a pinned note and a
        recording, both of which own the display more than this does -
        and each says why, because a gesture that does nothing at all is
        one the user presses again and again.
        """
        with self.menu_lock:
            lcd_config = self.config.get("lcd") or {}
            if lcd_config.get("screen") != "discord" \
                    or not lcd_config.get("enabled", True):
                self.pick_discord_screen()
            if self.record_state:
                log("discord browse refused: something is recording")
                return False
            if self.note is not None:
                log("discord browse refused: a note is pinned - clear it "
                    "first with: g510 note clear")
                return False
            if not self.ensure_tap():
                log("discord browse cannot take keys: "
                    + (self.keytap.error if self.keytap else "no event tap"))
                return False
            if not self.menu.closed:
                self.close_menu()
            if self.mixer.active:
                self.close_mixer("the browser took the panel")
            if self.browse.active:
                return True
            self.browse.open()
            self.last_lcd = 0.0
            log("discord browse on - arrows pick, enter opens, esc lets go")
            return True

    def close_browse(self, reason=""):
        """Hand the keys back to whatever else wants them."""
        with self.menu_lock:
            if not self.browse.active:
                return False
            self.browse.close()
            self.last_lcd = 0.0
            log("discord browse off" + (f" - {reason}" if reason else ""))
            return True

    def feed_browse(self, name, character):
        """One key the tap took while the Discord screen is being driven."""
        with self.menu_lock:
            state = self.link.state() if self.link is not None else {}
            now = time.time()
            self.browse.update(state, now)
            for action in self.browse.press(name, character, now):
                self.run_browse(action, now)
            if self.browse.active:
                self.last_lcd = 0.0

    def run_browse(self, action, now=None):
        """One thing the browser asked for, on its way to the plugin.

        The pane queues *what* and decides nothing about how: the link,
        the connection check and the log all live here, so wanting a
        channel is a line in the log and, when the plugin is not there, a
        reason on the panel rather than silence.
        """
        kind = str(action[0])
        payload = {"action": kind}
        if kind in ("open", "list"):
            payload["name"] = str(action[1])
        elif kind == "older":
            payload["limit"] = int(action[1])
        elif kind == "suggest":
            payload["text"] = str(action[1])
        elif kind == "say":
            payload["text"] = str(action[1])
        elif kind == "reply":
            payload["id"], payload["text"] = str(action[1]), str(action[2])
        else:
            return
        reply = self.vencord_command(payload) or {}
        if not reply.get("ok"):
            problem = str(reply.get("error") or "that did not work")
            log(f"discord {kind}: {problem}")
            self.browse.set_error(problem, now)
            return
        if kind == "suggest":
            return                      # one log line per keystroke is noise
        detail = str(payload.get("name") or payload.get("id")
                     or payload.get("limit") or "")
        log(f"discord {kind}" + (f" {detail}" if detail else ""))
        if kind in ("say", "reply"):
            # Whose answer matters: the next ack with this number or past
            # it is the one saying whether the message went out.
            self.browse.sent(reply.get("n"), now)

    # -- the mixer (P7) -----------------------------------------------------

    def start_mixer(self):
        """Bring the mixer up - what holding L4 asks for.

        The same answer as `press_menu` is not: esc lets go of this one,
        so a second hold would just be a hold at nothing. What is owed
        instead is a scan for apps that are making sound, and that runs
        on a worker because making the taps has been seen to take eleven
        seconds on this machine while this loop holds the only handle to
        the keyboard (P5).
        """
        with self.menu_lock:
            if not self.ensure_tap():
                # Without it the arrows are never taken, so the panel
                # would be a picture that cannot be moved - which is the
                # one thing it is for.
                log("mixer cannot take keys: "
                    + (self.keytap.error if self.keytap else "no event tap"))
                return False
            if not self.menu.closed:
                self.close_menu()
            if self.note_draft is not None:
                self.note_draft = None
            if self.mixer.active:
                self.mixer.touch = time.time()
                return True
            self.mixer.open()
            self.mixer_generation += 1
            self.mixer_tapped = ()
            self.mixer_levels = None
            self.mixer_building = False
            self.mixer_seen = 0.0
            self.mixer_poll = 0.0
            self.last_lcd = 0.0
            log("mixer on - arrows pick, left right volume, esc lets go")
        self.refresh_mixer(time.time())
        return True

    def close_mixer(self, why):
        """Let the mixer go, and the taps with it."""
        with self.menu_lock:
            if not self.mixer.active and self.mixer_levels is None \
                    and not self.mixer_building:
                return
            self.mixer.close()
            # Bumped first: a worker still opening the taps will see the
            # new number and drop what it made rather than leave an
            # aggregate device listening in the background for ever.
            self.mixer_generation += 1
            levels, self.mixer_levels = self.mixer_levels, None
            self.mixer_building = False
            self.mixer_tapped = ()
            self.last_lcd = 0.0
            log(f"mixer off - {why}")
        if levels is not None:
            self._stop_mixer_levels(levels)

    @staticmethod
    def _stop_mixer_levels(levels):
        """Close a tap off the poll thread: stopping a device has been
        seen to block, and this loop holds the only handle to the
        keyboard."""
        threading.Thread(target=levels.stop, daemon=True,
                         name="mixer-stop").start()

    def feed_mixer(self, name, character=None):
        """One key the tap took, while the mixer has the panel."""
        with self.menu_lock:
            if not self.mixer.active:
                return
            for action in self.mixer.press(name, character):
                self._mixer_action(action)
            # Every key draws: picking a row moves only a `>`, and
            # waiting the normal second for the panel to say so makes
            # the keys feel like they were not heard.
            self.last_lcd = 0.0

    def _mixer_action(self, action):
        """Run what a key asked for. The keys only ever *ask*: this is
        the one place that touches CoreAudio's volume properties, so a
        refused write lands here rather than in the panel."""
        if not action:
            return
        kind = action[0]
        if kind == "close":
            self.close_mixer("esc")
        elif kind == "volume":
            self._mixer_volume(str(action[1]), int(action[2]))
        elif kind == "mute":
            self._mixer_mute()

    def _mixer_volume(self, key, delta):
        if key == mixer.SYSTEM:
            # Straight property writes: microseconds, so this can be
            # done where it was asked for and the number shows up on
            # the panel in the same breath.
            current = audio.volume()
            if current is None:
                return
            value = audio.set_volume((current * 100.0 + delta) / 100.0)
            if value is not None:
                self.mixer.set_volume(mixer.SYSTEM, value * 100.0)
                self.last_lcd = 0.0
        else:
            # An app's own volume is an AppleScript round trip with a
            # three second timeout (see `audio.app_volume`), and this
            # thread holds the only handle to the keyboard - so Music
            # being wedged gets to be slow over there instead of
            # freezing every key for three seconds at a time. Queued
            # behind one worker so two presses cannot both read the
            # same old volume and lose one of the writes.
            self._mixer_async(lambda: self._set_app_volume(key, delta))

    def _set_app_volume(self, key, delta):
        value = audio.change_app_volume(key, delta)
        if value is not None:
            with self.menu_lock:
                if self.mixer.set_volume(key, value):
                    self.last_lcd = 0.0

    def _mixer_mute(self):
        on = not bool(audio.muted() or False)
        if audio.set_muted(on) is not None:
            self.mixer.set_muted(on)
            self.last_lcd = 0.0

    def _mixer_async(self, job):
        """Queue `job` on the mixer's one worker, or run it if there is
        no worker yet - the first press makes it.

        One thread and a queue rather than a thread per press: the jobs
        have to happen in the order they were asked for, or a second
        left-press could write the volume the first one just read.
        """
        with self.menu_lock:
            if self.mixer_jobs is None:
                # One worker for the life of the agent, and a daemon
                # thread: it goes with the process rather than having to
                # be told to stop.
                self.mixer_jobs = queue.Queue()
                threading.Thread(target=self._mixer_worker, daemon=True,
                                 name="mixer-jobs").start()
            self.mixer_jobs.put(job)

    def _mixer_worker(self):
        jobs = self.mixer_jobs
        while True:
            try:
                jobs.get()()
            except Exception as exc:
                log(f"mixer: {exc}")

    def _spawn_mixer(self, generation, apps):
        """Open the taps for `apps`, on a thread of its own.

        The generation is the whole point: `AudioDeviceCreateIOProcID`
        has been seen to take eleven seconds, and esc can be pressed
        while it does. A worker that comes back to a newer number knows
        the panel is gone and closes what it opened instead of leaving
        an aggregate device running with nothing watching it.
        """
        def job():
            self._mixer_job(generation, apps)
        self.mixer_building = True
        threading.Thread(target=job, daemon=True, name="mixer-open").start()

    def _mixer_job(self, generation, apps):
        levels = audio.Levels(log=log)
        try:
            opened = levels.start(apps)
        except Exception as exc:
            log(f"mixer: the taps could not be opened: {exc}")
            opened = False
        with self.menu_lock:
            if generation != self.mixer_generation or not self.mixer.active:
                stale = True
            else:
                stale = False
                self.mixer_building = False
                if opened:
                    self.mixer_levels = levels
                else:
                    # One attempt per set of pids, and no retry loop: an
                    # app that will not be tapped is a fact about that
                    # app, and asking again a second later would be a
                    # spin on the poll loop's own thread.
                    self.mixer_tapped = tuple(
                        int(app.get("pid") or 0) for app in apps)
        if stale:
            levels.stop()
            return
        self.last_lcd = 0.0

    def refresh_mixer(self, now):
        """One pass over the mixer while it is up, from the poll loop.

        Only the levels are read here - a lock and a dict copy, and a
        redraw only when a bar actually moved. *Finding* the apps is
        left to a worker, because that asks NSWorkspace what is running
        and Music what its volume is, and the second of those is an
        AppleScript that can wait three seconds on a wedged app: this
        loop holds the only handle to the keyboard (P5).
        """
        with self.menu_lock:
            if not self.mixer.active:
                return
            levels = self.mixer_levels
            if levels is not None and now - self.mixer_poll >= MIXER_POLL:
                self.mixer_poll = now
                if self.mixer.set_levels(levels.poll()):
                    self.last_lcd = 0.0
            if now - self.mixer_seen < MIXER_RESCAN or self.mixer_building:
                return
            self.mixer_seen = now
            generation = self.mixer_generation
        self._scan_async(generation)

    def _scan_async(self, generation):
        """Find what is playing, on a thread of its own."""
        threading.Thread(target=self._mixer_scan, args=(generation,),
                         daemon=True, name="mixer-scan").start()

    def _mixer_scan(self, generation):
        """What is making sound, then the taps for exactly that.

        The taps are rebuilt here rather than in the poll loop for the
        same reason they are opened on a thread: an app that starts
        playing has to be added to the aggregate, and opening one has
        been seen to take eleven seconds on this machine.
        """
        apps = audio.candidates()
        volume, muted = audio.volume(), audio.muted()
        with self.menu_lock:
            if generation != self.mixer_generation or not self.mixer.active:
                return
            self.mixer.set_system(volume, muted)
            self.mixer.set_apps(apps)
            wanted = tuple(int(app.get("pid") or 0) for app in apps)
            if wanted != self.mixer_tapped and not self.mixer_building:
                # The apps changed - something new started playing, or an
                # old one quit - so the taps have to be rebuilt around
                # them. `mixer_tapped` is written before the worker is
                # even started, so the next scan a second later does not
                # ask for the same thing again while the last one is
                # still opening.
                self.mixer_tapped = wanted
                self._spawn_mixer(generation, apps)
                names = ", ".join(str(app.get("name") or "?")
                                  for app in apps) or "nothing"
                log(f"mixer: hearing {names}")
            self.last_lcd = 0.0

    def mixer_command(self, payload):
        """What `g510 mixer [on|off]` asks the agent to do."""
        action = str(payload.get("action") or "show")
        with self.menu_lock:
            if action in ("on", "open", "show"):
                if not self.start_mixer():
                    return {"ok": False,
                            "error": "the mixer cannot take keys - the "
                                     "event tap would not start (check "
                                     "Input Monitoring)"}
            elif action in ("off", "close"):
                self.close_mixer("the command said so")
            return {"ok": True, "active": self.mixer.active}

    def vencord_command(self, payload):
        """What `g510 discord ...` asks of the plugin behind the screen.

        Nothing here can read Discord: the plugin can, and it says what it
        found over the link. So this side matches a name to an id against
        the last thing that was pushed, passes the command on, and lets the
        next push be the proof it ran.
        """
        action = str(payload.get("action") or "state")
        status = self.link.status() if self.link is not None else {
            "connected": False, "error": "the link is off",
            "state": {}, "waiting": 0, "port": 0, "seen": None}
        if action == "state":
            return {"ok": True, **status}
        if action == "browse":
            # Whether the keys belong to the screen is this process's own
            # runtime state - the config file and the plugin both know
            # nothing about it - so starting it and reporting it happen
            # here, before any connection to Discord is asked about.
            want = payload.get("on")
            if want is None:
                return {"ok": True, "active": self.browse.active}
            if bool(want):
                return {"ok": True, "active": self.start_browse()}
            self.close_browse("asked for")
            return {"ok": True, "active": False}
        if not status.get("connected"):
            # The plugin pushes once a second while Discord is running, so
            # "not seen for a while" is the whole diagnosis.
            return {"ok": False, "error": "the Vencord plugin has not "
                    "connected - is Vesktop running with the G510 "
                    "plugin enabled?", "status": status}
        if action == "open":
            wanted = str(payload.get("name") or "").strip()
            if wanted.lower() in ("@me", "friends", "dms"):
                # The list of DMs rather than a channel - what the panel
                # shows as "nothing open" - and a route, not an id.
                number = self.link.send({"cmd": "open", "id": "@me"})
                return {"ok": True, "n": number,
                        "channel": {"id": "@me", "name": "friends"},
                        "status": status}
            channels = (status.get("state") or {}).get("channels") or []
            found = _find_channel(channels, wanted)
            if isinstance(found, str):
                return {"ok": False, "error": found, "status": status}
            number = self.link.send({"cmd": "open", "id": found["id"]})
            return {"ok": True, "n": number, "channel": found,
                    "status": status}
        if action == "older":
            number = self.link.send(
                {"cmd": "older", "limit": int(payload.get("limit") or 30)})
            return {"ok": True, "n": number, "status": status}
        if action == "say":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "no text given"}
            number = self.link.send({"cmd": "say", "text": text})
            return {"ok": True, "n": number, "status": status}
        if action == "reply":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "no text given"}
            messages = (status.get("state") or {}).get("messages") or []
            # The word off the command line - `last`, or the reference
            # `history` printed - is resolved here, because this is the
            # side that holds the messages. The plugin is only ever given
            # an id it can put in a reply reference.
            target, problem = link.resolve(messages, payload.get("id"))
            if problem:
                return {"ok": False, "error": problem, "status": status}
            number = self.link.send({"cmd": "reply", "id": target,
                                     "text": text})
            return {"ok": True, "n": number, "status": status}
        if action == "list":
            # Which list of channels the panel walks: the DMs or one
            # server. The client's own view does not move - a server can
            # be read here without Discord being dragged over to it.
            wanted = str(payload.get("name") or "").strip()
            if not wanted:
                return {"ok": False, "error": "no list given"}
            number = self.link.send({"cmd": "list", "id": wanted})
            return {"ok": True, "n": number, "status": status}
        if action == "suggest":
            # Whoever fits the word being written - people, roles, and
            # @everyone - asked for one word at a time as it is typed.
            number = self.link.send(
                {"cmd": "suggest", "text": str(payload.get("text") or "")})
            return {"ok": True, "n": number, "status": status}
        return {"ok": False, "error": f"unknown action {action!r}. "
                "Try: state open older say reply list suggest browse"}

    def feed_note(self, name, character):
        """One key while a note is being written. `enter` pins it."""
        self.note_touch = time.time()
        if name == "enter":
            text = (self.note_draft or "").strip()
            self.note_draft = None
            self.note = text[:NOTE_MAX] or None
            if self.note:
                log(f"note pinned: {self.note}")
            self.last_lcd = 0.0
            return
        if name == "esc":
            self.note_draft = None
            self.last_lcd = 0.0
            log("note cancelled")
            return
        if name == "backspace":
            self.note_draft = (self.note_draft or "")[:-1]
        elif character and len(self.note_draft or "") < NOTE_MAX:
            self.note_draft = (self.note_draft or "") + character
        self.last_lcd = 0.0

    def note_command(self, payload):
        """What `g510 note ...` asks the agent to do.

        The same note the triple press writes, for when the text is easier
        to type at a shell than on the panel.
        """
        action = str(payload.get("action") or "show")
        with self.menu_lock:
            if action in ("set", "pin"):
                text = str(payload.get("text") or "").strip()
                if not text:
                    return {"ok": False, "error": "no text given"}
                self.note_draft = None
                self.note = text[:NOTE_MAX]
                self.last_lcd = 0.0
                log(f"note pinned: {self.note}")
            elif action in ("clear", "off"):
                self.note = None
                self.note_draft = None
                self.last_lcd = 0.0
                log("note cleared")
            return {"ok": True, "note": self.note,
                    "typing": self.note_draft is not None}

    def autoscreen_command(self, payload):
        """Freeze and thaw on request: `g510 autoscreen freeze|unfreeze`.

        The freeze lives in the agent, so this is the only place that can
        answer for it - the config file says nothing about it.
        """
        action = str(payload.get("action") or "status")
        if action == "freeze":
            self.autoscreen.freeze(True)
            self.last_lcd = 0.0
            log("autoscreen frozen")
        elif action == "unfreeze":
            self.autoscreen.freeze(False)
            self.last_lcd = 0.0
            log("autoscreen unfrozen")
        elif action == "toggle":
            self.toggle_freeze()
        return {"ok": True, "frozen": bool(self.autoscreen.frozen),
                "enabled": bool((self.config.get("autoscreen") or {})
                                .get("enabled", True))}

    def save_config_change(self, apply):
        """Write one change and re-apply what it drives.

        Re-reading keeps whatever the CLI or the GUI wrote while the menu
        was open, and re-applying is what `reload_config` already does for
        them: the font, the backlight and the chat reader all read the
        config object rather than the file, so a change is not real until
        they have seen it.
        """
        with self.config_lock:
            fresh = config.load()
            apply(fresh)
            if not config.save(fresh):
                log(f"config not saved: {config.last_error}")
        self.reload_config()
        self.last_lcd = 0.0

    def menu_action(self, name, arg=None):
        """Everything the menu can ask for that is not a config value."""
        try:
            return self._menu_action(name, arg)
        except (OSError, ValueError, KeyError, TypeError,
                config.ColorError) as exc:
            log(f"menu {name} failed: {type(exc).__name__}: {exc}")
            return f"{name} failed: {exc}"

    def _menu_action(self, name, arg=None):
        if name == "leave":
            # Nothing to do here: the menu closes itself the moment this
            # returns (`Menu._select` calls `close()` right after), and
            # `press_menu` notices and logs it. Closing again from inside
            # the call would have the panel shut the same menu twice on one
            # press of Leave - and the second log reads as a second gesture.
            # The ways *out* that the panel owns - esc, the hold, the idle
            # timeout - all go through `close_menu()` themselves.
            return None
        if name == "next":
            return f"screen -> {self.advance_screen(1)}"
        if name == "bank":
            return f"bank {self.select_bank(arg)}"
        if name == "reload":
            self.reload_config()
            self.last_lcd = 0.0
            return "config reloaded"
        if name == "timer_start":
            reply = self.timer_command({"action": "start", "value": arg})
            if not reply.get("ok"):
                return str(reply.get("error") or "timer did not start")
            item = reply.get("timer") or {}
            return (f"{item.get('label') or arg}: "
                    f"{timers.left(item.get('seconds') or 0)}")
        if name == "timer_cancel":
            reply = self.timer_command({"action": "cancel", "value": "all"})
            gone = reply.get("cancelled") or []
            if not gone:
                return str(reply.get("error") or "no timer is running")
            return "cancelled " + ", ".join(
                str(item.get("label") or item.get("name") or "timer")
                for item in gone)
        if name == "trigger_fire":
            reply = self.trigger_command({"action": "fire", "name": arg})
            if not reply.get("ok"):
                return str(reply.get("error") or "did not fire")
            return f"fired {reply['fired']} -> {reply['screen']}"
        if name == "poweron":
            rgb = config.parse_color(str(arg))
            with self.lock:
                if self.keyboard is None:
                    return "keyboard not connected"
                self.keyboard.set_power_on_backlight(rgb)
            return f"power-on -> {arg}"
        if name == "mkeys":
            with self.lock:
                if self.keyboard is None:
                    return "keyboard not connected"
                self.keyboard.set_mkeys([] if arg == "off" else [str(arg)])
            return f"M-keys -> {arg}"
        if name == "when_add":
            dotted = str(arg)
            self.save_config_change(
                lambda fresh: menu.add_when(fresh, dotted))
            return f"{dotted.rsplit(menu.SEP, 1)[-1]}: added"
        if name == "when_del":
            dotted = str(arg)
            self.save_config_change(
                lambda fresh: menu.del_when(fresh, dotted))
            return "condition removed"
        if name == "field_add":
            path = str(arg)
            self.save_config_change(lambda fresh: menu.add_field(fresh, path))
            return f"{path.rsplit(menu.SEP, 1)[-1]}: set"
        if name == "field_del":
            path = str(arg)
            self.save_config_change(lambda fresh: menu.del_field(fresh, path))
            return f"{path.rsplit(menu.SEP, 1)[-1]}: cleared"
        if name == "rule_add":
            path = str(arg)
            self.save_config_change(lambda fresh: menu.add_rule(fresh, path))
            return "rule added"
        if name == "rule_del":
            path = str(arg)
            self.save_config_change(lambda fresh: menu.drop(fresh, path))
            return "rule deleted"
        if name == "gauge_clear":
            self.save_config_change(
                lambda fresh: fresh.update({"gauges": []}))
            return "gauges cleared"
        if name == "gauge_add":
            return self._add_gauge(str(arg or ""))
        if name == "app_color_add":
            return self._add_app_colour(str(arg or ""))
        return f"unknown menu action {name!r}"

    def _add_gauge(self, raw):
        """A gauge from "label|command" - a command that prints a number."""
        label, sep, command = raw.partition("|")
        label, command = label.strip(), command.strip()
        if not sep or not label or not command:
            return "use: label|command"
        if len(self.config.get("gauges") or []) >= 3:
            return "three gauges is all the panel has room for"
        def apply(fresh):
            fresh.setdefault("gauges", []).append(
                {"label": label, "command": command})
        self.save_config_change(apply)
        return f"gauges: {label}"

    def _add_app_colour(self, raw):
        """A colour for one app from "app|colour"."""
        app, sep, colour = raw.partition("|")
        app, colour = app.strip(), colour.strip()
        if not sep or not app or not colour:
            return "use: app|colour"
        config.parse_color(colour)          # raises ColorError if it is not
        def apply(fresh):
            fresh.setdefault("app_colors", {})[app] = colour
        self.save_config_change(apply)
        return f"{app} -> {colour}"

    def menu_command(self, payload):
        """What `g510 menu ...` asks the agent to do."""
        action = str(payload.get("action") or "show")
        if action == "open":
            if not self.open_menu():
                return {"ok": False,
                        "error": (self.keytap.error
                                  if self.keytap else "no event tap")}
        elif action == "close":
            self.close_menu()
        elif action == "key":
            name = str(payload.get("name") or "")
            if not name:
                return {"ok": False, "error": "no key given"}
            if self.menu.closed:
                if name == "esc":
                    # Esc walks out of things; it does not open one.
                    return {"ok": True, "open": False}
                if not self.open_menu():
                    return {"ok": False,
                            "error": (self.keytap.error
                                      if self.keytap else "no event tap")}
            self.press_menu(name)
        reply = {"ok": True, "open": not self.menu.closed}
        if not self.menu.closed:
            reply["view"] = self.menu.view()
        return reply

    def refresh_lcd(self, now):
        lcd_config = self.config.get("lcd", {})
        if self.browse.active and (
                not lcd_config.get("enabled", True)
                or lcd_config.get("screen") != "discord"):
            # The browser holds the keyboard, so it may only ever be up
            # behind a panel it is actually showing. A soft key, the CLI
            # or a rule moving the display elsewhere is the display
            # saying it has something else to be - so the keys go in the
            # same breath, or a pane nobody can see would go on swallowing
            # arrows from a game.
            if lcd_config.get("enabled", True):
                self.close_browse("the display moved to another screen")
            else:
                self.close_browse("the display was switched off")
        menu_open = not self.menu.closed
        note = self.note_active
        browsing = self.browse.active
        mixing = self.mixer.active
        if not lcd_config.get("enabled", True) and not (menu_open or note
                                                        or mixing):
            # Blank: nothing to keep in step, so autoscreen forgets what it
            # last put up and takes the config as it finds it when the
            # display comes back. The menu is exempt - it is how the display
            # gets turned back on - and so is a note, because the key press
            # that put it there has to be visible while it is answered, and
            # so is the mixer for the same reason: bars nobody can see are
            # bars that may as well not be measured.
            # (A browser never gets here: it is closed above.)
            self.autoscreen.rest()
            return
        if self.record_state or menu_open or note or browsing or mixing:
            # MR while it records, the menu while it is open, a note
            # while it is up, the browser while it is being driven and the
            # mixer while it is up all own the display, so autoscreen
            # stands down rather than switching under them. Between them
            # they cover every moment, so the screen underneath is never
            # touched and simply comes back.
            self.autoscreen.rest()
        else:
            self.refresh_autoscreen(now)
            # autoscreen saves the config, which rebuilds it: this dict may
            # be one the reload just threw away, so ask again.
            lcd_config = self.config.get("lcd", {})
        # An interruption borrows the display without touching `lcd.screen`,
        # so the screen underneath goes on being chosen the whole time and
        # is simply what comes back when the interruption is over. An alert
        # is time-boxed and outranks a note: it rings, then the note is
        # still there.
        alert = None if (self.record_state or menu_open) else \
            self.alerts.current(now)
        if alert and browsing:
            # The browser is the one thing an alert must not borrow the
            # display from. It holds the keys precisely so a message can
            # be read, so a banner over the top would cover the very text
            # it was opened for: it waits the way it does behind the menu
            # and a recording, and because the queue prunes itself against
            # the moment it is next asked, a banner nobody could have seen
            # is gone by the time the keys come back rather than popping
            # up over a read that finished. Only the sight of it is given
            # up - the sound has rung already.
            if self.held_alert != alert["until"]:
                self.held_alert = alert["until"]
                log("banner held back - the browser has the keys")
            alert = None
        elif alert and mixing:
            # The same bargain with the mixer, for a slightly different
            # reason: it does not hold text that a banner would cover, it
            # holds *bars*, and a box over the top row for four seconds
            # is four seconds of the levels not being readable - which is
            # the entire thing the panel was opened for. Its own log
            # line, because "the browser has the keys" would be a lie
            # about which panel swallowed the arrows.
            if self.held_alert != alert["until"]:
                self.held_alert = alert["until"]
                log("banner held back - the mixer has the keys")
            alert = None
        if bool(alert) != self.alerting:
            self.alerting = bool(alert)
            self.last_lcd = 0.0          # one starts or ends: draw it now
        if now - self.last_lcd < float(lcd_config.get("refresh_seconds", 1.0)):
            return
        self.last_lcd = now
        # What the plugin last said, in reach of every screen below: the
        # Discord screen draws it, and any other screen ignores it.
        discord_state = self.link.state() if self.link else {}
        self.state["discord"] = discord_state
        if browsing:
            # One pass over the newest push here rather than on every key:
            # what the browser waits for - a channel opening, a list
            # arriving, an acknowledgement - has no key attached to it,
            # and a redraw is exactly often enough to notice it.
            self.browse.update(discord_state, now)
        if menu_open:
            with self.menu_lock:
                self.state["menu"] = self.menu.view()
            try:
                pixels = screens.render("menu", self.state)
            finally:
                self.state.pop("menu", None)
        elif self.record_state:
            self.state["record_state"] = self.record_state
            self.state["record_target"] = self.record_target
            active = self.recorder
            self.state["record_steps"] = len(active.steps) if active else 0
            pixels = screens.render("record", self.state)
        else:
            self.state.pop("record_state", None)
            if alert:
                self.state["alert"] = alert["data"]
                try:
                    pixels = screens.render(alert["screen"], self.state)
                finally:
                    self.state.pop("alert", None)
            elif note:
                # The note is what is on the panel until the same triple
                # press takes it down; the screen underneath is untouched.
                entry = self.note_draft is not None
                self.state["note"] = self.note_draft if entry else self.note
                self.state["note_entry"] = entry
                try:
                    pixels = screens.render("note", self.state)
                finally:
                    self.state.pop("note", None)
                    self.state.pop("note_entry", None)
            elif browsing:
                # The browser does not borrow the display the way an alert
                # does: it *is* the Discord screen, walked with keys, so
                # `lcd.screen` stays "discord" underneath it the whole
                # time and letting go of the keys is all it takes to be
                # back to reading it.
                self.state["browse"] = self.browse.view()
                try:
                    pixels = screens.render("discord", self.state)
                finally:
                    self.state.pop("browse", None)
            elif mixing:
                # An interruption, not a screen: the mixer does not
                # write `lcd.screen`, so whatever the display was
                # showing goes on being chosen underneath it and is
                # simply what comes back when esc lets go. Lowest of the
                # interruptions because everything above it can close it
                # - a note takes it, the browser takes it, an alert is
                # held back behind it only for as long as it is up.
                with self.menu_lock:
                    self.state["mixer"] = self.mixer.view()
                try:
                    pixels = screens.render("mixer", self.state)
                finally:
                    self.state.pop("mixer", None)
            else:
                pixels = screens.render(lcd_config.get("screen", "status"),
                                        self.state)
        with self.lock:
            self.keyboard.send_lcd(pixels)

    INDICATORS = ("off", "printing", "claude", "recording")

    def indicator_state(self, what):
        """Whether a given indicator should be lit right now."""
        if what == "printing":
            import printer
            return printer.is_printing(printer.status())
        if what == "claude":
            reading = screens.claude_utilization()
            value = reading.get("five_hour")
            return value is not None and value >= 80
        if what == "recording":
            return self.record_state is not None
        return False

    def refresh_indicators(self, now):
        """Drive the two lock LEDs macOS leaves dark.

        Only written when the answer changes, so this is not touching the
        keyboard interface every second.
        """
        wanted = self.config.get("indicators") or {}
        if not any(v and v != "off" for v in wanted.values()):
            return
        if now - self.last_leds < 2.0:
            return
        self.last_leds = now
        try:
            if self.leds is None:
                self.leds = device.LockLeds()
            self.leds.set(num=self.indicator_state(wanted.get("numlock")),
                          scroll=self.indicator_state(wanted.get("scrolllock")))
        except Exception as exc:
            log(f"could not drive the lock LEDs: {exc}")
            self.leds = None

    def refresh_app_color(self, now):
        """Track the frontmost app: both colours and profiles depend on it."""
        if now - self.last_app_check < APP_CHECK_INTERVAL:
            return
        self.last_app_check = now
        name = screens.frontmost_app()
        if name == self.current_app:
            return
        self.current_app = name
        # Told rather than polled: an app coming forward is the single
        # most useful thing for another program to react to, since it is
        # what this whole agent's colour and profile logic keys off.
        self.publish("app", {"app": name})
        if self.pulse_end > now:
            # A beat has the colour until its pulse has climbed back,
            # and the pulse puts `active_color()` back itself when it
            # runs out - so the new app's colour arrives with it rather
            # than as one full-bright frame through the middle of a dip.
            return
        with self.lock:
            self.keyboard.set_backlight(self.active_color())

    def refresh_beat(self, now):
        """Dip the backlight on each beat of the music, and climb back.

        This is the only thing that writes the colour while a pulse is
        running, and it writes it thirty times a second rather than on
        every tick of the loop - see `beat.WRITE_INTERVAL`. The pulse
        owns the colour from the beat until `pulse_end`, which is what
        the rest of the agent checks before touching the backlight.
        """
        block = self.config.get("beat") or {}
        if not block.get("enabled", False):
            if self.beat is not None:
                listener, self.beat = self.beat, None
                listener.stop()
                log("beat tap off")
            if self.pulse_end > now:
                # Switched off mid-dip: the colour it was dipping is the
                # colour to go back to, and nothing else would put it
                # there.
                self.pulse_end = 0.0
                self.write_backlight(self.active_color())
            return
        if self.beat is None:
            return                              # the tap would not open
        fresh, level = self.beat.poll(now)
        if fresh and self.beat_detector.push(level, now):
            amount, decay = beat.pulse_settings(block)
            self.pulse_amount = amount
            self.pulse_decay = decay
            self.pulse_start = now
            self.pulse_end = now + decay
            self.pulse_at = now
            self.write_backlight(self.beat_color(now))
            # A beat is the one event here at up to five a second, so it
            # carries no level and no colour - a program reacting to it
            # wants the fact, and the backlog is its own business.
            self.publish("beat", {"amount": amount, "decay": decay})
            return
        if self.pulse_end <= now:
            if self.pulse_end:                  # the tail ran out
                self.pulse_end = 0.0
                self.write_backlight(self.active_color())
            return
        if now - self.pulse_at < beat.WRITE_INTERVAL:
            return
        self.pulse_at = now
        self.write_backlight(self.beat_color(now))

    def beat_color(self, now):
        """The colour at `now` inside a pulse.

        Straight down to `amount`% the instant the beat lands, then a
        straight line back to full over `decay` seconds - a dip rather
        than a flash, because the panel is showing it to somebody who
        is listening to the music rather than watching for it.
        """
        span = self.pulse_decay or 0.001
        done = (now - self.pulse_start) / span
        done = 0.0 if done < 0.0 else (1.0 if done > 1.0 else done)
        scale = self.pulse_amount + (100.0 - self.pulse_amount) * done
        return config.apply_brightness(self.active_color(), int(round(scale)))

    def write_backlight(self, rgb):
        """One backlight write, under the lock every device touch takes.

        The keyboard can be gone by the time a socket thread or the poll
        loop gets here - it lives on a dock - and a missing device is a
        normal state for this agent, not an error to stop for.
        """
        with self.lock:
            if self.keyboard is not None:
                self.keyboard.set_backlight(rgb)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log("g510 daemon starting")
        self.server.start()
        self.sync_watcher()
        self.sync_link()
        self.sync_beat()
        self.sync_api()
        log(f"control socket at {ipc.SOCKET_PATH}")
        while self.running:
            if self.keyboard is None:
                if not self.connect():
                    time.sleep(RECONNECT_DELAY)
                    continue
            try:
                now = time.time()
                self.handle_gkeys()
                self.refresh_gestures(now)
                self.refresh_menu(now)
                self.refresh_mixer(now)
                self.refresh_alerts(now)
                self.refresh_triggers(now)
                self.refresh_lcd(now)
                self.refresh_beat(now)
                self.refresh_app_color(now)
                self.refresh_indicators(now)
                self.sweep_screens(now)
            except (OSError, ValueError) as exc:
                self.drop(exc)
                time.sleep(RECONNECT_DELAY)
                continue
            time.sleep(POLL_INTERVAL)
        self.shutdown()

    def shutdown(self):
        log("g510 daemon stopping")
        # The mixer's taps, before anything else: an aggregate device
        # left running would go on listening to every application that
        # plays a sound after this process is gone.
        levels, self.mixer_levels = self.mixer_levels, None
        self.mixer_generation += 1
        if levels is not None:
            try:
                levels.stop()
            except Exception:
                pass
        if self.keytap is not None:
            try:
                self.keytap.stop()
            except Exception:
                pass
        listener, self.beat = self.beat, None
        if listener is not None:
            try:
                listener.stop()       # and lets the audio tap go too
            except Exception:
                pass
        watcher, self.watcher = self.watcher, None
        if watcher is not None:
            try:
                watcher.stop()          # hands the enhanced-interface switch
            except Exception:           # back to the chat app
                pass
        endpoint, self.link = self.link, None
        if endpoint is not None:
            try:
                endpoint.stop()         # and lets go of the local port
            except Exception:
                pass
        public, self.api = self.api, None
        if public is not None:
            try:
                public.stop()           # and lets go of the API port
            except Exception:
                pass
        # Outside screens belong to a program that was talking to *this*
        # agent, so they go with it - leaving them in `screens.SCREENS`
        # would put dead names in the cycle for whoever starts next.
        try:
            extern.reset()
        except Exception:
            pass
        self.server.stop()
        if self.leds is not None:
            try:
                self.leds.close()
            except Exception:
                pass
        with self.lock:
            keyboard, self.keyboard = self.keyboard, None
            if keyboard:
                try:
                    keyboard.clear_lcd()
                except Exception:
                    pass
                try:
                    keyboard.close()
                except Exception:
                    pass


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main():
    Daemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
