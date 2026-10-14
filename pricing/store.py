"""SQLite tables for the pricing engine.

They live in the same SQLite file as `vehicles` (db.DB_PATH, the Railway volume).
`connect()` reads db.DB_PATH at call time, so tests can point it at a temp file.

Tables
------
sold_history       our own eBay sales (one row per order line). Contains NO
                   buyer data: only order/item identifiers, title, money, dates.
market_snapshots   cached Browse API competitor stats per part number (Phase 3)
price_suggestions  every suggestion the engine served + what the logger did (Phase 6)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import db as _db

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS sold_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        part_number_norm TEXT NOT NULL DEFAULT '',
        base_number TEXT NOT NULL DEFAULT '',
        price REAL NOT NULL,            -- total: item + P&P
        shipping REAL NOT NULL DEFAULT 0,
        item_price REAL,
        sold_date TEXT NOT NULL,        -- ISO yyyy-mm-dd
        channel TEXT NOT NULL DEFAULT 'ebay',
        source TEXT NOT NULL,           -- csv_clean | csv_raw | trading_api
        vehicle_ref TEXT,
        raw_title TEXT NOT NULL DEFAULT '',
        part_type TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        generation TEXT NOT NULL DEFAULT '',
        engine_group TEXT NOT NULL DEFAULT '',
        order_number TEXT NOT NULL DEFAULT '',
        item_number TEXT NOT NULL DEFAULT '',
        sales_record_number TEXT NOT NULL DEFAULT '',
        dispatched_date TEXT,
        imported_at TEXT NOT NULL,
        transaction_id TEXT NOT NULL DEFAULT '',   -- eBay transaction ID (Trading API / raw report)
        UNIQUE (order_number, item_number, sales_record_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_sold_pn ON sold_history (part_number_norm, sold_date)",
    "CREATE INDEX IF NOT EXISTS ix_sold_base ON sold_history (base_number, sold_date)",
    "CREATE INDEX IF NOT EXISTS ix_sold_type ON sold_history (part_type, model, generation, engine_group)",
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        part_number_norm TEXT NOT NULL,
        fetched_at TEXT NOT NULL,       -- ISO UTC timestamp
        count INTEGER NOT NULL,
        lq REAL, median REAL, min REAL, max REAL,
        query TEXT,
        raw_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_market_pn ON market_snapshots (part_number_norm, fetched_at)",
    """
    CREATE TABLE IF NOT EXISTS price_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        part_number_norm TEXT NOT NULL,
        vehicle_ref TEXT,
        part_type TEXT,
        suggested REAL,
        confidence TEXT,
        source TEXT,
        reason TEXT,
        final_price REAL,
        overridden INTEGER,             -- NULL until the logger's final price is known
        sold_history_id INTEGER,        -- linked once the item sells
        sold_price REAL,
        days_to_sell INTEGER,
        mode TEXT,                      -- shadow | live (PRICING_MODE when served)
        current_price REAL              -- current-method price the logger was shown first
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_sugg_pn ON price_suggestions (part_number_norm, timestamp)",
]


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


# Columns added after a table was first created: (table, column, type).
_MIGRATIONS = [
    ("sold_history", "transaction_id", "TEXT NOT NULL DEFAULT ''"),
    ("price_suggestions", "mode", "TEXT"),
    ("price_suggestions", "current_price", "REAL"),
]


def init_pricing_db(conn: sqlite3.Connection | None = None) -> None:
    """Create pricing tables and add any missing columns (idempotent)."""
    own = conn is None
    conn = conn or connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        for table, col, typ in _MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.commit()
    finally:
        if own:
            conn.close()
