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
5. When the suggestion is linked to a real eBay listing (pricing/listings.py,
   daily ActiveList snapshot), the "listed" price is the listing's FIRST eBay
   price, not the logger's exported price (prices are often changed in
   BreakerPro after logging), and the sale is that listing's own sale (same
   item number). The ebay_listings section adds re-pricing, sell-through,
   long-unsold and Best Offer figures.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import statistics
from datetime import date, datetime, timezone

import pricing_config as cfg
from pricing import listings

LINK_WINDOW_DAYS = 365
WITHIN_PCT = 15
SAME_PRICE_TOLERANCE = 0.005       # prices within half a penny are "the same"
ENGINE_BAND_PCT = 5                # listed within ±5% of the engine = "at engine price"
METHODS = (("current", "current_price"), ("engine", "suggested"), ("final", "final_price"))
ENDED_NOTE = ("'Ended without an eBay sale' = the listing left eBay with no eBay sale under its item number. "
              "Parts sold over the counter or to trade and then ended on eBay are counted here too.")


def log_suggestion(conn: sqlite3.Connection, result: dict, vehicle_ref: str | None, mode: str = "shadow",
                   duration_ms: int | None = None, market_status: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, vehicle_ref, part_type, suggested, "
        "confidence, source, reason, mode, duration_ms, market_status, engine_nwl) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), result["part_number"], vehicle_ref or None,
         result.get("part_type") or "", result["suggested_price"], result["confidence"], result["source"],
         result["reason"], mode, duration_ms, market_status, int(bool(result.get("not_worth_listing")))),
    )
    conn.commit()
    return cur.lastrowid


def record_warning(conn: sqlite3.Connection, suggestion_id: int, price_shown: float, basis: str = "") -> bool:
    """The logger was shown the 'not worth listing' warning for this card."""
    cur = conn.execute(
        "UPDATE price_suggestions SET nwl_warned = 1, nwl_price_shown = ?, nwl_basis = ? WHERE id = ?",
        (round(float(price_shown), 2), (basis or "")[:200], suggestion_id),
    )
    conn.commit()
    return cur.rowcount > 0


