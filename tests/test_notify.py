"""What arrives on the panel, and where it comes from.

Two sources, pinned down here. The first is a chat window nobody hands to
macOS: Discord - and Vesktop, and the clients built like it - keeps its
messages in its own window: there is no notification to show and no store on
disk to read. What the window does have is an Accessibility tree, and built
fully it carries every message on screen.

So the parts pinned down here are the ones that turn that tree into
sentences: finding the list the channel is drawn into, reading one message
group out of it, telling news from history, and how often the reader is
allowed to ask. Reading a real window is not tested - that needs a chat app
running and the Accessibility permission, which is what `g510 discord` is
for.

The second source is everything *else*, which macOS announces itself and
writes into a store of its own. That store is a real file on any Mac this
suite runs on, so `setUpModule` points the path at a directory of our own
and the tests below build a store in it - reading the machine's actual
notifications to decide whether a unit test passes would be no test at all.
"""
import os
import plistlib
import shutil
import sqlite3
import tempfile
import time
import unittest

import notify


# -- the store macOS keeps, redirected for the length of this module --------

_REAL_DB = notify.NOTIFICATIONS_DB
_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="g510-notifications-")
    notify.NOTIFICATIONS_DB = os.path.join(_TMP, "db")


def tearDownModule():
    notify.NOTIFICATIONS_DB = _REAL_DB
    shutil.rmtree(_TMP, ignore_errors=True)


_APP_IDS = {}


def store_row(rec_id, identifier, title, body=None, when=0.0):
    """One notification the way macOS writes one: an app, a row, a plist."""
    request = {"titl": title}
    if body is not None:                # a plist has no None to write
        request["body"] = body
    payload = plistlib.dumps({"styl": 0, "app": identifier, "date": when,
                              "req": request})
    app_id = _APP_IDS.setdefault(identifier, len(_APP_IDS) + 1)
    connection = sqlite3.connect(notify.NOTIFICATIONS_DB)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO app (app_id, identifier) VALUES (?, ?)",
            (app_id, identifier))
        connection.execute(
            "INSERT INTO record (rec_id, app_id, data, delivered_date)"
            " VALUES (?, ?, ?, ?)",
            (rec_id, app_id, payload, when))
        connection.commit()
    finally:
        connection.close()


def fresh_store():
    """An empty store in the shape macOS writes one, ready to be filled."""
    path = notify.NOTIFICATIONS_DB
    if os.path.exists(path):
        os.remove(path)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR,"
            " badge INTEGER);"
            "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER,"
            " uuid BLOB, data BLOB, request_date REAL, request_last_date REAL,"
            " delivered_date REAL, presented BOOL, style INTEGER,"
            " snooze_fire_date REAL);")
        connection.commit()
    finally:
        connection.close()


def node(role, identifier=None, title=None, value=None, description=None,
         subrole=None, children=()):
    """One element of the tree, in the shape the reader builds."""
    return {"role": role, "subrole": subrole, "identifier": identifier,
            "title": title, "value": value, "description": description,
            "children": list(children)}


def text(value, **fields):
    return node("AXStaticText", value=value, **fields)


def message_group(identifier, author=None, stamp=None, content=None,
                  title=None, extra=()):
    """A `chat-messages-...` group the way Discord draws one."""
    children = []
    if author is not None:
        children.append(node("AXGroup", identifier="message-username-1",
                             title=author))
    if stamp is not None:
        children.append(node("AXGroup", identifier="message-timestamp-1",
                             value=stamp))
    if content is not None:
        children.append(node("AXGroup", identifier="message-content-1",
                             children=[text(content)]))
    children.extend(extra)
    return node("AXGroup", identifier=identifier, title=title,
                children=children)


def listing(*children):
    return node("AXList", description="Messages in #general",
                children=list(children))


