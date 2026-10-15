"""Which part types genuinely come in a left and a right.

The AI (Claude) often adds a side to parts that don't have one, e.g.
"Headlight Switch Passenger Side". For any part that ISN'T one of the sided
types below, the side words are stripped from the AI's answer before the logger
sees it. Sided parts keep their side.

How a description is judged, in order:
  1. contains anything in ALWAYS_SIDED     -> sided (beats the exceptions)
  2. contains anything in NOT_SIDED_WORDS  -> not sided
  3. contains anything in SIDED_PART_TYPES -> sided
  4. otherwise                             -> not sided

This only touches the AI's answer. Names from our own BreakerPro database and
names a logger typed in are never changed.

Edit these lists and redeploy; no other code changes needed. Matching ignores
case, matches whole words only, and also matches the plural ("doors",
"switches").
"""

# Always sided, even though they contain an exception word below
# ("ABS Sensor" has "sensor", "Window Switch" has "switch").
ALWAYS_SIDED = [
    "abs sensor",
    "window switch", "window switch pack",
    "door control module", "door module",
    "seat belt", "seat belt pretensioner",
    "headlight washer jet",
]

# Part types that have a side. A description containing any of these is
# treated as sided (unless it also contains a word from NOT_SIDED_WORDS).
SIDED_PART_TYPES = [
    "headlight", "headlamp",
    "tail light", "tail lamp", "rear light", "rear lamp",
    "mirror",
    "door",
    "wing",
    "window regulator",
    "seat",
    "driveshaft", "drive shaft",
    "hub",
    "fog light", "fog lamp",
    "indicator", "repeater",
    "quarter glass",
    "wishbone", "control arm",
    "spring", "shock absorber", "strut",
    "brake caliper", "caliper",
    "track rod end",
    "arch liner", "wheel arch liner",
    "bumper bracket",
]

# A description containing one of these is NOT sided, even if it also names a
# sided part type above. "Headlight Switch" is a switch, not a headlight.
NOT_SIDED_WORDS = [
    "switch", "switches", "button", "buttons",
    "relay", "module", "control unit", "ecu", "fuse",
    "sensor", "interior mirror", "rear view mirror",
    "clock spring", "stalk",
    "boot strut", "bonnet strut", "tailgate strut",  # gas struts come as a pair, not a left and right
    "fuel filler door",
]

# "Seat" followed by one of these is the SEAT car make, not a seat:
# "SEAT Leon Fuel Pump" is a fuel pump. Add new SEAT models here.
SEAT_MODELS = [
    "leon", "ibiza", "arona", "ateca", "tarraco", "alhambra", "altea",
    "toledo", "cordoba", "exeo", "mii", "arosa", "inca", "marbella",
]

# Side words stripped from the AI's answer for non-sided parts. Longer phrases
# first, so "Passenger Side" goes as a whole rather than leaving "Side".
SIDE_WORDS = [
    "passenger's side", "passengers side", "passenger side",
    "driver's side", "drivers side", "driver side",
    "left hand side", "right hand side", "left hand", "right hand",
    "near side", "off side", "nearside", "offside",
    "n/s/f", "o/s/f", "n/s/r", "o/s/r", "n/s", "o/s",
    "nsf", "osf", "nsr", "osr", "ns", "os",
    "lhs", "rhs", "lh", "rh",
    "left", "right",
]
