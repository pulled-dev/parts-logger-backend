"""The pricing engine (PRICING-ENGINE-SPEC.md §5).

suggest() is a pure function of (sold_history, market stats, config, as_of
date), so the Phase 7 backtest can replay it "as of" any past sale.

Ladder
------
1. Own sales of this part number (exact; or the base number if the exact number
   is thin). Recency-weighted median over OWN_LOOKBACK_DAYS.
2. Blend with live competitors (market midpoint = average of lower quartile
   and median):
     own >= MIN_OWN_SALES      -> own median; nudged MARKET_NUDGE toward the
                                  market if the market median is more than
                                  MARKET_DIVERGENCE_PCT away.
     own 1-2 + market usable   -> 50/50 own median / market midpoint.
     own 1-2, no usable market -> own median (Medium). The spec doesn't cover
                                  this case; a real sale of this exact part beats
                                  any fallback.
     own 0 + market usable     -> market midpoint.
3. Fallbacks: own sales of the same part type on the same car (§4c), then the
   BreakerPro 6-digit group average, then the category default.
Then rounding and the per-category floor.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import date, timedelta

import pricing_config as cfg
from pricing import history
from pricing.normalise import base_number, middle_group, normalise
from pricing.titles import canonical_part_type, find_part_type

HIGH, MEDIUM, LOW = "high", "medium", "low"


# ── Maths helpers ───────────────────────────────────────────────────────

def recency_weight(sold_date: str, as_of: date) -> float:
    age = max(0, (as_of - date.fromisoformat(sold_date)).days)
    return 0.5 ** (age / cfg.OWN_RECENCY_HALF_LIFE_DAYS)


def weighted_median(values: list[tuple[float, float]]) -> float:
    """[(value, weight)] -> weighted median (lower median on an exact tie,
    averaged with the next value)."""
    pts = sorted(values)
    total = sum(w for _, w in pts)
    if total <= 0:
        return statistics.median(v for v, _ in pts)
    acc = 0.0
    for i, (v, w) in enumerate(pts):
        acc += w
        if math.isclose(acc, total / 2):
            return (v + pts[i + 1][0]) / 2 if i + 1 < len(pts) else v
        if acc > total / 2:
            return v
    return pts[-1][0]


def round_price(x: float) -> float:
    """ROUNDING_RULE: nearest £x.99 under the threshold, nearest £5 - 1p above."""
    if cfg.ROUNDING_RULE == "none":
        return round(x, 2)
    if x + 0.01 < cfg.ROUNDING_THRESHOLD:
        return round(max(1, math.floor(x + 0.51)) - 0.01, 2)
    return round(max(1, math.floor((x + 0.01) / 5 + 0.5)) * 5 - 0.01, 2)


def floor_for(part_type: str) -> float:
    return max(cfg.MIN_LIST_PRICE, cfg.MIN_LIST_PRICE_BY_TYPE.get(part_type, 0.0))


def _gbp(x: float) -> str:
    return f"£{x:,.0f}" if abs(x - round(x)) < 0.005 or x >= 100 else f"£{x:,.2f}"


def _span(dates: list[str], as_of: date) -> str:
    oldest = min(date.fromisoformat(d) for d in dates)
    months = max(1, round((as_of - oldest).days / 30.4))
    return f"last {months} month{'s' if months != 1 else ''}"


# ── Part type resolution ────────────────────────────────────────────────

def resolve_part_type(conn: sqlite3.Connection, pn: str, given: str | None) -> str:
    """Part type key for the §4c index.

    Our own titles are what the index is keyed on, so the part type we've
    actually sold under the same base number (then the same 6-digit group) wins.
    A description ("WINDOW MOTOR OS" from /lookup) would otherwise canonicalise
    to "WINDOW MOTOR" and miss the "WINDOW MOTOR FRONT" index rows. Only when
    we've never sold that group do we fall back to the caller's part type, then
    the BreakerPro/learned description."""
    if pn:
        for sql, arg in (
            ("base_number = ?", base_number(pn)),
            ("substr(part_number_norm, 4, 6) = ?", middle_group(pn)),
        ):
            if not arg:
                continue
            row = conn.execute(
                f"SELECT part_type, COUNT(*) n FROM sold_history WHERE {sql} AND part_type != '' "
                "GROUP BY part_type ORDER BY n DESC, part_type LIMIT 1",
                (arg,),
            ).fetchone()
            if row:
                return row[0]
    for text in (given, _db_description(pn)):
        if text:
            pt = find_part_type(text) or canonical_part_type(text)
            if pt:
                return pt
    return ""


def _db_description(pn: str) -> str:
    if not pn:
        return ""
    try:
        from vag_lookup import lookup_part
        hit = lookup_part(pn)
    except Exception:
        return ""
    return (hit or {}).get("description", "")


def _breakerpro_group_price(pn: str) -> float | None:
    grp = middle_group(pn)
    if not grp:
        return None
    try:
        from vag_lookup import get_db
        entry = get_db().get("groups", {}).get(grp)
    except Exception:
        return None
    price = (entry or {}).get("avg_price")
    return float(price) if price else None


# ── Main entry point ────────────────────────────────────────────────────

def suggest(conn: sqlite3.Connection, part_number: str, *, part_type: str | None = None,
            car: dict | None = None, market: dict | None = None, as_of: date | None = None) -> dict:
    """Suggest a total (item + P&P) listing price.

    car:    {"model", "generation", "engine_group"} for the part-type fallback.
    market: get_market() stats or None.
    as_of:  only sales strictly before this date are used (default: tomorrow,
            i.e. everything up to and including today)."""
    as_of = as_of or (date.today() + timedelta(days=1))
    before = as_of.isoformat()
    look = cfg.OWN_LOOKBACK_DAYS
    pn = normalise(part_number)
    base = base_number(pn) if pn else ""
    car = car or {}
    ptype = resolve_part_type(conn, pn, part_type)

    # 1. Own comps by part number.
    exact = history.own_sales(conn, part_number=pn, before=before, lookback_days=look) if pn else []
    by_base = history.own_sales(conn, base=base, before=before, lookback_days=look) if base else []
    if len(exact) >= cfg.MIN_OWN_SALES or (exact and len(by_base) < cfg.MIN_OWN_SALES):
        own, match = exact, "exact"
    elif by_base:
        own, match = by_base, ("exact" if len(by_base) == len(exact) else "base")
    else:
        own, match = [], "none"

    own_block = None
    own_med = None
    if own:
        own_med = weighted_median([(p, recency_weight(d, as_of)) for p, d in own])
        own_block = {"count": len(own), "median": round(own_med, 2), "last_sold": max(d for _, d in own)}

    # 2. Market.
    mkt_ok = bool(market and (market.get("count") or 0) >= cfg.MIN_MARKET_COMPS and market.get("median"))
    mkt_block = None
    mkt_mid = None
    mkt_tight = False
    if market and market.get("count"):
        mkt_block = {"count": market["count"], "lower_quartile": market.get("lq"), "median": market.get("median")}
    if mkt_ok:
        lq = market.get("lq") or market["median"]
        mkt_mid = (lq + market["median"]) / 2
        mkt_tight = (market["median"] - lq) / market["median"] <= cfg.MARKET_TIGHT_SPREAD
    mkt_high = mkt_ok and market["count"] >= cfg.MARKET_HIGH_CONF_COMPS and mkt_tight

    reasons: list[str] = []
    own_txt = ""
    if own:
        what = "of our sales" if match == "exact" else f"of our sales of {base}* (index letter differs)"
        own_txt = f"{len(own)} {what} (median {_gbp(own_med)}, {_span([d for _, d in own], as_of)})"
    mkt_txt = (f"{market['count']} live competitors ({_gbp(market['min'])}–{_gbp(market['max'])})"
               if mkt_ok and market.get("min") is not None else "")

    price = None
    if own and len(own) >= cfg.MIN_OWN_SALES:
        price, source = own_med, "own_sold"
        conf = HIGH if match == "exact" else MEDIUM
        reasons.append(own_txt)
        if mkt_ok:
            reasons.append(mkt_txt)
            gap = (market["median"] - own_med) / own_med
            if abs(gap) * 100 > cfg.MARKET_DIVERGENCE_PCT:
                price = own_med + cfg.MARKET_NUDGE * (mkt_mid - own_med)
                source = "own_sold+market"
                reasons.append(f"market {abs(gap) * 100:.0f}% {'higher' if gap > 0 else 'lower'}, "
                               f"nudged {cfg.MARKET_NUDGE * 100:.0f}% toward it")
            if mkt_high:
                conf = HIGH
    elif own:
        reasons.append(own_txt)
        if mkt_ok:
            price, source = (own_med + mkt_mid) / 2, "own_sold+market"
            reasons.append(mkt_txt)
            reasons.append("50/50 blend of our sales and the market")
            conf = HIGH if mkt_high else MEDIUM
        else:
            price, source, conf = own_med, "own_sold", MEDIUM
    elif mkt_ok:
        price, source = mkt_mid, "market"
        conf = HIGH if mkt_high else MEDIUM
        reasons.append(mkt_txt + ", priced at the lower half")
        match = "market"

    # 3. Fallbacks.
    if price is None:
        price, source, conf, match, fb_reason = _fallback(conn, pn, ptype, car, before, look)
        reasons.append(fb_reason)
        if mkt_block:
            reasons.append(f"only {market['count']} live competitor{'s' if market['count'] != 1 else ''}")

    raw = price
    suggested = round_price(raw)
    floor = floor_for(ptype)
    if suggested < floor:
        suggested = floor
        reasons.append(f"raised to the {_gbp(floor)} minimum")
    if conf == LOW:
        reasons.append("check price")

    return {
        "suggested_price": suggested,
        "confidence": conf,
        "reason": " · ".join(r for r in reasons if r),
        "own": own_block,
        "market": mkt_block,
        "match": match,
        "source": source,
        "part_number": pn,
        "part_type": ptype,
        "check_price": conf == LOW,
        "raw_price": round(raw, 2),
    }


def _fallback(conn, pn, ptype, car, before, look):
    model = (car.get("model") or "").upper()
    gen = (car.get("generation") or "").upper()
    eng = (car.get("engine_group") or "").upper()
    nothing = "no sales or live listings for this number"

    if ptype and model:
        level, rows = history.part_type_sales(
            conn, ptype, model, gen, eng, min_sales=cfg.PART_TYPE_MIN_SALES, before=before, lookback_days=look,
        )
        if rows:
            med = statistics.median(p for p, _ in rows)
            car_label = " ".join(x for x in (model.title(), gen, eng if "engine" in level else "") if x)
            conf = MEDIUM if len(rows) >= cfg.PART_TYPE_MEDIUM_SALES else LOW
            return (med, "part_type", conf, "part_type",
                    f"{nothing} · {len(rows)} of our {car_label} {ptype.lower()} sales (median {_gbp(med)})")

    bp = _breakerpro_group_price(pn)
    if bp:
        return (bp, "breakerpro_group", LOW, "group",
                f"{nothing} · BreakerPro average for this part group, all models ({_gbp(bp)})")

    if ptype and ptype in cfg.CATEGORY_DEFAULTS:
        v = cfg.CATEGORY_DEFAULTS[ptype]
        return v, "category_default", LOW, "none", f"{nothing} · {ptype.lower()} default ({_gbp(v)})"
    if ptype:
        rows = history.part_type_any_model(conn, ptype, before=before, lookback_days=look)
        if len(rows) >= cfg.PART_TYPE_MIN_SALES:
            med = statistics.median(p for p, _ in rows)
            return (med, "category_default", LOW, "none",
                    f"{nothing} · {len(rows)} of our {ptype.lower()} sales, any model (median {_gbp(med)})")
    return (cfg.DEFAULT_PRICE, "category_default", LOW, "none",
            f"{nothing} · general default ({_gbp(cfg.DEFAULT_PRICE)})")