def account_panel(name="Niko", state="Idle", custom=None):
    """The panel at the bottom of the sidebar: whose client this is."""
    return node("AXGroup", description="User status and settings", children=[
        node("AXGroup", children=[
            node("AXButton", description="Manage profile and status")]),
        node("AXGroup", children=[text(name)]),
        node("AXGroup", children=[node("AXGroup", children=[
            node("AXStaticText", value=custom or "grok_by_xai"),
            node("AXGroup", children=[text(state)])])]),
        node("AXCheckBox", description="Mute"),
        node("AXGroup", children=[text("Unmute")]),
    ])


class CurrentUserTests(unittest.TestCase):
    """Whose messages these are - read off the account's own panel."""

    def test_the_name_the_panel_shows(self):
        window = node("AXWindow", children=[account_panel("Niko")])
        self.assertEqual(notify.current_user(window), "Niko")

    def test_the_state_word_under_the_avatar_is_not_a_name(self):
        window = node("AXWindow", children=[
            node("AXGroup", description="User status and settings",
                 children=[text("Idle"), text("Niko")])])
        self.assertEqual(notify.current_user(window), "Niko")

    def test_no_panel_means_nothing_is_claimed(self):
        self.assertIsNone(notify.current_user(node("AXWindow", children=[])))
        self.assertIsNone(notify.current_user(None))


class FindListTests(unittest.TestCase):
    """Which list in the window the channel is drawn into.

    The sidebar is lists all the way down, so shape alone would find the
    wrong one.
    """

    def test_it_goes_by_what_the_list_calls_itself(self):
        sidebar = node("AXList", description="Servers",
                       children=[node("AXListItem", title="osu!")])
        tree = node("AXWindow", children=[
            sidebar,
            node("AXGroup", children=[listing()]),
        ])
        found = notify.find_message_list(tree)
        self.assertIs(found, tree["children"][1]["children"][0])

    def test_a_sidebar_list_is_not_the_conversation(self):
        tree = node("AXWindow", children=[
            node("AXList", description="Direct Messages"),
        ])
        self.assertIsNone(notify.find_message_list(tree))

    def test_nothing_to_look_at(self):
        self.assertIsNone(notify.find_message_list(None))


