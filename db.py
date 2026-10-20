"""
SQLite vehicle database for Parts Logger v2.0.

Storage path is configurable via DB_PATH (defaults to ./vehicles.db). On
Railway, DB_PATH should point to a mounted volume, e.g. /data/vehicles.db.

All helpers return plain dicts so FastAPI can serialise them straight to JSON.
"""

from __future__ import annotations

import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional

from paint_codes import lookup_paint_name

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "./vehicles.db"


def _resolve_db_path() -> str:
    """Resolve DB_PATH env var; fall back to ./vehicles.db if parent dir is
    missing or not writable."""
    requested = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    parent = os.path.dirname(os.path.abspath(requested)) or "."
    if not os.path.isdir(parent):
        log.warning("DB_PATH parent dir %s missing — falling back to %s", parent, DEFAULT_DB_PATH)
        return DEFAULT_DB_PATH
    if not os.access(parent, os.W_OK):
        log.warning("DB_PATH parent dir %s not writable — falling back to %s", parent, DEFAULT_DB_PATH)
        return DEFAULT_DB_PATH
    return requested


DB_PATH = _resolve_db_path()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    d = {k: row[k] for k in row.keys()}
    # Surface is_active as a real bool to clients (stored as INTEGER 0/1).
    if "is_active" in d and d["is_active"] is not None:
        d["is_active"] = bool(d["is_active"])
    return d


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Schema ──────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    ref TEXT PRIMARY KEY,
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    year_range TEXT,
    paint_code TEXT,
    paint_name TEXT,
    engine_code TEXT,
    transmission TEXT,
    vin TEXT,
    notes TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    status_changed_at TEXT,
    status_changed_by TEXT,
    damage_notes TEXT,
    arrived_at TEXT
)
"""

# Where a donor car is: on_yard (being broken, parts still on it), stripped
# (everything worth keeping is off and on the shelf), crushed (gone), or
# unknown (never set; every vehicle that existed before statuses did).
VEHICLE_STATUSES = ("on_yard", "stripped", "crushed", "unknown")

# Additive migrations: (column, definition). Each is only ever ADDed, never
# altered or dropped, so existing rows keep every value they had and pick up
# the column default (status -> 'unknown', the rest -> NULL).
_ADDED_COLUMNS = (
    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("status_changed_at", "TEXT"),
    ("status_changed_by", "TEXT"),
    ("damage_notes", "TEXT"),
    ("arrived_at", "TEXT"),
)


class RefAlreadyExists(Exception):
    """Raised when create_vehicle is called with a ref that already exists."""


def init_db() -> None:
    """Create the vehicles table if it doesn't already exist, and run any
    idempotent column migrations."""
    # Re-resolve in case DB_PATH was changed after module import (tests).
    global DB_PATH
    DB_PATH = _resolve_db_path()
    with _connect() as conn:
        conn.execute(_CREATE_TABLE_SQL)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(vehicles)")}
        for col, definition in _ADDED_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE vehicles ADD COLUMN {col} {definition}")
        conn.commit()


# ── Ref normalisation ───────────────────────────────────────────────────

def normalise_ref(value: Optional[str]) -> str:
    """Strip whitespace, uppercase, prefix 'VEH' unless the ref already starts
    with VEH.  Raises ValueError on empty / invalid input.

    Examples:
        "1234"      → "VEH1234"
        "VEH1234"   → "VEH1234"
        "veh1234"   → "VEH1234"
        " VEH1234 " → "VEH1234"
        "VEH 1234"  → "VEH1234"
        "VEH-1234"  → "VEH1234"
        "VEG1234"   → "VEHVEG1234"  (intentional — user typo)
        ""          → raises ValueError
        "VEH"       → raises ValueError
        "   "       → raises ValueError
    """
    import re as _re

    if value is None or str(value).strip() == "":
        raise ValueError("ref cannot be empty")

    s = str(value).strip().upper()

    if s.startswith("VEH"):
        remainder = s[3:]
        if not remainder:
            raise ValueError("ref cannot be just 'VEH'")
        # Strip non-alphanumeric from remainder
        cleaned = _re.sub(r"[^A-Z0-9]", "", remainder)
        if not cleaned:
            raise ValueError("ref must contain alphanumeric characters")
        return f"VEH{cleaned}"

    # Does NOT start with VEH — strip non-alphanumeric, prefix VEH
    cleaned = _re.sub(r"[^A-Z0-9]", "", s)
    if not cleaned:
        raise ValueError("ref must contain alphanumeric characters")
    return f"VEH{cleaned}"


# ── CRUD helpers ────────────────────────────────────────────────────────

_FIELDS = [
    "ref", "make", "model", "year_range", "paint_code", "paint_name",
    "engine_code", "transmission", "vin", "notes", "is_active",
    "damage_notes", "arrived_at",
]


def _auto_paint_name(data: dict) -> dict:
    """If paint_code is present and paint_name is empty/None, fill paint_name
    from the paint code dictionary. Mutates and returns data. A user-supplied
    paint_name is never overwritten."""
    code = data.get("paint_code")
    if code:
        existing = data.get("paint_name")
        if existing is None or str(existing).strip() == "":
            resolved = lookup_paint_name(code)
            if resolved:
                data["paint_name"] = resolved
    return data


def get_vehicle(ref: str) -> Optional[dict]:
    norm = normalise_ref(ref)
    with _connect() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE ref = ?", (norm,)).fetchone()
    return _row_to_dict(row)


def list_vehicles(include_inactive: bool = False) -> list[dict]:
    """Return vehicles ordered by most-recently-created first.

    By default only active vehicles are returned. Pass include_inactive=True
    to include soft-deleted rows (is_active = 0)."""
    sql = "SELECT * FROM vehicles"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_vehicle(data: dict) -> dict:
    """Insert a new vehicle. Auto-fills paint_name from paint_code if not
    user-supplied. Raises RefAlreadyExists if the ref is already present
    (whether active or soft-deleted). Returns the saved record."""
    payload = dict(data)
    payload["ref"] = normalise_ref(payload.get("ref"))
    if not payload.get("make"):
        raise ValueError("make is required")
    if not payload.get("model"):
        raise ValueError("model is required")

    _auto_paint_name(payload)

    payload["is_active"] = 0 if payload.get("is_active") is False else 1

    if get_vehicle(payload["ref"]) is not None:
        raise RefAlreadyExists(payload["ref"])

    now = _now()
    values = {f: payload.get(f) for f in _FIELDS}
    values["created_at"] = now
    values["updated_at"] = now

    cols = list(values.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    with _connect() as conn:
        try:
            conn.execute(
                f"INSERT INTO vehicles ({col_list}) VALUES ({placeholders})",
                tuple(values[c] for c in cols),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise RefAlreadyExists(payload["ref"])
    return get_vehicle(payload["ref"])


def update_vehicle(ref: str, data: dict) -> Optional[dict]:
    """Update an existing vehicle. Returns the updated record or None if the
    ref does not exist."""
    norm = normalise_ref(ref)
    existing = get_vehicle(norm)
    if existing is None:
        return None

    payload = dict(data)
    # Force the ref to match the URL path so callers can't rename.
    payload["ref"] = norm

    # Paint code and name are a pair: a new code sent without a name must not
    # keep the OLD code's name. Clear it so it is re-derived for the new code
    # (or left blank if the code is unknown). Re-sending the same code keeps
    # the stored name rather than swapping in the dictionary's.
    if "paint_code" in payload and "paint_name" not in payload:
        old = (existing.get("paint_code") or "").strip().upper()
        new = (payload.get("paint_code") or "").strip().upper()
        payload["paint_name"] = None if new != old else existing.get("paint_name")

    _auto_paint_name(payload)

    merged = dict(existing)
    for f in _FIELDS:
        if f in payload:
            merged[f] = payload[f]
    merged["updated_at"] = _now()

    cols = _FIELDS + ["updated_at"]
    set_clause = ", ".join(f"{c} = ?" for c in cols if c != "ref")
    values = [merged[c] for c in cols if c != "ref"]
    values.append(norm)

    with _connect() as conn:
        conn.execute(
            f"UPDATE vehicles SET {set_clause} WHERE ref = ?",
            tuple(values),
        )
        conn.commit()
    return get_vehicle(norm)


def delete_vehicle(ref: str) -> bool:
    """Soft delete: sets is_active = 0. Returns True if a row was updated."""
    norm = normalise_ref(ref)
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE vehicles SET is_active = 0, updated_at = ? WHERE ref = ?",
            (now, norm),
        )
        conn.commit()
        return cur.rowcount > 0


_UNSET = object()


def set_vehicle_status(ref: str, status: str, damage_notes=_UNSET,
                       changed_by: Optional[str] = None) -> Optional[dict]:
    """Set a vehicle's status (and optionally its damage notes). Returns the
    updated record, or None if the ref does not exist.

    status_changed_at / status_changed_by only move when the status actually
    changes, so "stripped since 3 Oct" stays true if someone re-taps Stripped.
    damage_notes is left alone unless passed; None or "" clears it."""
    if status not in VEHICLE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(VEHICLE_STATUSES)}")
    norm = normalise_ref(ref)
    existing = get_vehicle(norm)
    if existing is None:
        return None

    now = _now()
    sets = {"updated_at": now}
    if existing.get("status") != status:
        sets.update(status=status, status_changed_at=now,
                    status_changed_by=(changed_by or "").strip() or None)
    if damage_notes is not _UNSET:
        sets["damage_notes"] = (damage_notes or "").strip() or None

    set_clause = ", ".join(f"{c} = ?" for c in sets)
    with _connect() as conn:
        conn.execute(f"UPDATE vehicles SET {set_clause} WHERE ref = ?",
                     (*sets.values(), norm))
        conn.commit()
    return get_vehicle(norm)