def not_worth_listing_report(rows, threshold: float) -> dict:
    """Warnings shown, how many were listed anyway (exported with a final price),
    and how those sold."""
    warned = [r for r in rows if r["nwl_warned"]]
    listed = [r for r in warned if r["final_price"] is not None]
    sold = [r for r in listed if r["sold_price"] is not None]
    return {
        "threshold": threshold,
        "warned": len(warned),
        "listed_anyway": len(listed),
        "not_listed": len(warned) - len(listed),
        "listed_anyway_pct": round(100 * len(listed) / len(warned), 1) if warned else None,
        "listed_anyway_sold": len(sold),
        "sold_median_price": _median(r["sold_price"] for r in sold),
        "sold_median_days": _median(r["days_to_sell"] for r in sold),
        "sold_at_or_above_threshold": sum(r["sold_price"] >= threshold for r in sold),
        "engine_flagged": sum(1 for r in rows if r["engine_nwl"]),
    }


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


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in 0-100)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    rank = max(1, -(-len(vals) * pct // 100))  # ceil(len * pct / 100)
    return vals[int(rank) - 1]


def response_times(rows) -> dict:
    ms = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
    statuses: dict[str, int] = {}
    for r in rows:
        if r["market_status"]:
            statuses[r["market_status"]] = statuses.get(r["market_status"], 0) + 1
    return {"n": len(ms), "median_ms": _median(ms), "p95_ms": percentile(ms, 95), "market_status": statuses}


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


def with_listing(r, hist: dict) -> dict:
    """The suggestion row, with the REAL eBay listed price and that listing's
    own sale when it's linked to a listing; the logger's export otherwise."""
    d = dict(r)
    d["logger_final_price"] = d["final_price"]
    it = hist.get(d.get("listing_item_id") or "")
    if it:
        d["final_price"] = it["first_price"]
        d["sold_price"] = it["sold_price"]
        d["days_to_sell"] = it["days_to_sell"]
        d["listed_price_source"] = "ebay"
    else:
        d["listed_price_source"] = "logger_export" if d["final_price"] is not None else None
    d["listing"] = it
    return d


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _round1(v):
    return None if v is None else round(v, 1)


def _vs_engine(price, engine) -> str | None:
    if price is None or not engine:
        return None
    gap = (price - engine) / engine * 100
    return "above_engine" if gap > ENGINE_BAND_PCT else "below_engine" if gap < -ENGINE_BAND_PCT else "at_engine"


def _price_moves(triples) -> dict:
    """triples: (old price, new price, engine price or None). How many moved,
    which way, by how much, and whether they moved toward the engine's price."""
    moved = [(o, n, e) for o, n, e in triples if o and n is not None and abs(n - o) > SAME_PRICE_TOLERANCE]
    with_engine = [(o, n, e) for o, n, e in moved if e]
    return {
        "compared": len(triples),
        "repriced": len(moved),
        "repriced_pct": _pct(len(moved), len(triples)),
        "up": sum(n > o for o, n, _ in moved),
        "down": sum(n < o for o, n, _ in moved),
        "median_change_pct": _round1(_median((n - o) / o * 100 for o, n, _ in moved)),
        "toward_engine": sum(abs(n - e) < abs(o - e) - SAME_PRICE_TOLERANCE for o, n, e in with_engine),
        "away_from_engine": sum(abs(n - e) > abs(o - e) + SAME_PRICE_TOLERANCE for o, n, e in with_engine),
        "no_engine_price": len(moved) - len(with_engine),
    }


def ebay_listings_report(rows: list[dict], hist: dict, today: date, snapshots: dict | None = None) -> dict:
    """Real eBay listings. `rows` are with_listing() suggestion rows; `hist` is
    listings.listing_history(); `snapshots` is listings.snapshot_runs()."""
    linked = [r for r in rows if r["listing"]]
    sold = [r for r in linked if r["listing"]["outcome"] == "sold"]
    outcomes = [it["outcome"] for it in hist.values()]

    # Sell-through: a listing only counts for the N-day window once it has been
    # up N days, so brand-new listings don't drag the rate down.
    sell_through = {}
    for group in ("all", "above_engine", "at_engine", "below_engine", "no_engine_price"):
        members = [r for r in linked if group == "all"
                   or (_vs_engine(r["listing"]["first_price"], r["suggested"]) or "no_engine_price") == group]
        block = {"listings": len(members)}
        for days in cfg.SELL_THROUGH_DAYS:
            eligible = [r for r in members if (today - date.fromisoformat(r["listing"]["start_date"])).days >= days]
            hit = sum(1 for r in eligible
                      if r["listing"]["days_to_sell"] is not None and r["listing"]["days_to_sell"] <= days)
            block[f"eligible_{days}d"] = len(eligible)
            block[f"sold_within_{days}d_pct"] = _pct(hit, len(eligible))
        sell_through[group] = block

    # Long unsold: still listed, or ended without a sale, after more than N days.
    long_days = cfg.UNSOLD_LONG_DAYS
    stale = [it for it in hist.values() if it["outcome"] != "sold" and it["days_listed"] > long_days]
    engine_by_item = {r["listing"]["item_id"]: r["suggested"] for r in linked}
    stale_vs = [(it["current_price"], engine_by_item.get(it["item_id"])) for it in stale]
    stale_vs = [(p, e) for p, e in stale_vs if e]

    # Best Offer: sold for less than the price that was on eBay that day.
    sold_all = [it for it in hist.values() if it["outcome"] == "sold" and it["listed_price_at_sale"]]
    under = [it for it in sold_all if it["sold_price"] < it["listed_price_at_sale"] - SAME_PRICE_TOLERANCE]

    return {
        "snapshots": snapshots or {"runs": 0, "last": None},
        "note": ENDED_NOTE,
        "tracked_listings": {
            "total": len(hist), "active": outcomes.count("active"), "sold": outcomes.count("sold"),
            "ended_without_ebay_sale": outcomes.count("ended_without_ebay_sale"),
            "with_price_change": sum(1 for it in hist.values() if it["price_changes"]),
        },
        "linked_suggestions": len(linked),
        # engine vs current method vs the REAL first eBay price, each judged against the sold price
        "vs_sold_price": {
            "n_sold": len(sold),
            "current": _accuracy(sold, "current_price"),
            "engine": _accuracy(sold, "suggested"),
            "first_listed": _accuracy(sold, "final_price"),
            "median_days_to_sell": _median(r["days_to_sell"] for r in sold),
        },
        "repriced_after_logging": {
            # the logger's exported price -> the first price that actually went on eBay
            "logger_to_first_listed": _price_moves(
                [(r["logger_final_price"], r["listing"]["first_price"], r["suggested"])
                 for r in linked if r["logger_final_price"] is not None]),
            # the first eBay price -> the latest one (or the one it sold at): changes made while listed
            "while_listed": _price_moves(
                [(r["listing"]["first_price"], r["listing"]["listed_price_at_sale"] or r["listing"]["current_price"],
                  r["suggested"]) for r in linked]),
        },
        "first_listed_vs_engine": {g: sum(1 for r in linked if _vs_engine(r["listing"]["first_price"],
                                                                          r["suggested"]) == g)
                                   for g in ("above_engine", "at_engine", "below_engine")},
        "sell_through": sell_through,
        f"unsold_over_{long_days}_days": {
            "count": len(stale),
            "still_listed": sum(it["outcome"] == "active" for it in stale),
            "ended_without_ebay_sale": sum(it["outcome"] == "ended_without_ebay_sale" for it in stale),
            "with_engine_price": len(stale_vs),
            "median_pct_vs_engine": _round1(_median((p - e) / e * 100 for p, e in stale_vs)),
            "above_engine": sum(_vs_engine(p, e) == "above_engine" for p, e in stale_vs),
            "at_engine": sum(_vs_engine(p, e) == "at_engine" for p, e in stale_vs),
            "below_engine": sum(_vs_engine(p, e) == "below_engine" for p, e in stale_vs),
        },
        "best_offer": {
            "sold": len(sold_all),
            "sold_below_listed": len(under),
            "sold_below_listed_pct": _pct(len(under), len(sold_all)),
            "median_discount_pct": _round1(_median(
                (it["listed_price_at_sale"] - it["sold_price"]) / it["listed_price_at_sale"] * 100 for it in under)),
            "best_offer_enabled_listings": sum(it["best_offer_enabled"] for it in hist.values()),
        },
    }


def report(conn: sqlite3.Connection, top: int = 15, items: int = 200, today: date | None = None) -> dict:
    link_sales(conn)
    listings.link_listings(conn)
    today = today or datetime.now(timezone.utc).date()
    hist = listings.listing_history(conn, today)
    rows = [with_listing(r, hist) for r in
            conn.execute("SELECT * FROM price_suggestions ORDER BY timestamp, id").fetchall()]
    finals = [r for r in rows if r["final_price"] is not None]
    sold = [r for r in rows if r["sold_price"] is not None]

    # Before the sale: how do the three prices relate?
    listing = {
        "with_final_price": len(finals),
        # where "final" comes from: the real eBay listing when linked, else the logger's export
        "listed_price_source": {src: sum(r["listed_price_source"] == src for r in finals)
                                for src in ("ebay", "logger_export")},
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
         "days_to_sell": r["days_to_sell"], "listed_price_source": r["listed_price_source"],
         "logger_final_price": r["logger_final_price"], "listing_item_id": r["listing_item_id"]}
        for r in sorted(sold, key=lambda r: r["timestamp"], reverse=True)[:items]
    ]
    return {
        "total_suggestions": len(rows),
        "by_mode": by_mode,
        "response_time": response_times(rows),
        "not_worth_listing": not_worth_listing_report(rows, cfg.NOT_WORTH_LISTING_BELOW),
        "listing": listing,
        "after_sale": after_sale,
        "days_to_sell": days,
        "by_confidence": by_conf,
        "most_overridden_part_types": overridden[:top],
        "sold_items": sold_items,
        "ebay_listings": ebay_listings_report(rows, hist, today, listings.snapshot_runs(conn)),
    }


CSV_COLUMNS = ["timestamp", "mode", "part_number", "vehicle_ref", "part_type", "confidence", "source",
               "current_price", "engine_price", "final_price", "overridden", "sold_price", "days_to_sell",
               "current_abs_pct_error", "engine_abs_pct_error", "final_abs_pct_error",
               "not_worth_listing_warned", "not_worth_listing_price_shown", "not_worth_listing_basis",
               # real eBay listing (blank until the suggestion is linked to one)
               "listed_price", "listed_price_source", "listed_abs_pct_error", "listing_item_id",
               "ebay_first_listed_price", "ebay_current_price", "ebay_price_changes", "listing_outcome",
               "listing_days_listed"]


def _blank(v):
    return "" if v is None else v


def report_csv(conn: sqlite3.Connection, today: date | None = None) -> str:
    """One row per suggestion. final_price is the logger's exported price;
    listed_price is the real first eBay price when linked (else the export).
    sold_price / days_to_sell are the linked listing's own sale when linked."""
    link_sales(conn)
    listings.link_listings(conn)
    hist = listings.listing_history(conn, today or datetime.now(timezone.utc).date())
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for raw in conn.execute("SELECT * FROM price_suggestions ORDER BY timestamp, id"):
        r = with_listing(raw, hist)
        it = r["listing"] or {}
        errs = [_err_pct(r[col], r["sold_price"]) for col in ("current_price", "suggested", "logger_final_price",
                                                              "final_price")]
        errs = ["" if e is None else round(e, 1) for e in errs]
        w.writerow([r["timestamp"], _blank(r["mode"]), r["part_number_norm"], r["vehicle_ref"] or "",
                    r["part_type"] or "", r["confidence"], r["source"],
                    _blank(r["current_price"]), _blank(r["suggested"]), _blank(r["logger_final_price"]),
                    "" if r["overridden"] is None else ("yes" if r["overridden"] else "no"),
                    _blank(r["sold_price"]), _blank(r["days_to_sell"]),
                    *errs[:3],
                    "yes" if r["nwl_warned"] else "", _blank(r["nwl_price_shown"]), r["nwl_basis"] or "",
                    _blank(r["final_price"]), _blank(r["listed_price_source"]), errs[3],
                    _blank(r["listing_item_id"]), _blank(it.get("first_price")), _blank(it.get("current_price")),
                    len(it["price_changes"]) if it else "", _blank(it.get("outcome")),
                    _blank(it.get("days_listed"))])
    return out.getvalue()
