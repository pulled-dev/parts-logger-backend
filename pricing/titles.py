"""Parse our own eBay titles into part type + model/generation (+ engine code).

Feeds the §4c "own sales by part type" fallback, e.g. "GOLF MK7 · WINDOW MOTOR".

    parse_title("2012-2017 Volkswagen Golf Mk7 1.6 TDI CLHA Brake Master Cylinder 5Q2 611 301")
    -> {"part_type": "BRAKE MASTER CYLINDER", "model": "GOLF", "generation": "MK7",
        "engine_group": "CLH", ...}

Part type comes from the BreakerPro description vocabulary in vag_parts_db.json
(longest phrase found in the title wins) plus EXTRA_PART_TYPES below, then gets
canonicalised: side words dropped, common synonyms merged. Engine code is only
recorded as its first 3 letters ("DSGB" -> "DSG"; the 4th letter doesn't matter).
"""

from __future__ import annotations

import re
from functools import lru_cache

from pricing.normalise import extract_part_numbers

MAKES = {
    "VOLKSWAGEN": "VOLKSWAGEN", "VW": "VOLKSWAGEN", "AUDI": "AUDI", "SEAT": "SEAT",
    "SKODA": "SKODA", "CUPRA": "CUPRA", "PORSCHE": "PORSCHE", "LEVC": "LEVC",
}

# Multi-word models first so "GOLF PLUS" beats "GOLF".
MODELS = [
    "GOLF PLUS", "GOLF SV", "T-ROC", "T-CROSS", "ID.3", "ID3", "ID4", "UP!",
    "POLO", "GOLF", "PASSAT", "TIGUAN", "TOURAN", "TOUAREG", "CADDY", "TRANSPORTER",
    "UP", "FOX", "SHARAN", "SCIROCCO", "BEETLE", "JETTA", "EOS", "ARTEON", "TAIGO",
    "CRAFTER", "AMAROK", "CC",
    "A1", "A3", "A4", "A5", "A6", "A7", "A8", "Q2", "Q3", "Q5", "Q7", "Q8", "TT",
    "IBIZA", "LEON", "ALTEA", "ALHAMBRA", "ARONA", "ATECA", "TARRACO", "MII", "TOLEDO",
    "FORMENTOR", "BORN",
    "FABIA", "OCTAVIA", "SUPERB", "CITIGO", "YETI", "RAPID", "KAROQ", "KODIAQ",
    "KAMIQ", "SCALA", "ROOMSTER", "ENYAQ", "TX",
]
_MODEL_RES = [(m, re.compile(r"(?<![A-Z0-9])" + re.escape(m) + r"(?![A-Z0-9])")) for m in MODELS]
_MAKE_MODEL_RE = re.compile(
    r"\b(?:" + "|".join(MAKES) + r")\s+(?=(?:" + "|".join(re.escape(m) for m in MODELS) + r")(?![A-Z0-9]))"
)
_MODEL_CANON = {"ID.3": "ID3", "UP!": "UP"}

# Generation tokens: Mk7 / Mk7.5, B8, T6, 6R, 9N3, 1T3, 8V, 2G, C20...
_GEN_RE = re.compile(r"^(MK\d{1,2}(?:\.5)?|B[5-9]|T[4-7]|C20|\d[A-Z]\d?|\d[A-Z]{2}|8[A-Z])$")
_GEN_EXCLUDE = {"3D", "5D", "4X", "2D"}

# Words that look like engine codes but aren't.
_NOT_ENGINE = {
    "TDI", "TSI", "TFSI", "FSI", "GTI", "GTD", "GTE", "DSG", "SRS", "ABS", "ECU", "LED",
    "HID", "AIR", "CON", "FOR", "AND", "THE", "NON", "OEM", "VRS", "MPV", "LWB", "SWB",
    "CAR", "VAN", "DOOR", "SIDE", "REAR", "LEFT", "PAIR", "UNIT", "AUTO", "SPEED", "WITH",
    "LINE", "SPORT", "HATCHBACK", "ESTATE", "PETROL", "DIESEL", "TECH", "MOVE", "MATCH",
    "LIFE", "BEATS", "COMFORT", "PLUS", "VISTA", "STYLE", "BLUE", "BLACK", "WHITE",
    "GREY", "SILVER", "RED", "MANUAL", "QUATTRO", "BLUEMOTION", "GENUINE", "FRONT",
    "WING", "FUEL", "BOOT", "DASH", "SEAT", "HEAD", "GEAR", "HORN", "PUMP", "LOCK",
    "RACK", "ROOF", "TRIM", "HUB", "LEG", "ARM", "FULL", "BARE", "CAP", "KIT", "SET",
    "FUSE", "BOX",
}
_DISPLACEMENT_RE = re.compile(r"\d\.\d")
_FUEL_RE = re.compile(r"^(TDI|TSI|TFSI|FSI|PETROL|DIESEL|MPI|HYBRID)$")
_ENGINE_TOKEN_RE = re.compile(r"^[A-Z]{3,4}$")

