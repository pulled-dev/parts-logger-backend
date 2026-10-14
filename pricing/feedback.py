"""Suggestion log + feedback loop.

1. Every /price/suggest response is logged in price_suggestions, with the
   PRICING_MODE it was served in (shadow / live).
2. When the logger exports the CSV, the frontend posts each card's final price
   and the current-method price it started from (the old pre-fill).
   `overridden` = the final price differs from the price the logger was SHOWN:
   the engine price in live mode, the current-method price in shadow mode (or
   in live mode when the engine gave no price).
3. When the item later appears in sold_history (same part number, sold on or
   after the suggestion), it's linked one-to-one: sold price and days to sell.
4. report() / report_csv() compare current method vs engine vs final listed
   price, before and after the sale.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import statistics
from datetime import date, datetime, timezone

LINK_WINDOW_DAYS = 365
WITHIN_PCT = 15
SAME_PRICE_TOLERANCE = 0.005       # prices within half a penny are "the same"
ENGINE_BAND_PCT = 5                # listed within ±5% of the engine = "at engine price"
METHODS = (("current", "current_price"), ("engine", "suggested"), ("final", "final_price"))


def log_suggestion(conn: sqlite3.Connection, result: dict, vehicle_ref: str | None, mode: str = "shadow") -> int:
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, vehicle_ref, part_type, suggested, "
        "confidence, source, reason, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), result["part_number"], vehicle_ref or None,
         result.get("part_type") or "", result["suggested_price"], result["confidence"], result["source"],
         result["reason"], mode),
    )
    conn.commit()
    return cur.lastrowid


def shown_price(row) -> float | None:
    """The price the logger actually saw pre-filled on the card."""
    if row["mode"] == "live" and row["suggested"] is not None:
        return row["suggested"]
    return row["current_price"]


def record_finals(conn: sqlite3.Connection, items: list[dict]) -> dict:
    updated, unknown = 0, 0
    for it in items:
        sid, final = it.get("suggestion_id"), it.get("final_price")
        if sid is None or final is None:
            continue
        row = conn.execute("SELECT * FROM price_suggestions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            unknown += 1
            continue
        final = round(float(final), 2)
        current = it.get("current_price")
        current = round(float(current), 2) if current is not None else row["current_price"]
        shown = shown_price({"mode": row["mode"], "suggested": row["suggested"], "current_price": current})
        overridden = None if shown is None else int(abs(final - shown) > SAME_PRICE_TOLERANCE)
        conn.execute(
            "UPDATE price_suggestions SET final_price = ?, current_price = ?, overridden = ? WHERE id = ?",
            (final, current, overridden, sid),
        )
        updated += 1
    conn.commit()
    return {"updated": updated, "unknown": unknown}


def link_sales(conn: sqlite3.Connection) -> int:
    """Link unlinked suggestions to the first later sale of the same part number
    that isn't already linked. Suggestions are processed oldest first."""
    used = {r[0] for r in conn.execute(
        "SELECT sold_history_id FROM price_suggestions WHERE sold_history_id IS NOT NULL")}
    linked = 0
    pending = conn.execute(
        "SELECT id, timestamp, part_number_norm FROM price_suggestions "
        "WHERE sold_history_id IS NULL AND part_number_norm != '' ORDER BY timestamp, id"
    ).fetchall()
    for sid, ts, pn in pending:
        day = ts[:10]
        for hid, sold_date, price in conn.execute(
            "SELECT id, sold_date, price FROM sold_history WHERE part_number_norm = ? AND sold_date >= ? "
            "ORDER BY sold_date, id", (pn, day),
        ):
            days = (date.fromisoformat(sold_date) - date.fromisoformat(day)).days
            if days > LINK_WINDOW_DAYS:
                break
            if hid in used:
                continue
            conn.execute(
                "UPDATE price_suggestions SET sold_history_id = ?, sold_price = ?, days_to_sell = ? WHERE id = ?",
                (hid, price, days, sid),
            )
            used.add(hid)
            linked += 1
            break
    conn.commit()
    return linked


def _err_pct(predicted, actual) -> float | None:
    if predicted is None or not actual:
        return None
    return abs(predicted - actual) / actual * 100


def _accuracy(rows, col: str, target: str = "sold_price") -> dict:
    pairs = [(r[col], r[target]) for r in rows if r[col] is not None and r[target]]
    if not pairs:
        return {"n": 0, "median_abs_pct_error": None, "pct_within_15": None, "median_bias_pct": None}
    errs = [abs(p - t) / t * 100 for p, t in pairs]
    return {
        "n": len(pairs),
        "median_abs_pct_error": round(statistics.median(errs), 1),
        "pct_within_15": round(100 * sum(e <= WITHIN_PCT for e in errs) / len(errs), 1),
        "median_bias_pct": round(statistics.median((p - t) / t * 100 for p, t in pairs), 1),
    }


