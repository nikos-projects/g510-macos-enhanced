/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 Philipp and contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

/*
 * The agent's half of the Discord screen, running where Discord's own data
 * actually lives.
 *
 * Nothing outside this process can read a Discord conversation: there is no
 * notification, no store on disk, and the window is only as deep as the
 * Accessibility tree lets it be. Inside the client it is all in memory and
 * already sorted - which is what this reads, and then hands to the agent
 * over localhost so the panel can draw it.
 *
 * Two directions, deliberately different in kind:
 *
 *   state  - this pushes whenever what it has changed. The panel draws
 *            whatever the last one said, so a missed push costs a second.
 *   commands - the agent holds these until asked for them, so a command
 *            sent while Vesktop is restarting is not lost, and the panel
 *            learns the command ran by seeing it in the next state.
 *
 * The transport is a plain fetch to 127.0.0.1. Discord's own content
 * security policy would refuse that, but Vencord patches the header on the
 * way in and whitelists localhost in `connect-src` already (see
 * `src/main/csp/index.ts`), so no CSP work of our own is needed - which is
 * the whole reason this is a Vencord plugin rather than an injected script.
 */
import { sendMessage } from "@utils/discord";
import { Logger } from "@utils/Logger";
import type { Message } from "@vencord/discord-types";
import definePlugin from "@utils/types";
import {
    ChannelRouter,
    ChannelStore,
    GuildMemberStore,
    GuildRoleStore,
    GuildStore,
    MessageStore,
    NavigationRouter,
    ReadStateStore,
    RestAPI,
    SelectedChannelStore,
    SelectedGuildStore,
    SortedGuildStore,
    UserStore,
} from "@webpack/common";

const log = new Logger("G510");

/** Where the agent listens. Must equal `vencord.port` in its config. */
const HOST = "http://127.0.0.1:47832";
/** How often the stores are read and compared. The panel redraws at 1s too. */
const TICK = 1000;
/** How long before an unchanged state is sent anyway.
 *
 * What is pushed only when it changes, so a Discord left alone would stop
 * pushing at all - and the agent reads a long silence as "the plugin is
 * gone", which is also how it reports `not connected` on the command
 * line. So a quiet client says so on its own, at well under the agent's
 * own patience (15s), for one small localhost POST.
 */
const HEARTBEAT = 10000;
/** Messages held for paging back through, and fetched per page. */
const PAGE = 30;
/** A ceiling on the buffer, so a long session cannot grow without bound. */
const MAX_HISTORY = 400;
/** How long one command call waits for the agent to have something. */
const PULL_TIMEOUT = 25000;
/** How long to wait before asking again when the agent is not up. */
const PULL_RETRY = 2000;
/** How many people and roles one mention is offered. The panel shows
 * three at a time and arrows through the rest, so this is a ceiling on
 * what one push of state has to carry rather than a limit anybody sees. */
const SUGGEST_LIMIT = 40;

interface Command {
    n: number;
    cmd: string;
    id?: string;
    text?: string;
    limit?: number;
}

let running = false;
let ticker: ReturnType<typeof setInterval> | undefined;
/** Commands already handled: the agent sends everything after this. */
let cursor = 0;
/** The last state handed over, so an unchanged second is not sent again. */
let lastSent = "";
/** When that was, so a quiet client still says it is there - see HEARTBEAT. */
let lastPushAt = 0;
/** What the panel pages back through, oldest first, of one channel. */
let history: any[] = [];
let historyChannel = "";
/** Whether there is more history behind what is held - see `has_more`. */
let moreBefore = false;
/** The last command this side ran, so the panel can see it happened. */
let ack: { n: number; ok: boolean; error?: string } | null = null;
/** Which list of channels `channels` is for: `"@me"` or one server id,
 * and `null` for "whatever the client has open".
 *
 * Walking servers with the arrow keys must not drag Discord's own view
 * along with it - the whole point of this screen is reading one conversation
 * without the window ever coming forward. So a list the panel asked for
 * stays put until it asks for another, and `open` is what puts it back to
 * following: opening a channel is the one command here that does move the
 * client.
 */
