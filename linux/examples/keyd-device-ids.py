"""Work out the device ids keyd would print, without needing root.

keyd's `monitor` is the documented way to get these, and it needs root. The
id is deterministic though - vendor:product:djb2 over the device's key count,
its EV_ABS/EV_REL masks and its name - so it can be computed from the same
ioctls keyd uses, which an ordinary user may call on /dev/input/event*.

Getting this wrong is safe: keyd only ever matches an id exactly, so a bad
hash means no config matches and every keyboard is left exactly as it is.
The failure that would matter is matching too MUCH, and that comes from using
a two-field id, which this never produces.

Algorithm from keyd v2.5.0 src/device.c.
"""
import ctypes
import fcntl
import glob
import os
import struct

EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
BTN_LEFT = 0x110
WORDS = BTN_LEFT // 32 + 1              # keyd reads exactly this many uint32
KEY_BYTES = WORDS * 4


def _eviocgbit(ev, length):
    return (2 << 30) | (length << 16) | (ord('E') << 8) | (0x20 + ev)


def _ioctl_name(fd):
    buf = ctypes.create_string_buffer(256)
    fcntl.ioctl(fd, (2 << 30) | (256 << 16) | (ord('E') << 8) | 0x06, buf)
    return buf.value.decode("utf-8", "replace")


def generate_uid(num_keys, absmask, relmask, name):
    h = 5183
    for shift in (24, 16, 8, 0):
        h = (h * 33 + ((num_keys >> shift) & 0xFF)) & 0xFFFFFFFF
    h = (h * 33 + absmask) & 0xFFFFFFFF
    h = (h * 33 + relmask) & 0xFFFFFFFF
    for ch in name.encode("utf-8"):
        h = (h * 33 + ch) & 0xFFFFFFFF
    return h


def describe(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        name = _ioctl_name(fd)
        keys = ctypes.create_string_buffer(KEY_BYTES)
        fcntl.ioctl(fd, _eviocgbit(EV_KEY, KEY_BYTES), keys)
        num_keys = sum(bin(w).count("1")
                       for w in struct.unpack("<%dI" % WORDS, keys.raw))
        rel = ctypes.create_string_buffer(1)
        fcntl.ioctl(fd, _eviocgbit(EV_REL, 1), rel)
        abs_ = ctypes.create_string_buffer(1)
        fcntl.ioctl(fd, _eviocgbit(EV_ABS, 1), abs_)
        ident = open(os.path.join("/sys/class/input",
                                  os.path.basename(path), "device", "id",
                                  "")) if False else None
        vendor = int(open("/sys/class/input/%s/device/id/vendor"
                          % os.path.basename(path)).read(), 16)
        product = int(open("/sys/class/input/%s/device/id/product"
                           % os.path.basename(path)).read(), 16)
        uid = generate_uid(num_keys, abs_.raw[0], rel.raw[0], name)
        return name, "%04x:%04x:%08x" % (vendor, product, uid), num_keys
    finally:
        os.close(fd)


for path in sorted(glob.glob("/dev/input/event*"),
                   key=lambda p: int(p.rsplit("event", 1)[1])):
    try:
        name, ident, nkeys = describe(path)
    except (OSError, IOError) as exc:
        continue
    if nkeys < 1:
        continue
    print("%-18s %-42s %-22s keys=%d" % (path, name, ident, nkeys))
