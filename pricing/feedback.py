"""Suggestion log + feedback loop (Phase 6).

1. Every /price/suggest response is logged in price_suggestions.
2. When the logger exports the CSV, the frontend posts each card's final price;
   `overridden` = the logger changed the suggested price.
3. When the item later appears in sold_history (same part number, sold on or
   after the suggestion), it's linked one-to-one: we then know suggested,
   listed (final), sold price and days to sell.
4. report() / report_csv() summarise accuracy by confidence and the most
   overridden part types.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import statistics
from datetime import date, datetime, timezone

LINK_WINDOW_DAYS = 365
WITHIN_PCT = 15


def log_suggestion(conn: sqlite3.Connection, result: dict, vehicle_ref: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, vehicle_ref, part_type, suggested, "
        "confidence, source, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), result["part_number"], vehicle_ref or None,
         result.get("part_type") or "", result["suggested_price"], result["confidence"], result["source"],
         result["reason"]),
    )
    conn.commit()
    return cur.lastrowid


def record_finals(conn: sqlite3.Connection, items: list[dict]) -> dict:
    updated, unknown = 0, 0
    for it in items:
        sid, final = it.get("suggestion_id"), it.get("final_price")
        if sid is None or final is None:
            continue
        row = conn.execute("SELECT suggested FROM price_suggestions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            unknown += 1
            continue
        final = round(float(final), 2)
        overridden = int(row[0] is None or abs(final - row[0]) > 0.005)
        conn.execute("UPDATE price_suggestions SET final_price = ?, overridden = ? WHERE id = ?",
                     (final, overridden, sid))
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


def _accuracy(rows: list[sqlite3.Row], col: str) -> dict:
    errs = [e for e in (_err_pct(r[col], r["sold_price"]) for r in rows) if e is not None]
    if not errs:
        return {"n": 0, "median_abs_pct_error": None, "pct_within_15": None}
    return {
        "n": len(errs),
        "median_abs_pct_error": round(statistics.median(errs), 1),
        "pct_within_15": round(100 * sum(e <= WITHIN_PCT for e in errs) / len(errs), 1),
    }


def report(conn: sqlite3.Connection, top: int = 15) -> dict:
    link_sales(conn)
    rows = conn.execute("SELECT * FROM price_suggestions").fetchall()
    by_conf = {}
    for level in ("high", "medium", "low"):
        lvl = [r for r in rows if r["confidence"] == level]
        sold = [r for r in lvl if r["sold_price"] is not None]
        finals = [r for r in lvl if r["overridden"] is not None]
        days = [r["days_to_sell"] for r in sold if r["days_to_sell"] is not None]
        by_conf[level] = {
            "suggestions": len(lvl),
            "with_final_price": len(finals),
            "override_rate_pct": round(100 * sum(r["overridden"] for r in finals) / len(finals), 1) if finals else None,
            "sold": len(sold),
            "suggested_vs_sold": _accuracy(sold, "suggested"),
            "listed_vs_sold": _accuracy([r for r in sold if r["final_price"] is not None], "final_price"),
            "median_days_to_sell": statistics.median(days) if days else None,
        }

    types: dict[str, list] = {}
    for r in rows:
        if r["overridden"] is None:
            continue
        types.setdefault(r["part_type"] or "(unknown)", []).append(r)
    overridden = []
    for pt, rs in types.items():
        n_over = sum(r["overridden"] for r in rs)
        deltas = [(r["final_price"] - r["suggested"]) / r["suggested"] * 100
                  for r in rs if r["overridden"] and r["suggested"]]
        overridden.append({
            "part_type": pt, "with_final_price": len(rs), "overridden": n_over,
            "override_rate_pct": round(100 * n_over / len(rs), 1),
            "median_change_pct": round(statistics.median(deltas), 1) if deltas else None,
        })
    overridden.sort(key=lambda t: (-t["overridden"], -t["override_rate_pct"], t["part_type"]))
    return {
        "total_suggestions": len(rows),
        "by_confidence": by_conf,
        "most_overridden_part_types": overridden[:top],
    }


CSV_COLUMNS = ["timestamp", "part_number", "vehicle_ref", "part_type", "suggested", "confidence", "source",
               "final_price", "overridden", "sold_price", "days_to_sell", "suggested_abs_pct_error"]


def report_csv(conn: sqlite3.Connection) -> str:
    link_sales(conn)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in conn.execute("SELECT * FROM price_suggestions ORDER BY timestamp, id"):
        err = _err_pct(r["suggested"], r["sold_price"])
        w.writerow([r["timestamp"], r["part_number_norm"], r["vehicle_ref"] or "", r["part_type"] or "",
                    r["suggested"], r["confidence"], r["source"],
                    "" if r["final_price"] is None else r["final_price"],
                    "" if r["overridden"] is None else ("yes" if r["overridden"] else "no"),
                    "" if r["sold_price"] is None else r["sold_price"],
                    "" if r["days_to_sell"] is None else r["days_to_sell"],
                    "" if err is None else round(err, 1)])
    return out.getvalue()
