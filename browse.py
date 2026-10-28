"""The Discord screen with keys: a browser over what the plugin pushed.

P6 handed the panel Discord's own view of one channel - a notification,
three lines, that nothing could be done about. This is the other half of
it: the event tap the menu already borrows the keyboard with is pointed at
this screen instead, so a channel can be picked, a message answered and a
mention written, all while the game keeps the front of the screen.

Three panes, and the keys move between them:

  channels  one list of channels at a time - direct messages, then each
            server, walked with left and right - and up and down along
            it. Enter opens the one under the cursor.
  messages  the conversation, a message at a time: up and down move,
            enter answers the message on show, right writes a new one
            instead, left goes back to the channels.
  compose   writing, printable keys and all. Enter sends, esc throws the
            draft away; an @ that starts a word opens the picker, where
            up and down choose between the people and the roles that fit,
            and enter puts the one chosen into the text.

Which keys the panel takes is `wants`, and it is deliberately narrow: in
a pane only the keys that mean something to it, so arrows come here and
letters still go to the game; while writing, everything printable, because
there is no game to be playing at with a sentence half typed. Cmd and the
other shortcut modifiers are never seen by any of this - `keytap` keeps
them for the application, which is how the panel is left again from
anywhere.

Nothing here talks to Discord, to the link or to the keyboard. `press`
takes one key and queues what to do about it; the caller runs those
(`drain`) and hands what came back through `update`. `view` hands the
screen what to draw - rows and marks, no pixels. That is the whole shape,
and it is why every part of it can be tested with a dict and a key name.
"""
import time

PANE_CHANNELS = "channels"
PANE_MESSAGES = "messages"
PANE_COMPOSE = "compose"

VISIBLE = 3              # rows the panel has under the header
PICKS = 3                # suggestions that fit while a draft is on show
DRAFT_MAX = 500          # longer than any message the panel could show
IDLE_COMPOSE = 60.0      # writing nobody goes on with gives the keys back
ERROR_SECONDS = 5.0      # how long a refusal stays on the panel
WAIT_SECONDS = 2.0       # how long a command is believed to be in flight
PAGE = 30                # messages one page of history asks the plugin for
PAGE_MAX = 100           # the most Discord will answer for in one request,
                         # which is what a page grows towards
PREFETCH = 2             # messages from the oldest held where the page
                         # behind them is already asked for, so climbing
                         # never stops dead at a page edge
OLDER_GAP = 1.0          # one page of history in flight at a time, however
                         # quickly the arrow key is tapped
SCROLL_LEAD = 2.5        # seconds a freshly highlighted message is held still,
                         # under the redraw's own second so the first line
                         # is never skipped by a phase that lands early
SCROLL_LAG = 0.6         # seconds per line: under the redraw interval, so
                         # every redraw climbs exactly one line rather than
                         # two redraws in a row being read as one delay
SCROLL_MAX = 400         # a ceiling, so one huge message cannot climb forever at the top

# What each pane takes. The names are `keytap`'s: nav keys are always
# these, and a printable character arrives as its own name with itself as
# the character.
PANE_KEYS = ("up", "down", "left", "right", "enter", "esc")
COMPOSE_KEYS = PANE_KEYS + ("backspace",)


def _index_of(rows, key, value):
    """Where a row with this id sits, or -1 for none at all."""
    if value is None:
        return -1
    wanted = str(value)
    for index, row in enumerate(rows or []):
        if str(row.get(key) or "") == wanted:
            return index
    return -1


def _token(draft):
    """The mention being written at the end of a draft, or None.

    Only an @ that *starts* a word counts, so an address in the middle of
    one (`somebody@example`) is not somebody being pinged. The word is
    taken whole - including the @ - because that is exactly the stretch of
    text a completed mention has to replace.
    """
    end = len(draft)
    start = end
    while start > 0 and not draft[start - 1].isspace():
        start -= 1
    word = draft[start:end]
    return word if word.startswith("@") else None


