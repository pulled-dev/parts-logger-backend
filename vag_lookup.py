"""
VAG Part Number Lookup Module

Provides database-first part identification with exact match, middle-group match,
and side designation logic. Falls back to None when part is not in database,
signalling the caller to use Claude as fallback.

The database lives in SQLite on the Railway volume (see part_names.py), not in
vag_parts_db.json: that file is only the one-time seed, so a redeploy can't wipe
learned entries or the loggers' corrections. A logger's correction always wins.
"""

import re
from datetime import datetime, timezone

import db as _vehicles_db
import part_names

SEED_PATH = part_names.SEED_PATH
DB_PATH = SEED_PATH  # legacy name for the seed file
_db = None
_db_path = None  # the SQLite file _db was loaded from


# ── Database I/O ─────────────────────────────────────────────────────────────

def _load_db() -> dict:
    global _db, _db_path
    with part_names.session() as conn:
        part_names.init(conn)
        _db = part_names.load_all(conn)
    _db_path = _vehicles_db.DB_PATH
    return _db


def get_db() -> dict:
    if _db is None or _db_path != _vehicles_db.DB_PATH:
        _load_db()
    return _db


def reload_db() -> dict:
    """Reload the database from SQLite."""
    return _load_db()


def save_db(db: dict):
    """Write every exact/groups/learned entry back to SQLite (never to the
    git seed file)."""
    with part_names.session() as conn:
        part_names.init(conn)
        for section in part_names.SECTIONS:
            for key, entry in (db.get(section) or {}).items():
                part_names.put_entry(conn, section, key, entry)


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

def _lookup_uncorrected(db: dict, pn: str) -> dict | None:
    """What the database / AI says for this part number, ignoring corrections."""
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

    # 3. Learned match (an AI answer): no side unless the part really has one
    if pn in db.get("learned", {}):
        entry = db["learned"][pn]
        return {
            "description":     part_names.guard_ai_side(entry["description"]),
            "source":          "learned",
            "confidence":      "medium",
            "breakerpro_price": None,
            "vehicle":         None,
        }

    return None


def lookup_part(raw: str) -> dict | None:
    """
    Look up a part number in the database.

    Lookup order:
      0. Logger's correction -> source 'corrected', always wins
      1. Exact match  -> confidence 'high'
      2. Middle-group -> confidence 'medium', side designation appended
      3. Learned      -> confidence 'medium'
      4. None         -> caller should fall back to Claude

    Returns dict with keys: description, source, confidence,
                             breakerpro_price, vehicle,
                             ai_description (the name before any correction),
                             corrected, corrected_at
    """
    db  = get_db()
    pn  = normalise(raw)

    base = _lookup_uncorrected(db, pn)
    corr = db.get("corrections", {}).get(pn)
    if corr:
        return {
            "description":      corr["description"],
            "source":           "corrected",
            "confidence":       "high",
            "breakerpro_price": (base or {}).get("breakerpro_price"),
            "vehicle":          (base or {}).get("vehicle"),
            "ai_description":   corr.get("ai_description") or (base or {}).get("description"),
            "corrected":        True,
            "corrected_at":     corr.get("corrected_at"),
        }
    if base:
        base.update(ai_description=base["description"], corrected=False, corrected_at=None)
    return base


def lookup_uncorrected(raw: str) -> dict | None:
    """The database / AI answer for a part number, ignoring any correction."""
    return _lookup_uncorrected(get_db(), normalise(raw))


# ── Corrections ───────────────────────────────────────────────────────────────

def save_correction(part_number: str, description: str, ai_description: str | None = None,
                    price: float | None = None) -> dict | None:
    """Store a logger's correction (latest wins) and apply it straight away.
    The AI value is what the database / AI says without corrections; the
    page's copy is only used when we have no answer of our own."""
    pn = normalise(part_number)
    base = lookup_uncorrected(pn)
    ai = (base or {}).get("description") or ai_description
    with part_names.session() as conn:
        part_names.init(conn)
        row = part_names.save_correction(conn, pn, description, ai, price)
    corrections = get_db().setdefault("corrections", {})
    if row:
        corrections[pn] = row
    else:
        corrections.pop(pn, None)
    return row


# ── Auto-learning ─────────────────────────────────────────────────────────────

def save_learned(part_number: str, description: str, raw_description: str | None = None):
    """
    Persist a Claude-identified part to the 'learned' section of the database.
    Called automatically after every successful Claude fallback. raw_description
    is Claude's exact answer (before the side guard), kept for audit.
    """
    pn = normalise(part_number)
    entry = {
        "description": description,
        "learned_at":  datetime.now(timezone.utc).isoformat(),
    }
    if raw_description and raw_description != description:
        entry["ai_raw_description"] = raw_description
    with part_names.session() as conn:
        part_names.init(conn)
        part_names.put_entry(conn, "learned", pn, entry)
    get_db().setdefault("learned", {})[pn] = entry
