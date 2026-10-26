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
import definePlugin from "@utils/types";
import {
    ChannelRouter,
    ChannelStore,
    GuildStore,
    MessageStore,
    NavigationRouter,
    ReadStateStore,
    RestAPI,
    SelectedChannelStore,
    SelectedGuildStore,
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

function channelList(current: string) {
    const out: any[] = [];
    for (const channel of ChannelStore.getSortedPrivateChannels()) {
        out.push({
            id: channel.id,
            name: channelName(channel),
            dm: true,
            unread: !!ReadStateStore.hasUnread(channel.id),
            mentions: ReadStateStore.getMentionCount(channel.id) || 0,
            current: channel.id === current,
        });
    }
    const guildId = SelectedGuildStore.getGuildId();
    if (guildId) {
        for (const id of ChannelStore.getChannelIds(guildId)) {
            const channel = ChannelStore.getChannel(id);
            if (!channel || channel.type !== 0) continue;   // text only
            out.push({
                id: channel.id,
                name: channelName(channel),
                dm: false,
                unread: !!ReadStateStore.hasUnread(id),
                mentions: ReadStateStore.getMentionCount(id) || 0,
                current: id === current,
            });
        }
    }
    return out;
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
    return {
        plugin: "g510",
        v: 1,
        channel: channel
            ? { id: channel.id, name: channelName(channel), dm: channel.type === 1 || channel.type === 3 }
            : null,
        channels: channelList(id ?? ""),
        messages: shaped,
        // Whether there is older history behind what the panel is holding:
        // a full page in the store, or a full page the last `older` came
        // back with.
        has_more: moreBefore,
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
                await sendMessage(id, { content: command.text }, true, {
                    messageReference: {
                        message_id: command.id,
                        channel_id: id,
                        guild_id: channel?.guild_id,
                        fail_if_not_exists: false,
                    },
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
    authors: [{ name: "g510", id: 0n }],
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
    },
});