class Browser:
    """One browsing session: which pane, where the cursor is, what is owed.

    The cursors are *ids*, not positions, because the lists under them
    change while they are being read - a message arrives, the plugin
    restarts, a server is left. An id that is still there keeps its place;
    one that has gone falls back to a sensible row, which `update` is what
    does, once per redraw, from the state the plugin last pushed.
    """

    def __init__(self):
        self.active = False
        self.pane = PANE_CHANNELS
        # Which list the panel is walking (`@me` or a server id) - the
        # plugin's, not the client's: moving through servers here must not
        # drag Discord's own view along with it.
        self.list_id = "@me"
        self.channel_id = None
        self.message_id = None
        # How far down the highlighted message the reader has been carried,
        # in wrapped lines - what `_scroll` works out and the screen draws
        # a window onto, because the panel only has two lines for it.
        self.scroll = 0
        self._scroll_for = None
        self._scroll_at = 0.0
        # What a compose answers: set by `enter` on a message, cleared by
        # `right` for a plain message.
        self.reply_to = None
        self.reply_name = ""
        self.draft = ""
        # How the panel says a mention once one has been put in the text:
        # `<@123456>` is the only thing Discord understands, and the only
        # thing nobody can read off a two-line panel.
        self.labels = {}
        self.pick = None              # the suggestion highlighted, if any
        self.asked = ""               # the partial the last question was about
        self.pending_list = None      # a list the plugin has not confirmed
        self.pending_open = None
        self.pending_title = ""
        self.pending_until = 0.0
        self.sent_n = None            # whose failure would be worth telling
        self.error = None
        self.error_until = 0.0
        self.older_at = 0.0
        self.older_for = None         # whose history `older_n` counts pages of
        self.older_n = 0              # pages asked for there: the page grows
        self.touch = 0.0              # the last key, for the idle timeout
        self._state = {}
        self._outbox = []

    # -- life ---------------------------------------------------------------

    def open(self, now=None):
        """Take the keys. The cursors stay where they were left."""
        self.active = True
        if self.pane == PANE_COMPOSE:
            # A draft is half a sentence and nothing has asked for it yet:
            # coming back starts from the channels, where leaving did.
            self.pane = PANE_CHANNELS
        self.touch = time.time() if now is None else now
        return self

    def close(self):
        """Let the keys go, and take the unfinished text with them."""
        self.active = False
        self.pane = PANE_CHANNELS
        self.draft = ""
        self.pick = None
        self.asked = ""
        self.reply_to = None
        self.reply_name = ""
        self.error = None
        self.pending_list = None
        self.pending_open = None
        self._outbox = []
        return self

    def abandon(self):
        """The compose timed out: back to the messages, draft and all.

        The note does the same thing after `NOTE_IDLE` for the same
        reason - a pane nobody is at must not go on holding the keyboard
        from a game the user has gone back to.
        """
        self.draft = ""
        self.pick = None
        self.asked = ""
        self.reply_to = None
        self.reply_name = ""
        self.pane = PANE_MESSAGES
        return self

    @property
    def typing(self):
        """A compose is on show, so the panel wants the printable keys."""
        return self.active and self.pane == PANE_COMPOSE

    def wants(self, name, character=None):
        """Whether one key belongs to the panel while browsing.

        Called on the tap's own thread, so this reads flags and nothing
        else - the pane, and whether a character came with the key.
        """
        if not self.active:
            return False
        if self.pane == PANE_COMPOSE:
            return name in COMPOSE_KEYS or bool(character)
        return name in PANE_KEYS

    # -- what the plugin said -----------------------------------------------

    def update(self, state, now=None):
        """Take the newest push: settle what moved, and what was asked.

        Everything that can be *learned* from a push happens here rather
        than when a key is pressed, because most of what the browser waits
        for has no key attached: a channel opening, a list arriving, an
        acknowledgement coming back from a send that failed.
        """
        now = time.time() if now is None else now
        self._state = state if isinstance(state, dict) else {}

        open_id = str((self._state.get("channel") or {}).get("id") or "")
        if open_id != self.older_for:
            # Another conversation: the history behind this one is its own,
            # so the pages start small again instead of at whatever the last
            # channel had grown to - and a channel with nothing open behind
            # it says so rather than inheriting a count from nowhere.
            self.older_for = open_id
            self.older_n = 0

        # A client with a hidden-channel plugin lists every hidden channel
        # it can see, and Discord has a great many of them - enough that
        # the handful anyone can actually open is a page of scrolling
        # away. Filtered here rather than in the plugin so nothing has to
        # be rebuilt, and by name because the name is all the push
        # carries for them.
        listed_channels = self._state.get("channels") or []
        shown = [row for row in listed_channels
                 if "hidden" not in str(row.get("name") or "").lower()]
        if shown != listed_channels:
            self._state = {**self._state, "channels": shown}

        listed = str(self._state.get("list") or "")
        if listed and self.pending_list == listed:
            self.pending_list = None
        if listed and self.pending_list is None:
            # The plugin follows the client's own selection when nothing
            # has asked for a list, so this is how a server changed by
            # hand on the computer comes back to the panel.
            self.list_id = listed

        channels = self._state.get("channels") or []
        if self.channel_id is not None and not channels:
            self.channel_id = None
        elif self.channel_id is not None \
                and _index_of(channels, "id", self.channel_id) < 0:
            # The list changed underneath the cursor - a list switch, a
            # server left, a plugin restarted. Point at the top of what
            # is there now rather than at a channel that is not.
            self.channel_id = str(channels[0].get("id") or "") or None

        messages = self._state.get("messages") or []
        if self.message_id is not None \
                and _index_of(messages, "id", self.message_id) < 0:
            # None means "the newest one" to the view, which is where a
            # conversation that dropped its place should land.
            self.message_id = None

        if self.pending_open and (
                (self._state.get("channel") or {}).get("id")
                == self.pending_open or now >= self.pending_until):
            self.pending_open = None
        if self.pending_list and now >= self.pending_until:
            self.pending_list = None

        ack = self._state.get("ack") or {}
        if self.sent_n is not None and ack.get("n") is not None \
                and int(ack.get("n")) >= self.sent_n:
            if not ack.get("ok"):
                self.set_error(str(ack.get("error") or "it did not send"), now)
            self.sent_n = None

        if self.error and now >= self.error_until:
            self.error = None
        self._scroll(messages, now)
        return self

    def _scroll(self, messages, now):
        """Carry the highlighted message up the panel, a line at a time.

        The panel has two lines under the author, so anything longer than
        that used to end in "..." with the rest of it out of reach - and no
        key is spare to scroll with, because the arrows are what move
        between messages. Highlighting a message therefore reads it: the
        first two lines are held still long enough to be taken in, then the
        text climbs a line at a time until it has all gone past or the
        cursor moves, which starts it again from the top.

        The climb is driven from here, once per redraw, rather than by a
        timer of its own: `update` is the one place that already knows both
        the moment and what is on show, and a redraw a second is exactly
        often enough for text to be read at.
        """
        if not self.active or self.pane != PANE_MESSAGES:
            self.scroll = 0
            self._scroll_for = None
            return
        index = _index_of(messages, "id", self.message_id)
        row = messages[index] if index >= 0 else (
            messages[-1] if messages else None)
        # What is on show, as one key: the channel, the message and its
        # words. Any of them changing is a different thing to read, and
        # the view falls back to the newest message the same way this does.
        key = (self.channel_id,
               str(row.get("id") or "") if row else "",
               str(row.get("text") or "") if row else "")
        if row is None:
            # An empty channel: there is nothing here to climb.
            self.scroll = 0
            self._scroll_for = key
            return
        if key != self._scroll_for:
            self._scroll_for = key
            self.scroll = 0
            self._scroll_at = now + SCROLL_LEAD
            return
        if self.scroll < SCROLL_MAX and now >= self._scroll_at:
            self.scroll += 1
            self._scroll_at = now + SCROLL_LAG

    def set_error(self, text, now=None):
        """Put a refusal on the panel for a few seconds, and keep it short.

        What is written here is what the panel draws in one line, so the
        reason is cut at the first " - " the agent writes: the detail
        lives in the log, and the panel only has sixteen characters of
        opinion to give.
        """
        now = time.time() if now is None else now
        short = str(text or "").split(" - ")[0].strip()[:120]
        self.error = short or "that did not work"
        self.error_until = now + ERROR_SECONDS
        return self.error

    def sent(self, number, now=None):
        """A send went out: remember whose answer would matter."""
        try:
            self.sent_n = int(number)
        except (TypeError, ValueError):
            self.sent_n = None
        return self.sent_n

    def drain(self):
        """Everything the panes asked for since the last call."""
        out, self._outbox = self._outbox, []
        return out

    # -- keys ---------------------------------------------------------------

    def press(self, name, character=None, now=None):
        """One key, and what the panes do about it: the actions to run."""
        if not self.active:
            return []
        now = time.time() if now is None else now
        self.touch = now
        if self.pane == PANE_MESSAGES:
            # Every key here moves the cursor onto a different message, so
            # reading the new one starts at its first line - and it does so
            # on the press rather than on the next push, so the panel never
            # shows the tail of the message that has just been left.
            self.scroll = 0
            self._scroll_for = None
        if self.pane == PANE_CHANNELS:
            self._press_channels(name, now)
        elif self.pane == PANE_MESSAGES:
            self._press_messages(name, now)
        else:
            self._press_compose(name, character)
        return self.drain()

    def _press_channels(self, name, now):
        state = self._state
        channels = state.get("channels") or []
        if name in ("up", "down"):
            if not channels:
                return
            index = _index_of(channels, "id", self.channel_id)
            if index < 0:
                # Nothing selected yet: down starts at the top, up at the
                # bottom, which is the way each key points.
                index = 0 if name == "down" else len(channels) - 1
            else:
                index = (index + (-1 if name == "up" else 1)) % len(channels)
            self.channel_id = str(channels[index].get("id") or "") or None
            return
        if name == "enter":
            row = self._channel(channels)
            if not row:
                return
            self.pending_open = str(row.get("id"))
            self.pending_title = str(row.get("name") or "")
            self.pending_until = now + WAIT_SECONDS
            self.message_id = None
            self.reply_to = None
            self.reply_name = ""
            self.pane = PANE_MESSAGES
            self._outbox.append(("open", self.pending_open))
            return
        if name in ("left", "right"):
            # One list at a time: the DMs, then each server in the order
            # the sidebar holds them. Asking the plugin for a list costs
            # a round trip, so nothing here pretends to know its channels
            # before the answer arrives - `pending_list` is what the view
            # waits on.
            lists = state.get("lists") or []
            if not lists:
                return
            index = _index_of(lists, "id", self.list_id)
            if index < 0:
                index = 0 if name == "right" else len(lists) - 1
            else:
                index = (index + (-1 if name == "left" else 1)) % len(lists)
            wanted = str(lists[index].get("id") or "")
            if not wanted or wanted == self.list_id:
                return
            self.list_id = wanted
            self.channel_id = None
            self.pending_list = wanted
            self.pending_title = str(lists[index].get("name") or "")
            self.pending_until = now + WAIT_SECONDS
            self._outbox.append(("list", wanted))
            return
        if name == "esc":
            self.close()

    def _ask_older(self, state, now):
        """Queue one page of the history behind what is held.

        The page grows while the reader climbs - 30, then 60, then as much
        as Discord will answer for in one request - because the further
        back somebody reads, the fewer keystrokes they are prepared to
        spend on the way there. It grows per channel and starts over in
        the next one, where there is no depth behind it yet.

        Two things stop this running away: the gap, which keeps one page
        in flight however fast the arrow is tapped, and `has_more`, which
        the plugin drops once it can no longer make the buffer any bigger.
        """
        if not state.get("has_more"):
            return
        if now - self.older_at < OLDER_GAP:
            return
        self.older_at = now
        size = min(PAGE << min(self.older_n, 4), PAGE_MAX)
        self.older_n += 1
        self._outbox.append(("older", size))

    def _press_messages(self, name, now):
        state = self._state
        messages = state.get("messages") or []
        if name in ("up", "down"):
            if not messages:
                return
            index = _index_of(messages, "id", self.message_id)
            if index < 0:
                index = len(messages) - 1        # start on the newest
            if name == "up":
                if index == 0:
                    # Nothing above to move onto, so this press *is* the
                    # ask for the page behind what is held - but only one
                    # of those at a time: a key held down must not turn
                    # into fifty requests the plugin queues up.
                    self._ask_older(state, now)
                    return
                index -= 1
                if index <= PREFETCH:
                    # Coming up on the oldest held: ask for the next page
                    # while there is still somewhere for the cursor to go.
                    # Waiting until there is nothing left reads as a stall
                    # at every page edge, which is the whole reason the
                    # first line of a long scroll used to feel stuck.
                    self._ask_older(state, now)
            else:
                if index >= len(messages) - 1:
                    return
                index += 1
            self.message_id = str(messages[index].get("id") or "") or None
            return
        if name == "enter":
            row = self._message(messages)
            self._compose(reply=row)
            return
        if name == "right":
            self._compose(reply=None)
            return
        if name == "left":
            self.pane = PANE_CHANNELS
            return
        if name == "esc":
            self.close()

    def _compose(self, reply=None):
        """Start writing: against a message, or plainly in the channel."""
        self.pane = PANE_COMPOSE
        self.draft = ""
        self.pick = None
        self.asked = ""
        self.reply_to = str(reply.get("id")) if reply else None
        self.reply_name = str(reply.get("author") or "") if reply else ""

    def _press_compose(self, name, character):
        if name == "enter":
            picks = self.picks()
            if self.pick is not None and picks:
                self._insert(picks[self.pick])
                return
            self._send()
            return
        if name == "esc":
            if self.pick is not None:
                # Back out of the picker first: without this, a list that
                # has been arrowed through could never be left *and* sent.
                self.pick = None
                return
            # The draft is thrown away, the same as a note's is: esc
            # means "not this", everywhere on the panel.
            self.pane = PANE_MESSAGES
            self.reply_to = None
            self.reply_name = ""
            self.draft = ""
            self.asked = ""
            return
        if name in ("up", "down"):
            self._move_pick(name)
            return
        if name == "backspace":
            self.draft = self.draft[:-1]
            self.pick = None
            self._ask()
            return
        if character and len(self.draft) < DRAFT_MAX:
            self.draft += character
            self.pick = None
            self._ask()

    def _move_pick(self, name):
        picks = self.picks()
        if not picks:
            return
        if self.pick is None or self.pick >= len(picks):
            # Nothing highlighted yet: down takes the first offer, up the
            # last - each key points the way it looks.
            self.pick = 0 if name == "down" else len(picks) - 1
        else:
            self.pick = (self.pick + (1 if name == "down" else -1)) % len(picks)

    def _insert(self, item):
        """Put one suggestion into the text, as the code Discord reads.

        The panel shows it afterwards as `@name` (see `say_it`), so the
        raw `<@123456>` is never what has to be read back off the panel -
        but it is exactly what gets sent.
        """
        raw = str(item.get("insert") or "")
        token = _token(self.draft)
        if not raw or token is None:
            return
        self.labels[raw] = str(item.get("label") or raw)
        self.draft = self.draft[:len(self.draft) - len(token)] + raw + " "
        self.pick = None
        self.asked = ""

    def _send(self):
        """Hand the draft over - as an answer, or as a new message."""
        text = (self.draft or "").strip()
        self.draft = ""
        self.pick = None
        self.asked = ""
        self.pane = PANE_MESSAGES
        if not text:
            # Nothing written: enter just closes the compose. Sending an
            # empty message is not a thing anybody means to do.
            self.reply_to = None
            self.reply_name = ""
            return
        if self.reply_to:
            self._outbox.append(("reply", self.reply_to, text))
        else:
            self._outbox.append(("say", text))
        self.reply_to = None
        self.reply_name = ""

    def _ask(self):
        """Ask the plugin for whoever fits the word being written.

        Asked once per word - `asked` is what stops a keystroke from
        costing a command when nothing about the mention has changed - and
        never when there is no mention being written at all.
        """
        token = _token(self.draft)
        if token is None:
            self.asked = ""
            self.pick = None
            return
        partial = token[1:]              # without the @: that is not a name
        if partial == self.asked:
            return
        self.asked = partial
        self.pick = None
        self._outbox.append(("suggest", partial))

    # -- what is being shown -------------------------------------------------

    def picks(self):
        """The suggestions that fit the word being written, in order.

        Only the answer to the *current* word counts: the plugin answers
        a command that was sent a second ago, and a list for a word that
        has already moved on would put the wrong person in the text.
        """
        token = _token(self.draft)
        if token is None:
            return []
        offered = self._state.get("suggest") or {}
        if str(offered.get("for") or "") != token[1:]:
            return []
        return [row for row in (offered.get("items") or []) if row]

    def say_it(self, text):
        """The draft as the panel reads it: mention codes as their names."""
        out, index = "", 0
        text = str(text or "")
        while index < len(text):
            if text[index] == "<":
                end = text.find(">", index)
                if end != -1:
                    code = text[index:end + 1]
                    label = self.labels.get(code)
                    if label is not None:
                        out += "@" + str(label)
                        index = end + 1
                        continue
            out += text[index]
            index += 1
        return out

    def _channel(self, channels):
        index = _index_of(channels, "id", self.channel_id)
        if index < 0:
            return channels[0] if channels else None
        return channels[index]

    def _message(self, messages):
        index = _index_of(messages, "id", self.message_id)
        if index < 0:
            return messages[-1] if messages else None
        return messages[index]

    def view(self):
        """What the screen draws: rows and marks, no pixels.

        The screen fits a header and three rows, so every pane answers to
        that shape - and the things a pane cannot know itself (which row
        is in the middle, how much history is behind) come from the state
        the plugin last pushed, which `update` has already settled.
        """
        if self.pane == PANE_MESSAGES:
            return self._view_messages()
        if self.pane == PANE_COMPOSE:
            return self._view_compose()
        return self._view_channels()

    def _header(self, title, count=""):
        out = {"mode": self.pane, "title": str(title or "DISCORD")}
        if count:
            out["count"] = str(count)
        if self.error:
            out["error"] = self.error
        return out

    def _view_channels(self):
        state = self._state
        lists = state.get("lists") or []
        active = next((row for row in lists
                       if str(row.get("id") or "") == self.list_id), None)
        title = str((active or {}).get("name")
                    or ("DMs" if self.list_id == "@me" else "DISCORD"))
        if self.pending_list:
            # The plugin has not said what the new list holds yet, and
            # showing the old one under the new name would be a lie the
            # user acts on.
            return {**self._header(self.pending_title or title), "waiting": "..."}
        channels = state.get("channels") or []
        index = _index_of(channels, "id", self.channel_id)
        count = f"{index + 1}/{len(channels)}" if index >= 0 else ""
        out = self._header(title, count)
        if not channels:
            out["rows"] = []
            return out
        start = 0
        if index >= 0:
            start = max(0, min(index - 1, len(channels) - VISIBLE))
        rows = []
        for row in channels[start:start + VISIBLE]:
            rows.append({
                "mark": self._mark(row, index, channels),
                "name": str(row.get("name") or row.get("id") or "?"),
            })
        out["rows"] = rows
        return out

    def _mark(self, row, index, channels):
        """What one line of the channel list says about itself.

        The cursor wins, because it is the one that decides: everything
        else is news about the channel, and there is one character of
        room for it.
        """
        here = channels[index] if 0 <= index < len(channels) else None
        if row is here:
            return ">"
        if row.get("mentions"):
            return "@"
        if row.get("unread"):
            return "*"
        if row.get("current"):
            return "."
        return " "

    def _view_messages(self):
        state = self._state
        if self.pending_open:
            return {**self._header(self.pending_title or "DISCORD"),
                    "waiting": "opening..."}
        channel = state.get("channel") or {}
        title = str(channel.get("name") or self.pending_title or "DISCORD")
        messages = state.get("messages") or []
        index = _index_of(messages, "id", self.message_id)
        row = messages[index] if index >= 0 else None
        count = f"{index + 1}/{len(messages)}" if row else ""
        if state.get("has_more"):
            count = "<" + count
        out = self._header(title, count)
        if not messages:
            out["empty"] = True
            return out
        if row is None:
            row = messages[-1]
            index = len(messages) - 1
            out["count"] = ("<" if state.get("has_more") else "") \
                + f"{index + 1}/{len(messages)}"
        out["author"] = str(row.get("author") or "?")
        out["text"] = str(row.get("text") or "")
        # How far down it the reader already is: the screen windows the
        # wrapped lines from here, so a message longer than the two lines
        # the panel has for it goes on being read rather than cut short.
        out["scroll"] = self.scroll
        if row.get("reply_to"):
            out["reply"] = str(row.get("reply_to"))
        return out

    def _view_compose(self):
        state = self._state
        channel = state.get("channel") or {}
        out = self._header(str(channel.get("name") or "DISCORD"))
        out["draft"] = self.say_it(self.draft)
        if self.reply_to:
            out["reply"] = self.reply_name or "someone"
        else:
            out["reply"] = None
        picks = self.picks()
        if self.pick is not None and self.pick >= len(picks):
            self.pick = None                   # the list moved under it
        if picks:
            start = 0
            if self.pick is not None:
                start = max(0, min(self.pick - PICKS + 1,
                                   len(picks) - PICKS))
            rows = []
            for offset, item in enumerate(picks[max(0, start):start + PICKS]):
                rows.append({
                    "cursor": start + offset == self.pick,
                    "label": "@" + str(item.get("label") or "?"),
                })
            out["picks"] = rows
            out["count"] = (f"{self.pick + 1}/{len(picks)}"
                            if self.pick is not None else str(len(picks)))
        return out