class ReadMessageTests(unittest.TestCase):
    """One message group as {id, author, time, text}."""

    def test_id_comes_off_the_end_of_the_dom_identifier(self):
        group = message_group(
            "chat-messages-1557515832391634945-1563995524103802923",
            author="Niko", stamp="20:19", content="hello there")
        self.assertEqual(
            group["identifier"].rsplit("-", 1)[-1],
            notify.read_message(group)["id"])

    def test_the_whole_message(self):
        group = message_group("chat-messages-1-2", author="Niko",
                              stamp="20:19", content="hello there")
        self.assertEqual(notify.read_message(group),
                         {"id": "2", "author": "Niko", "time": "20:19",
                          "text": "hello there"})

    def test_only_what_is_inside_the_content_counts(self):
        # The timestamp Discord shows when you hover a message is text too,
        # and would otherwise end up in the middle of the sentence.
        group = node("AXGroup", identifier="chat-messages-1-2", children=[
            node("AXGroup", identifier="message-content-1", children=[
                node("AXGroup", identifier="message-timestamp-hover",
                     value="Sunday, October 25, 2026 at 20:19"),
                node("AXHeading", children=[text("YO WTH")]),
                text("the words themselves"),
            ]),
        ])
        self.assertEqual(notify.read_message(group)["text"],
                         "YO WTH the words themselves")

    def test_a_reply_is_not_read_twice(self):
        # The words it replies to sit in their own subtree, and are a copy
        # of something already read from further up the channel.
        group = message_group(
            "chat-messages-1-2", author="Moonberry", stamp="20:19",
            content="agreed",
            extra=[node("AXGroup", identifier="message-reply-context",
                        children=[node("AXGroup",
                                       identifier="message-content-9",
                                       children=[text("the words replied to")])])])
        self.assertEqual(notify.read_message(group)["text"], "agreed")

    def test_reactions_and_buttons_are_not_said_out_loud(self):
        group = message_group(
            "chat-messages-1-2", author="Niko", stamp="20:19", content="hi",
            extra=[node("AXGroup", identifier="message-reactions",
                        children=[text("2")]),
                   node("AXGroup", identifier="message-buttons",
                        children=[text("Reply")])])
        self.assertEqual(notify.read_message(group)["text"], "hi")

    def test_several_paragraphs_come_out_as_one_line(self):
        group = message_group("chat-messages-1-2", author="Niko",
                              content=None)
        group["children"].append(
            node("AXGroup", identifier="message-content-1",
                 children=[text("first"), text("second"), text("third")]))
        self.assertEqual(notify.read_message(group)["text"],
                         "first second third")

    def test_an_attachment_without_words_still_says_what_it_is(self):
        # Discord summarises a whole message in the article's title:
        # "author , what it carries , time".
        group = message_group("chat-messages-1-2", author="Niko",
                              title="Niko , vote.png , 20:19",
                              extra=[node("AXStaticText",
                                         subrole="AXDocumentArticle",
                                         title="Niko , vote.png , 20:19")])
        self.assertEqual(notify.read_message(group)["text"], "vote.png")

    def test_a_message_with_nothing_at_all_to_show(self):
        group = node("AXGroup", identifier="chat-messages-1-2")
        self.assertEqual(notify.read_message(group)["text"], "(attachment)")

    def test_a_group_without_an_identifier_does_not_raise(self):
        message = notify.read_message(node("AXGroup"))
        self.assertEqual(message["id"], "")
        self.assertEqual(message["text"], "(attachment)")

    def test_an_emoji_on_its_own_is_read_from_the_button_that_is_it(self):
        # An emoji, and a message that is only one, is drawn as a button
        # whose label is the character - there is no text node at all.
        group = message_group("chat-messages-1-2", author="Moonberry",
                              stamp="20:19")
        group["children"].append(node(
            "AXGroup", identifier="message-content-1", children=[
                node("AXButton", title="\U0001F62D",
                     children=[node("AXImage", description="\U0001F62D")])]))
        self.assertEqual(notify.read_message(group)["text"], "\U0001F62D")

    def test_a_mention_is_said_once_rather_than_twice(self):
        # The button's label and the text inside it are the same words, and
        # a message that reads them both comes out as "@Niko @Niko hello".
        group = message_group("chat-messages-1-2", author="Moonberry",
                              stamp="20:19")
        group["children"].append(node(
            "AXGroup", identifier="message-content-1", children=[
                node("AXButton", title="@Niko", children=[text("@Niko")]),
                text(" look at this")]))
        self.assertEqual(notify.read_message(group)["text"],
                         "@Niko look at this")

    def test_the_hover_menu_is_not_part_of_the_message(self):
        # Discord hangs its toolbar inside the content group, and every
        # button in it is labelled by a text node of its own.
        group = message_group("chat-messages-1-2", author="Niko",
                              stamp="20:19")
        group["children"].append(node(
            "AXGroup", identifier="message-content-1", children=[
                text("the actual sentence"),
                node("AXGroup", description="Message Actions", children=[
                    node("AXGroup", children=[text("Quick Mention")]),
                    node("AXGroup", children=[text("Edit")])])]))
        self.assertEqual(notify.read_message(group)["text"],
                         "the actual sentence")

    def test_the_clock_and_the_date_of_an_edit_are_not_words(self):
        # Both sit inside the content group: "(edited)" as a button with a
        # time group around it, and the full timestamp beside it.
        edited = node("AXGroup", children=[
            node("AXGroup", children=[
                node("AXGroup", subrole="AXTimeGroup", children=[
                    node("AXButton", title="(edited)", children=[
                        text("("), text("edited"), text(")")])])]),
            node("AXGroup", children=[
                node("AXGroup", children=[
                    text("Sunday, October 25, 2026 at 21:34")])])])
        group = message_group("chat-messages-1-2", author="Moonberry",
                              stamp="20:19")
        group["children"].append(node(
            "AXGroup", identifier="message-content-1",
            children=[text("Oh good"), text(" "), edited]))
        self.assertEqual(notify.read_message(group)["text"], "Oh good")

    def test_a_message_that_is_only_a_time_still_says_it(self):
        # The clock is only noise when there is something else to read.
        group = message_group("chat-messages-1-2", author="Niko",
                              stamp="20:19")
        group["children"].append(node(
            "AXGroup", identifier="message-content-1",
            children=[text("21:39")]))
        self.assertEqual(notify.read_message(group)["text"], "21:39")

    def test_the_summary_leaves_out_the_author_and_the_commas(self):
        # A bot's message with no content group of its own is read from the
        # summary line: "greed Verified App , :warn: ... , , , 20:19". The
        # badge is not the name, and the bare commas are Discord shrugging.
        group = message_group(
            "chat-messages-1-2", author="greed",
            title="greed Verified App , :warn: @Niko : Nothing is playing. "
                  ", , , 20:19",
            extra=[node("AXStaticText", subrole="AXDocumentArticle",
                        title="greed Verified App , :warn: @Niko : Nothing "
                              "is playing. , , , 20:19")])
        message = notify.read_message(group)
        self.assertEqual(message["author"], "greed")
        self.assertEqual(message["text"], ":warn: @Niko : Nothing is playing.")


