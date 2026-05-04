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
    return {k: row[k] for k in row.keys()}


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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def init_db() -> None:
    """Create the vehicles table if it doesn't already exist."""
    # Re-resolve in case DB_PATH was changed after module import (tests).
    global DB_PATH
    DB_PATH = _resolve_db_path()
    with _connect() as conn:
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()


# ── Ref normalisation ───────────────────────────────────────────────────

def normalise_ref(value: Optional[str]) -> str:
    """Strip whitespace, uppercase, prefix 'VEH' if input is digits-only."""
    if value is None:
        return ""
    s = str(value).strip().upper()
    if not s:
        return ""
    if s.isdigit():
        return f"VEH{s}"
    return s


# ── CRUD helpers ────────────────────────────────────────────────────────

_FIELDS = [
    "ref", "make", "model", "year_range", "paint_code", "paint_name",
    "engine_code", "transmission", "vin", "notes",
]


def _auto_paint_name(data: dict) -> dict:
    """Hook for paint_name auto-resolution. Filled in Task 2 when the paint
    code dictionary lands. For now this is a no-op."""
    return data


def get_vehicle(ref: str) -> Optional[dict]:
    norm = normalise_ref(ref)
    with _connect() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE ref = ?", (norm,)).fetchone()
    return _row_to_dict(row)


def list_vehicles() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM vehicles ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_vehicle(data: dict) -> dict:
    """Insert or replace a vehicle. Auto-fills paint_name from paint_code if
    not user-supplied. Returns the saved record."""
    payload = dict(data)
    payload["ref"] = normalise_ref(payload.get("ref"))
    if not payload["ref"]:
        raise ValueError("ref is required")
    if not payload.get("make"):
        raise ValueError("make is required")
    if not payload.get("model"):
        raise ValueError("model is required")

    _auto_paint_name(payload)

    now = _now()
    # Preserve existing created_at on upsert
    existing = get_vehicle(payload["ref"])
    created_at = existing["created_at"] if existing else now
    updated_at = now

    values = {f: payload.get(f) for f in _FIELDS}
    values["created_at"] = created_at
    values["updated_at"] = updated_at

    cols = list(values.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO vehicles ({col_list}) VALUES ({placeholders})",
            tuple(values[c] for c in cols),
        )
        conn.commit()
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
    norm = normalise_ref(ref)
    with _connect() as conn:
        cur = conn.execute("DELETE FROM vehicles WHERE ref = ?", (norm,))
        conn.commit()
        return cur.rowcount > 0
