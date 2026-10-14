"""Part number normalisation.

normalise("6R2 880 201 B")                -> "6R2880201B"
normalise("Part Number is 6r2.880.201.b") -> "6R2880201B"
base_number("6R2880201B")                 -> "6R2880201"   (index letters dropped)

VAG numbers are [prefix: digit + 2 alnum][3 digits][3 digits][0-3 index letters].
The index letters are often (not always) interchangeable, so base-number matches
are treated as looser / lower confidence by the engine.
"""

from __future__ import annotations

import re

_SEP = r"[ .\-]*"
# Lookbehind/lookahead stop us matching inside longer alphanumeric runs.
_VAG_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"([0-9][A-Z0-9]{2})" + _SEP +
    r"([0-9]{3})" + _SEP +
    r"([0-9]{3})"
    r"(?:[ .\-]?([A-Z]{1,3}))?"
    r"(?![A-Z0-9])"
)
_FULL_VAG_RE = re.compile(r"^([0-9][A-Z0-9]{2})([0-9]{3})([0-9]{3})([A-Z]{0,3})$")
_STRIP_RE = re.compile(r"[\s.\-/]+")
_GENERIC_RE = re.compile(r"^(?=.*[0-9])[A-Z0-9]{5,20}$")
_DIGITS_ONLY_RE = re.compile(r"^[0-9 .\-/]+$")
_YEAR_RANGE_RE = re.compile(r"^(19|20)[0-9]{2}\s*[-/]\s*(19|20)[0-9]{2}$")


def _fix_prefix(prefix: str) -> str:
    # Letter O typed for zero in the 3rd prefix char ("5KO" -> "5K0"). VAG
    # prefixes end in a digit or a letter, but never the letter O in practice.
    if prefix[2] == "O":
        prefix = prefix[:2] + "0"
    return prefix


def extract_part_numbers(text: str | None) -> list[str]:
    """Return every VAG-style part number found in free text, normalised, in
    order of appearance, without duplicates."""
    if not text:
        return []
    out: list[str] = []
    for m in _VAG_RE.finditer(str(text).upper()):
        pn = _fix_prefix(m.group(1)) + m.group(2) + m.group(3) + (m.group(4) or "")
        if pn not in out:
            out.append(pn)
    return out


def normalise(pn: str | None) -> str:
    """Normalise a part number (or text containing one). Returns "" for junk.

    VAG numbers are pulled out of free text. Anything else is accepted only if,
    once spaces/dots/dashes are stripped, it's a single 5-20 char alphanumeric
    code containing a digit (e.g. Bosch "0 281 002 315")."""
    if pn is None:
        return ""
    s = str(pn).strip().upper()
    if not s:
        return ""
    cleaned = _STRIP_RE.sub("", s)
    # 1. The whole input is one VAG number (spaced, dotted, dashed or bare).
    m = _FULL_VAG_RE.match(cleaned)
    if m:
        return _fix_prefix(m.group(1)) + "".join(m.groups()[1:])
    # 2. All-digit codes (Bosch etc.) - don't carve a VAG-looking substring out.
    if _DIGITS_ONLY_RE.match(s):
        if _YEAR_RANGE_RE.match(s):
            return ""
        return cleaned if _GENERIC_RE.match(cleaned) else ""
    # 3. Free text containing a VAG number.
    found = extract_part_numbers(s)
    if found:
        return found[0]
    # 4. A single non-VAG code token (no words around it).
    if " " in s:
        return ""
    return cleaned if _GENERIC_RE.match(cleaned) else ""


def base_number(pn: str | None) -> str:
    """Normalised number with the trailing VAG index letters removed. Non-VAG
    numbers are returned normalised but otherwise unchanged."""
    n = normalise(pn)
    m = _FULL_VAG_RE.match(n)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return n


def middle_group(pn: str | None) -> str:
    """6-digit VAG group ('880201') used by the BreakerPro groups fallback."""
    m = _FULL_VAG_RE.match(normalise(pn))
    return m.group(2) + m.group(3) if m else ""


def spaced(pn: str | None) -> str:
    """Breaker title format: '6R2880201B' -> '6R2 880 201 B'."""
    n = normalise(pn)
    m = _FULL_VAG_RE.match(n)
    if not m:
        return n
    return " ".join(p for p in m.groups() if p)