# Extra part types (body panels, drivetrain and common phrasings that aren't in
# the BreakerPro vocabulary).
EXTRA_PART_TYPES = [
    "DOOR", "DOOR BARE", "DOOR SHELL", "DOOR LOCK", "DOOR CARD", "DOOR PANEL", "DOOR GLASS",
    "DOOR MIRROR", "WING MIRROR", "DOOR HANDLE", "DOOR HINGE", "DOOR CAP", "BONNET",
    "BONNET LATCH", "WING", "BUMPER", "FRONT BUMPER", "REAR BUMPER", "TAILGATE",
    "BOOT LID", "BOOT LID HANDLE", "FUEL FLAP", "SLAM PANEL", "RAD PACK", "GRILLE",
    "SLIP RING", "SQUIB", "AIRBAG", "AIRBAG MODULE", "AIRBAG CONTROL MODULE",
    "ALLOY WHEEL", "SPACE SAVER WHEEL", "ENGINE", "GEARBOX", "TURBO", "STARTER MOTOR",
    "ALTERNATOR", "AIR CON COMPRESSOR", "RADIATOR", "INJECTOR", "WIRING LOOM",
    "INFOTAINMENT SCREEN", "AERIAL", "HEATER FLAP MOTOR", "SEAT", "POLLEN FILTER COVER",
    "HEADLIGHT", "REAR LIGHT", "TAIL LIGHT", "FOG LIGHT", "WINDOW REGULATOR",
    "WINDOW MOTOR", "WIPER MOTOR", "STEERING RACK", "DRIVESHAFT", "CALIPER",
    "MASTER CYLINDER", "COOLANT PUMP", "VACUUM PUMP", "AIR INTAKE", "HEATER MOTOR",
    "LEG AND HUB", "WHEEL TRIM", "DASH VENTS", "ARCH TRIM", "CD PLAYER",
]

# (pattern, replacement) applied to the upper-cased title before matching, so
# synonyms collapse to one part type.
_SYNONYMS = [
    (r"\bA[ -]?BAG\b", "AIRBAG"), (r"\bAIR BAG\b", "AIRBAG"), (r"\bSRS\s+AIRBAG\b", "AIRBAG"),
    (r"\bHEADLIGHT/HEADLAMP\b", "HEADLIGHT"), (r"\bHEADLAMP\b", "HEADLIGHT"),
    (r"\bREAR/TAIL LIGHT\b", "REAR LIGHT"), (r"\bTAIL LIGHT\b", "REAR LIGHT"),
    (r"\bSEATBELT\b", "SEAT BELT"), (r"\bSPEEDO CLOCKS?\b", "SPEEDO CLUSTER"),
    (r"\bFUEL/PETROL FLAP\b", "FUEL FLAP"), (r"\bDOOR SHELL\b", "DOOR BARE"),
    (r"\bAIR CONDITIONING\b", "AIR CON"), (r"\bBOOTLID\b", "BOOT LID"),
    (r"\bDOOR LOCK MECH(ANISM)?\b", "DOOR LOCK"),
    (r"\bECU\s*\(ENGINE\)", "ENGINE ECU"), (r"\bENGINE\s+CONTROL\s+(UNIT|MODULE)\b", "ENGINE ECU"),
]
_SYNONYM_RES = [(re.compile(p), r) for p, r in _SYNONYMS]

# Side words removed from part types (front/rear is kept: prices differ).
_SIDE_RE = re.compile(
    r"\b(DRIVERS?|PASSENGERS?|NEARSIDE|OFFSIDE|N/S|O/S|NSF|OSF|NSR|OSR|NS|OS|LEFT|RIGHT|SIDE|LH|RH)\b"
)

# Part types where the engine code matters for price.
_ENGINE_RELEVANT_WORDS = {
    "ENGINE", "GEARBOX", "TURBO", "ECU", "INJECTOR", "INJECTORS", "LOOM", "STARTER",
    "ALTERNATOR", "EGR", "DPF", "INTAKE", "MANIFOLD", "THROTTLE", "COMPRESSOR",
    "FUEL", "PUMP", "FLYWHEEL", "CLUTCH", "SUMP", "HEAD",
}


def _apply_synonyms(text: str) -> str:
    for rx, rep in _SYNONYM_RES:
        text = rx.sub(rep, text)
    return text


