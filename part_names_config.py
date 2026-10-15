"""Which part types genuinely come in a left and a right.

The AI (Claude) often adds a side to parts that don't have one, e.g.
"Headlight Switch Passenger Side". For any part that ISN'T one of the sided
types below, the side words are stripped from the AI's answer before the logger
sees it. Sided parts keep their side.

This only touches the AI's answer. Names from our own BreakerPro database and
names a logger typed in are never changed.

Edit these lists and redeploy; no other code changes needed. Matching ignores
case, matches whole words only, and also matches the plural ("doors").
"""

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
]

# A description containing one of these is NOT sided, even if it also names a
# sided part type above. "Headlight Switch" is a switch, not a headlight.
NOT_SIDED_WORDS = [
    "switch", "switches", "button", "buttons",
    "relay", "module", "control unit", "ecu", "fuse",
    "sensor", "interior mirror", "rear view mirror",
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
