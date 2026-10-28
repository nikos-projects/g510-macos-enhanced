"""The backlight dipping to the beat of the music - P5.

Two halves that meet only in a bool. `Listener` is a CoreAudio process
tap: it hears everything the Mac is playing and hands over the loudest
sample of each callback, which is a level about ninety times a second.
`Detector` decides whether such a level was a beat, and the agent turns
a "yes" into a dip of the backlight that climbs back over `decay`
seconds.

The tap is expensive enough to be worth opting into - it is a
system-wide audio tap plus an aggregate device - so `beat.enabled`
defaults to off and nothing here opens until the config says so. Every
CoreAudio call is guarded: a machine that will not hand over a tap, or
one that dies when the sound device changes, gets a log line and a
backlight that simply does not move. It never raises into the agent's
poll loop, which holds the only handle to the keyboard.
"""
import ctypes
import math
import os
import threading
import time

# What a beat looks like. Not a threshold on the raw level - quiet music
# still has beats, loud music is often above any fixed line all the time
# - but the *fast* average of the level jumping clear of the slow one:
# that is what arriving faster than it can be averaged away means, and
# it works at any volume.
GAP = 0.2         # seconds between two hits worth counting
RATIO = 1.6       # fast has to be this far ahead of slow
FLOOR = 0.05      # under this it is room tone, not music
FAST = 0.05       # seconds: how fast the fast average follows
SLOW = 0.6        # seconds: how fast the slow one does

# How eager the detector is, 0 to 100. It is the one number of the five
# worth putting on a menu: the gap and the two time constants are about
# what a beat *is*, while this is about how hard you have to hit for the
# keyboard to flinch. 50 is exactly `RATIO` and `FLOOR` as written above,
# so the default behaves the way it did before the knob existed.
DEFAULT_SENSITIVITY = 50

# How often the agent may write the colour while a pulse runs. The poll
# loop ticks at 250 Hz, which is far more often than anything on a
# 160x43 panel can show: thirty writes a second is smooth and costs
# almost nothing in a loop that is already running.
WRITE_INTERVAL = 1.0 / 30.0

GLOBAL_SCOPE = 0x676c6f62          # 'glob'
KCFSTRING_UTF8 = 0x08000100        # kCFStringEncodingUTF8


class Address(ctypes.Structure):
    """AudioObjectPropertyAddress - which property, of what, where."""
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", AudioBuffer * 1)]


# The IOProc's shape: device, when, in, when, out, when, client. It is a
# ctypes callback on purpose - a pyobjc-made function pointer comes back
# as something `AudioDeviceStart` refuses with 'undefined' argument.
IOCB = ctypes.CFUNCTYPE(ctypes.c_int,
                        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p)


def _fourcc(code):
    """A CoreAudio status as the four characters it spells, if it spells any.

    'nope' and 'who?' are what these actually come back as, and a bare
    1852797029 in a log line is nobody's idea of an explanation.
    """
    try:
        text = code.to_bytes(4, "big").decode("latin-1")
    except (AttributeError, OverflowError):
        return str(code)
    if text.isprintable():
        return f"{text!r} ({code})"
    return str(code)


def pulse_settings(block):
    """(dip %, climb-back seconds) out of a config block.

    Clamped to what the panel and the CLI already allow, so a hand-edited
    config cannot ask for a dip below zero or a climb that never ends -
    and so all three of them agree on the range.
    """
    try:
        amount = float(block.get("amount", 60))
    except (TypeError, ValueError):
        amount = 60.0
    try:
        decay = float(block.get("decay", 0.35))
    except (TypeError, ValueError):
        decay = 0.35
    return (max(0.0, min(100.0, amount)),
            max(0.05, min(5.0, decay)))


def sensitivity_of(block):
    """How eager the detector should be, out of a config block.

    The same 0-100 the panel spinner and the CLI refuse to go outside,
    so all three agree - and a block that is not numbers at all falls
    back to the default rather than to the dullest setting, because
    guessing "off" for a typo is a worse surprise than guessing "50".
    """
    try:
        value = float(block.get("sensitivity", DEFAULT_SENSITIVITY))
    except (TypeError, ValueError):
        value = float(DEFAULT_SENSITIVITY)
    return max(0.0, min(100.0, value))


