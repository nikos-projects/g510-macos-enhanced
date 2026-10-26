"""AppKit building blocks for the settings window.

Nothing here knows what a pane is for. These are the primitives the panes are
assembled from - a label, a rounded card, a table - plus the geometry they all
share, kept apart so that changing how a card looks is one edit in one place
and a pane module can be read without boilerplate in the way.

Everything is a plain module function. None of it is reachable as a selector,
so none of it needs to live on an NSObject: the controller stays the single
target for every control, and these just build views for it.
"""
import objc
from AppKit import (NSBox, NSColor, NSFont, NSImage, NSMakeRect, NSMakeSize,
                    NSTableColumn, NSTableView, NSTextField, NSView)

WIDTH = 520
MARGIN = 20
GAP = 24
CARD_WIDTH = WIDTH - MARGIN * 2
MIN_HEIGHT = 380
MAX_HEIGHT = 900

NSBoxCustom = 4
NSNoTitle = 0
NSTableViewStyleInset = 2
NSLineBreakByTruncatingTail = 4
NSViewWidthSizable, NSViewHeightSizable = 2, 16

# Two panes name screens: the Keyboard pane offers them as switch positions,
# the Display pane picks one to show. Shared vocabulary lives in the module
# neither pane owns, so neither has to import the other.
SCREEN_LABELS = {
    "status": "Status", "clock": "Clock", "claude": "Claude Usage",
    "media": "Now Playing", "printer": "3D Printer", "gkeys": "G-key Echo",
    "app": "Active App",
}


def text(value, size=12, bold=False, secondary=False):
    field = NSTextField.alloc().init()
    field.setStringValue_(value)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
    if secondary:
        field.setTextColor_(NSColor.secondaryLabelColor())
    field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    return field


def place(view, x, y, width, height):
    view.setFrame_(NSMakeRect(x, y, width, height))
    return view


def swatch_image(rgb, size=18):
    """A filled circle, for the colour preset buttons."""
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()
    red, green, blue = (channel / 255.0 for channel in rgb)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0).setFill()
    path = objc.lookUpClass("NSBezierPath").bezierPathWithOvalInRect_(
        NSMakeRect(1, 1, size - 2, size - 2))
    path.fill()
    NSColor.separatorColor().setStroke()
    path.setLineWidth_(1.0)
    path.stroke()
    image.unlockFocus()
    return image


def symbol(name, size=15):
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is not None:
        image.setTemplate_(True)
        image.setSize_(NSMakeSize(size, size))
    return image


def card(parent, parent_height, y_from_top, height, title=None):
    """A rounded settings-style group, returned with its content view."""
    y = parent_height - y_from_top - height
    box = NSBox.alloc().initWithFrame_(
        NSMakeRect(MARGIN, y, CARD_WIDTH, height))
    box.setBoxType_(NSBoxCustom)
    box.setTitlePosition_(NSNoTitle)
    box.setCornerRadius_(10.0)
    box.setBorderWidth_(1.0)
    box.setBorderColor_(NSColor.separatorColor())
    box.setFillColor_(NSColor.controlBackgroundColor())
    box.setContentViewMargins_(NSMakeSize(0, 0))
    parent.addSubview_(box)
    if title:
        header = text(title, 11, bold=True, secondary=True)
        place(header, MARGIN, parent_height - y_from_top + 4, 300, 15)
        parent.addSubview_(header)
    return box.contentView()


def build_pane(win, sections):
    """Lay out a column of cards, sizing the pane to what it holds.

    Each section is (height, card title or None, fill), and fill is called as
    fill(controller, content_view, card_height) - a plain function from a pane
    module rather than a method, so the pane's layout and the controller that
    owns its controls stay separable.
    """
    total = MARGIN + sum(h + GAP for h, _, _ in sections) - GAP + MARGIN
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, total))
    cursor = MARGIN
    for height, title, fill in sections:
        fill(win, card(view, total, cursor, height, title), height)
        cursor += height + GAP
    return view


def make_table(win, *columns):
    """A borderless inset table, wired to the controller as its data source.

    The controller is the one object AppKit talks to for both tables, so it
    can tell them apart by identity; a per-pane owner would have to be kept
    alive by hand while its table is still on screen.
    """
    table = NSTableView.alloc().init()
    for title, width in columns:
        column = NSTableColumn.alloc().initWithIdentifier_(title.lower())
        column.headerCell().setStringValue_(title)
        column.setWidth_(width)
        table.addTableColumn_(column)
    try:
        table.setStyle_(NSTableViewStyleInset)
    except AttributeError:
        pass
    table.setRowHeight_(22.0)
    table.setUsesAlternatingRowBackgroundColors_(False)
    table.setGridStyleMask_(0)
    table.setBackgroundColor_(NSColor.clearColor())
    table.setDataSource_(win)
    table.setDelegate_(win)
    table.setTarget_(win)
    return table