let listId: string | null = null;
/** The last answer to "whoever fits this word being written".
 *
 * Carried with the word it belongs to (`for`) because commands are not
 * instant: a list offered for a word that has already moved on would put
 * the wrong person in the text, so the panel only ever uses the answer to
 * the word it is on.
 */
let suggestion: { for: string; items: { label: string; insert: string; }[]; } =
    { for: "", items: [] };

function channelName(channel: any): string {
    if (!channel) return "";
    // A DM carries its people rather than a name of its own; a group DM
    // names itself only sometimes, so the recipients are the truth either way.
    if (channel.type === 1 || channel.type === 3) {
        const people = (channel.rawRecipients ?? [])
            .map((r: any) => r.globalName || r.username)
            .filter(Boolean);
        return channel.name || people.join(", ") || "DM";
    }
    const guild = GuildStore.getGuild(channel.guild_id);
    const marker = channel.type === 2 || channel.type === 13 ? "" : "#";
    return `${guild?.name ?? ""} ${marker}${channel.name ?? ""}`.trim();
}

function stamp(iso: any): string {
    const when = new Date(iso ?? Date.now());
    const hh = String(when.getHours()).padStart(2, "0");
    const mm = String(when.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
}

function shape(message: any, mine: string) {
    const author = message.author ?? {};
    return {
        id: String(message.id),
        author: String(author.globalName || author.username || "?"),
        self: String(author.id ?? "") === mine,
        text: String(message.content ?? ""),
        time: stamp(message.timestamp),
        reply: message.messageReference
            ? String(message.messageReference.message_id ?? "") : "",
        // Left null here; gather() fills it in for the messages whose target
        // it is actually holding, which is the only case worth a name on a
        // panel this size.
        reply_to: null as string | null,
    };
}

function loaded(id: string): any[] {
    const messages = MessageStore.getMessages(id);
    return (messages?._array ?? []) as any[];
}

/** Keep the paging buffer in step with what the store has loaded. */
function syncHistory(id: string) {
    if (!id) {
        historyChannel = "";
        history = [];
        moreBefore = false;
        return;
    }
    const fresh = loaded(id);
    if (id !== historyChannel) {
        historyChannel = id;
        history = fresh.slice(-PAGE);
        // A full page behind the newest one is as much as the store will
        // admit to without being asked; `older` is what asks.
        moreBefore = fresh.length >= PAGE;
        return;
    }
    if (!history.length) {
        history = fresh.slice(-PAGE);
        return;
    }
    // The store is not a queue. It refills when the channel is reopened,
    // trims what nobody is looking at, and empties outright when Discord
    // decides to reload - so anchoring on the newest line held only works
    // until one of those happens, and the code that used to live here then
    // did nothing at all: the panel sat on an old conversation while the
    // window next to it showed the new one, with nothing to say why.
    //
    // So the two lists are merged as sets and put back into id order.
    // Message ids are snowflakes and therefore time-ordered, which is the
    // order the panel reads, and anything the store has that the buffer
    // does not is a message this side has simply not caught up with.
    const known = new Set(history.map((m: any) => String(m.id)));
    const merged = history.concat(
        fresh.filter((m: any) => !known.has(String(m.id))));
    merged.sort((a: any, b: any) => {
        const left = String(a.id);
        const right = String(b.id);
        return (left.length - right.length)
            || (left < right ? -1 : left > right ? 1 : 0);
    });
    if (merged.some((m: any, index: number) => index < history.length
        && String(m.id) !== String(history[index]?.id))) {
        // The store gave back something older than where the buffer
        // started, so there is very likely more behind that too.
        const oldest = String(history[0].id);
        if (merged.some((m: any) => String(m.id) < oldest)) moreBefore = true;
    }
    history = merged.slice(-MAX_HISTORY);
}

function channelRow(channel: any, current: string) {
    return {
        id: channel.id,
        name: channelName(channel),
        dm: channel.type === 1 || channel.type === 3,
        unread: !!ReadStateStore.hasUnread(channel.id),
        mentions: ReadStateStore.getMentionCount(channel.id) || 0,
        current: channel.id === current,
    };
}

/** The list the panel walks - the DMs, or every text channel of one
 * server, in the order that server's own sidebar uses.
 *
 * `getChannelIds` hands back ids in no order anybody could walk with an
 * arrow key, which is what first made the list useless for browsing: the
 * channels are put back into the shape the sidebar draws them in,
 * categories first and channels by position underneath.
 */
function channelList(current: string, guildId: string) {
    if (guildId === "@me") {
        // Already sorted by the store: whoever was written to last.
        return ChannelStore.getSortedPrivateChannels()
            .map((channel: any) => channelRow(channel, current));
    }
    const channels: any[] = [];
    for (const id of ChannelStore.getChannelIds(guildId)) {
        const channel = ChannelStore.getChannel(id);
        // Text channels only: voice has no conversation to read here and
        // would sit in the list as a channel that opens nothing.
        if (!channel || channel.type !== 0) continue;
        channels.push(channel);
    }
    const parentPosition = (channel: any) => {
        const parent = channel.parent_id
            ? ChannelStore.getChannel(channel.parent_id) : null;
        return parent ? Number(parent.position) || 0 : -1;
    };
    channels.sort((a: any, b: any) =>
        parentPosition(a) - parentPosition(b)
        || (Number(a.position) || 0) - (Number(b.position) || 0)
        || String(a.name ?? "").localeCompare(String(b.name ?? "")));
    return channels.map((channel: any) => channelRow(channel, current));
}

/** Every list there is to walk: the DMs first, then the servers in the
 * order the sidebar holds them. The client is not moved by any of this. */
function guildLists(active: string) {
    const out: any[] = [{ id: "@me", name: "DMs", current: active === "@me" }];
    for (const id of SortedGuildStore.getFlattenedGuildIds()) {
        const guild = GuildStore.getGuild(id);
        if (!guild) continue;
        out.push({ id, name: guild.name, current: id === active });
    }
    return out;
}

/** The list this panel would be showing if it were following the client:
 * whatever holds whatever is open, falling back to the last server used. */
function followList(): string {
    const id = SelectedChannelStore.getChannelId();
    const channel = id ? ChannelStore.getChannel(id) : null;
    if (channel) return channel.guild_id ?? "@me";
    return SelectedGuildStore.getGuildId() || "@me";
}

/** Whoever fits the word after an @ - people, roles, @everyone and
 * @here, for the channel this is being written into.
 *
 * Read out of the stores rather than searched for over the API: this
 * runs on every keystroke, and the members a server has actually shown
 * this client are exactly the ones worth offering first. A DM has no
 * roles and nobody to ping who is not already in it, so it answers with
 * nothing rather than with suggestions that could not be sent.
 */
function findSuggestions(partial: string) {
    const id = SelectedChannelStore.getChannelId();
    const channel = id ? ChannelStore.getChannel(id) : null;
    const guildId = channel?.guild_id;
    if (!guildId) return { for: partial, items: [] };
    const word = partial.toLowerCase();
    const items: { label: string; insert: string; rank: number; }[] = [];
    const offer = (label: string, insert: string) => {
        const lower = String(label).toLowerCase();
        if (word && !lower.includes(word)) return;
        // Names that begin with what has been typed come first, which is
        // the order every mention box anyone has ever used uses.
        items.push({ label: String(label), insert, rank: lower.startsWith(word) ? 0 : 1 });
    };
    offer("everyone", "<@everyone>");
    offer("here", "<@here>");
    for (const role of GuildRoleStore.getSortedRoles(guildId)) {
        if (role.name === "everyone" || role.id === guildId) continue;
        offer(role.name, `<@&${role.id}>`);
    }
    for (const member of GuildMemberStore.getMembers(guildId)) {
        const user = UserStore.getUser(member.userId);
        offer(user?.globalName || user?.username || member.nick
            || member.userId, `<@${member.userId}>`);
    }
    items.sort((a, b) => a.rank - b.rank
        || a.label.localeCompare(b.label));
    return {
        for: partial,
        items: items.slice(0, SUGGEST_LIMIT)
            .map(({ label, insert }) => ({ label, insert })),
    };
}

function gather() {
    const id = SelectedChannelStore.getChannelId();
    const channel = id ? ChannelStore.getChannel(id) : null;
    syncHistory(id);
    const mine = UserStore.getCurrentUser()?.id ?? "";
    const byId = new Map(history.map((m: any) => [String(m.id), m]));
    const shaped = history.map(m => {
        const row = shape(m, mine);
        // Who that was a reply to, when the message it answered is one of
        // the ones held here - the ids alone mean nothing on a panel.
        const target = row.reply ? byId.get(row.reply) : null;
        row.reply_to = target
            ? String(target.author?.globalName || target.author?.username
                     || "?") : null;
        return row;
    });
    if (listId && listId !== "@me" && !GuildStore.getGuild(listId)) {
        // A server left while it was being read: back to following the
        // client rather than showing a list that can never fill again.
        listId = null;
    }
    const active = listId ?? followList();
    return {
        plugin: "g510",
        v: 2,
        channel: channel
            ? { id: channel.id, name: channelName(channel), dm: channel.type === 1 || channel.type === 3 }
            : null,
        // Which list the channels below belong to, and every list there
        // is to walk to with the panel's left and right keys.
        list: active,
        lists: guildLists(active),
        channels: channelList(id ?? "", active),
        messages: shaped,
        // Whether there is older history behind what the panel is holding:
        // a full page in the store, or a full page the last `older` came
        // back with.
        has_more: moreBefore,
        // Whoever fits the word being written, with the word it is for -
        // see `suggestion`.
        suggest: suggestion,
        ack,
    };
}

async function push() {
    const text = JSON.stringify(gather());
    if (text === lastSent && Date.now() - lastPushAt < HEARTBEAT) return;
    lastSent = text;
    lastPushAt = Date.now();
    try {
        // No Content-Type header on purpose: a simple request needs no
        // preflight, and the agent reads the body as JSON regardless.
        await fetch(`${HOST}/state`, { method: "POST", body: text });
    } catch {
        // The agent is not up (or is restarting). The next tick tries again,
        // and nothing here is lost: this side holds no state the agent needs.
    }
}

async function fetchOlder(limit = PAGE) {
    if (!historyChannel || !history.length) return;
    const { body } = await RestAPI.get({
        // The leading slash matters: RestAPI builds the path itself, and
        // without it the request comes back as something that is not a
        // message list at all - which arrives as `null`.
        url: `/channels/${historyChannel}/messages`,
        query: { limit, before: history[0].id },
        retries: 2,
    });
    // The API answers newest first; the panel reads oldest first.
    if (!Array.isArray(body)) {
        throw new Error("Discord did not send a message list back");
    }
    const older = (body as any[]).slice().reverse();
    history = older.concat(history).slice(-MAX_HISTORY);
    // A full page means there is very likely more behind it; a short one
    // means the beginning of the channel has been reached.
    moreBefore = older.length >= limit;
}

async function handle(command: Command) {
    switch (command.cmd) {
        case "open":
            if (command.id === "@me") {
                // The DM list, not a channel: a route rather than an id,
                // and what "nothing open" on the panel looks like.
                NavigationRouter.transitionTo("/channels/@me");
            } else if (command.id) {
                ChannelRouter.transitionToChannel(command.id);
            }
            // Opening is the one command here that moves Discord, so the
            // panel's list goes back to following it: from now on the
            // channels it walks and the conversation the client is in are
            // the same place.
            listId = null;
            break;
        case "list":
            // Walk somewhere else without moving the client: this is a
            // command about reading, and nothing else about it changes.
            listId = command.id || null;
            break;
        case "suggest":
            suggestion = findSuggestions(command.text ?? "");
            break;
        case "older":
            await fetchOlder(command.limit);
            break;
        case "say": {
            const id = SelectedChannelStore.getChannelId();
            if (id && command.text) await sendMessage(id, { content: command.text });
            break;
        }
        case "reply": {
            const id = SelectedChannelStore.getChannelId();
            const channel = id ? ChannelStore.getChannel(id) : null;
            if (id && command.id && command.text) {
                // The reference goes in the *options*, which is where
                // Discord's own send reads it from; in the message body it
                // is simply not looked at, and the reply lands as an
                // ordinary message.
                //
                // fail_if_not_exists is a fourth field Discord really takes
                // (Vencord's own type stops at the three ids): with it off,
                // replying to a message that got deleted while this row sat
                // on the panel still sends, instead of erroring the whole
                // send away.
                const reference = {
                    message_id: command.id,
                    channel_id: id,
                    guild_id: channel?.guild_id,
                    fail_if_not_exists: false,
                } as Message["messageReference"];
                await sendMessage(id, { content: command.text }, true, {
                    messageReference: reference,
                });
            }
            break;
        }
        case "refresh":
            break;
        default:
            throw new Error(`unknown command ${command.cmd}`);
    }
}

async function run(command: Command) {
    try {
        await handle(command);
        ack = { n: command.n, ok: true };
    } catch (error: any) {
        ack = { n: command.n, ok: false, error: String(error?.message ?? error) };
        log.error("command failed:", command.cmd, error);
    }
    // The panel learns what happened from the next push rather than from a
    // reply of its own: one direction carries state, the other commands.
    await push();
}

async function pull() {
    while (running) {
        try {
            const response = await fetch(`${HOST}/cmd?since=${cursor}`, {
                signal: AbortSignal.timeout(PULL_TIMEOUT),
            });
            const body = await response.json();
            if (!running) return;
            for (const command of (body.commands ?? []) as Command[]) {
                cursor = Math.max(cursor, command.n);
                await run(command);
            }
        } catch {
            // No agent listening, or it went away mid-call. Say nothing and
            // ask again shortly: this must not cost the user a console full
            // of errors every two seconds while the panel is switched off.
            await new Promise(resolve => setTimeout(resolve, PULL_RETRY));
        }
    }
}

export default definePlugin({
    name: "G510",
    description: "Hands Discord's own view of the conversation to the G510 panel",
    // The credits. Vencord has no field of its own for them - the authors
    // line is what the plugin's page shows - and `id: 0n` is its way of
    // saying "a name with nobody to look up", so all four are shown as
    // they are written here rather than fetched as users.
    authors: [
        { name: "g510", id: 0n },
        { name: "Niko", id: 0n },
        { name: "MiMo-V2.6-Flash (OpenCode)", id: 0n },
        { name: "g510-macos-enhanced team", id: 0n },
    ],
    enabledByDefault: true,

    start() {
        running = true;
        cursor = 0;
        lastSent = "";
        lastPushAt = 0;
        ticker = setInterval(() => {
            push().catch(() => { });
        }, TICK);
        push().catch(() => { });
        pull().catch(() => { });
    },

    stop() {
        running = false;
        if (ticker) clearInterval(ticker);
        ticker = undefined;
        history = [];
        historyChannel = "";
        moreBefore = false;
        ack = null;
        // Start over on the next load: which list the panel was walking and
        // who was last offered as a mention are both things a fresh session
        // has no business inheriting - a stale `listId` would show a server
        // nobody asked for, and a stale suggestion would offer a name for a
        // word that has long since been deleted.
        listId = null;
        suggestion = { for: "", items: [] };
    },
});
