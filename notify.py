"""What has arrived: chat read off the app's own window, and everything
else off the store macOS keeps.

Discord - and Vesktop, and the other desktop clients built on it - keeps its
messages inside its own window: there is no notification for macOS to show
and no message store on disk to read. What the window does have is an
Accessibility tree, and fully built it contains every message currently
rendered - author, text, timestamp, message id - which is everything needed to
put a message on the LCD.

Two things make that possible:

  * this process has to hold the Accessibility permission, the same one the
    macro bindings already ask for (g510 permissions);

  * Electron only builds the whole tree when macOS says the app is showing an
    "enhanced user interface" - the switch VoiceOver flips. Without it the
    window offers its chrome and a handful of empty groups. Setting it here is
    what turns that into the actual conversation, and it is handed back when
    the watcher stops.

Reading happens on its own thread at its own pace, so the agent's key polling
and its redraw never wait for a tree walk.

One window is all that route can follow, so a second source sits beside it:
everything macOS is *told* is written into macOS's own store before it is
drawn, and `Notifications` reads that back. Two sources, one watcher, one
queue - the difference is what can be said, not how it is said.
"""
import ctypes
import os
import plistlib
import queue
import re
import sqlite3
import threading
import time

DEFAULT_APPS = (
    "dev.vencord.vesktop",
    "com.hnc.Discord",
    "com.hnc.DiscordPTB",
    "com.hnc.DiscordCanary",
    "Discord",
    "Vesktop",
)

DEFAULT_POLL_SECONDS = 2.0

# What one node of the tree is asked for, all in a single round trip: asking
# for each of these separately costs one message to the app apiece, and a
# window with a chat in it is four thousand nodes.
ATTRIBUTE_NAMES = ("AXRole", "AXSubrole", "AXDOMIdentifier", "AXTitle",
                   "AXValue", "AXDescription", "AXChildren")

TEXT_ROLES = ("AXStaticText", "AXTextField", "AXTextArea")

# Parts of a message that are not its text: what it replies to, what it
# carries, how it was reacted to. They hold their own copies of the same
# words, so leaving them in would say everything twice.
NOT_TEXT = ("message-reply-context", "message-replies", "message-reactions",
            "message-accessories", "message-buttons", "message-edited")

# The hover toolbar Discord hangs inside the content group: react, translate,
# edit, forward. It is the only thing in there that is not the message, and
# its buttons have no text of their own, so reading buttons would otherwise
# announce the menu instead of the sentence.
ACTIONS_LABEL = "Message Actions"

# Whose messages these are, read off the panel at the bottom of the sidebar.
ACCOUNT_LABEL = "User status and settings"

MESSAGE_GROUP = "chat-messages-"
LIST_PREFIX = "Messages in "
MESSAGE_TAIL = 24          # how much of the bottom of a channel is read

_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
# "Sunday, October 25, 2026 at 21:33" - the full timestamp Discord hangs on
# a message for assistive technology, which the summary title also carries.
_DATE_AT = re.compile(r",?\s*" + _WEEKDAY + r",\s*\w+\s+\d{1,2},?\s+\d{4}"
                      r"(?:\s+at\s+\d{1,2}:\d{2})?")
_CLOCK = re.compile(r"\d{1,2}:\d{2}")
_EDITED = re.compile(r"\s*\( ?edited ?\)", re.IGNORECASE)


# -- the Accessibility API, through ctypes ---------------------------------
#
# pyobjc does not ship this framework, and asking for another dependency in
# order to read a chat window would be a poor trade. Everything used here is
# a plain C entry point in ApplicationServices or CoreFoundation.

