#!/usr/bin/env python3
"""Generate the compose sequences ~/.XCompose needs for a keyd layout.

keyd cannot type a character the active XKB layout does not contain. For
those it emits Cancel plus three base36 keys and leaves the compose table to
turn the sequence back into the character.

keyd documents including its whole table:

    include "/usr/share/keyd/keyd.compose"

Do not. Qt honours that include and GTK does not, so Kate types "ä" while
Firefox types "02s" - and a test in the wrong toolkit says everything is
fine. keyd's own manual warns separately that GTK4 crashes on XCompose files
that large. Both toolkits read sequences written out in the file, so write out
only the ones the layouts in use can actually produce.

    ./make-compose.py de gb >> ~/.XCompose

with `include "%L"` already at the top of that file, so the ordinary compose
sequences keep working.
"""
import re
import sys
import unicodedata

LAYOUTS = "/usr/share/keyd/layouts/%s"
COMPOSE = "/usr/share/keyd/keyd.compose"

# A section header is a line that is nothing but [...]. It is not the `[` key,
# which on a German layout is ü and whose line also starts with a bracket -
# skipping those is how the first version of this silently dropped ü and Ü.
SECTION = re.compile(r'^\[[^\]]*\]\s*$')
ENTRY = re.compile(r'^(<Cancel>(?:\s*<[^>]+>)+)\s*:\s*"(.+)"')


def characters(names):
    """Every non-ASCII character the named layouts can produce."""
    found = set()
    for name in names:
        with open(LAYOUTS % name, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if SECTION.match(line) or "=" not in line:
                    continue
                value = line.split("=", 1)[1].strip()
                if len(value) == 1 and ord(value) > 127:
                    found.add(value)
    return found


def main(names):
    wanted = characters(names)
    sequences = {}
    with open(COMPOSE, encoding="utf-8") as handle:
        for line in handle:
            match = ENTRY.match(line.strip())
            if match and match.group(2) in wanted:
                sequences.setdefault(match.group(2), match.group(1))

    missing = wanted - set(sequences)
    if missing:
        print("# no keyd sequence for: %s" % " ".join(sorted(missing)),
              file=sys.stderr)
    for char in sorted(sequences):
        print('%s : "%s"   # %s'
              % (sequences[char], char, unicodedata.name(char, "?")))
    print("# %d sequences for layouts: %s" % (len(sequences), " ".join(names)),
          file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:] or ["de"])