class Detector:
    """Levels in, "that was a beat" out.

    Pure, so it can be shown to work without an audio device anywhere
    near it: it is a function of levels and times only, which is also
    why the agent can keep one for the life of the process while the
    tap under it is opened and closed by the config.

    It fires on the *rise* - fast crossing clear of slow, once - rather
    than on fast simply being ahead. A passage that stays loud keeps
    fast ahead of slow for as long as it lasts, and a test that only
    asked "is fast ahead" would pulse the backlight five times a second
    through the whole chorus instead of on the hits.

    `sensitivity` (0-100, default 50) is the only knob, and it moves
    the two numbers that decide how much of a rise counts: at 50 they
    are `RATIO` and `FLOOR` exactly, at 100 any rise at all over any
    level counts, and at 0 the rise has to be twice as far above the
    slow average with twice the room tone under it. It is settable
    while the averages keep running, so turning it up mid-song makes
    the next hit count rather than starting detection over.
    """

    def __init__(self, sensitivity=DEFAULT_SENSITIVITY):
        self.fast = None          # average that follows the level closely
        self.slow = None          # one that does not
        self.at = 0.0             # when the last level arrived
        self.last = 0.0           # when the last beat was called one
        self.raised = False       # whether fast is already clear of slow
        self.set_sensitivity(sensitivity)

    def set_sensitivity(self, sensitivity):
        """Take the knob, leaving whatever was already averaged alone."""
        try:
            value = float(sensitivity)
        except (TypeError, ValueError):
            value = float(DEFAULT_SENSITIVITY)
        value = max(0.0, min(100.0, value))
        scale = (100.0 - value) / 50.0      # 1 at the default, 0 at max
        self.sensitivity = value
        # How far ahead the fast average has to be, and what counts as
        # room tone. The ratio never drops below 1.0: at the very top
        # of the range a beat is still a *rise*, just the smallest one
        # that is distinguishable from noise.
        self.ratio = 1.0 + (RATIO - 1.0) * scale
        self.floor = FLOOR * scale

    def push(self, level, now):
        """Take one level. True when it was a beat."""
        try:
            level = float(level)
        except (TypeError, ValueError):
            return False
        if self.fast is None:
            # The first level seeds both averages at itself: starting
            # them apart would call that first reading a jump.
            self.fast = self.slow = level
            self.at = now
            self.raised = False
            return False
        span = min(max(now - self.at, 0.001), 0.5)
        self.at = now
        self.fast += (level - self.fast) * (1.0 - math.exp(-span / FAST))
        self.slow += (level - self.slow) * (1.0 - math.exp(-span / SLOW))
        clear = self.fast > self.slow * self.ratio
        rising = clear and not self.raised
        # The edge is spent whether or not it counts: a second hit
        # inside the gap belongs to the first one, and the fast average
        # has to come back down before the next one can be a rise.
        self.raised = clear
        if not rising or level < self.floor or now - self.last < GAP:
            return False
        self.last = now
        return True


