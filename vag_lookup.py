"""
VAG Part Number Lookup Module

Provides database-first part identification with exact match, middle-group match,
and side designation logic. Falls back to None when part is not in database,
signalling the caller to use Claude as fallback.
"""

import json
import os
import re
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "vag_parts_db.json")
_db = None


# ── Database I/O ─────────────────────────────────────────────────────────────

def _load_db() -> dict:
    global _db
    with open(DB_PATH, "r", encoding="utf-8") as f:
        _db = json.load(f)
    return _db


def get_db() -> dict:
    if _db is None:
        _load_db()
    return _db


def reload_db() -> dict:
    """Reload database from disk — call after auto-learning adds new entries."""
    return _load_db()


def save_db(db: dict):
    """Write the database back to disk."""
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


# ── Normalisation ─────────────────────────────────────────────────────────────

def normalise(raw: str) -> str:
    """Strip spaces and uppercase a part number. '6j3 837 401 aj' -> '6J3837401AJ'"""
    return raw.strip().replace(" ", "").upper()


# VAG part number structure: [digit][1-3 letters][opt digit][6-digit group][letters]
# Backtracking resolves the trailing-digit-of-prefix ambiguity automatically.
_VAG_GROUP_RE = re.compile(r"^\d[A-Za-z]{1,3}\d?(\d{6})[A-Za-z]*$")


def extract_middle_group(part_number: str) -> str | None:
    """
    Extract the 6-digit middle group from a normalised VAG part number.
    '6J3837401AJ' -> '837401', '5Q0407272C' -> '407272', '5NA945096E' -> '945096'
    """
    m = _VAG_GROUP_RE.match(part_number)
    return m.group(1) if m else None


# ── Side designation ──────────────────────────────────────────────────────────

# Group prefixes (first 3 digits of 6-digit middle group) that are paired parts.
# Value = position suffix to append ('F' for front, 'R' for rear, '' for no position)
_PAIRED_GROUPS: dict[str, str] = {
    "837": "F",   # front door lock/mechanism  -> NSF / OSF
    "839": "R",   # rear door lock/mechanism   -> NSR / OSR
    "857": "",    # door mirrors               -> NS  / OS
    "843": "",    # convertible roof           -> NS  / OS (rare)
    "827": "",    # bonnet/tailgate            -> single, no side
    "941": "F",   # front headlights           -> NSF / OSF
    "943": "F",   # front indicator / DRL      -> NSF / OSF
    "945": "R",   # rear taillights            -> NSR / OSR
    "947": "R",   # rear reflector             -> NSR / OSR
    "407": "",    # front suspension arm       -> NS  / OS
    "411": "",    # front wishbone             -> NS  / OS
    "505": "",    # rear suspension arm        -> NS  / OS
    "511": "",    # rear wishbone              -> NS  / OS
    "615": "",    # brake caliper              -> NS  / OS
    "616": "",    # wheel hub / bearing        -> NS  / OS
    "617": "",    # hub carrier                -> NS  / OS
    "853": "F",   # front bumper corners       -> NSF / OSF
    "807": "R",   # rear bumper corners        -> NSR / OSR
    "867": "",    # door cards (front)         -> NS  / OS
    "868": "",    # door cards (rear)          -> NS  / OS
}

# Within group 959, only these sub-groups (last 3 digits) are paired window motors.
# 959857/858 are single window switch packs.
_PAIRED_959: set[str] = {"801", "802", "811", "812", "703", "704"}

# Regex to detect side keywords already present in a description
_SIDE_IN_DESC = re.compile(
    r"\b(OS|NS|NSF|OSF|NSR|OSR|O\.S|N\.S|left|right|driver|passenger|nearside|offside)\b",
    re.IGNORECASE,
)


def determine_side(part_number: str, group_code: str, base_description: str) -> str:
    """
    Determine side designation (NSF, OSF, NSR, OSR, NS, OS) or '' if not applicable.

    Uses the VAG odd/even rule:
      - Odd last digit of the sub-group (last 3 digits of 6-digit group) = Left = Nearside (NS)
      - Even last digit = Right = Offside (OS)
    """
    if not group_code or len(group_code) != 6:
        return ""

    # If description already contains side information, don't add more
    if _SIDE_IN_DESC.search(base_description):
        return ""

    group_prefix = group_code[:3]   # e.g. "837"
    sub_group    = group_code[3:]   # e.g. "401"
    last_digit   = int(sub_group[-1])

    # Special handling for 959 (window electrics) — only some sub-groups are paired
    if group_prefix == "959":
        if sub_group not in _PAIRED_959:
            return ""
        return "NS" if last_digit % 2 == 1 else "OS"

    if group_prefix not in _PAIRED_GROUPS:
        return ""

    position = _PAIRED_GROUPS[group_prefix]          # "F", "R", or ""
    lateral  = "NS" if last_digit % 2 == 1 else "OS"  # odd=NS, even=OS
    return lateral + position  # e.g. "NS" + "F" = "NSF"


# ── Lookup ────────────────────────────────────────────────────────────────────

def lookup_part(raw: str) -> dict | None:
    """
    Look up a part number in the database.

    Lookup order:
      1. Exact match  -> confidence 'high'
      2. Middle-group -> confidence 'medium', side designation appended
      3. Learned      -> confidence 'medium'
      4. None         -> caller should fall back to Claude

    Returns dict with keys: description, source, confidence,
                             breakerpro_price, vehicle
    """
    db  = get_db()
    pn  = normalise(raw)

    # 1. Exact match
    if pn in db.get("exact", {}):
        entry = db["exact"][pn]
        return {
            "description":     entry["description"],
            "source":          "database",
            "confidence":      "high",
            "breakerpro_price": entry.get("breakerpro_price"),
            "vehicle":         entry.get("vehicle"),
        }

    # 2. Middle-group match
    group = extract_middle_group(pn)
    if group and group in db.get("groups", {}):
        entry     = db["groups"][group]
        base_desc = entry["description"]
        side      = determine_side(pn, group, base_desc)
        combined  = f"{base_desc} {side}".strip() if side else base_desc
        return {
            "description":     combined,
            "source":          "database",
            "confidence":      "medium",
            "breakerpro_price": entry.get("avg_price"),
            "vehicle":         None,
        }

    # 3. Learned match
    if pn in db.get("learned", {}):
        entry = db["learned"][pn]
        return {
            "description":     entry["description"],
            "source":          "learned",
            "confidence":      "medium",
            "breakerpro_price": None,
            "vehicle":         None,
        }

    return None


# ── Auto-learning ─────────────────────────────────────────────────────────────

def save_learned(part_number: str, description: str):
    """
    Persist a Claude-identified part to the 'learned' section of the database.
    Called automatically after every successful Claude fallback.
    """
    db = get_db()
    pn = normalise(part_number)

    db.setdefault("learned", {})[pn] = {
        "description": description,
        "learned_at":  datetime.now(timezone.utc).isoformat(),
    }
    db.setdefault("_meta", {})["total_learned_entries"] = len(db["learned"])

    save_db(db)
    reload_db()