class ReadWindowTests(unittest.TestCase):
    """What one window offers: the channel, its messages, its title."""

    def test_the_channel_and_the_messages(self):
        window = node("AXWindow", title="(2) Discord | #general", children=[
            node("AXGroup", children=[
                listing(node("AXSplitter", description="October 25, 2026"),
                        message_group("chat-messages-1-10", author="Niko",
                                      stamp="20:19", content="first"),
                        message_group("chat-messages-1-11", content="second"),
                        message_group("chat-messages-1-12", content="third"),
                        node("AXButton", title="Mark As Read"))]),
        ])
        found = notify.read_window(window)
        self.assertEqual(found["channel"], "#general")
        self.assertEqual(found["title"], "(2) Discord | #general")
        self.assertEqual([m["text"] for m in found["messages"]],
                         ["first", "second", "third"])

    def test_a_run_of_messages_shares_the_author_and_the_time(self):
        # Discord names them on the first one only: a grouped run is one
        # person talking, and repeating it would fill the panel with names.
        window = node("AXWindow", title="Discord", children=[
            listing(message_group("chat-messages-1-10", author="Niko",
                                  stamp="20:19", content="first"),
                    message_group("chat-messages-1-11", content="second"))])
        messages = notify.read_window(window)["messages"]
        self.assertEqual([(m["author"], m["time"]) for m in messages],
                         [("Niko", "20:19"), ("Niko", "20:19")])

    def test_a_window_with_no_channel_in_it_says_so(self):
        found = notify.read_window(node("AXWindow", title="Settings"))
        self.assertEqual(found, {"channel": "", "title": "Settings",
                                 "messages": []})

    def test_every_message_knows_which_channel_it_came_from(self):
        window = node("AXWindow", children=[
            listing(message_group("chat-messages-1-10", content="hi"))])
        messages = notify.read_window(window)["messages"]
        self.assertEqual(messages[0]["channel"], "#general")