class Listener:
    """The process tap, as a level per callback.

    `start()` reports whether it got as far as hearing anything rather
    than raising: the agent asks for a tap only because the config said
    to, and a machine that will not hand one over should carry on being
    a keyboard.
    """

    def __init__(self, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.level = 0.0          # loudest sample since the last poll
        self.fresh = False        # whether there is one to hand over
        self.core = None
        self.desc = None          # the recipe, kept for the tap's life
        self.tap = 0
        self.agg = 0
        self.ioid = None
        self.callback = None

    # -- life -----------------------------------------------------------------

    def start(self):
        try:
            core = _coreaudio()
            self.core = core
            self.desc = core.tap_description()
            self.tap = core.create_tap(self.desc)
            uid = core.tap_uid(self.tap)
            self.agg = core.create_aggregate(uid)
            self.callback, self.ioid = core.create_ioproc(self.agg,
                                                          self._on_audio)
            core.start_device(self.agg, self.ioid)
        except Exception as exc:
            # Any of the calls above can fail for reasons that are the
            # machine's, not the caller's - no tap permission, a sound
            # device that came and went - so the failure is a line in
            # the log and a listener that is not there.
            self.log(f"beat tap could not be started: {exc}")
            self.stop()
            return False
        self.log("beat tap listening")
        return True

    def stop(self):
        """Let the tap go. Safe after a start that only got half way,
        and safe to call twice."""
        core, self.core = self.core, None
        desc, self.desc = self.desc, None
        tap, self.tap = self.tap, 0
        agg, self.agg = self.agg, 0
        ioid, self.ioid = self.ioid, None
        callback, self.callback = self.callback, None
        if core is None:
            return
        # The callback and the description are held in locals, not
        # dropped on the floor: CoreAudio may still be inside the IOProc
        # until AudioDeviceStop returns, and the tap's recipe belongs to
        # it for as long as the tap exists.
        core.destroy(tap, agg, ioid)
        del callback, desc

    def poll(self, now):
        """(whether a buffer arrived, the loudest sample in it).

        Two callbacks can land inside one 4ms tick of the loop, so the
        louder of them is kept rather than the later: losing the peak of
        a transient is exactly how a beat goes unnoticed.
        """
        with self.lock:
            fresh, self.fresh = self.fresh, False
            level, self.level = self.level, 0.0
        return fresh, level

    def _on_audio(self, _device, _when, input_list,
                  _in_time, _out_list, _out_time, _client):
        """One buffer of PCM from CoreAudio's own thread.

        Nothing may escape this: an exception here would unwind into
        CoreAudio, which is not a place Python exceptions come back
        from.
        """
        try:
            buffers = ctypes.cast(input_list,
                                  ctypes.POINTER(AudioBufferList)).contents
            peak = 0.0
            for index in range(buffers.mNumberBuffers):
                buf = buffers.mBuffers[index]
                if not buf.mData or not buf.mDataByteSize:
                    continue
                frames = buf.mDataByteSize // 4      # float32
                values = ctypes.cast(
                    buf.mData, ctypes.POINTER(ctypes.c_float * frames)).contents
                for value in values:
                    here = -value if value < 0 else value
                    if here > peak:
                        peak = here
            with self.lock:
                if not self.fresh or peak > self.level:
                    self.level = peak
                self.fresh = True
        except Exception:
            pass
        return 0


class _CoreAudio:
    """The C entry points, and the conversions pyobjc does not do for us.

    Loaded once, the first time a tap is wanted: importing CoreAudio and
    opening the framework costs something, and a config with the beat
    switched off should not pay it.
    """

    def __init__(self):
        import objc
        from CoreAudio import CATapDescription, kAudioTapPropertyUID
        from Foundation import NSDictionary

        self.objc = objc
        self.description_class = CATapDescription
        self.uid_selector = kAudioTapPropertyUID
        self.dictionary_class = NSDictionary
        self.lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._declare()

    def _declare(self):
        """What each call takes and gives back.

        Without these ctypes guesses at the calling convention, and a
        wrong guess does not raise - it corrupts the stack.
        """
        lib, cf = self.lib, self.cf
        void_p, uint, uint_p = ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)
        lib.AudioHardwareCreateProcessTap.argtypes = [void_p, uint_p]
        lib.AudioHardwareDestroyProcessTap.argtypes = [uint]
        lib.AudioHardwareCreateAggregateDevice.argtypes = [void_p, uint_p]
        lib.AudioHardwareDestroyAggregateDevice.argtypes = [uint]
        lib.AudioDeviceCreateIOProcID.argtypes = [
            uint, void_p, void_p, ctypes.POINTER(void_p)]
        lib.AudioDeviceDestroyIOProcID.argtypes = [uint, void_p]
        lib.AudioDeviceStart.argtypes = [uint, void_p]
        lib.AudioDeviceStop.argtypes = [uint, void_p]
        lib.AudioObjectGetPropertyData.argtypes = [
            uint, ctypes.c_void_p, uint, ctypes.c_void_p,
            ctypes.POINTER(uint), ctypes.c_void_p]
        cf.CFStringGetCString.argtypes = [
            void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        for name in ("AudioHardwareCreateProcessTap",
                     "AudioHardwareDestroyProcessTap",
                     "AudioHardwareCreateAggregateDevice",
                     "AudioHardwareDestroyAggregateDevice",
                     "AudioDeviceCreateIOProcID",
                     "AudioDeviceDestroyIOProcID",
                     "AudioDeviceStart", "AudioDeviceStop",
                     "AudioObjectGetPropertyData"):
            getattr(lib, name).restype = ctypes.c_int

    # -- the sequence ---------------------------------------------------------

    def tap_description(self):
        """A mono tap of everything, excluding nothing.

        `initMono...` is an initializer rather than a constructor, so it
        is alloc'd first - calling it on the class itself raises
        "Expecting instance of CATapDescription as self, got one of
        list", which is a confusing way to learn that.
        """
        return self.description_class.alloc() \
            .initMonoGlobalTapButExcludeProcesses_([])

    def create_tap(self, desc):
        tap = ctypes.c_uint()
        status = self.lib.AudioHardwareCreateProcessTap(
            ctypes.c_void_p(self.objc.pyobjc_id(desc)), ctypes.byref(tap))
        if status or not tap.value:
            raise OSError(f"AudioHardwareCreateProcessTap {_fourcc(status)}")
        return tap.value

    def tap_uid(self, tap):
        """The tap's uid, which is what an aggregate's `taps` list wants.

        It comes back as a CFStringRef - eight bytes of *pointer*, not a
        C string, which is why reading it as one gives back mojibake -
        and it is deliberately not CFRelease'd: AudioObject's getters
        hand back an unowned reference, and over-releasing one would
        take the process down over a string a few bytes long.
        """
        address = Address(self.uid_selector, GLOBAL_SCOPE, 0)
        size = ctypes.c_uint(8)
        buf = ctypes.create_string_buffer(8)
        status = self.lib.AudioObjectGetPropertyData(
            tap, ctypes.byref(address), 0, None, ctypes.byref(size), buf)
        if status:
            raise OSError(f"the tap has no uid: {_fourcc(status)}")
        pointer = ctypes.c_void_p.from_buffer_copy(buf.raw[:8]).value
        text = ctypes.create_string_buffer(512)
        if not self.cf.CFStringGetCString(ctypes.c_void_p(pointer), text,
                                          512, KCFSTRING_UTF8):
            raise OSError("the tap's uid would not read")
        return text.value.decode("utf-8", "replace")

    def create_aggregate(self, uid):
        """A private device whose only content is that tap.

        `taps` wants dictionaries, not bare uid strings - a list of
        strings makes the device create and then refuse to start with
        'nope' - and `private` keeps it out of the user's sound settings.
        """
        spec = {"uid": f"g510.beat.{os.getpid()}.{int(time.time())}",
                "name": "g510 beat", "taps": [{"uid": uid}],
                "private": True}
        bridged = self.dictionary_class.dictionaryWithDictionary_(spec)
        agg = ctypes.c_uint()
        status = self.lib.AudioHardwareCreateAggregateDevice(
            ctypes.c_void_p(self.objc.pyobjc_id(bridged)), ctypes.byref(agg))
        if status or not agg.value:
            raise OSError(f"AudioHardwareCreateAggregateDevice {_fourcc(status)}")
        return agg.value

    def create_ioproc(self, agg, handler):
        """Register `handler`, returning (its ctypes handle, its id).

        Both have to be kept: the handle because ctypes would otherwise
        collect the callback while CoreAudio is still calling it, the id
        because that is what stops and destroys it.
        """
        callback = IOCB(handler)
        ioid = ctypes.c_void_p()
        status = self.lib.AudioDeviceCreateIOProcID(
            agg, ctypes.cast(callback, ctypes.c_void_p), None,
            ctypes.byref(ioid))
        if status:
            raise OSError(f"AudioDeviceCreateIOProcID {_fourcc(status)}")
        return callback, ioid.value

    def start_device(self, agg, ioid):
        status = self.lib.AudioDeviceStart(agg, ioid)
        if status:
            raise OSError(f"AudioDeviceStart {_fourcc(status)}")

    def destroy(self, tap, agg, ioid):
        """Undo all of it, in reverse, and never raise while doing so -
        a failed cleanup must not cost the caller the rest of it."""
        if agg and ioid is not None:
            try:
                self.lib.AudioDeviceStop(agg, ioid)
            except Exception:
                pass
            try:
                self.lib.AudioDeviceDestroyIOProcID(agg, ioid)
            except Exception:
                pass
        if agg:
            try:
                self.lib.AudioHardwareDestroyAggregateDevice(agg)
            except Exception:
                pass
        if tap:
            try:
                self.lib.AudioHardwareDestroyProcessTap(tap)
            except Exception:
                pass


_BINDING = None


def _coreaudio():
    """The loaded binding, built the first time somebody wants a tap."""
    global _BINDING
    if _BINDING is None:
        _BINDING = _CoreAudio()
    return _BINDING
