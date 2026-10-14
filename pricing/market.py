"""Competitor market comps from the eBay Browse API (official API, no scraping).

Search the normalised part number on EBAY_GB (falling back to the spaced
breaker format, e.g. "6R2 880 201 B", if the first search finds nothing) with:
used condition, item located in GB, Buy It Now / Best Offer only, Vehicle Parts
& Accessories, and our own sellers excluded. Each listing is priced as
item + cheapest shipping. Outliers are dropped with the IQR rule and we keep
count / lower quartile / median / min / max.

Snapshots are cached in market_snapshots for MARKET_CACHE_DAYS.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

import httpx

import pricing_config as cfg
from pricing import store
from pricing.normalise import base_number, extract_part_numbers, normalise, spaced

log = logging.getLogger(__name__)

# eBay app-token and Browse calls: connect 3s / read 5s.
TIMEOUT = httpx.Timeout(5.0, connect=3.0)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

_token_cache = {"token": None, "expires": 0.0}


def is_configured() -> bool:
    return bool(os.environ.get("EBAY_APP_ID") and os.environ.get("EBAY_CERT_ID"))


async def get_app_token(client: httpx.AsyncClient) -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires"]:
        return _token_cache["token"]
    creds = base64.b64encode(
        f"{os.environ.get('EBAY_APP_ID', '')}:{os.environ.get('EBAY_CERT_ID', '')}".encode()
    ).decode()
    resp = await client.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"eBay app token failed: HTTP {resp.status_code}")
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 7200)) - 60
    return data["access_token"]


def search_filter() -> str:
    parts = [
        "conditionIds:{" + "|".join(cfg.EBAY_USED_CONDITION_IDS) + "}",
        "itemLocationCountry:GB",
        "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
    ]
    if cfg.EXCLUDED_SELLERS:
        parts.append("excludeSellers:{" + "|".join(cfg.EXCLUDED_SELLERS) + "}")
    return ",".join(parts)


async def search(client: httpx.AsyncClient, token: str, query: str) -> list[dict]:
    resp = await client.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": cfg.EBAY_MARKETPLACE,
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DGB",
        },
        params={"q": query, "category_ids": cfg.EBAY_CATEGORY_ID, "filter": search_filter(),
                "limit": str(cfg.EBAY_SEARCH_LIMIT)},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Browse search failed: HTTP {resp.status_code}")
    return resp.json().get("itemSummaries", []) or []


def _money(obj) -> float | None:
    try:
        if (obj or {}).get("currency", "GBP") != "GBP":
            return None
        return float(obj["value"])
    except (KeyError, TypeError, ValueError):
        return None


def listing_total(item: dict) -> float | None:
    """Item price + cheapest shipping option (missing shipping counts as free)."""
    price = _money(item.get("price"))
    if price is None:
        return None
    costs = [c for c in (_money(o.get("shippingCost")) for o in item.get("shippingOptions") or []) if c is not None]
    return round(price + (min(costs) if costs else 0.0), 2)


def _is_excluded_seller(item: dict) -> bool:
    user = ((item.get("seller") or {}).get("username") or "").lower()
    return user in {s.lower() for s in cfg.EXCLUDED_SELLERS}


def _is_used(item: dict) -> bool:
    cid = str(item.get("conditionId") or "")
    if cid:
        return cid in cfg.EBAY_USED_CONDITION_IDS
    return (item.get("condition") or "").strip().lower() == "used"


def _title_has_part(item: dict, pn: str) -> bool:
    base = base_number(pn)
    found = extract_part_numbers(item.get("title", ""))
    return any(base_number(f) == base for f in found) or base in (item.get("title", "").upper().replace(" ", ""))


def filter_listings(items: list[dict], pn: str) -> list[dict]:
    """Apply the §4b rules client-side as well (belt and braces: the API filter
    can't be relied on for seller exclusion or condition)."""
    out = []
    for it in items:
        if _is_excluded_seller(it) or not _is_used(it):
            continue
        loc = ((it.get("itemLocation") or {}).get("country") or "GB").upper()
        if loc != "GB":
            continue
        opts = it.get("buyingOptions") or ["FIXED_PRICE"]
        if opts == ["AUCTION"]:
            continue
        if cfg.MARKET_REQUIRE_PN_IN_TITLE and not _title_has_part(it, pn):
            continue
        total = listing_total(it)
        if total is None or total <= 0:
            continue
        out.append({"item_id": it.get("itemId"), "title": it.get("title", ""), "total": total,
                    "seller": (it.get("seller") or {}).get("username")})
    return out


def iqr_filter(prices: list[float]) -> list[float]:
    """Drop outliers outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR]. Needs 4+ points."""
    if len(prices) < 4:
        return sorted(prices)
    q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return sorted(p for p in prices if lo <= p <= hi)


def summarise(prices: list[float]) -> dict:
    kept = iqr_filter(prices)
    if not kept:
        return {"count": 0, "lq": None, "median": None, "min": None, "max": None, "dropped": len(prices)}
    lq = statistics.quantiles(kept, n=4, method="inclusive")[0] if len(kept) >= 2 else kept[0]
    return {
        "count": len(kept),
        "lq": round(lq, 2),
        "median": round(statistics.median(kept), 2),
        "min": kept[0],
        "max": kept[-1],
        "dropped": len(prices) - len(kept),
    }


def cached_snapshot(conn: sqlite3.Connection, pn: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=cfg.MARKET_CACHE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT * FROM market_snapshots WHERE part_number_norm = ? AND fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (pn, cutoff),
    ).fetchone()
    if row is None:
        return None
    return {"count": row["count"], "lq": row["lq"], "median": row["median"], "min": row["min"],
            "max": row["max"], "fetched_at": row["fetched_at"], "cached": True}


async def get_market(conn: sqlite3.Connection, part_number: str, client: httpx.AsyncClient | None = None,
                     now: datetime | None = None, force: bool = False) -> dict | None:
    """Market stats for a part number, from cache or the Browse API.

    Returns None when the API isn't configured or the call fails (the engine
    then carries on without market data). Zero results is a real answer and is
    cached too, so we don't hammer the API for parts nobody else lists."""
    pn = normalise(part_number)
    if not pn:
        return None
    now = now or datetime.now(timezone.utc)
    if not force:
        hit = cached_snapshot(conn, pn, now)
        if hit:
            return hit
    if not is_configured():
        return None
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        token = await get_app_token(client)
        query = pn
        items = await search(client, token, query)
        if not items and spaced(pn) != pn:
            query = spaced(pn)
            items = await search(client, token, query)
    except Exception as e:
        print(f"Market lookup failed for {pn}: {type(e).__name__}: {e}")
        return None
    finally:
        if own_client:
            await client.aclose()

    listings = filter_listings(items, pn)
    stats = summarise([l["total"] for l in listings])
    fetched_at = now.isoformat()
    conn.execute(
        "INSERT INTO market_snapshots (part_number_norm, fetched_at, count, lq, median, min, max, query, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pn, fetched_at, stats["count"], stats["lq"], stats["median"], stats["min"], stats["max"], query,
         json.dumps({"raw_count": len(items), "kept": listings})),
    )
    conn.commit()
    return {k: stats[k] for k in ("count", "lq", "median", "min", "max")} | {"fetched_at": fetched_at, "cached": False}