class OwnMessageTests(unittest.TestCase):
    """Your own message does not come back as something that just arrived."""

    def window(self, *messages):
        return node("AXWindow", title="Discord | #general",
                    children=[account_panel("Niko"),
                              listing(*messages)])

    def test_what_i_send_is_not_announced_to_me(self):
        found = notify.read_window(self.window(
            message_group("chat-messages-1-10", author="Moonberry",
                          stamp="20:19", content="hi there"),
            message_group("chat-messages-1-11", author="Niko",
                          stamp="20:20", content="testing"),
            message_group("chat-messages-1-12", author="A.W.",
                          stamp="20:21", content="welcome")))
        self.assertEqual([m["text"] for m in found["messages"]],
                         ["hi there", "welcome"])

    def test_the_first_of_my_run_has_no_name_to_go_by(self):
        # Discord hides the name on a message continuing a run, so the first
        # one in the window can arrive nameless - the summary line still
        # says who wrote it.
        group = message_group(
            "chat-messages-1-10", stamp="20:19", content="im writing code",
            extra=[node("AXStaticText", subrole="AXDocumentArticle",
                        title="Niko Web , im writing code , 20:19")])
        found = notify.read_window(self.window(
            group,
            message_group("chat-messages-1-11", author="Moonberry",
                          stamp="20:20", content="keep going")))
        self.assertEqual([m["text"] for m in found["messages"]],
                         ["keep going"])

    def test_somebody_else_whose_name_starts_like_mine_is_left_alone(self):
        found = notify.read_window(self.window(
            message_group("chat-messages-1-10", author="Niko the Great",
                          stamp="20:19", content="not me")))
        self.assertEqual([m["text"] for m in found["messages"]], ["not me"])

    def test_without_an_account_panel_nothing_is_filtered(self):
        window = node("AXWindow", title="Discord", children=[
            listing(message_group("chat-messages-1-10", author="Niko",
                                  stamp="20:19", content="mine"))])
        self.assertEqual([m["text"] for m in
                          notify.read_window(window)["messages"]], ["mine"])


class UnreadTests(unittest.TestCase):
    """The count the window title carries: '(2) Discord | ...'."""

    def test_a_count(self):
        self.assertEqual(notify.unread_count("(2) Discord | #general"), 2)

    def test_no_count_means_nothing_waiting(self):
        for title in (None, "", "Discord | #general", "• Discord",
                      "2 Discord"):
            with self.subTest(title=title):
                self.assertEqual(notify.unread_count(title), 0)


def snapshot(messages, title="Discord"):
    return {"channel": "#general", "title": title, "messages": list(messages)}


class TakeNewTests(unittest.TestCase):
    """Telling what just arrived from what was already on the screen."""

    def setUp(self):
        self.reader = notify.Reader()

    def test_the_first_read_is_a_baseline_not_news(self):
        # Whatever was on the screen got there before anybody asked for it.
        self.reader.snapshot = snapshot([{"id": "1", "text": "old"}])
        self.assertEqual(self.reader.take_new(), [])

    def test_only_what_turns_up_afterwards_counts(self):
        self.reader.snapshot = snapshot([{"id": "1", "text": "old"}])
        self.reader.take_new()
        self.reader.snapshot = snapshot([{"id": "1", "text": "old"},
                                         {"id": "2", "author": "Niko",
                                          "text": "new"}])
        events = self.reader.take_new()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "message")
        self.assertEqual(events[0]["id"], "2")
        self.assertEqual(events[0]["text"], "new")
        self.assertEqual(events[0]["app"], "Discord")

        # And the same message is not news twice.
        self.assertEqual(self.reader.take_new(), [])

    def test_a_window_a_message_did_not_come_from_still_counts(self):
        self.reader.snapshot = snapshot([], title="Discord")
        self.reader.take_new()
        self.reader.snapshot = snapshot([{"id": "1", "text": "new"}],
                                        title="(1) Discord")
        events = self.reader.take_new()
        self.assertEqual([event["kind"] for event in events], ["message"])

    def test_a_count_rising_on_its_own_is_the_only_sign_of_the_others(self):
        # Another channel is where messages land that this window cannot
        # show; the title's count is all there is to go on.
        self.reader.snapshot = snapshot([], title="Discord")
        self.reader.take_new()
        self.reader.snapshot = snapshot([], title="(3) Discord")
        events = self.reader.take_new()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "unread")
        self.assertEqual(events[0]["count"], 3)

    def test_a_count_going_down_is_not_news(self):
        self.reader.snapshot = snapshot([], title="(3) Discord")
        self.reader.take_new()
        self.reader.snapshot = snapshot([], title="Discord")
        self.assertEqual(self.reader.take_new(), [])

    def test_a_message_that_already_raised_the_count_does_not_also_say_it(self):
        self.reader.snapshot = snapshot([], title="Discord")
        self.reader.take_new()
        self.reader.snapshot = snapshot([{"id": "1", "text": "new"}],
                                        title="(1) Discord")
        events = self.reader.take_new()
        self.assertEqual([event["kind"] for event in events], ["message"])

    def test_nothing_to_read_is_not_an_error(self):
        self.assertEqual(self.reader.take_new(), [])

    def test_status_reports_what_a_cli_would_ask_about(self):
        status = self.reader.status()
        for key in ("trusted", "app", "running", "error", "channel",
                    "title", "seconds"):
            self.assertIn(key, status)