def canonical_part_type(text: str | None) -> str:
    """Clean a part-type string: synonyms merged, parentheticals, side words and
    punctuation removed, whitespace collapsed."""
    if not text:
        return ""
    s = _apply_synonyms(str(text).upper())
    s = re.sub(r"\(.*?\)", " ", s)
    s = _SIDE_RE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9&/ ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" /&")
    return s


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[tuple[str, re.Pattern], ...]:
    from vag_lookup import get_db  # local import: heavy JSON, load once

    phrases: set[str] = set()
    db = get_db()
    for section in ("exact", "groups"):
        for entry in db.get(section, {}).values():
            c = canonical_part_type(entry.get("description"))
            if len(c) >= 3:
                phrases.add(c)
    phrases.update(canonical_part_type(p) for p in EXTRA_PART_TYPES)
    phrases.discard("")
    ordered = sorted(phrases, key=len, reverse=True)
    return tuple(
        (p, re.compile(r"(?<![A-Z])" + re.escape(p) + r"(?![A-Z])")) for p in ordered
    )


def find_part_type(text: str | None) -> str:
    """Longest known part-type phrase in the text, or "" if none."""
    if not text:
        return ""
    # Canonicalise the haystack the same way as the vocabulary, but keep the
    # parenthetical *content* (e.g. "DOOR LOCK(FRONT DRIVER SIDE)").
    hay = _apply_synonyms(str(text).upper())
    hay = _MAKE_MODEL_RE.sub(" ", hay)  # "SEAT Ibiza" is the make, not a seat
    hay = _SIDE_RE.sub(" ", hay)
    hay = re.sub(r"[^A-Z0-9&/ ]", " ", hay)
    hay = re.sub(r"\s+", " ", hay)
    for phrase, rx in _vocabulary():
        if rx.search(hay):
            return phrase
    return ""


def engine_relevant(part_type: str) -> bool:
    return bool(set(part_type.split()) & _ENGINE_RELEVANT_WORDS)


def engine_group(code: str | None) -> str:
    """First 3 letters of an engine code ('CLHA' -> 'CLH', 'DSGB' -> 'DSG')."""
    if not code:
        return ""
    c = re.sub(r"[^A-Z]", "", str(code).upper())
    return c[:3] if len(c) >= 3 else ""


def parse_model(text: str | None) -> tuple[str, str, str]:
    """(make, model, generation) from a title or vehicle string."""
    if not text:
        return "", "", ""
    t = str(text).upper()
    # Ignore everything from the first part number onwards for the gen search.
    pns = extract_part_numbers(t)
    tokens = re.split(r"[\s,]+", t)
    make = next((MAKES[tok] for tok in tokens if tok in MAKES), "")
    model, model_end = "", -1
    for m, rx in _MODEL_RES:
        hit = rx.search(t)
        if hit:
            model, model_end = _MODEL_CANON.get(m, m), hit.end()
            break
    gen = ""
    if model:
        tail = t[model_end:]
        if pns:
            # Cut at the first part number so its prefix isn't read as a gen.
            first = re.search(r"[0-9][A-Z0-9]{2}[ .\-]*[0-9]{3}", tail)
            if first:
                tail = tail[: first.start()]
        for tok in re.split(r"[\s,]+", tail)[:6]:
            tok = tok.strip("()")
            if tok in _GEN_EXCLUDE:
                continue
            if _GEN_RE.match(tok):
                gen = tok
                break
    return make, model, gen


def parse_engine_code(text: str | None) -> str:
    """Engine code that follows a displacement / fuel token ('1.6 TDI CLHA')."""
    if not text:
        return ""
    tokens = [t.strip("(),") for t in str(text).upper().split()]
    for i, tok in enumerate(tokens):
        if not _ENGINE_TOKEN_RE.match(tok) or tok in _NOT_ENGINE:
            continue
        prev = tokens[i - 1] if i >= 1 else ""
        prev2 = tokens[i - 2] if i >= 2 else ""
        # "1.6 CAY", "1.9TDI BXE", "1.0 PETROL CHY", "1.6 TDI CLHA"
        if _DISPLACEMENT_RE.search(prev):
            return tok
        if _FUEL_RE.match(prev) and (_DISPLACEMENT_RE.search(prev2) or prev in ("PETROL", "DIESEL")):
            return tok
    return ""


def parse_title(title: str | None) -> dict:
    make, model, gen = parse_model(title)
    part_type = find_part_type(title)
    eng = parse_engine_code(title)
    return {
        "part_type": part_type,
        "make": make,
        "model": model,
        "generation": gen,
        "engine_code": eng,
        "engine_group": engine_group(eng),
    }