def _median(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _same(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) <= SAME_PRICE_TOLERANCE


def _days_block(rows) -> dict:
    days = [r["days_to_sell"] for r in rows if r["days_to_sell"] is not None]
    return {"sold": len(days), "median_days_to_sell": _median(days)}


def _listed_vs_engine(r) -> str | None:
    if r["final_price"] is None or r["suggested"] is None:
        return None
    gap = (r["final_price"] - r["suggested"]) / r["suggested"] * 100
    if gap > ENGINE_BAND_PCT:
        return "listed_above_engine"
    if gap < -ENGINE_BAND_PCT:
        return "listed_below_engine"
    return "listed_at_engine"


def report(conn: sqlite3.Connection, top: int = 15, items: int = 200) -> dict:
    link_sales(conn)
    rows = conn.execute("SELECT * FROM price_suggestions ORDER BY timestamp, id").fetchall()
    finals = [r for r in rows if r["final_price"] is not None]
    sold = [r for r in rows if r["sold_price"] is not None]

    # Before the sale: how do the three prices relate?
    listing = {
        "with_final_price": len(finals),
        "final_equals_current_pct": round(100 * sum(_same(r["final_price"], r["current_price"]) for r in finals)
                                          / len(finals), 1) if finals else None,
        "final_equals_engine_pct": round(100 * sum(_same(r["final_price"], r["suggested"]) for r in finals)
                                         / len(finals), 1) if finals else None,
        "engine_no_price": sum(r["suggested"] is None for r in rows),
        # how far each method was from the price the logger finally listed at
        "current_vs_final": _accuracy(finals, "current_price", "final_price"),
        "engine_vs_final": _accuracy(finals, "suggested", "final_price"),
    }

    # After the sale: which price was closest to what it actually sold for?
    after_sale = {name: _accuracy(sold, col) for name, col in METHODS}

    days = {"all": _days_block(sold)}
    for bucket in ("listed_below_engine", "listed_at_engine", "listed_above_engine"):
        days[bucket] = _days_block([r for r in sold if _listed_vs_engine(r) == bucket])
    days["final_equals_current"] = _days_block([r for r in sold if _same(r["final_price"], r["current_price"])])
    days["final_differs_from_current"] = _days_block(
        [r for r in sold if r["final_price"] is not None and r["current_price"] is not None
         and not _same(r["final_price"], r["current_price"])])

    by_conf = {}
    for level in ("high", "medium", "low"):
        lvl = [r for r in rows if r["confidence"] == level]
        lvl_sold = [r for r in lvl if r["sold_price"] is not None]
        with_override = [r for r in lvl if r["overridden"] is not None]
        by_conf[level] = {
            "suggestions": len(lvl),
            "with_final_price": sum(r["final_price"] is not None for r in lvl),
            "override_rate_pct": round(100 * sum(r["overridden"] for r in with_override) / len(with_override), 1)
            if with_override else None,
            "sold": len(lvl_sold),
            "after_sale": {name: _accuracy(lvl_sold, col) for name, col in METHODS},
            "median_days_to_sell": _median(r["days_to_sell"] for r in lvl_sold),
        }

    by_mode = {m: sum(r["mode"] == m for r in rows) for m in ("shadow", "live")}

    types: dict[str, list] = {}
    for r in rows:
        if r["overridden"] is None:
            continue
        types.setdefault(r["part_type"] or "(unknown)", []).append(r)
    overridden = []
    for pt, rs in types.items():
        n_over = sum(r["overridden"] for r in rs)
        deltas = []
        for r in rs:
            shown = shown_price(r)
            if r["overridden"] and shown:
                deltas.append((r["final_price"] - shown) / shown * 100)
        overridden.append({
            "part_type": pt, "with_final_price": len(rs), "overridden": n_over,
            "override_rate_pct": round(100 * n_over / len(rs), 1),
            "median_change_pct": round(statistics.median(deltas), 1) if deltas else None,
        })
    overridden.sort(key=lambda t: (-t["overridden"], -t["override_rate_pct"], t["part_type"]))

    sold_items = [
        {"timestamp": r["timestamp"], "part_number": r["part_number_norm"], "part_type": r["part_type"],
         "mode": r["mode"], "confidence": r["confidence"], "current_price": r["current_price"],
         "engine_price": r["suggested"], "final_price": r["final_price"], "sold_price": r["sold_price"],
         "days_to_sell": r["days_to_sell"]}
        for r in sorted(sold, key=lambda r: r["timestamp"], reverse=True)[:items]
    ]
    return {
        "total_suggestions": len(rows),
        "by_mode": by_mode,
        "listing": listing,
        "after_sale": after_sale,
        "days_to_sell": days,
        "by_confidence": by_conf,
        "most_overridden_part_types": overridden[:top],
        "sold_items": sold_items,
    }


CSV_COLUMNS = ["timestamp", "mode", "part_number", "vehicle_ref", "part_type", "confidence", "source",
               "current_price", "engine_price", "final_price", "overridden", "sold_price", "days_to_sell",
               "current_abs_pct_error", "engine_abs_pct_error", "final_abs_pct_error"]


def _blank(v):
    return "" if v is None else v


def report_csv(conn: sqlite3.Connection) -> str:
    link_sales(conn)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in conn.execute("SELECT * FROM price_suggestions ORDER BY timestamp, id"):
        errs = [_err_pct(r[col], r["sold_price"]) for _, col in METHODS]
        w.writerow([r["timestamp"], _blank(r["mode"]), r["part_number_norm"], r["vehicle_ref"] or "",
                    r["part_type"] or "", r["confidence"], r["source"],
                    _blank(r["current_price"]), _blank(r["suggested"]), _blank(r["final_price"]),
                    "" if r["overridden"] is None else ("yes" if r["overridden"] else "no"),
                    _blank(r["sold_price"]), _blank(r["days_to_sell"]),
                    *["" if e is None else round(e, 1) for e in errs]])
    return out.getvalue()
