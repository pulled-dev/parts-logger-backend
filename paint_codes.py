"""
VAG paint code → friendly name dictionary.

Used to auto-populate paint_name on vehicle records when only a code has been
entered. Codes are normalised to uppercase + stripped before lookup.
"""

from __future__ import annotations

from typing import Optional


PAINT_CODES: dict[str, str] = {
    # Reflex Silver
    "LC9X": "Reflex Silver",
    "LZ7G": "Reflex Silver",
    "LA7W": "Reflex Silver",
    # Whites
    "LB9A": "Candy White",
    "LZ9Y": "Pure White",
    "B4B4": "Pure White",
    # Blacks
    "LC9Z": "Deep Black Pearl",
    "LC9A": "Black Magic Pearl",
    "LB9Y": "Black Uni",
    # Silvers / greys
    "LD7X": "Tungsten Silver",
    "LA7T": "Sharkskin",
    "LX7Z": "Mythos Black",
    "LY7G": "Quartz Grey",
    "LH7Z": "Indium Grey",
    # Reds
    "LD5R": "Tornado Red",
    "LP3G": "Tornado Red",
    "LY3D": "Flash Red",
    "LA3W": "Mars Red",
    # Blues
    "LB5K": "Lapiz Blue",
    "LD5Q": "Atlantic Blue",
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
    "LA7N": "Monsoon Grey",
    # Beige
    "LK7X": "Sand Beige",
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
