"""Queries over sold_history: own comps by part number and the part-type index.

Every query takes `before` (ISO date, exclusive) so the backtest can ask "what did
we know before this sale?" without peeking at the sale itself.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from pricing.titles import engine_relevant


def _window(before: str | None, lookback_days: int | None) -> tuple[str, list]:
    clauses, params = [], []
    if before:
        clauses.append("sold_date < ?")
        params.append(before)
    if lookback_days:
        end = date.fromisoformat(before) if before else date.today() + timedelta(days=1)
        clauses.append("sold_date >= ?")
        params.append((end - timedelta(days=lookback_days)).isoformat())
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def own_sales(conn: sqlite3.Connection, *, part_number: str = "", base: str = "",
              before: str | None = None, lookback_days: int | None = None) -> list[tuple[float, str]]:
    """[(price, sold_date)] for an exact normalised number, or a base number."""
    if part_number:
        where, val = "part_number_norm = ?", part_number
    elif base:
        where, val = "base_number = ?", base
    else:
        return []
    extra, params = _window(before, lookback_days)
    rows = conn.execute(
        f"SELECT price, sold_date FROM sold_history WHERE {where}{extra} ORDER BY sold_date DESC",
        [val, *params],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def part_type_levels(part_type: str, model: str, generation: str, engine_group: str) -> list[tuple[str, dict]]:
    """Most specific grouping first. Engine group is only used where it matters."""
    if not part_type:
        return []
    levels = []
    if model and generation and engine_group and engine_relevant(part_type):
        levels.append(("model+gen+engine", {"model": model, "generation": generation, "engine_group": engine_group}))
    if model and engine_group and engine_relevant(part_type):
        levels.append(("model+engine", {"model": model, "engine_group": engine_group}))
    if model and generation:
        levels.append(("model+gen", {"model": model, "generation": generation}))
    if model:
        levels.append(("model", {"model": model}))
    return levels


def part_type_sales(conn: sqlite3.Connection, part_type: str, model: str = "", generation: str = "",
                    engine_group: str = "", *, min_sales: int = 1, before: str | None = None,
                    lookback_days: int | None = None) -> tuple[str, list[tuple[float, str]]]:
    """Own sales of the same part type on the same car, most specific level that
    has at least `min_sales`. Returns (level_label, [(price, date)]) or ("", [])."""
    extra, params = _window(before, lookback_days)
    for label, filt in part_type_levels(part_type, model, generation, engine_group):
        where = " AND ".join(f"{k} = ?" for k in filt)
        rows = conn.execute(
            f"SELECT price, sold_date FROM sold_history WHERE part_type = ? AND {where}{extra} "
            "ORDER BY sold_date DESC",
            [part_type, *filt.values(), *params],
        ).fetchall()
        if len(rows) >= min_sales:
            return label, [(r[0], r[1]) for r in rows]
    return "", []


def part_type_any_model(conn: sqlite3.Connection, part_type: str, *, before: str | None = None,
                        lookback_days: int | None = None) -> list[tuple[float, str]]:
    """Own sales of this part type on any car (category-default rung)."""
    if not part_type:
        return []
    extra, params = _window(before, lookback_days)
    rows = conn.execute(
        f"SELECT price, sold_date FROM sold_history WHERE part_type = ?{extra}",
        [part_type, *params],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def part_type_index(conn: sqlite3.Connection, min_sales: int = 1) -> list[dict]:
    """The part-type index: one row per (part type, model, generation, engine
    group) with count and median, biggest groups first."""
    groups: dict[tuple, list[float]] = {}
    for r in conn.execute(
        "SELECT part_type, model, generation, engine_group, price FROM sold_history WHERE part_type != ''"
    ):
        key = (r[0], r[1], r[2], r[3] if engine_relevant(r[0]) else "")
        groups.setdefault(key, []).append(r[4])
    out = [
        {"part_type": k[0], "model": k[1], "generation": k[2], "engine_group": k[3],
         "count": len(v), "median": round(statistics.median(v), 2)}
        for k, v in groups.items() if len(v) >= min_sales
    ]
    out.sort(key=lambda g: (-g["count"], g["part_type"]))
    return out
