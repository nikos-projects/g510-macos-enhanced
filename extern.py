"""Screens that outside programs own - the half of P8 that is not HTTP.

A program registers a screen, pushes what it wants drawn on it, and that
screen appears in the cycle, in the menu, in `g510 lcd <name>`, in the
Settings pickers and to the autoscreen rules for as long as the program
keeps pushing. Take the program away and the screen goes with it, which
is the point: this is not a plugin directory that outlives whatever
filled it.

It writes into `screens.SCREENS` on purpose. Fifteen places across this
tree read that dict to decide what a screen *is* - the cycle, the menu,
the CLI's error message, the window's pickers - and every one of them
should see an outside screen exactly the way it sees a built-in one.
Teaching each of them about a second kind of screen would be fifteen
chances to forget one, and the dict already degrades well: the cycle
filters names it does not know, and `render` falls back to `status`.

Two things are deliberate about ownership:

  * The handle is not a secret. Nothing here is authenticated - a
    program that can reach the API can read everything and drive the
    keyboard - so the handle exists to stop two programs that both want
    a screen called "weather" from overwriting each other, not to stop
    anybody reading anything.
  * Screens expire. A program that crashes cannot unregister, so a
    screen with nothing pushed for `TTL` seconds is assumed to belong to
    a program that has gone and is taken down with it. A live screen is
    pushed constantly, so this costs a well-behaved program nothing.

Drawing is a list of operations replayed onto the canvas the built-in
screens draw on - `text`, `centered`, `rect`, `bar` - rather than a
bitmap. It is the same vocabulary the daemon already has, it can be
emitted from any language without knowing what a CoreGraphics context
is, and it means the agent invents no layout scheme of its own to
maintain.
"""
import secrets
import threading
import time

from lcd import LCD_HEIGHT, LCD_WIDTH

# How long a screen survives without a push. Long enough that a program
# being briefly busy is not mistaken for one that has gone, short enough
# that a crashed program's screen is not still on the panel a minute
# later.
TTL = 30.0

# A screen is 160x43 and this is drawn from a program that may be
# anything: a generous cap keeps one bad client from turning the panel
# refresh into a benchmark, and every value is clamped to the panel
# rather than trusted.
MAX_OPS = 200

_OPS = ("text", "centered", "rect", "bar")

# name -> {"handle", "title", "ops", "at"}
_LIVE = {}
_LOCK = threading.Lock()


