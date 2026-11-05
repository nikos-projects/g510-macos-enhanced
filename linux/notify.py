"""The notification sources this agent reads, which are macOS's.

daemon.py imports notify unconditionally - it is one of the things the
agent drives - and cli.py imports it for `g510 discord`, and both of those
files are shared with macOS. With linux/ ahead of the repo root on sys.path
(see _path.py) this is what `import notify` finds here, so both import
cleanly and the platform question is asked once, as a fact:

    notify.AVAILABLE   False here, True on the Mac this is shared with

Nothing is available because there is nothing to read on this side:

  * the window reader walks a chat app's Accessibility tree. Wayland gives
    an unprivileged client no way to read another window's contents - the
    same wall that rules out per-app profiles (LINUX.md) - and X11 would
    need a grab that takes the whole desktop with it;

  * the banner reader asks macOS's store for everything macOS was told.
    Linux desktops hand each notification to the session and keep no
    history that an agent may read back.

So no watcher is started here: _boot skips sync_watcher, and sync_watcher
itself asks AVAILABLE before touching `self.watcher`, because reload_config
reaches it on every config reload. `g510 discord` says as much rather than
reading nothing or promising a read that cannot happen. Chat still reaches
the display - a Vencord plugin inside the client pushes it over the agent's
link port, which is what `g510 discord channels` and the Discord screen use.

Watcher, probe, Notifications and find_app are deliberately not stubbed out:
every caller asks AVAILABLE first, and a name that raises when it is reached
is better than one that quietly does nothing.
"""
AVAILABLE = False
