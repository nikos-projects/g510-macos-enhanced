"""What the mixer reads off CoreAudio - P7.

Two halves that meet only in the panel. `volume()` and `mute()` are the
*system* output, read and written straight off the default output device;
`Levels` is the application side - one process tap per app that is making
sound, all of them hanging off a single private aggregate device, so one
IOProc callback hands over one buffer per app and the loudest sample of
each is that app's level.

Three things about this machine are written down here because they cost
time to find out:

  * A tap is made from a **process object id**, not a pid. Asking for one
    with a pid comes back as `'obj'`; the id comes from
    `kAudioHardwarePropertyTranslatePIDToProcessObject`.
  * Object ids are recycled the moment a process exits, so the id that
    comes back is asked who it belongs to before it is believed - a
    mismatch would put another program's sound under this one's name.
  * This headset has no master volume property at all. `volm` on the
    output scope's elements 1 and 2 is the volume, and element 0 answers
    `'who?'` - so both channels are written, or one of them would only
    pan the sound.

Nothing here raises into the agent's poll loop, which holds the only
handle to the keyboard: a machine that will not hand over a tap gets a
log line and a mixer with no bars on it.
"""
import ctypes
import subprocess
import threading
import time

# `beat` already holds the three pieces of CoreAudio plumbing this needs -
# the property address struct, the IOProc callback type (a pyobjc-made
# function pointer comes back as something `AudioDeviceStart` refuses) and
# the status decoder - and they are the same for any CoreAudio caller.
from beat import Address, IOCB, _fourcc

GLOB = 0x676c6f62                  # 'glob'
OUTP = 0x6f757470                  # 'outp'
KCFSTRING_UTF8 = 0x08000100

VOLUME_SCALAR = 0x766f6c6d         # 'volm'
MUTE = 0x6d757465                  # 'mute'
DEVICE_UID = 0x75696420            # 'uid '
DEFAULT_OUTPUT = 0x644f7574        # 'dOut', on the system object
SYSTEM_OBJECT = 1                  # the id every hardware property hangs off

MAX_TAPS = 16                      # as many apps as one aggregate may carry
# Apps that keep a volume of their own and will say what it is over
# AppleScript - the only per-app volume macOS has, because it is the app's
# own rather than the system's. The number is how far that app's slider
# goes: VLC counts to 256, the others to 100, and the panel only ever
# speaks percent.
VOLUME_APPS = {"com.apple.Music": ("Music", 100),
               "com.spotify.client": ("Spotify", 100),
               "org.videolan.vlc": ("VLC", 256)}


class AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class AudioBufferList(ctypes.Structure):
    """The IOProc's buffers, wide enough for every tap at once.

    `beat.AudioBufferList` declares a single buffer, which is right for
    the one global tap P5 opens and wrong here: with three taps the list
    has three entries, and reading index 1 of a one-element array raises
    `IndexError` inside the callback, where a Python exception has
    nowhere to unwind to. CoreAudio allocates exactly as many as the
    aggregate has taps, so anything up to `MAX_TAPS` is safe to address.
    """
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", AudioBuffer * MAX_TAPS)]


