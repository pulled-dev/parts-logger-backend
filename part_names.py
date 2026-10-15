"""Part names that must survive a redeploy: the lookup database, the loggers'
manual corrections, and a record of every CSV export.

All of it lives in the SQLite file on the Railway volume (db.DB_PATH), next to
`vehicles` and the pricing tables. vag_parts_db.json in git is only the SEED:
it is copied in once, on the first boot that finds no seeded copy, and never
read again after that. So a redeploy can't wipe what the loggers taught it.

Tables
------
part_names           the lookup database. section = exact | groups | learned,
                     key = part number (or 6-digit group), data = the JSON entry
part_names_meta      seeded_at, seed_source, and the git file's _meta
part_corrections     the logger's name for a part number. The latest correction
                     wins. ai_description keeps what the system said before, for audit
part_correction_log  every correction ever received, oldest first (audit trail)
export_log           one row per exported CSV row: part number, AI name, final
                     name, edited or not, vehicle ref, export time
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import db as _db
import part_names_config as cfg

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vag_parts_db.json")
SECTIONS = ("exact", "groups", "learned")

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS part_names (
        section TEXT NOT NULL,          -- exact | groups | learned
        key TEXT NOT NULL,              -- normalised part number, or 6-digit group
        data TEXT NOT NULL,             -- the entry as JSON
        updated_at TEXT NOT NULL,
        PRIMARY KEY (section, key)
    )
    """,
    "CREATE TABLE IF NOT EXISTS part_names_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS part_corrections (
        part_number_norm TEXT PRIMARY KEY,
        description TEXT NOT NULL,      -- the logger's name: always wins
        ai_description TEXT,            -- what the system said before the first correction
        price REAL,
        first_corrected_at TEXT NOT NULL,
        corrected_at TEXT NOT NULL,     -- latest correction
        times_corrected INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS part_correction_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        part_number_norm TEXT NOT NULL,
        description TEXT NOT NULL,
        ai_description TEXT,
        corrected_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_corr_log_pn ON part_correction_log (part_number_norm, corrected_at)",
    """
    CREATE TABLE IF NOT EXISTS export_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        export_id TEXT NOT NULL,        -- one per CSV export, made by the page
        row_index INTEGER NOT NULL,
        exported_at TEXT NOT NULL,      -- when the logger pressed Export (page clock)
        received_at TEXT NOT NULL,      -- when it reached us (may be later: retries)
        part_number TEXT NOT NULL DEFAULT '',
        part_number_norm TEXT NOT NULL DEFAULT '',
        ai_description TEXT,            -- the AI / database name first shown
        final_description TEXT NOT NULL,-- the Part Description in the CSV
        edited INTEGER NOT NULL DEFAULT 0,
        correction_applied INTEGER NOT NULL DEFAULT 0,  -- a stored correction was auto-applied
        vehicle_ref TEXT NOT NULL DEFAULT '',
        UNIQUE (export_id, row_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_export_pn ON export_log (part_number_norm, exported_at)",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise(raw: str | None) -> str:
    return (raw or "").strip().replace(" ", "").upper()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Schema + one-time seed ──────────────────────────────────────────────

def init(conn: sqlite3.Connection | None = None, seed_path: str | None = None) -> dict:
    """Create the tables and, the first time only, copy the git seed file in.

    Once part_names_meta.seeded_at is set, the seed file is never read again:
    a redeploy (which ships a fresh copy of vag_parts_db.json) leaves the
    stored names, learned entries and corrections exactly as they were."""
    own = conn is None
    conn = conn or connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")  # one seeder at a time
        try:
            row = conn.execute("SELECT value FROM part_names_meta WHERE key = 'seeded_at'").fetchone()
            if row:
                conn.rollback()
                return {"seeded": False, "seeded_at": row[0]}
            summary = _seed(conn, seed_path or SEED_PATH)
            conn.commit()
            return summary
        except BaseException:
            conn.rollback()
            raise
    finally:
        if own:
            conn.close()


def _seed(conn: sqlite3.Connection, path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        src = json.load(fh)
    now = _now()
    counts = {}
    for section in SECTIONS:
        entries = src.get(section) or {}
        conn.executemany(
            "INSERT OR IGNORE INTO part_names (section, key, data, updated_at) VALUES (?, ?, ?, ?)",
            [(section, normalise(k) if section != "groups" else k, json.dumps(v, ensure_ascii=False), now)
             for k, v in entries.items()],
        )
        counts[section] = len(entries)
    # corrections made the old way (exact entries flagged corrected) become real corrections
    old = [(normalise(k), v) for k, v in (src.get("exact") or {}).items() if v.get("corrected")]
    for pn, v in old:
        at = v.get("corrected_at") or now
        conn.execute(
            "INSERT OR IGNORE INTO part_corrections (part_number_norm, description, ai_description, price, "
            "first_corrected_at, corrected_at) VALUES (?, ?, NULL, ?, ?, ?)",
            (pn, v["description"], v.get("breakerpro_price"), at, at))
    counts["corrections"] = len(old)
    meta = {
        "seeded_at": now,
        "seed_source": os.path.basename(path),
        "seed_meta": json.dumps(src.get("_meta") or {}, ensure_ascii=False),
    }
    conn.executemany("INSERT OR REPLACE INTO part_names_meta (key, value) VALUES (?, ?)", meta.items())
    return {"seeded": True, "seeded_at": now, "counts": counts}


def meta(conn: sqlite3.Connection) -> dict:
    out = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM part_names_meta")}
    try:
        out["seed_meta"] = json.loads(out.get("seed_meta") or "{}")
    except ValueError:
        out["seed_meta"] = {}
    return out


def load_all(conn: sqlite3.Connection) -> dict:
    """Everything the lookup needs, in the old vag_parts_db.json shape plus
    a `corrections` section."""
    out = {s: {} for s in SECTIONS}
    for r in conn.execute("SELECT section, key, data FROM part_names"):
        out.setdefault(r["section"], {})[r["key"]] = json.loads(r["data"])
    out["corrections"] = {
        r["part_number_norm"]: dict(r) for r in conn.execute("SELECT * FROM part_corrections")
    }
    m = meta(conn)
    out["_meta"] = dict(m.get("seed_meta") or {}, seeded_at=m.get("seeded_at"))
    return out


def put_entry(conn: sqlite3.Connection, section: str, key: str, data: dict) -> None:
    if section not in SECTIONS:
        raise ValueError(f"unknown section {section!r}")
    conn.execute(
        "INSERT OR REPLACE INTO part_names (section, key, data, updated_at) VALUES (?, ?, ?, ?)",
        (section, key, json.dumps(data, ensure_ascii=False), _now()))


# ── Corrections ─────────────────────────────────────────────────────────

def save_correction(conn: sqlite3.Connection, part_number: str, description: str,
                    ai_description: str | None, price: float | None = None, at: str | None = None) -> dict | None:
    """Store the logger's name for this part number. The latest wins. The AI
    value from the FIRST correction is kept (later corrections don't overwrite
    it). Correcting back to the AI value removes the correction. Every call is
    also appended to part_correction_log. Returns the stored row, or None if
    the correction was removed."""
    pn = normalise(part_number)
    description = (description or "").strip()
    if not pn or not description:
        raise ValueError("part_number and description are required")
    at = at or _now()
    existing = conn.execute("SELECT * FROM part_corrections WHERE part_number_norm = ?", (pn,)).fetchone()
    ai = existing["ai_description"] if existing and existing["ai_description"] else ai_description
    conn.execute(
        "INSERT INTO part_correction_log (part_number_norm, description, ai_description, corrected_at) "
        "VALUES (?, ?, ?, ?)", (pn, description, ai, at))
    if ai is not None and description == ai.strip():
        conn.execute("DELETE FROM part_corrections WHERE part_number_norm = ?", (pn,))
        return None
    if existing:
        conn.execute(
            "UPDATE part_corrections SET description = ?, ai_description = ?, price = COALESCE(?, price), "
            "corrected_at = ?, times_corrected = times_corrected + 1 WHERE part_number_norm = ?",
            (description, ai, price, at, pn))
    else:
        conn.execute(
            "INSERT INTO part_corrections (part_number_norm, description, ai_description, price, "
            "first_corrected_at, corrected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (pn, description, ai, price, at, at))
    return dict(conn.execute("SELECT * FROM part_corrections WHERE part_number_norm = ?", (pn,)).fetchone())


# ── Export log ──────────────────────────────────────────────────────────

def record_export(conn: sqlite3.Connection, export_id: str, exported_at: str, rows: list[dict]) -> dict:
    """Store one CSV export. Safe to resend (the page retries until it gets
    a 2xx): rows already stored for this export_id are ignored."""
    now = _now()
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO export_log (export_id, row_index, exported_at, received_at, part_number, "
        "part_number_norm, ai_description, final_description, edited, correction_applied, vehicle_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(export_id, i, exported_at, now, r.get("part_number") or "", normalise(r.get("part_number")),
          r.get("ai_description"), r.get("final_description") or "", int(bool(r.get("edited"))),
          int(bool(r.get("correction_applied"))), r.get("vehicle_ref") or "")
         for i, r in enumerate(rows)],
    )
    inserted = conn.total_changes - before
    return {"export_id": export_id, "rows": len(rows), "inserted": inserted, "already_had": len(rows) - inserted}


# ── Report: edited name exists, but the export went out with the AI name ─

def edit_report(conn: sqlite3.Connection, sample_size: int = 20) -> dict:
    """Read-only. An exported row is AFFECTED when, by the time it was
    exported, a logger had already corrected that part number to something
    other than the AI name, yet the CSV still carried the AI name."""
    rows = conn.execute(
        """
        SELECT e.export_id, e.exported_at, e.part_number, e.part_number_norm, e.ai_description,
               e.final_description, e.edited, e.vehicle_ref,
               (SELECT l.description FROM part_correction_log l
                 WHERE l.part_number_norm = e.part_number_norm AND l.corrected_at <= e.exported_at
                 ORDER BY l.corrected_at DESC, l.id DESC LIMIT 1) AS correction
        FROM export_log e
        WHERE e.part_number_norm != ''
        ORDER BY e.exported_at DESC, e.id
        """
    ).fetchall()
    checked = len(rows)
    with_correction = [r for r in rows if r["correction"]]
    affected = [
        r for r in with_correction
        if r["ai_description"] is not None
        and r["correction"].strip() != (r["ai_description"] or "").strip()
        and r["final_description"].strip() == (r["ai_description"] or "").strip()
    ]
    counts = {
        "export_rows": conn.execute("SELECT COUNT(*) FROM export_log").fetchone()[0],
        "exports": conn.execute("SELECT COUNT(DISTINCT export_id) FROM export_log").fetchone()[0],
        "export_rows_with_part_number": checked,
        "export_rows_edited": sum(1 for r in rows if r["edited"]),
        "export_rows_with_earlier_correction": len(with_correction),
        "affected": len(affected),
        "corrections_stored": conn.execute("SELECT COUNT(*) FROM part_corrections").fetchone()[0],
        "corrections_received": conn.execute("SELECT COUNT(*) FROM part_correction_log").fetchone()[0],
    }
    sample = [
        {"exported_at": r["exported_at"], "part_number": r["part_number"], "vehicle_ref": r["vehicle_ref"],
         "exported_description": r["final_description"], "corrected_description": r["correction"]}
        for r in affected[:sample_size]
    ]
    return {
        "counts": counts,
        "sample": sample,
        "note": ("Only exports recorded since export logging started can be checked. Before that, logged "
                 "parts lived only in each phone's browser and were deleted on export, so older exports "
                 "can't be checked."),
    }


# ── Side guard for AI names ─────────────────────────────────────────────

def _phrase_re(phrases: list[str], plural: bool) -> re.Pattern:
    alts = sorted({p.strip().lower() for p in phrases if p.strip()}, key=len, reverse=True)
    body = "|".join(re.escape(p).replace(r"\ ", r"[\s\-]+") for p in alts)
    suffix = "(?:e?s)?" if plural else ""
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body}){suffix}(?![A-Za-z0-9])", re.IGNORECASE)


_ALWAYS_SIDED_RE = _phrase_re(cfg.ALWAYS_SIDED, plural=True)
_SIDED_RE = _phrase_re(cfg.SIDED_PART_TYPES, plural=True)
_NOT_SIDED_RE = _phrase_re(cfg.NOT_SIDED_WORDS, plural=True)
# "SEAT Leon" etc.: the car make, removed before looking for part words
_SEAT_MARQUE_RE = re.compile(
    r"(?<![A-Za-z0-9])seat\s+(?:" + "|".join(re.escape(m) for m in cfg.SEAT_MODELS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE)
_SIDE_RE = _phrase_re(cfg.SIDE_WORDS, plural=False)


def is_sided_part(description: str | None) -> bool:
    """True when the description names a part that has a left and a right
    (part_names_config.py: ALWAYS_SIDED, then NOT_SIDED_WORDS, then
    SIDED_PART_TYPES). "Seat" as the SEAT car make doesn't count."""
    text = _SEAT_MARQUE_RE.sub(" ", description or "")
    if _ALWAYS_SIDED_RE.search(text):
        return True
    return bool(_SIDED_RE.search(text)) and not _NOT_SIDED_RE.search(text)


def strip_side(description: str) -> str:
    s = _SIDE_RE.sub(" ", description)
    s = re.sub(r"\(\s*[-/,]*\s*\)", " ", s)        # "( )" left behind
    s = re.sub(r"\s*([-/,])\s*(?=[-/,]|$)", " ", s)  # dangling separators
    s = re.sub(r"\s+", " ", s).strip(" -/,")
    return s


def guard_ai_side(description: str | None) -> str | None:
    """The AI's name with any side removed, unless the part genuinely has a side."""
    if not description or is_sided_part(description):
        return description
    stripped = strip_side(description)
    return stripped or description
