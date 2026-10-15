"""
VAG paint code → friendly name dictionary.

Used to auto-populate paint_name on vehicle records when only a code has been
entered. Codes are normalised to uppercase + stripped before lookup.

Oct 2026: several entries were wrong (LC9X said "Reflex Silver" but is a
black) and the wrong names were auto-filled onto vehicles. Entries marked
"BreakerPro" were corrected from our own stock exports. A test checks every
entry against the colour families in paint_config.py.
"""

from __future__ import annotations

import re
from typing import Optional

from paint_config import NAME_FAMILY_WORDS, PAINT_CODE_FAMILIES


PAINT_CODES: dict[str, str] = {
    # Reflex Silver
    "LZ7G": "Reflex Silver",
    "LA7W": "Reflex Silver",
    # Whites
    "LB9A": "Candy White",
    "LC9A": "Pure White",             # BreakerPro (was "Black Magic Pearl")
    "B4B4": "Pure White",
    # Blacks
    "LC9X": "Deep Black Pearl",       # BreakerPro (was "Reflex Silver")
    "LZ9Y": "Phantom Black",          # BreakerPro (was "Pure White")
    "LC9Z": "Deep Black Pearl",
    "LB9Y": "Black Uni",
    # Silvers / greys
    "LD7X": "Platinum Grey",          # BreakerPro (was "Tungsten Silver")
    "LA7T": "Sharkskin",
    "LX7Z": "Mythos Black",
    "LY7G": "Quartz Grey",
    "LH7Z": "Indium Grey",
    # Reds
    "LD5R": "Tornado Red",
    "LP3G": "Flash Red",              # BreakerPro (was "Tornado Red")
    "LY3D": "Tornado Red",            # BreakerPro (was "Flash Red")
    "LA3W": "Mars Red",
    # Blues
    "LB5K": "Lapiz Blue",
    "LD5Q": "Shadow Blue",            # PartsLink24 (was "Atlantic Blue")
    "LA5W": "Night Blue Pearl",
    # Greys
    "LD7W": "Pepper Grey",
    "LB7Z": "Urano Grey",
    # Yellows / greens
    "LD8U": "Honey Yellow",
    "LB6V": "Viper Green",
    # Blues continued
    "LP5C": "Reef Blue",
    "LD6P": "Cornflower Blue",
    "LR5W": "Caribbean Blue",
    "LB5W": "Pacific Blue",
    # Greys continued
    "LZ7H": "Steel Grey",
    "LA7N": "Limestone Grey",         # BreakerPro (was "Monsoon Grey")
    "LI7F": "Urano Grey",             # BreakerPro
    "LK7X": "Island Grey",            # BreakerPro (was "Sand Beige")
}


def lookup_paint_name(code: Optional[str]) -> Optional[str]:
    """Return the friendly name for a paint code, or None if not found.

    Input is normalised to uppercase and stripped of whitespace before lookup.
    None input returns None."""
    if code is None:
        return None
    key = str(code).strip().upper()
    if not key:
        return None
    return PAINT_CODES.get(key)


# ── Paint sanity check (code vs name colour family) ─────────────────────

def colour_families_of_code(code: Optional[str]) -> tuple[str, ...]:
    """Colour families a paint code belongs to; () if the code is unknown."""
    if not code:
        return ()
    return PAINT_CODE_FAMILIES.get(str(code).strip().upper(), ())


def colour_families_in_name(name: Optional[str]) -> set[str]:
    """Colour families named in a paint name, by whole word ("Blue Graphite"
    → {"blue", "grey"}). Empty if the name has no colour word."""
    if not name:
        return set()
    words = set(re.findall(r"[a-z]+", str(name).lower()))
    return {fam for fam, keys in NAME_FAMILY_WORDS.items() if words & set(keys)}


def paint_family_conflict(code: Optional[str], name: Optional[str]) -> Optional[str]:
    """Return a short warning if the paint name's colour clearly contradicts
    the paint code (e.g. LC9X + "Reflex Silver"), else None. Unknown codes and
    names without a colour word are never flagged."""
    code_fams = colour_families_of_code(code)
    name_fams = colour_families_in_name(name)
    if not code_fams or not name_fams or name_fams & set(code_fams):
        return None
    return (f"Paint code {str(code).strip().upper()} is {'/'.join(code_fams)}"
            f" but the name says {'/'.join(sorted(name_fams))} ({str(name).strip()})")
