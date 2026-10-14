"""Backtest: would the engine have priced our past sales better than the old method?

For every sale in the window we ask the engine for a suggestion "as of" the sale
date, so only sales strictly before that day are visible (the sale itself never
is). The car (model / generation / engine) comes from the sale's own title,
standing in for the vehicle ref the logger would have entered.

Old method = what the logger pre-filled before this build: `breakerpro_price`
from vag_parts_db.json (exact entry, else the 6-digit group average). Its other
leg, a live eBay search, can't be replayed historically, so sales where the old
method had no DB price are reported as "old method: no replayable price".

There's no historical competitor data either, so the engine runs WITHOUT its
market leg here. That understates the engine for parts with no own history.

Hybrid = the engine price where the engine gives one, otherwise the current
method's price. That is what the logger shows in live mode (the engine only
replaces the pre-fill when it has evidence).
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta

import pricing_config as cfg
from pricing import engine

WITHIN_PCT = 15.0


def old_method_price(pn: str) -> tuple[float | None, str]:
    """(price, 'exact' | 'group' | '') as the pre-pricing-engine logger did it."""
    from vag_lookup import lookup_part
    hit = lookup_part(pn) if pn else None
    if not hit or hit.get("breakerpro_price") in (None, 0):
        return None, ""
    return float(hit["breakerpro_price"]), ("exact" if hit.get("confidence") == "high" else "group")


def abs_pct_err(pred: float | None, actual: float) -> float | None:
    if pred is None or not actual:
        return None
    return abs(pred - actual) / actual * 100


def bias(rows: list[dict], key: str) -> float | None:
    """Median signed % error (negative = priced below what it sold for)."""
    vals = [(r[key] - r["actual"]) / r["actual"] * 100 for r in rows if r.get(key) is not None and r["actual"]]
    return round(statistics.median(vals), 1) if vals else None


def three_way(rows: list[dict]) -> dict:
    """Current method vs engine vs hybrid on the same rows."""
    out = {}
    for name, key, err in (("current", "old", "old_err"), ("engine", "new", "new_err"),
                           ("hybrid", "hybrid", "hybrid_err")):
        m = metrics([r[err] for r in rows])
        m["bias"] = bias(rows, key)
        out[name] = m
    out["sales"] = len(rows)
    out["hybrid_from_engine"] = sum(r["hybrid_from"] == "engine" for r in rows)
    out["hybrid_from_current"] = sum(r["hybrid_from"] == "current" for r in rows)
    return out


def metrics(errors: list[float]) -> dict:
    errs = [e for e in errors if e is not None]
    if not errs:
        return {"n": 0, "mdape": None, "within": None}
    return {
        "n": len(errs),
        "mdape": round(statistics.median(errs), 1),
        "within": round(100 * sum(e <= WITHIN_PCT for e in errs) / len(errs), 1),
    }


@contextmanager
def _flag(name: str, value):
    old = getattr(cfg, name)
    setattr(cfg, name, value)
    try:
        yield
    finally:
        setattr(cfg, name, old)


def run(conn: sqlite3.Connection, end: date, days: int = 365, breakerpro_exact: bool = False) -> list[dict]:
    """One result row per sale in (end - days, end]. breakerpro_exact=True runs
    the engine with the opt-in USE_BREAKERPRO_EXACT rung switched on."""
    with _flag("USE_BREAKERPRO_EXACT", breakerpro_exact):
        return _run(conn, end, days)


def _run(conn: sqlite3.Connection, end: date, days: int) -> list[dict]:
    start = (end - timedelta(days=days)).isoformat()
    sales = conn.execute(
        "SELECT * FROM sold_history WHERE sold_date > ? AND sold_date <= ? ORDER BY sold_date, id",
        (start, end.isoformat()),
    ).fetchall()
    out = []
    for s in sales:
        row = {
            "id": s["id"], "sold_date": s["sold_date"], "actual": s["price"], "item_price": s["item_price"],
            "shipping": s["shipping"], "part_number": s["part_number_norm"], "title_part_type": s["part_type"],
        }
        if not s["part_number_norm"]:
            row["skipped"] = "no part number"
            out.append(row)
            continue
        car = {"model": s["model"], "generation": s["generation"], "engine_group": s["engine_group"]}
        res = engine.suggest(conn, s["part_number_norm"], car=car, market=None,
                             as_of=date.fromisoformat(s["sold_date"]))
        old, old_kind = old_method_price(s["part_number_norm"])
        row.update({
            "new": res["suggested_price"], "new_conf": res["confidence"], "new_source": res["source"],
            "part_type": res["part_type"] or s["part_type"] or "(unknown)",
            "old": old, "old_kind": old_kind,
            "new_err": abs_pct_err(res["suggested_price"], s["price"]),
            "old_err": abs_pct_err(old, s["price"]),
            "old_err_item": abs_pct_err(old, s["item_price"]),
            "hybrid": res["suggested_price"] if res["suggested_price"] is not None else old,
            "hybrid_from": "engine" if res["suggested_price"] is not None else ("current" if old is not None else ""),
        })
        row["hybrid_err"] = abs_pct_err(row["hybrid"], s["price"])
        out.append(row)
    return out


def summarise(rows: list[dict]) -> dict:
    priced = [r for r in rows if "skipped" not in r]
    # The engine deliberately gives no price for variant-sensitive parts with no
    # exact match ("Check price"). Head-to-head = sales where BOTH gave a price;
    # the no-price sales are reported separately so they can't hide.
    no_price = [r for r in priced if r["new"] is None]
    both = [r for r in priced if r["old"] is not None and r["new"] is not None]
    s = {
        "sales_in_window": len(rows),
        "with_part_number": len(priced),
        "no_part_number": len(rows) - len(priced),
        "old_has_price": sum(r["old"] is not None for r in priced),
        "engine_no_price": {"count": len(no_price),
                            "old": metrics([r["old_err"] for r in no_price if r["old"] is not None])},
        "both_priced": len(both),
        "head_to_head": {"old": metrics([r["old_err"] for r in both]),
                         "new": metrics([r["new_err"] for r in both])},
        "new_all": metrics([r["new_err"] for r in priced]),
        "old_vs_item_price": metrics([r["old_err_item"] for r in both]),
        "new_wins": sum(r["new_err"] < r["old_err"] for r in both),
        "old_wins": sum(r["old_err"] < r["new_err"] for r in both),
        "ties": sum(r["old_err"] == r["new_err"] for r in both),
    }
    for key, field in (("by_confidence", "new_conf"), ("by_source", "new_source")):
        groups = defaultdict(list)
        for r in priced:
            groups[r[field]].append(r)
        s[key] = {
            k: {"new": metrics([r["new_err"] for r in g]),
                "old": metrics([r["old_err"] for r in g if r["old"] is not None])}
            for k, g in sorted(groups.items())
        }
    by_type = defaultdict(list)
    for r in priced:
        by_type[r["part_type"]].append(r)
    s["by_part_type"] = sorted(
        ({"part_type": k,
          "new": metrics([r["new_err"] for r in g]),
          "old": metrics([r["old_err"] for r in g if r["old"] is not None])}
         for k, g in by_type.items()),
        key=lambda t: -t["new"]["n"],
    )
    by_kind = defaultdict(list)
    for r in both:
        by_kind[r["old_kind"]].append(r)
    s["old_by_kind"] = {k: {"old": metrics([r["old_err"] for r in g]), "new": metrics([r["new_err"] for r in g])}
                        for k, g in sorted(by_kind.items())}
    own = [r for r in both if r["new_source"].startswith("own_sold")]
    s["head_to_head_own_history"] = {"old": metrics([r["old_err"] for r in own]),
                                     "new": metrics([r["new_err"] for r in own])}
    fb = [r for r in both if not r["new_source"].startswith("own_sold")]
    s["head_to_head_fallback"] = {"old": metrics([r["old_err"] for r in fb]),
                                  "new": metrics([r["new_err"] for r in fb])}
    s["median_signed_pct"] = {
        "new": round(statistics.median([(r["new"] - r["actual"]) / r["actual"] * 100 for r in both]), 1)
        if both else None,
        "old": round(statistics.median([(r["old"] - r["actual"]) / r["actual"] * 100 for r in both]), 1)
        if both else None,
    }
    periods = defaultdict(list)
    for r in both:
        periods[r["sold_date"][:7]].append(r)
    s["by_quarter"] = {}
    for month, g in sorted(periods.items()):
        y, mth = month.split("-")
        q = f"{y} Q{(int(mth) - 1) // 3 + 1}"
        s["by_quarter"].setdefault(q, []).extend(g)
    s["by_quarter"] = {
        q: {"old": metrics([r["old_err"] for r in g]), "new": metrics([r["new_err"] for r in g]),
            "old_exact_equals_item_pct": round(100 * sum(
                r["old_kind"] == "exact" and abs(r["old"] - r["item_price"]) < 0.01 for r in g) / len(g), 1)}
        for q, g in s["by_quarter"].items()
    }
    # Hybrid: compared on every sale the current method could price, so all
    # three lines are judged on the same sales (the engine line covers only the
    # ones it priced; its n says how many).
    base = [r for r in priced if r["old"] is not None]
    s["three_way"] = three_way(base)
    s["three_way_by_confidence"] = {
        level: three_way([r for r in base if r["new_conf"] == level]) for level in ("high", "medium", "low")
    }
    quarters: dict[str, list] = {}
    for r in base:
        y, mth = r["sold_date"][:7].split("-")
        quarters.setdefault(f"{y} Q{(int(mth) - 1) // 3 + 1}", []).append(r)
    s["three_way_by_quarter"] = {q: three_way(g) for q, g in sorted(quarters.items())}
    no_current = [r for r in priced if r["old"] is None]
    s["hybrid_where_current_has_no_price"] = {
        "sales": len(no_current),
        "hybrid_priced": sum(r["hybrid"] is not None for r in no_current),
        "hybrid": metrics([r["hybrid_err"] for r in no_current]),
    }
    free = [r for r in both if r["shipping"] == 0]
    s["head_to_head_free_postage"] = {"old": metrics([r["old_err"] for r in free]),
                                      "new": metrics([r["new_err"] for r in free])}
    return s