CFR = ctypes.c_void_p
_AS = ctypes.CDLL(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_CF = ctypes.CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

_AS.AXIsProcessTrusted.restype = ctypes.c_bool
_AS.AXIsProcessTrusted.argtypes = []
_AS.AXUIElementCreateApplication.restype = CFR
_AS.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
_AS.AXUIElementCopyAttributeValue.restype = ctypes.c_int
_AS.AXUIElementCopyAttributeValue.argtypes = [CFR, CFR, ctypes.POINTER(CFR)]
_AS.AXUIElementCopyMultipleAttributeValues.restype = ctypes.c_int
_AS.AXUIElementCopyMultipleAttributeValues.argtypes = [
    CFR, CFR, ctypes.c_uint32, ctypes.POINTER(CFR)]
_AS.AXUIElementSetAttributeValue.restype = ctypes.c_int
_AS.AXUIElementSetAttributeValue.argtypes = [CFR, CFR, CFR]

_CF.CFStringCreateWithCString.restype = CFR
_CF.CFStringCreateWithCString.argtypes = [CFR, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [CFR, ctypes.c_char_p, ctypes.c_size_t,
                                   ctypes.c_uint32]
_CF.CFRelease.argtypes = [CFR]
_CF.CFArrayGetCount.restype = ctypes.c_long
_CF.CFArrayGetCount.argtypes = [CFR]
_CF.CFArrayGetValueAtIndex.restype = CFR
_CF.CFArrayGetValueAtIndex.argtypes = [CFR, ctypes.c_long]
_CF.CFArrayCreate.restype = CFR
_CF.CFArrayCreate.argtypes = [CFR, ctypes.c_void_p, ctypes.c_long, CFR]
_CF.CFGetTypeID.restype = ctypes.c_ulong
_CF.CFGetTypeID.argtypes = [CFR]
_CF.CFStringGetTypeID.restype = ctypes.c_ulong
_CF.CFStringGetTypeID.argtypes = []
_CF.CFArrayGetTypeID.restype = ctypes.c_ulong
_CF.CFArrayGetTypeID.argtypes = []

_ENCODING = 0x08000100                     # kCFStringEncodingUTF8
_STRING_ID = _CF.CFStringGetTypeID()
_ARRAY_ID = _CF.CFArrayGetTypeID()
_TRUE = CFR(ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue").value)
_FALSE = CFR(ctypes.c_void_p.in_dll(_CF, "kCFBooleanFalse").value)


def _cfstring(text):
    return _CF.CFStringCreateWithCString(None, text.encode("utf-8"), _ENCODING)


# Key strings are made once and kept for the life of the process: a fresh one
# per attribute per node would leak tens of thousands of them per poll.
_KEYS = {name: _cfstring(name)
         for name in ATTRIBUTE_NAMES + ("AXWindows", "AXEnhancedUserInterface")}
_ATTRIBUTES = None


def _attribute_names():
    """The one array of attribute names, built once and kept alive."""
    global _ATTRIBUTES
    if _ATTRIBUTES is None:
        refs = (ctypes.c_void_p * len(ATTRIBUTE_NAMES))(*(
            _KEYS[name] for name in ATTRIBUTE_NAMES))
        callbacks = ctypes.c_void_p.in_dll(_CF, "kCFTypeArrayCallBacks")
        _ATTRIBUTES = _CF.CFArrayCreate(None, refs, len(ATTRIBUTE_NAMES),
                                        callbacks)
    return _ATTRIBUTES


def _pystring(ref):
    """A CFString as text, or None when the value is not a string."""
    if not ref or _CF.CFGetTypeID(ref) != _STRING_ID:
        return None
    buffer = ctypes.create_string_buffer(4096)
    if not _CF.CFStringGetCString(ref, buffer, 4096, _ENCODING):
        return None
    return buffer.value.decode("utf-8", "replace")


def is_trusted():
    """Whether this process may read other apps' windows."""
    return bool(_AS.AXIsProcessTrusted())


def _node(element, depth=0, budget=None):
    """One element of the tree as a plain dict, children included.

    `budget` counts nodes rather than depth: a window with a chat in it nests
    deeply in places, and past a point more of it is somebody else's history
    rather than more of the same conversation.
    """
    empty = {"role": None, "subrole": None, "identifier": None, "title": None,
             "value": None, "description": None, "children": []}
    if budget is None:
        budget = [40000]
    budget[0] -= 1
    values = CFR()
    error = _AS.AXUIElementCopyMultipleAttributeValues(
        element, _attribute_names(), 0, ctypes.byref(values))
    if error or not values.value:
        return empty
    try:
        source = values.value
        if _CF.CFGetTypeID(source) != _ARRAY_ID:
            return empty
        # One slot per requested attribute, in the order asked for, with
        # kCFNull where the app had nothing to say.
        found = [_pystring(_CF.CFArrayGetValueAtIndex(source, index))
                 for index in range(min(len(ATTRIBUTE_NAMES),
                                        _CF.CFArrayGetCount(source)))]
        found += [None] * (len(ATTRIBUTE_NAMES) - len(found))
        node = dict(zip(("role", "subrole", "identifier", "title", "value",
                         "description"), found), children=[])
        children = _CF.CFArrayGetValueAtIndex(
            source, ATTRIBUTE_NAMES.index("AXChildren"))
        if children and _CF.CFGetTypeID(children) == _ARRAY_ID:
            total = _CF.CFArrayGetCount(children)
            first = 0
            if str(node.get("description") or "").startswith(LIST_PREFIX):
                # Only the end of the list is read. It is the bulk of the
                # window by an order of magnitude, and the only messages that
                # can be new are the ones at the bottom of it - which is also
                # where Discord keeps them while a game is in front.
                first = max(0, total - MESSAGE_TAIL)
            for index in range(first, total):
                if budget[0] <= 0 or depth > 60:
                    break
                child = _CF.CFArrayGetValueAtIndex(children, index)
                if child:
                    node["children"].append(_node(child, depth + 1, budget))
        return node
    finally:
        _CF.CFRelease(values.value)


# -- reading the tree ------------------------------------------------------

def find_message_list(node):
    """The list the chat draws into, or None.

    The sidebar is full of lists too, so this goes by what a list calls
    itself - "Messages in #general" - rather than by shape.
    """
    if node is None:
        return None
    description = str(node.get("description") or "")
    if node.get("role") == "AXList" and description.startswith(LIST_PREFIX):
        return node
    for child in node.get("children") or ():
        found = find_message_list(child)
        if found is not None:
            return found
    return None


def _first(node, key):
    """The first non-empty `key` under this node, searching down."""
    value = node.get(key)
    if value:
        return value
    for child in node.get("children") or ():
        found = _first(child, key)
        if found:
            return found
    return None


def _by_description(node, label):
    """The first node under `node` that calls itself `label`."""
    if node is None:
        return None
    if str(node.get("description") or "") == label:
        return node
    for child in node.get("children") or ():
        found = _by_description(child, label)
        if found is not None:
            return found
    return None


# What the account panel says before it says the name: the state word under
# the avatar. Not a username in any language, and would otherwise be taken
# for one.
_ACCOUNT_STATES = {"online", "idle", "do not disturb", "invisible", "offline",
                   "mute", "unmute"}


def current_user(node):
    """Whoever is signed in - the name the account panel shows.

    Nowhere else in the window says it: a message only carries the name its
    author writes under, which is a server nickname as often as not, and the
    window title is about the channel. The panel at the bottom of the sidebar
    is the account's own, and it is labelled rather than named, so the label
    is what is looked for.

    Returns None when there is no panel to read - a window on the Friends
    page, or a client that words it differently - and None means nothing is
    filtered, rather than everything.
    """
    panel = _by_description(node, ACCOUNT_LABEL)
    if panel is None:
        return None

    def texts(n):
        if n.get("role") in TEXT_ROLES:
            value = str(n.get("value") or "").strip()
            if value:
                yield value
        for child in n.get("children") or ():
            yield from texts(child)

    for value in texts(panel):
        if value.lower() not in _ACCOUNT_STATES:
            return value
    return None


def _article_title(group):
    for child in group.get("children") or ():
        if child.get("subrole") == "AXDocumentArticle":
            return str(child.get("title") or "")
    return str(group.get("title") or "")


def _fallback_text(group, author):
    """What a message with no text of its own can still say about itself.

    Discord puts a summary of the whole message in the article's title -
    "author , what it carries , time" - which is enough to show that
    something arrived even when it was only a file or an image. The author
    and the clock are already said elsewhere, so they are taken back out:
    the author arrives as "greed Verified App" or as "Niko replying to A.W.",
    neither of which equals the name on its own.
    """
    parts = [part.strip() for part in _article_title(group).split(" , ")
             if part.strip(" ,.;:!?")]
    if author and parts and (parts[0] == author
                             or parts[0].startswith(author + " ")):
        parts = parts[1:]
    cleaned = []
    for part in parts:
        part = _EDITED.sub("", _DATE_AT.sub("", part)).strip()
        if part and not _CLOCK.fullmatch(part):
            cleaned.append(part)
    return " , ".join(cleaned) or "(attachment)"


def _has_words(node):
    """Whether anything under this node already says what it shows."""
    if node.get("role") in TEXT_ROLES and node.get("value"):
        return True
    return any(_has_words(child) for child in node.get("children") or ())


def _is_stamp(value):
    """A time or a date, as Discord writes one for assistive technology."""
    value = str(value).strip()
    return bool(_CLOCK.fullmatch(value) or _DATE_AT.fullmatch(value))


def read_message(group):
    """One `chat-messages-...` group as {id, author, time, text}.

    Discord names the author and the time on the first message of a run and
    leaves them off the rest, so both are carried down from one message to the
    next as the list is walked.

    Only the nodes Discord marks as the message's own content are collected.
    Everything else in there - the timestamp it shows on hover, the reply
    above it, the buttons under it - reads as text too, and would otherwise
    end up in the middle of the sentence.

    A message can also be nothing *but* a button: an emoji on its own, or a
    mention, is drawn as a button whose label is the words. Those are read
    from the label, but only where the button has no text of its own, or the
    same mention would be said twice.
    """
    identifier = str(group.get("identifier") or "")
    message_id = identifier[len(MESSAGE_GROUP):].rsplit("-", 1)[-1]
    author, stamp, words = "", "", []

    def visit(node, in_content=False):
        nonlocal author, stamp
        ident = str(node.get("identifier") or "")
        if any(ident.startswith(prefix) for prefix in NOT_TEXT):
            return
        if str(node.get("description") or "") == ACTIONS_LABEL:
            return
        if ident.startswith("message-username-"):
            author = author or _first(node, "title") or ""
            return
        if ident.startswith("message-timestamp-"):
            stamp = stamp or _first(node, "value") or ""
            return
        if node.get("subrole") == "AXTimeGroup":
            # The clock Discord hangs on a message: when it was sent in full,
            # and the "(edited)" marker, both of which live inside the content
            # group and both of which are said elsewhere or not at all.
            return
        if ident.startswith("message-content-"):
            in_content = True
        role, value = node.get("role"), node.get("value")
        if in_content and role in TEXT_ROLES and value:
            # The date under an edited message reads as content, and so would
            # a message that is only a timestamp - hence only dropping it when
            # there is something else to say.
            if not (words and _is_stamp(value)):
                words.append(str(value))
        elif in_content and role == "AXButton" and not _has_words(node):
            title = str(node.get("title") or "").strip()
            if title and not _EDITED.fullmatch(title):
                words.append(title)
        for child in node.get("children") or ():
            visit(child, in_content)

    visit(group)
    text = re.sub(r"\s+", " ", " ".join(words)).strip()
    if not text:
        text = _fallback_text(group, author)
    return {"id": message_id, "author": author, "time": stamp, "text": text}


def read_window(node):
    """Everything one window offers: its channel, its messages, its title.

    Messages written by whoever is signed in are left out. Sending one puts
    it back on the screen as an incoming message a moment later, and the
    preview of what just arrived is not supposed to be a mirror.
    """
    listing = find_message_list(node)
    title = node.get("title")
    if listing is None:
        return {"channel": "", "title": title, "messages": []}
    channel = str(listing.get("description") or "")[len(LIST_PREFIX):].strip()
    mine = current_user(node)
    messages, author, stamp = [], "", ""
    for child in listing.get("children") or ():
        if not str(child.get("identifier") or "").startswith(MESSAGE_GROUP):
            continue
        message = read_message(child)
        author = message["author"] or author
        stamp = message["time"] or stamp
        message["author"], message["time"] = author, stamp
        message["channel"] = channel
        if mine and _written_by(message, child, mine):
            continue
        messages.append(message)
    return {"channel": channel, "title": title, "messages": messages}


def _article_head(group):
    """Who the message's summary says wrote it, before it says what."""
    parts = [part.strip() for part in _article_title(group).split(" , ")
             if part.strip(" ,.;:!?")]
    return parts[0] if parts else ""


_CLIENT_TAG = re.compile(r"\s+(?:Web|Mobile|Desktop|iPhone|Android|Steam)$",
                         re.IGNORECASE)


def _written_by(message, group, mine):
    """Whether this message is the signed-in account's own.

    Most messages carry their author and are matched exactly - a "Niko" must
    not swallow a "Niko the Great". Discord leaves the name off a message
    that continues a run from the same author, so the first of a run can
    arrive nameless, and those are checked against the summary line instead:
    it opens with the name and closes with the client it was written from
    ("Niko Web , im writing fix code , 21:32").
    """
    if not mine:
        return False
    author = str(message.get("author") or "").strip()
    if author:
        return author.casefold() == str(mine).strip().casefold()
    head = _CLIENT_TAG.sub("", _article_head(group)).strip()
    return bool(head) and head.casefold() == str(mine).strip().casefold()


def unread_count(title):
    """How many the window title claims are waiting: '(2) Discord | ...'."""
    match = re.match(r"\((\d+)\)", str(title or ""))
    return int(match.group(1)) if match else 0


# -- the app ---------------------------------------------------------------

def find_app(identifiers):
    """(pid, name) of the first running app these ids or names point at."""
    from AppKit import NSWorkspace
    wanted = {str(item) for item in identifiers}
    lower = {item.lower() for item in wanted}
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        bundle = str(app.bundleIdentifier() or "")
        name = str(app.localizedName() or "")
        if bundle in wanted or name in wanted or name.lower() in lower:
            pid = int(app.processIdentifier())
            if pid > 0:
                return pid, name
    return None, None


def _voiceover_running():
    """Whether somebody else already owns the enhanced-interface switch."""
    import subprocess
    try:
        return subprocess.run(["pgrep", "-x", "VoiceOver"], capture_output=True,
                              timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Reader:
    """Holds one chat app's window open, and takes new messages off it.

    The first read is a baseline: whatever is already on screen arrived
    before anybody asked, so it is remembered rather than announced. Only
    what turns up afterwards counts as news.
    """

    def __init__(self, block=None, app=None):
        self.block = block or {}
        self.app = tuple(app) if app else None
        self.app_name = None
        self.last_error = None
        self.snapshot = None
        self.seconds = 0.0
        self._pid = None
        self._element = None
        self._enhanced = False
        self._seen = set()
        self._primed = False
        self._unread = None

    @property
    def connected(self):
        return self._element is not None

    @property
    def identifiers(self):
        """Which apps count as "the chat app" - config first, so a reload
        changes it without the reader being rebuilt."""
        if self.app:
            return self.app
        return tuple(self.block.get("apps") or DEFAULT_APPS)

    def connect(self):
        """Find the chat app and take the enhanced-interface switch for it."""
        if not is_trusted():
            self.last_error = ("macOS has not been asked for Accessibility - "
                               "run: g510 permissions")
            return False
        pid, name = find_app(self.identifiers)
        if not pid:
            self.app_name, self.last_error = None, "no chat app is running"
            self.close()
            return False
        if pid == self._pid and self._element is not None:
            return True
        self.close()
        self._pid, self.app_name = pid, name
        self._element = _AS.AXUIElementCreateApplication(pid)
        # The error code here is not one macOS means - the switch takes
        # anyway, which is why nothing is done with it.
        _AS.AXUIElementSetAttributeValue(
            self._element, _KEYS["AXEnhancedUserInterface"], _TRUE)
        self._enhanced = True
        self._primed = False
        self._seen = set()
        self._unread = None
        return True

    def close(self):
        """Hand the switch back, unless somebody else was holding it."""
        element, self._element = self._element, None
        self._pid = None
        if element is not None and self._enhanced and not _voiceover_running():
            _AS.AXUIElementSetAttributeValue(
                element, _KEYS["AXEnhancedUserInterface"], _FALSE)
        self._enhanced = False

    def windows(self):
        """The app's windows as plain dicts."""
        if self._element is None:
            return []
        ref = CFR()
        error = _AS.AXUIElementCopyAttributeValue(
            self._element, _KEYS["AXWindows"], ctypes.byref(ref))
        if error or not ref.value:
            return []
        try:
            if _CF.CFGetTypeID(ref.value) != _ARRAY_ID:
                return []
            budget = [40000]
            return [_node(_CF.CFArrayGetValueAtIndex(ref.value, index), 0, budget)
                    for index in range(_CF.CFArrayGetCount(ref.value))]
        finally:
            _CF.CFRelease(ref.value)

    def read(self):
        """Read the window the chat is in. None if the app will not answer."""
        if self._element is None and not self.connect():
            return None
        started = time.time()
        found = None
        try:
            for window in self.windows():
                found = read_window(window)
                if found.get("messages") or found.get("channel"):
                    break
        except OSError as exc:
            self.last_error = str(exc)
            self.close()
            return None
        self.seconds = time.time() - started
        self.snapshot = found or {"channel": "", "title": None, "messages": []}
        self.last_error = None
        return self.snapshot

    def take_new(self):
        """What has arrived since the last call. The first call yields none.

        The window title carries a count of its own - "(2) Discord | ..." -
        which is the only sign of anything happening in a channel other than
        the one on screen. It rises alongside a message that just landed in
        the open channel too, so it is only spoken for when nothing else was.
        """
        snapshot = self.snapshot
        if not snapshot:
            return []
        fresh = [message for message in snapshot.get("messages") or []
                 if message.get("id") and message["id"] not in self._seen]
        for message in fresh:
            self._seen.add(message["id"])
        count = unread_count(snapshot.get("title"))
        was = self._unread
        self._unread = count
        if not self._primed:
            # Whatever was already on the screen got there on its own.
            self._primed = True
            return []
        events = [dict(message, kind="message",
                       app=self.app_name or "Discord") for message in fresh]
        if not fresh and was is not None and count > was:
            events.append({"kind": "unread", "count": count,
                           "app": self.app_name or "Discord",
                           "channel": snapshot.get("channel") or "",
                           "title": snapshot.get("title") or ""})
        return events

    def latest(self, count=3):
        """The last few messages on screen, news or not."""
        messages = (self.snapshot or {}).get("messages") or []
        return messages[-count:]

    def status(self):
        return {"trusted": is_trusted(), "app": self.app_name,
                "running": self.connected, "error": self.last_error,
                "channel": (self.snapshot or {}).get("channel") or "",
                "title": (self.snapshot or {}).get("title") or "",
                "seconds": self.seconds}


def probe(block=None, count=3):
    """One read from a standing start, for `g510 discord`."""
    reader = Reader(block)
    started = time.time()
    if not reader.connect():
        return dict(reader.status(), messages=[], waited=0.0)
    snapshot = reader.read()
    waited = time.time() - started
    for _ in range(6):
        if snapshot and snapshot.get("messages"):
            break
        time.sleep(0.7)
        snapshot = reader.read()
        waited = time.time() - started
    out = dict(reader.status(), messages=reader.latest(count), waited=waited)
    reader.close()
    return out


# -- what macOS itself was told --------------------------------------------
#
# The window reader above can follow exactly one app: the one whose window it
# understands, and only while that window will answer. Everything else the
# machine has to say - Messages, Xcode, a launcher, Discord when its window
# cannot be read - is said by macOS, which writes every one of those
# announcements into its own store before it draws them. That store is a
# plain SQLite database in a group container, and unlike the Accessibility
# routes it needs no permission at all: it is the one channel left that
# carries ordinary apps' notifications.

NOTIFICATIONS_DB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db")
_CF_EPOCH = 978307200            # CFAbsoluteTime, as Unix time


def app_name(identifier):
    """What to call an app whose bundle id is all the store knows.

    The panel gives a name nine characters, so a bundle id would arrive cut
    off in the middle of itself. An app that is running can be asked for its
    own name; one that is not keeps the last part of its id, which at least
    reads like a name rather than a path.
    """
    if not identifier:
        return "macOS"
    try:
        from AppKit import NSWorkspace
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if str(app.bundleIdentifier() or "") == identifier:
                name = str(app.localizedName() or "")
                if name:
                    return name
    except (ImportError, OSError):
        pass
    tail = str(identifier).rsplit(".", 1)[-1]
    return tail or str(identifier)


def _banner(row, seconds):
    """One row of the store as an event the display can draw.

    A notification with neither a title nor a body says nothing worth the
    panel, and a row whose payload is in a shape this does not know is not
    news either - both are dropped rather than turned into a guess.
    """
    try:
        decoded = plistlib.loads(bytes(row["data"] or b""))
        request = decoded.get("req") if isinstance(decoded, dict) else None
        if not isinstance(request, dict):
            request = {}
        title = str(request.get("titl") or "").strip()
        body = str(request.get("body") or "").strip()
        when = row["delivered_date"]
        if not isinstance(when, (int, float)):
            when = decoded.get("date") if isinstance(decoded, dict) else None
    except (ValueError, TypeError, AttributeError, OSError):
        return None
    text = body or title
    if not text:
        return None
    clock = ""
    if isinstance(when, (int, float)):
        clock = time.strftime("%H:%M", time.localtime(when + _CF_EPOCH))
    return {"kind": "banner",
            "app": app_name(row["identifier"]),
            "bundle": str(row["identifier"] or ""),
            # With a body the title is who it is from, and goes where the
            # author of a message goes; without one it is the whole message
            # and is shown as the words themselves.
            "author": title if body else "",
            "text": text, "channel": clock, "time": clock,
            "seconds": seconds}


class Notifications:
    """macOS's record of what it has shown, read back a row at a time.

    Two properties of that record shape everything here:

      * **it is late.** The store commits about five seconds after the most
        recent notification, in one batch - so this is a mirror of what has
        already happened rather than a feed, and no polling interval can
        make it faster. What can be done is not saying the same thing twice
        (see `Watcher._from_the_window`) and not pretending it is live.
      * **it forgets.** Rows are pruned, so rec_ids have holes, and clearing
        the notification centre takes the newest ids with it. So what is
        tracked is the newest id read, and when the newest id in the store
        falls below that, the store has been cleared and the reader draws a
        new line rather than waiting for ids that will never come back.
    """

    def __init__(self, block=None):
        self.block = block or {}
        self.last = None               # newest rec_id handed over
        self.error = None
        self.polls = 0

    @property
    def wanted(self):
        """Whether banners are being asked for at all."""
        return bool(self.block.get("banners", True))

    @property
    def seconds(self):
        """How long one holds the panel for."""
        try:
            return max(0.0, float(self.block.get("banners_seconds", 10) or 0))
        except (TypeError, ValueError):
            return 10.0

    def status(self):
        return {"wanted": self.wanted, "error": self.error,
                "polls": self.polls, "newest": self.last,
                "seconds": self.seconds}

    def poll(self):
        """What the store gained since the last call, oldest first.

        The first call takes the backlog as its baseline and hands back
        nothing: those arrived before anybody was listening, and reading
        them out now would be announcing yesterday's post.
        """
        if not self.wanted:
            return []
        connection = self._open()
        if connection is None:
            return []
        try:
            rows = self._fresh(connection)
        except sqlite3.Error as exc:
            self.error = f"notification store: {exc}"
            return []
        finally:
            connection.close()
        events = [_banner(row, self.seconds) for row in rows]
        events = [event for event in events if event]
        self.error = None
        self.polls += 1
        return events

    def _open(self):
        """The store, read-only, or the reason it could not be opened."""
        connection = None
        try:
            # Opened afresh every pass rather than held: the store is
            # rewritten by the system, and a connection held across that
            # would go on answering about a file that is no longer there.
            connection = sqlite3.connect(
                f"file:{NOTIFICATIONS_DB}?mode=ro", uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            connection.execute("SELECT 1 FROM record LIMIT 1").fetchone()
            self.error = None
            return connection
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.close()
            self.error = f"notification store: {type(exc).__name__}: {exc}"
            return None

    def _fresh(self, connection):
        """Rows past `self.last`, and where the store now ends."""
        newest = connection.execute(
            "SELECT max(rec_id) FROM record").fetchone()[0]
        if newest is None:
            # Nothing there at all: cleared, or never filled in. The next
            # row to arrive is news rather than backlog, so the line is
            # drawn at zero rather than left standing at nothing - or a
            # store that starts empty would swallow everything that came
            # after it.
            self.last = 0
            return []
        if self.last is None or newest < self.last:
            # The first look at a store that already has a past, or a store
            # cleared behind us: either way the newest row is a line drawn
            # in the sand, not news.
            self.last = newest
            return []
        rows = connection.execute(
            "SELECT r.rec_id, r.delivered_date, r.data, a.identifier"
            " FROM record r LEFT JOIN app a ON a.app_id = r.app_id"
            " WHERE r.rec_id > ? ORDER BY r.rec_id", (self.last,)).fetchall()
        if rows:
            # The highest id *read*, not the highest that exists: a row
            # written while this query was running belongs to the next pass,
            # and taking its id now would hand it over twice.
            self.last = rows[-1]["rec_id"]
        return rows


# -- the watcher -----------------------------------------------------------

class Watcher:
    """Polls what an app draws *and* what macOS was told, on its own thread.

    Two sources, one queue: the agent drains them together and shows them
    the same way, which is the whole point of a second source - it changes
    what can be said, not how it is said.
    """

    def __init__(self, block=None, log=None):
        self.block = block or {}
        self.log = log or (lambda message: None)
        self.events = queue.Queue(maxsize=64)
        self.reader = Reader(self.block)
        self.banners = Notifications(self.block)
        self.running = False
        self.last_error = None
        self.polls = 0
        self._thread = None
        self._said = None             # the last problem worth mentioning

    @property
    def interval(self):
        try:
            return max(0.5, float(self.block.get("poll_seconds",
                                                  DEFAULT_POLL_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_POLL_SECONDS

    def start(self):
        if self._thread is not None:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="g510-chat")
        self._thread.start()

    def stop(self):
        self.running = False
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self.reader.close()

    def drain(self):
        """Everything that arrived, oldest first."""
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def poll_once(self):
        """One pass over both sources: the window, and what macOS was told.

        They are independent - a chat app that is not running, or a window
        that will not answer, says nothing about whether the rest of the
        machine may announce itself - so neither stops the other, and both
        land in the same queue for the agent to drain.
        """
        events = []
        chatty = self.poll_chat(events)
        self.poll_banners(events)
        for event in events:
            try:
                self.events.put_nowait(event)
            except queue.Full:
                break                       # the display is already behind
        if chatty:
            self.polls += 1
        return events

    def poll_chat(self, out):
        """What the window has. False when it had nothing to say.

        A failure here is written down rather than raised: the other source
        is still worth its poll, and an app that is not running for an hour
        must not silence the rest of the machine for an hour.
        """
        try:
            if not self.reader.connect():
                self.last_error = self.reader.last_error
                return False
            if not self.reader.read():
                self.last_error = self.reader.last_error
                return False
            out.extend(self.reader.take_new())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = None
        return True

    def poll_banners(self, out):
        """What macOS was told, minus what the window has already said."""
        if not self.banners.wanted:
            return
        try:
            found = self.banners.poll()
        except Exception as exc:            # a reader bug is not a lost source
            self.banners.error = f"notification store: {exc}"
            return
        for event in found:
            if not self._from_the_window(event):
                out.append(event)

    def _from_the_window(self, event):
        """Whether the window reader is reading this app already.

        Discord both draws the message and posts a notification for it, and
        the window has it inside a second or two while the store takes five -
        so without this the same words arrive twice, the late copy overwriting
        the live one on the panel. The window wins whenever it is there at
        all; when it is not (no app, no Accessibility, a different app
        entirely) the banner is the only route left and is let through.
        """
        if not getattr(self.reader, "connected", False):
            return False
        identifiers = {str(item) for item in
                       (getattr(self.reader, "identifiers", None) or ())}
        return (event.get("bundle") in identifiers
                or event.get("app") in identifiers)

    def _loop(self):
        while self.running:
            try:
                self.poll_once()
            except Exception as exc:             # never take the agent down
                self.last_error = f"{type(exc).__name__}: {exc}"
            # A chat app that is not running stays not running for as long as
            # it does, and saying so every two seconds would drown the log.
            # The store failing is the same kind of fact, said by the other
            # source, so only one of the two is ever spoken.
            problem = (f"chat watcher: {self.last_error}" if self.last_error
                       else (f"notification store: {self.banners.error}"
                             if self.banners.error else None))
            if problem != self._said:
                self._said = problem
                if problem:
                    self.log(problem)
            step = 0.25
            for _ in range(int(self.interval / step) or 1):
                if not self.running:
                    return
                time.sleep(step)