# ── Time budget + background refresh ─────────────────────────────────────
# One background lookup per part number at a time. A request that runs out of
# budget leaves the lookup running; it lands in market_snapshots for next time.

_inflight: dict[str, asyncio.Task] = {}


def client_factory() -> httpx.AsyncClient:
    """Overridable in tests (fake slow clients)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


async def _refresh(pn: str) -> dict | None:
    try:
        async with client_factory() as client:
            with store.session() as conn:
                return await get_market(conn, pn, client=client, force=True)
    except Exception as e:  # never let a background lookup crash the app
        log.warning("Background market lookup failed for %s: %s", pn, type(e).__name__)
        return None


def ensure_refresh(pn: str) -> asyncio.Task:
    """The running lookup for this part number, or a new one."""
    loop = asyncio.get_running_loop()
    task = _inflight.get(pn)
    if task is not None and not task.done() and task.get_loop() is loop:
        return task
    task = loop.create_task(_refresh(pn))
    _inflight[pn] = task

    def _done(t, pn=pn):
        if _inflight.get(pn) is t:
            del _inflight[pn]

    task.add_done_callback(_done)
    return task


def inflight_count() -> int:
    return sum(1 for t in _inflight.values() if not t.done())


async def market_within_budget(conn: sqlite3.Connection, part_number: str,
                               budget: float | None = None) -> tuple[dict | None, str]:
    """(market stats or None, status). Status: cache | live | timeout | error | unconfigured."""
    pn = normalise(part_number)
    if not pn:
        return None, "error"
    hit = cached_snapshot(conn, pn)
    if hit:
        return hit, "cache"
    if not is_configured():
        return None, "unconfigured"
    task = ensure_refresh(pn)
    budget = cfg.MARKET_TIME_BUDGET_SECONDS if budget is None else budget
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError:
        return None, "timeout"
    return (result, "live") if result is not None else (None, "error")


# ── Nightly pre-warm ─────────────────────────────────────────────────────

async def prewarm(conn: sqlite3.Connection, max_lookups: int | None = None,
                  now: datetime | None = None) -> dict:
    """Refresh stale market snapshots for the part numbers we sell most.

    Most-sold first; only where there's no snapshot younger than
    MARKET_CACHE_DAYS; at most MARKET_PREWARM_MAX_PER_NIGHT lookups."""
    if not is_configured():
        msg = "Market pre-warm skipped: EBAY_APP_ID / EBAY_CERT_ID not set"
        log.warning(msg)
        print(msg)
        return {"skipped": True, "reason": msg, "refreshed": 0}
    cap = cfg.MARKET_PREWARM_MAX_PER_NIGHT if max_lookups is None else max_lookups
    now = now or datetime.now(timezone.utc)
    candidates = [r[0] for r in conn.execute(
        "SELECT part_number_norm, COUNT(*) n FROM sold_history WHERE part_number_norm != '' "
        "GROUP BY part_number_norm ORDER BY n DESC, part_number_norm"
    )]
    refreshed, fresh, failed = 0, 0, 0
    for pn in candidates:
        if refreshed + failed >= cap:
            break
        if cached_snapshot(conn, pn, now):
            fresh += 1
            continue
        result = await ensure_refresh(pn)
        if result is None:
            failed += 1
        else:
            refreshed += 1
    return {"skipped": False, "candidates": len(candidates), "refreshed": refreshed,
            "already_fresh": fresh, "failed": failed, "cap": cap}