class NotificationStoreTests(unittest.TestCase):
    """The other source: what macOS itself was told, off its own store."""

    def setUp(self):
        fresh_store()

    def reader(self, **block):
        block.setdefault("banners", True)
        return notify.Notifications(block)

    def since(self, reader, *rows):
        """Post `rows`, take what the reader now hands over."""
        reader.poll()                       # draw the line where we stand
        for row in rows:
            store_row(*row)
        return reader.poll()

    def test_the_backlog_is_a_baseline_and_not_news(self):
        store_row(5, "com.example.one", "Old", "history")
        reader = self.reader()
        self.assertEqual(reader.poll(), [])
        self.assertEqual(reader.status()["newest"], 5)
        self.assertIsNone(reader.status()["error"])

    def test_what_turns_up_afterwards_is_handed_over_oldest_first(self):
        reader = self.reader()
        events = self.since(reader,
                            (6, "com.example.chat", "Mum", "tea?"),
                            (7, "com.example.chat", "Mum", "now"))
        self.assertEqual([event["text"] for event in events], ["tea?", "now"])
        self.assertEqual([event["kind"] for event in events],
                         ["banner", "banner"])
        self.assertEqual([event["author"] for event in events], ["Mum", "Mum"])

    def test_the_row_is_only_handed_over_once(self):
        reader = self.reader()
        self.since(reader, (6, "com.example.chat", "Mum", "tea?"))
        self.assertEqual(reader.poll(), [])
        self.assertEqual(reader.poll(), [])

    def test_an_app_that_is_running_is_called_by_its_own_name(self):
        # A bundle id is not something to put on a panel nine characters
        # wide, and Finder is always there to be asked.
        reader = self.reader()
        events = self.since(reader, (6, "com.apple.finder", "Mum", "tea?"))
        self.assertEqual(events[0]["app"], "Finder")

    def test_an_app_that_is_not_running_keeps_the_last_part_of_its_id(self):
        reader = self.reader()
        events = self.since(reader, (6, "com.example.someapp", "X", "y"))
        self.assertEqual(events[0]["app"], "someapp")

    def test_a_notification_with_only_a_title_is_its_own_text(self):
        # Nothing to attribute it to, so the title is the words themselves
        # rather than an author with an empty line underneath.
        reader = self.reader()
        event = self.since(reader, (6, "com.example.x", "Disk almost full"))[0]
        self.assertEqual(event["text"], "Disk almost full")
        self.assertEqual(event["author"], "")
        self.assertEqual(event["channel"], event["time"])

    def test_a_notification_that_says_nothing_at_all_is_not_shown(self):
        reader = self.reader()
        self.assertEqual(self.since(reader, (6, "com.example.x", ""),
                                    (7, "com.example.x", "", "")), [])

    def test_a_payload_in_a_shape_nobody_knows_is_dropped_not_raised(self):
        reader = self.reader()
        reader.poll()                           # the line, before the row
        connection = sqlite3.connect(notify.NOTIFICATIONS_DB)
        try:
            connection.execute(
                "INSERT INTO record (rec_id, app_id, data, delivered_date)"
                " VALUES (1, 1, ?, 0.0)", (b"not a plist at all",))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(reader.poll(), [])      # not news, and not a crash
        self.assertIsNone(reader.status()["error"])

    def test_a_store_that_cleared_itself_draws_a_new_line(self):
        # Clearing the notification centre takes the newest ids with it, so
        # waiting for the old ones would wait for ever. The rows already in
        # the store when that is noticed are not re-announced - they are
        # what was there when the line was redrawn - but everything after
        # them is followed from the new numbering.
        reader = self.reader()
        self.since(reader, (5, "com.example.x", "A", "one"),
                   (6, "com.example.x", "B", "two"))
        fresh_store()
        store_row(2, "com.example.x", "C", "three")
        self.assertEqual(reader.poll(), [])      # 2 is below 6: redraw
        store_row(3, "com.example.x", "D", "four")
        self.assertEqual([event["text"] for event in reader.poll()],
                         ["four"])

    def test_a_store_that_was_empty_when_asked_lets_the_next_row_through(self):
        # The line is drawn at zero rather than left standing at nothing,
        # or a store that starts empty would swallow everything after it.
        reader = self.reader()
        self.assertEqual(reader.poll(), [])
        store_row(1, "com.example.x", "C", "three")
        self.assertEqual([event["text"] for event in reader.poll()],
                         ["three"])

    def test_banners_can_be_turned_off(self):
        reader = self.reader(banners=False)
        store_row(1, "com.example.x", "Title", "body")
        self.assertEqual(reader.poll(), [])
        self.assertEqual(reader.status()["polls"], 0)
        self.assertIsNone(reader.status()["error"])   # never even opened

    def test_a_store_that_cannot_be_opened_is_a_state_not_a_crash(self):
        missing = os.path.join(_TMP, "no-such-dir", "db")
        notify.NOTIFICATIONS_DB = missing
        self.addCleanup(setattr, notify, "NOTIFICATIONS_DB",
                        os.path.join(_TMP, "db"))
        reader = self.reader()
        self.assertEqual(reader.poll(), [])
        self.assertIsNotNone(reader.status()["error"])

    def test_how_long_it_holds_the_panel_comes_from_the_config(self):
        reader = self.reader(banners_seconds=3)
        event = self.since(reader, (6, "com.example.x", "T", "b"))[0]
        self.assertEqual(event["seconds"], 3.0)
        self.assertEqual(self.reader().seconds, 10.0)
        self.assertEqual(self.reader(banners_seconds="junk").seconds, 10.0)


