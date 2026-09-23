# keyd: a German G510 next to a UK laptop keyboard

The G510 sold in Germany has QWERTZ legends. If the laptop it is plugged into
has a UK keyboard, there is no way to satisfy both from the desktop: KWin
applies one XKB layout to every keyboard at once, and has done since forever
(KDE bug 177889, open since 2008, still confirmed against Plasma 6 on
Wayland). `setxkbmap -device` is X11 only and does nothing under Wayland.

keyd sits below the display server, on evdev, and can be scoped to one
keyboard - so it can give the G510 a German layout while the built-in
keyboard is left alone.

## Two things that will bite

**Use the three-field device id.** keyd matches ids by prefix, so a plain
`046d:c22d` matches *every* evdev node the G510 produces - including the
`Logitech Gaming Keyboard Gaming Keys` node that hid-lg-g15 creates for G1-G18
and the keys under the display. keyd would grab that node and the G-keys and
macro recording in this project would stop working, with nothing to say why.
keyd 2.5 added a third field that distinguishes the nodes. Get the exact id
from `keyd monitor` (Debian calls the binary `keyd.rvaiya`) and use all three
fields, for the *main keyboard node only*.

**Non-ASCII needs the compose file.** keyd cannot type a character the active
layout does not contain, so for `ä` it emits `Cancel` plus three base36 keys
and relies on `/usr/share/keyd/keyd.compose` to turn that back into the
character. Without it you get a literal `02s`. Put this in `~/.XCompose`:

    include "%L"
    include "/usr/share/keyd/keyd.compose"

`include "%L"` first keeps every ordinary compose sequence working.

Check it resolves before trusting it - xkbcommon is the library Qt uses:

    python3 -c "
    from xkbcommon import xkb
    t = xkb.Context().compose_table_new_from_locale('en_US.UTF-8')
    s = t.compose_state_new()
    for k in ('Cancel','0','2','s'): s.feed(xkb.keysym_from_name(k))
    print(s.get_utf8())"

## The layout the desktop should be set to

`us`. keyd's own key table is US-positional, so a `de` or `gb` XKB layout
underneath it corrupts both the ordinary keys and the base36 sequences. Set
Plasma to `us` and let keyd give *both* keyboards their real layouts.

The cost is that if keyd ever fails to start, every keyboard falls back to
`us` rather than to something sensible. Keeping `gb` as a second Plasma layout
gives you a way back without a login shell.