class _Binding:
    """The C entry points and the pyobjc classes, loaded once.

    Opening the framework and importing CoreAudio cost something, so this
    happens the first time the mixer is asked for rather than at import -
    a run that never opens the mixer pays nothing, the same way
    `beat._CoreAudio` does.
    """

    def __init__(self):
        import CoreAudio as CA
        import objc
        from AppKit import NSWorkspace
        from CoreAudio import CATapDescription, kAudioTapPropertyUID
        from Foundation import NSDictionary

        self.objc = objc
        self.description_class = CATapDescription
        self.uid_selector = kAudioTapPropertyUID
        self.dictionary_class = NSDictionary
        self.workspace = NSWorkspace.sharedWorkspace()
        self.translate_pid = CA.kAudioHardwarePropertyTranslatePIDToProcessObject
        self.process_pid = CA.kAudioProcessPropertyPID
        self.cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

        self.lib.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.lib.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_void_p]
        self.lib.AudioObjectHasProperty.argtypes = [
            ctypes.c_uint, ctypes.c_void_p]
        self.lib.AudioObjectHasProperty.restype = ctypes.c_bool
        self.lib.AudioHardwareCreateProcessTap.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        self.lib.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint]
        self.lib.AudioHardwareCreateAggregateDevice.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        self.lib.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint]
        self.lib.AudioDeviceCreateIOProcID.argtypes = [
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        for name in ("AudioDeviceStart", "AudioDeviceStop",
                     "AudioDeviceDestroyIOProcID"):
            getattr(self.lib, name).restype = ctypes.c_int


_binding = None
_binding_lock = threading.Lock()


def _bind():
    """The binding, built the first time anything here is asked for."""
    global _binding
    with _binding_lock:
        if _binding is None:
            _binding = _Binding()
    return _binding


# -- property plumbing --------------------------------------------------------

def _read(obj, selector, scope=GLOB, element=0, size=4, qualifier=None):
    """One property read, or None if the machine does not have it.

    None rather than an exception: half the devices this runs against do
    not carry the properties the other half does, and a missing property
    is a fact about the machine rather than a failure.
    """
    bound = _bind()
    address = Address(selector, scope, element)
    data = ctypes.create_string_buffer(size)
    got = ctypes.c_uint(size)
    qsize, qdata = 0, None
    if qualifier is not None:
        qsize = ctypes.sizeof(qualifier)
        qdata = ctypes.byref(qualifier)
    status = bound.lib.AudioObjectGetPropertyData(
        obj, ctypes.byref(address), qsize, qdata, ctypes.byref(got), data)
    if status:
        return None
    return data.raw[:got.value]


def _has(obj, selector, scope=GLOB, element=0):
    bound = _bind()
    address = Address(selector, scope, element)
    try:
        return bool(bound.lib.AudioObjectHasProperty(
            obj, ctypes.byref(address)))
    except Exception:
        return False


def _write(obj, selector, value, scope=GLOB, element=0):
    """One property write. True when the machine accepted it."""
    bound = _bind()
    address = Address(selector, scope, element)
    try:
        status = bound.lib.AudioObjectSetPropertyData(
            obj, ctypes.byref(address), 0, None,
            ctypes.sizeof(value), ctypes.byref(value))
    except Exception:
        return False
    return status == 0


def _float(obj, selector, scope, element):
    raw = _read(obj, selector, scope, element, 4)
    if raw is None or len(raw) < 4:
        return None
    return ctypes.c_float.from_buffer_copy(raw[:4]).value


def _uint(obj, selector, scope, element):
    raw = _read(obj, selector, scope, element, 4)
    if raw is None or len(raw) < 4:
        return None
    return ctypes.c_uint32.from_buffer_copy(raw[:4]).value


def _string(obj, selector):
    raw = _read(obj, selector, GLOB, 0, 8)
    if raw is None or len(raw) < 8:
        return None
    bound = _bind()
    pointer = ctypes.c_void_p.from_buffer_copy(raw[:8]).value
    if not pointer:
        return None
    text = ctypes.create_string_buffer(512)
    if not bound.cf.CFStringGetCString(ctypes.c_void_p(pointer), text, 512,
                                       KCFSTRING_UTF8):
        return None
    return text.value.decode()


# -- the system output --------------------------------------------------------

def default_device():
    """The default output device, or 0 if there is no such thing."""
    raw = _read(SYSTEM_OBJECT, DEFAULT_OUTPUT, GLOB, 0, 4)
    if raw is None or len(raw) < 4:
        return 0
    return ctypes.c_uint32.from_buffer_copy(raw[:4]).value


def volume():
    """The system output volume as 0..1, or None if this device has none.

    The two *channel* elements rather than a master property, because
    there is no master: this headset answers `'who?'` on element 0 and on
    the global scope, and the channels are what macOS's own volume slider
    writes. Both are read and averaged so a channel left out of step by
    something else does not read as half volume.
    """
    device = default_device()
    if not device:
        return None
    values = [value for value in
              (_float(device, VOLUME_SCALAR, OUTP, 1),
               _float(device, VOLUME_SCALAR, OUTP, 2))
              if value is not None]
    if not values:
        return None
    return max(0.0, min(1.0, sum(values) / len(values)))


def set_volume(value):
    """Write the volume, and say what it became - or None if it would not.

    Both channels, every time: writing one of them would turn the pair
    into a pan control rather than a volume control. What comes back is
    *read again* rather than the number that was sent, because macOS has
    its own idea of where the volume really is - it quantises and it
    clamps - and a panel showing a number the machine did not take would
    be a panel that lies.
    """
    device = default_device()
    if not device:
        return None
    value = max(0.0, min(1.0, float(value)))
    written = False
    for element in (1, 2):
        if _has(device, VOLUME_SCALAR, OUTP, element):
            written = _write(device, VOLUME_SCALAR, ctypes.c_float(value),
                             OUTP, element) or written
    return volume() if written else None


def muted():
    """Whether the output is muted, or None if there is no such switch."""
    device = default_device()
    if not device:
        return None
    value = _uint(device, MUTE, OUTP, 0)
    return None if value is None else bool(value)


def set_muted(on):
    """Mute or unmute, and say what it became - or None if it would not."""
    device = default_device()
    if not device:
        return None
    if not _has(device, MUTE, OUTP, 0):
        return None
    if not _write(device, MUTE, ctypes.c_uint32(1 if on else 0), OUTP, 0):
        return None
    return bool(on)


# -- application detection ----------------------------------------------------

def process_object(pid):
    """The CoreAudio object behind a pid, or 0 if there is not one.

    Taps are made from object ids, never from pids. And an id is only
    valid for as long as the process that holds it: ids are handed out
    again the moment a process exits, so the id that comes back is asked
    whose it is before it is used. Anything unverified is 0, because
    tapping the wrong process would show another program's sound under
    this one's name on the panel.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return 0
    if pid <= 0:
        return 0
    raw = _read(SYSTEM_OBJECT, _bind().translate_pid, GLOB, 0, 4,
                qualifier=ctypes.c_uint32(pid))
    if raw is None or len(raw) < 4:
        return 0
    obj = ctypes.c_uint32.from_buffer_copy(raw[:4]).value
    if not obj:
        return 0
    owner = _uint(obj, _bind().process_pid, GLOB, 0)
    return obj if owner == pid else 0


def app_volume(bundle):
    """What an app keeps its own volume at, 0..100, or None.

    macOS has no per-app *output* volume - that would need a virtual
    device per app - but the few apps that keep one of their own will say
    what it is over AppleScript. Everything else returns None, which is
    what the panel draws as `--`. The answer is remembered, because this
    is re-read every time the app list is rebuilt and shelling out to
    `osascript` twice a second would be its own kind of stall.
    """
    entry = VOLUME_APPS.get(bundle)
    if not entry:
        return None
    name, top = entry
    with _volume_lock:
        if bundle in _volume_cache:
            return _volume_cache[bundle]
    raw = _ask_osascript(f'tell application "{name}" to get sound volume')
    if raw is None:
        return None
    value = _to_percent(raw, top)
    with _volume_lock:
        _volume_cache[bundle] = value
    return value


def set_app_volume(bundle, value):
    """Set an app's own volume, and say what it became - or None."""
    entry = VOLUME_APPS.get(bundle)
    if not entry:
        return None
    name, top = entry
    value = max(0, min(100, int(value)))
    raw = _from_percent(value, top)
    if _ask_osascript(
            f'tell application "{name}" to set sound volume to {raw}') \
            is None:
        return None
    with _volume_lock:
        _volume_cache[bundle] = value
    return value


def _to_percent(raw, top):
    """An app's own scale as the panel's 0..100, rounded the way a slider
    would land rather than the way the arithmetic goes."""
    return max(0, min(100, int(round(float(raw) * 100.0 / top))))


def _from_percent(value, top):
    """The panel's 0..100 as this app's own scale."""
    return max(0, min(int(top), int(round(float(value) * top / 100.0))))


def change_app_volume(bundle, delta):
    """Move an app's own volume by `delta` points, 0..100, or None.

    Read and written rather than tracked here: what the panel shows has
    to be what the app thinks, and the app is the only thing that knows.
    """
    current = app_volume(bundle)
    if current is None:
        return None
    return set_app_volume(bundle, current + int(delta))


def _ask_osascript(script):
    """One AppleScript question, 0..100 if it answered.

    Bounded with a timeout: `osascript` waits for an app that is wedged,
    and a wedged Music must not be able to hold a thread of the agent's
    for ever.
    """
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if done.returncode:
        return None
    try:
        return max(0, min(100, int(float((done.stdout or "").strip()))))
    except (TypeError, ValueError):
        return None


def candidates(limit=MAX_TAPS):
    """Apps that could be making sound, as the panel would list them.

    Regular apps only - a helper process holding an audio object is not
    something anybody would recognise on a panel - and only apps CoreAudio
    already has an object for, because an app that has never played
    anything has nothing to tap: the object is created the first time it
    makes a sound, which is exactly why this is re-run while the mixer is
    open rather than once when it opens.
    """
    try:
        apps = _bind().workspace.runningApplications()
    except Exception:
        return []
    found = []
    for app in apps:
        try:
            if int(app.activationPolicy()) != 0:
                continue
            pid = int(app.processIdentifier())
            name = str(app.localizedName() or "")
            bundle = str(app.bundleIdentifier() or "")
        except Exception:
            continue
        if not name or not process_object(pid):
            continue
        adjustable = bundle in VOLUME_APPS
        found.append({"pid": pid, "name": name, "bundle": bundle,
                      "adjustable": adjustable,
                      "volume": app_volume(bundle) if adjustable else None})
        if len(found) >= limit:
            break
    return found


_volume_cache = {}
_volume_lock = threading.Lock()


# -- the levels ---------------------------------------------------------------

class Levels:
    """One aggregate device carrying a process tap per app.

    Opened on a worker thread by the agent: `AudioDeviceCreateIOProcID`
    has been seen to take eleven seconds on this machine, and the poll
    loop holds the only handle to the keyboard. `poll()` is what the loop
    calls instead - a lock and a dict copy.
    """

    def __init__(self, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.order = []             # buffer index -> pid
        self.peaks = {}             # pid -> loudest since the last poll
        self.descriptions = []      # the recipes, for as long as the taps live
        self.taps = []
        self.aggregate = 0
        self.ioid = None
        self.callback = None

    def start(self, apps):
        """Hear `apps`. True when at least one of them got a tap."""
        bound = _bind()
        for app in apps:
            try:
                pid = int(app.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            obj = process_object(pid)
            if not obj:
                continue            # no audio object: nothing to tap yet
            tap, description = None, None
            try:
                description = bound.description_class.alloc() \
                    .initMonoMixdownOfProcessesIDs_([obj])
                raw = ctypes.c_uint()
                status = bound.lib.AudioHardwareCreateProcessTap(
                    ctypes.c_void_p(bound.objc.pyobjc_id(description)),
                    ctypes.byref(raw))
                if status or not raw.value:
                    raise OSError(_fourcc(status))
                tap = raw.value
            except Exception as exc:
                self.log(f"mixer: no tap for {app.get('name')}: {exc}")
                continue
            self.descriptions.append(description)
            self.taps.append(tap)
            self.order.append(pid)
            self.peaks[pid] = 0.0
        if not self.taps:
            return False
        try:
            self._open_device()
        except Exception as exc:
            self.log(f"mixer: the taps would not open: {exc}")
            self.stop()
            return False
        return True

    def _open_device(self):
        """The private aggregate those taps hang off, and one IOProc."""
        bound = _bind()
        uids = []
        for tap in self.taps:
            uid = _string(tap, bound.uid_selector)
            if not uid:
                raise OSError("a tap has no uid")
            uids.append({"uid": uid})
        spec = bound.dictionary_class.dictionaryWithDictionary_({
            "uid": f"g510.mixer.{int(time.time() * 1000)}",
            "name": "g510 mixer",
            "taps": uids,
            "private": True,
        })
        raw = ctypes.c_uint()
        status = bound.lib.AudioHardwareCreateAggregateDevice(
            ctypes.c_void_p(bound.objc.pyobjc_id(spec)), ctypes.byref(raw))
        if status or not raw.value:
            raise OSError(_fourcc(status))
        self.aggregate = raw.value
        # Kept on the instance: the callback has to outlive the call that
        # made it, or the IOProc becomes a dangling function pointer.
        self.callback = IOCB(self._on_audio)
        handle = ctypes.c_void_p()
        status = bound.lib.AudioDeviceCreateIOProcID(
            self.aggregate, ctypes.cast(self.callback, ctypes.c_void_p),
            None, ctypes.byref(handle))
        if status:
            raise OSError(_fourcc(status))
        self.ioid = handle
        status = bound.lib.AudioDeviceStart(self.aggregate, self.ioid)
        if status:
            raise OSError(_fourcc(status))

    def poll(self):
        """Every tapped app's loudest sample since the last call, then zero.

        A full dict rather than only the ones that were loud: a bar has
        to fall when an app stops, and silence is as much of an answer as
        a peak.
        """
        with self.lock:
            out = dict(self.peaks)
            for pid in self.order:
                self.peaks[pid] = 0.0
            return out

    def _on_audio(self, _device, _when, input_list,
                  _in_time, _out_list, _out_time, _client):
        """One buffer per tap, from CoreAudio's own thread.

        Nothing may escape this: an exception here would unwind into
        CoreAudio, which is not a place Python exceptions come back from.
        """
        try:
            buffers = ctypes.cast(input_list,
                                  ctypes.POINTER(AudioBufferList)).contents
            count = min(int(buffers.mNumberBuffers), len(self.order))
            for index in range(count):
                buf = buffers.mBuffers[index]
                if not buf.mData or not buf.mDataByteSize:
                    continue
                frames = buf.mDataByteSize // 4          # float32
                values = ctypes.cast(
                    buf.mData, ctypes.POINTER(ctypes.c_float * frames)).contents
                peak = 0.0
                for value in values:
                    here = -value if value < 0 else value
                    if here > peak:
                        peak = here
                pid = self.order[index]
                if peak > self.peaks.get(pid, 0.0):
                    self.peaks[pid] = peak
        except Exception:
            pass
        return 0

    def stop(self):
        """Let everything go. Safe after a start that only got half way.

        Taps last, because the aggregate's recipe is a tap's uid: the tap
        belongs to the description for as long as either exists.
        """
        bound = None
        try:
            bound = _bind()
        except Exception:
            return
        aggregate, self.aggregate = self.aggregate, 0
        ioid, self.ioid = self.ioid, None
        taps, self.taps = self.taps, []
        callback, self.callback = self.callback, None
        self.descriptions = []
        if aggregate and ioid is not None:
            try:
                bound.lib.AudioDeviceStop(aggregate, ioid)
            except Exception:
                pass
            try:
                bound.lib.AudioDeviceDestroyIOProcID(aggregate, ioid)
            except Exception:
                pass
        if aggregate:
            try:
                bound.lib.AudioHardwareDestroyAggregateDevice(aggregate)
            except Exception:
                pass
        for tap in taps:
            try:
                bound.lib.AudioHardwareDestroyProcessTap(tap)
            except Exception:
                pass
        with self.lock:
            self.order = []
            self.peaks = {}