class StubReader:
    """A reader standing in for a real chat app.

    `connected` and `identifiers` are the two things the banner source
    needs to know about it: whether a window is being read at all, and
    which apps that reading covers.
    """

    def __init__(self, events=(), ok=True, snapshot=None, identifiers=()):
        self.events = list(events)
        self.ok = ok
        self.snapshot = snapshot or {"channel": "#general", "messages": []}
        self.last_error = None
        self.reads = 0
        self.connected = ok
        self.identifiers = tuple(identifiers)

    def connect(self):
        if not self.ok:
            self.last_error = "no chat app is running"
        return self.ok

    def read(self):
        self.reads += 1
        return self.snapshot

    def take_new(self):
        gone, self.events = self.events, []
        return gone

    def close(self):
        pass


class WatcherTests(unittest.TestCase):
    """The poll that runs beside the key loop, on its own thread."""

    def setUp(self):
        fresh_store()                    # a store of our own, empty

    def watcher(self, **block):
        watcher = notify.Watcher(block)
        watcher.reader = StubReader()
        return watcher

    def test_what_turns_up_is_queued_for_the_agent_to_pick_up(self):
        watcher = self.watcher()
        watcher.reader.events = [{"kind": "message", "text": "hello"}]
        self.assertEqual([event["text"] for event in watcher.poll_once()],
                         ["hello"])
        self.assertEqual([event["text"] for event in watcher.drain()],
                         ["hello"])
        self.assertEqual(watcher.drain(), [])       # taken, not left lying
        self.assertEqual(watcher.polls, 1)
        self.assertIsNone(watcher.last_error)

    def test_an_app_that_is_not_running_is_a_state_not_a_crash(self):
        watcher = self.watcher()
        watcher.reader = StubReader(ok=False)
        self.assertEqual(watcher.poll_once(), [])
        self.assertEqual(watcher.last_error, "no chat app is running")
        self.assertEqual(watcher.drain(), [])

    def test_the_store_is_a_second_source_into_the_same_queue(self):
        # No chat app at all, and the rest of the machine still has the
        # panel: one source failing may not silence the other.
        watcher = notify.Watcher({"banners": True})
        watcher.reader = StubReader(ok=False)
        store_row(1, "com.example.x", "Title", "history")
        watcher.poll_once()                    # draws its line
        store_row(2, "com.example.x", "Title", "body")
        events = watcher.poll_once()
        self.assertEqual([event["kind"] for event in events], ["banner"])
        self.assertEqual([event["text"] for event in watcher.drain()],
                         ["body"])
        self.assertEqual(watcher.polls, 0)     # the window reader never ran

    def test_a_banner_the_window_is_reading_already_is_not_said_twice(self):
        # Discord draws the message *and* posts a notification for it: the
        # window has it inside a second or two, the store takes five - so
        # the late copy would otherwise land on top of the live one.
        watcher = notify.Watcher({"banners": True})
        watcher.reader = StubReader(identifiers=("com.example.chat",))
        store_row(1, "com.example.chat", "Mum", "history")
        watcher.poll_once()
        store_row(2, "com.example.chat", "Mum", "hello")
        store_row(3, "com.example.other", "Xcode", "done")
        self.assertEqual([event["text"] for event in watcher.poll_once()],
                         ["done"])

    def test_when_the_window_is_not_there_the_banner_is_the_only_route(self):
        watcher = notify.Watcher({"banners": True})
        watcher.reader = StubReader(ok=False,
                                    identifiers=("com.example.chat",))
        store_row(1, "com.example.chat", "Mum", "history")
        watcher.poll_once()
        store_row(2, "com.example.chat", "Mum", "hello")
        self.assertEqual([event["text"] for event in watcher.poll_once()],
                         ["hello"])

    def test_banners_can_be_turned_off_without_stopping_the_reader(self):
        watcher = notify.Watcher({"banners": False})
        watcher.reader = StubReader()
        store_row(1, "com.example.x", "Title", "history")
        watcher.poll_once()
        store_row(2, "com.example.x", "Title", "body")
        self.assertEqual(watcher.poll_once(), [])

    def test_the_poll_pace_comes_from_the_config(self):
        for given, expected in ((1.0, 1.0), (0.1, 0.5), (None, 2.0),
                                ("junk", 2.0)):
            with self.subTest(given=given):
                watcher = notify.Watcher({"poll_seconds": given})
                self.assertEqual(watcher.interval, expected)

    def test_a_problem_is_logged_once_rather_than_every_poll(self):
        # "no chat app is running" stays true for as long as it does, and
        # repeating it every two seconds would drown out everything else.
        said = []
        watcher = notify.Watcher({}, log=said.append)

        class Broken:
            last_error = None

            def __init__(self):
                self.calls = 0

            def connect(self):
                self.calls += 1
                if self.calls >= 2:
                    watcher.running = False
                raise RuntimeError("boom")

        watcher.reader = Broken()
        watcher.running = True
        watcher._loop()
        self.assertEqual(said, ["chat watcher: RuntimeError: boom"])

    def test_stopping_leaves_nothing_running(self):
        watcher = self.watcher()
        watcher.start()
        self.assertTrue(watcher.running)
        watcher.stop()
        self.assertFalse(watcher.running)


if __name__ == "__main__":
    unittest.main()
