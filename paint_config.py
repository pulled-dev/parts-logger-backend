"""
VAG paint code → colour family, for the paint sanity check.

Used by paint_codes.paint_family_conflict(): when a vehicle's paint NAME
clearly belongs to a different colour family than its paint CODE (e.g. LC9X,
a black, stored as "Reflex Silver"), the vehicle is flagged on import and in
the logger. Codes that aren't listed here are never flagged.

Every entry below was checked against our own BreakerPro stock exports
(vehicle-imports/done, Sep–Oct 2026): the Colour text for that code always
named a colour in this family. To add a code, add one line here; a test checks
that paint_codes.PAINT_CODES never contradicts this table.

A code may list more than one family when its colour genuinely sits between
two (e.g. a blue-grey); a name matching ANY of them is accepted.
"""

PAINT_CODE_FAMILIES: dict[str, tuple[str, ...]] = {
    # ── Black ──
    "LC9X": ("black",),   # Deep Black Pearl
    "LC9Z": ("black",),   # Black Magic / Diamond Black
    "LF9R": ("black",),   # Black Magic (Skoda)
    "LO41": ("black",),   # Flat Black (BreakerPro writes the letter O)
    "L041": ("black",),   # Flat Black (official spelling, zero)
    "LY9B": ("black",),   # Brilliant Black
    "LY9T": ("black",),   # Mythos Black
    "LZ9Y": ("black",),   # Phantom Black
    # ── White ──
    "LB9A": ("white",),   # Candy White
    "LC9A": ("white",),   # Pure White
    "LF9E": ("white",),   # Candy White (Skoda)
    "LS9R": ("white",),   # Glacier / Moon / Nevada White
    "LY9C": ("white",),   # Ibis White
    "LY9K": ("white",),   # Amalfi White
    # ── Silver ──
    "LA7W": ("silver",),  # Reflex Silver
    "LB7S": ("silver",),  # Pyrite Silver
    "LB7W": ("silver",),  # Tungsten Silver
    "LX7W": ("silver",),  # Ice Silver
    "LY7H": ("silver",),  # Moonstone / Akoya Silver
    "LZ7G": ("silver",),  # Floret / Urban Silver
    # ── Grey ──
    "LA7N": ("grey",),    # Limestone Grey
    "LA7T": ("grey",),    # United / Steel Grey
    "LC7Q": ("grey",),    # Dolphin Grey
    "LD7X": ("grey",),    # Platinum Grey
    "LF7C": ("grey",),    # Graphite Grey
    "LF7Y": ("grey",),    # Metal Grey
    "LF8J": ("grey",),    # Anthracite Grey
    "LI7F": ("grey",),    # Urano Grey
    "LK7X": ("grey",),    # Island Grey
    "LR7H": ("grey",),    # Indium Grey
    "LX7R": ("grey",),    # Monsoon Grey
    "LY7G": ("grey",),    # Quartz Grey
    "LZ7L": ("grey",),    # Lava Grey
    "LZ7S": ("grey",),    # Daytona Grey
    # ── Blue ──
    "LA5J": ("blue",),    # Pacific Blue
    "LC5F": ("blue",),    # Blue Graphite
    "LD5L": ("blue",),    # Blue Silk
    "LF5A": ("blue",),    # Pacific Blue (Skoda)
    "LF5Q": ("blue",),    # Storm Blue
    "LF5W": ("blue",),    # Race Blue
    "LP5M": ("blue",),    # Aquamarina Blue
    "LW5L": ("blue",),    # Mystery Blue
    "LY5J": ("blue",),    # Liquid Blue
    # ── Red ──
    "LA3H": ("red",),     # Salsa Red
    "LA3X": ("red",),     # Carman Red
    "LD3Y": ("red",),     # Crimson Red
    "LF3K": ("red",),     # Corrida Red
    "LP3G": ("red",),     # Flash Red
    "LS3H": ("red",),     # Emocion Red
    "LY3D": ("red",),     # Tornado Red
    # ── Green / beige ──
    "LF6Z": ("green",),   # Rallye Green
    "LF8H": ("beige",),   # Cappuccino Beige
}

# Words in a paint NAME that place it in a colour family. A name can hit more
# than one family ("Blue Graphite" → blue + grey); it is only flagged when it
# hits none of the code's families. Names with no colour word ("Sharkskin")
# are never flagged.
NAME_FAMILY_WORDS: dict[str, tuple[str, ...]] = {
    "black": ("black",),
    "white": ("white",),
    "silver": ("silver",),
    "grey": ("grey", "gray", "graphite", "anthracite"),
    "blue": ("blue",),
    "red": ("red",),
    "green": ("green",),
    "beige": ("beige", "cappuccino"),
    "yellow": ("yellow",),
    "orange": ("orange",),
    "brown": ("brown",),
    "gold": ("gold",),
    "purple": ("purple", "violet"),
}