def _number(value, default=0.0, low=-1e6, high=1e6):
    """A float out of whatever arrived, clamped to something drawable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(low, min(high, value))


def clean(ops):
    """Validate a pushed frame, raising ValueError with a usable reason.

    Everything is clamped to the panel rather than rejected for being
    outside it: a program that draws one pixel past the edge means its
    own bug, not that the whole frame should come back refused.
    """
    if isinstance(ops, str) or not isinstance(ops, (list, tuple)):
        raise ValueError('"ops" must be a list of drawing operations')
    if len(ops) > MAX_OPS:
        raise ValueError(f"a frame may carry at most {MAX_OPS} operations, "
                         f"not {len(ops)}")
    out = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"operation {index} must be an object")
        kind = op.get("op")
        if kind not in _OPS:
            raise ValueError(f"operation {index} is {kind!r}; it must be "
                             f"one of {', '.join(_OPS)}")
        item = {"op": kind}
        if kind in ("text", "centered"):
            item["s"] = str(op.get("s", ""))[:160]
            item["y"] = _number(op.get("y"), 12, 0, LCD_HEIGHT)
            item["size"] = _number(op.get("size"), 12, 6, 40)
            if kind == "text":
                item["x"] = _number(op.get("x"), 0, 0, LCD_WIDTH)
        else:
            item["x"] = _number(op.get("x"), 0, 0, LCD_WIDTH)
            item["y"] = _number(op.get("y"), 0, 0, LCD_HEIGHT)
            item["w"] = _number(op.get("w"), 8, 0, LCD_WIDTH)
            item["h"] = _number(op.get("h"), 8, 0, LCD_HEIGHT)
            if kind == "bar":
                item["fraction"] = _number(op.get("fraction"), 0, 0, 1)
            else:
                item["fill"] = bool(op.get("fill", True))
        out.append(item)
    return out


def _paint(canvas, ops):
    """Replay a frame onto a canvas. Nothing here may raise."""
    for item in ops:
        try:
            kind = item["op"]
            if kind == "text":
                canvas.text(item["s"], item["x"], item["y"], item["size"])
            elif kind == "centered":
                canvas.centered(item["s"], item["y"], item["size"])
            elif kind == "rect":
                canvas.rect(item["x"], item["y"], item["w"], item["h"],
                            filled=item["fill"])
            elif kind == "bar":
                canvas.bar(item["x"], item["y"], item["w"], item["h"],
                           item["fraction"])
        except Exception:
            # One malformed operation is the client's bug and costs that
            # operation, not the frame and certainly not the panel
            # refresh, which is holding the only handle to the keyboard.
            continue


def register(name, now=None, title=""):
    """Take a screen name, returning the handle that proves it is yours."""
    name = str(name).strip()[:24]
    if not name:
        raise ValueError("a screen needs a name")
    if not all(ch.isalnum() or ch in "-_" for ch in name):
        raise ValueError("a screen name may hold letters, digits, - and _")
    now = time.time() if now is None else now
    with _LOCK:
        held = _LIVE.get(name)
        if held is not None and now - held["at"] < TTL:
            raise ValueError(f"a screen called {name!r} is already up; "
                             "pick another name")
        handle = secrets.token_hex(8)
        _LIVE[name] = {"handle": handle, "title": str(title)[:40],
                       "ops": [], "at": now}
    _publish(name)
    return handle


def frame(name, handle, ops, now=None, title=None):
    """Replace what a screen shows. False when the handle is not its owner."""
    now = time.time() if now is None else now
    cleaned = clean(ops)
    with _LOCK:
        held = _LIVE.get(name)
        if held is None or held["handle"] != handle:
            return False
        held["ops"], held["at"] = cleaned, now
        if title is not None:
            held["title"] = str(title)[:40]
    return True


def drop(name, handle=None):
    """Take a screen down. With no handle, the caller says it is sure."""
    with _LOCK:
        held = _LIVE.get(name)
        if held is None:
            return False
        if handle is not None and held["handle"] != handle:
            return False
        del _LIVE[name]
    _forget(name)
    return True


def sweep(now=None):
    """Take down screens nobody has pushed to lately. Returns their names."""
    now = time.time() if now is None else now
    with _LOCK:
        gone = [name for name, held in _LIVE.items()
                if now - held["at"] >= TTL]
        for name in gone:
            _LIVE.pop(name, None)
    for name in gone:
        _forget(name)
    return gone


def names():
    """The live outside screens, in the order they were registered."""
    with _LOCK:
        return list(_LIVE)


def describe():
    """What `GET /v1/screens` says: every screen, and which are outside."""
    with _LOCK:
        return {name: {"title": held["title"],
                       "at": round(time.time() - held["at"], 1)}
                for name, held in _LIVE.items()}


def drawer(name):
    """The `(canvas, state)` function this screen is in `SCREENS` as."""
    def draw(canvas, _state):
        with _LOCK:
            held = _LIVE.get(name)
            ops = list(held["ops"]) if held else []
            title = held["title"] if held else ""
        if not ops:
            # Never a blank panel: a screen that has been registered but
            # not yet pushed to says so, which is more use than nothing
            # and is also how a program that forgot to push is found out.
            canvas.centered(title or name, 18, 12)
            canvas.centered("waiting for its program", 32, 9)
            return
        _paint(canvas, ops)

    return draw


def draw_once(ops, now=None):
    """Show one frame on the panel without claiming a screen name.

    A program that wants to put something up *now* - a toast, a status,
    a QR code - should not have to pick a name, keep a handle and police
    a collision with a screen somebody else already owns. This draws
    into a reserved slot instead, which is internal rather than in
    `SCREENS`: it renders and the display can be switched to it, but it
    never turns up in the cycle, in the menu or in `g510 lcd`'s list of
    things to ask for, because it is not a screen anybody chose.
    """
    name = RESERVED
    with _LOCK:
        entry = _LIVE.get(name)
        if entry is None:
            entry = {"handle": secrets.token_hex(8), "title": "",
                     "ops": [], "at": 0.0}
            _LIVE[name] = entry
        entry["ops"], entry["at"] = clean(ops), (time.time() if now is None
                                                 else now)
    _publish(name, internal=True)
    return name


RESERVED = "appdraw"


def _publish(name, internal=False):
    """Put the screen where `render` will find it.

    A screen a program means to keep goes in `SCREENS`, which is what
    the cycle, the menu and the CLI all read, so it becomes selectable
    everywhere at once. A one-shot draw goes in `SCREENS_INTERNAL`
    instead, which `render` also consults but nothing enumerates -
    present, and not on offer.
    """
    import screens
    table = screens.SCREENS_INTERNAL if internal else screens.SCREENS
    table[name] = drawer(name)


def _forget(name):
    """Take a screen back out of both tables.

    The panel being *on* it is the caller's business rather than this
    module's: taking a name out of `SCREENS` is local and cannot fail,
    while moving the display is a config write, and there are only two
    places that ever drop a screen - the sweep and the API's DELETE,
    both of which hold the agent and can ask it to move on.
    """
    import screens
    screens.SCREENS.pop(name, None)
    screens.SCREENS_INTERNAL.pop(name, None)


def reset():
    """Forget everything. For tests, which must not leak into each other."""
    with _LOCK:
        gone = list(_LIVE)
        _LIVE.clear()
    for name in gone:
        _forget(name)


def alive(name, now=None):
    """Refresh a screen's lease without drawing - a heartbeat."""
    now = time.time() if now is None else now
    with _LOCK:
        held = _LIVE.get(name)
        if held is None:
            return False
        held["at"] = now
    return True
