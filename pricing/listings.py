"""Daily snapshot of our ACTIVE eBay listings (Trading API GetMyeBaySelling).

Why: the logger's exported price isn't always what goes on eBay (prices get
changed in BreakerPro afterwards), and the sold-only report can't see items
that never sell. This records what is actually listed, at what price, and for
how long.

READ-ONLY. The only call made is GetMyeBaySelling (ActiveList). Nothing here
creates, revises or ends a listing. Same credentials and header pattern as
pricing/ebay_orders.py (EBAY_AUTH_TOKEN as the IAF token, plus EBAY_APP_ID /
EBAY_DEV_ID / EBAY_CERT_ID, EBAY_SITE_ID default 3 = UK).

Storage (listing_snapshots): a new row only when an item is first seen or its
price / title / Best Offer setting changes; on an unchanged day only
last_seen_date on its latest row moves on. So every item keeps its first-seen
day, every price change with its date, and its last-seen day. A run is only
written once ALL pages are fetched, so a failed run never makes listings look
ended.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timezone

import httpx

import pricing_config as cfg
from pricing import ebay_orders
from pricing.ebay_orders import NS, _money, _text
from pricing.normalise import normalise

log = logging.getLogger(__name__)

CALL_NAME = "GetMyeBaySelling"
ENTRIES_PER_PAGE = 200               # eBay's maximum for ActiveList
MAX_PAGES = 50                       # safety stop: 10,000 listings, i.e. 50 calls a day at most
PRICE_TOLERANCE = 0.005              # prices within half a penny are "the same"

missing_env = ebay_orders.missing_env


def build_request(page: int) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>{ENTRIES_PER_PAGE}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>"""


# ── Parsing ─────────────────────────────────────────────────────────────

def _shipping(item) -> float:
    """Cost of the first (priority 1) domestic postage option; 0 if none."""
    options = item.findall("e:ShippingDetails/e:ShippingServiceOptions", NS)
    if not options:
        return 0.0
    options.sort(key=lambda o: int(_text(o, "e:ShippingServicePriority") or 99))
    return _money(options[0], "e:ShippingServiceCost")


def _best_offer(item) -> bool:
    for path in ("e:BestOfferEnabled", "e:BestOfferDetails/e:BestOfferEnabled"):
        if _text(item, path).lower() == "true":
            return True
    # an auto-accept / minimum offer price only exists on a Best Offer listing
    return any(_text(item, p) for p in ("e:ListingDetails/e:BestOfferAutoAcceptPrice",
                                        "e:ListingDetails/e:MinimumBestOfferPrice"))


def item_to_listing(item) -> dict | None:
    item_id = _text(item, "e:ItemID")
    if not item_id:
        return None
    price = _money(item, "e:SellingStatus/e:CurrentPrice") or _money(item, "e:BuyItNowPrice")
    shipping = _shipping(item)
    title = _text(item, "e:Title")
    return {
        "item_id": item_id,
        "title": title,
        "part_number_norm": normalise(title),
        "item_price": round(price, 2),
        "shipping": round(shipping, 2),
        "current_price": round(price + shipping, 2),
        "start_time": _text(item, "e:ListingDetails/e:StartTime") or None,
        "best_offer_enabled": int(_best_offer(item)),
    }


def parse_response(xml_text: str) -> tuple[list[dict], int]:
    """(listings, total_pages). Raises AuthError / RuntimeError on Ack Failure."""
    root = ebay_orders.parse_root(xml_text, CALL_NAME)
    active = root.find("e:ActiveList", NS)
    if active is None:
        return [], 1
    listings = [x for x in (item_to_listing(i) for i in active.findall("e:ItemArray/e:Item", NS)) if x]
    try:
        pages = int(_text(active, "e:PaginationResult/e:TotalNumberOfPages") or 1)
    except ValueError:
        pages = 1
    return listings, max(pages, 1)


async def fetch_active(client: httpx.AsyncClient, sleep=asyncio.sleep, stats: dict | None = None) -> list[dict]:
    """Every page of ActiveList. De-duplicated by item ID (a listing can shift
    between pages while we read)."""
    stats = stats if stats is not None else {}
    found: dict[str, dict] = {}
    page = 1
    while True:
        xml_text = await ebay_orders._post(client, build_request(page), sleep, call_name=CALL_NAME, stats=stats)
        listings, pages = parse_response(xml_text)
        stats["pages"] = page
        for x in listings:
            found.setdefault(x["item_id"], x)
        if page >= pages:
            break
        if page >= MAX_PAGES:
            raise RuntimeError(f"{CALL_NAME}: more than {MAX_PAGES} pages; refusing a partial snapshot")
        page += 1
    return list(found.values())


# ── Storing ─────────────────────────────────────────────────────────────

def _same_state(row, x: dict) -> bool:
    return (abs(row["current_price"] - x["current_price"]) <= PRICE_TOLERANCE
            and row["title"] == x["title"] and row["best_offer_enabled"] == x["best_offer_enabled"])


def record_snapshot(conn: sqlite3.Connection, listings: list[dict], run_date: str, dry_run: bool = False) -> dict:
    """Write one day's ActiveList. Returns counts: new / changed / unchanged /
    gone (seen on the previous run, missing now)."""
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for x in listings:
        last = conn.execute("SELECT * FROM listing_snapshots WHERE item_id = ? ORDER BY snapshot_date DESC LIMIT 1",
                            (x["item_id"],)).fetchone()
        if last is None:
            counts["new"] += 1
        elif _same_state(last, x):
            counts["unchanged"] += 1
        else:
            counts["changed"] += 1
        if dry_run:
            continue
        if last is not None and _same_state(last, x):
            conn.execute("UPDATE listing_snapshots SET last_seen_date = ? WHERE id = ?",
                         (max(run_date, last["last_seen_date"]), last["id"]))
        elif last is not None and last["snapshot_date"] == run_date:
            # changed again on the same day: one row per item per day, keep the latest state
            conn.execute(
                "UPDATE listing_snapshots SET title = ?, part_number_norm = ?, current_price = ?, item_price = ?, "
                "shipping = ?, best_offer_enabled = ?, last_seen_date = ? WHERE id = ?",
                (x["title"], x["part_number_norm"], x["current_price"], x["item_price"], x["shipping"],
                 x["best_offer_enabled"], run_date, last["id"]))
        else:
            conn.execute(
                "INSERT INTO listing_snapshots (item_id, title, part_number_norm, current_price, item_price, "
                "shipping, start_time, snapshot_date, last_seen_date, best_offer_enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (x["item_id"], x["title"], x["part_number_norm"], x["current_price"], x["item_price"],
                 x["shipping"], x["start_time"] or (last["start_time"] if last else None), run_date, run_date,
                 x["best_offer_enabled"]))
    prev = conn.execute("SELECT MAX(run_date) FROM listing_snapshot_runs WHERE run_date < ?", (run_date,)).fetchone()[0]
    seen = {x["item_id"] for x in listings}
    gone = 0
    if prev:
        gone = sum(1 for (iid,) in conn.execute(
            "SELECT DISTINCT item_id FROM listing_snapshots WHERE last_seen_date = ?", (prev,)) if iid not in seen)
    counts["gone"] = gone
    return counts


async def snapshot(conn: sqlite3.Connection, client: httpx.AsyncClient | None = None, dry_run: bool = False,
                   now: datetime | None = None, sleep=asyncio.sleep) -> dict:
    """Fetch ActiveList and record it. Missing env vars -> logged and skipped
    (returns {"skipped": True, ...}); never raises for that."""
    missing = missing_env()
    if missing:
        msg = f"eBay listing snapshot skipped: missing env var(s) {', '.join(missing)}"
        log.warning(msg)
        print(msg)
        return {"skipped": True, "reason": msg, "missing": missing}
    started = now or datetime.now(timezone.utc)
    run_date = started.date().isoformat()
    stats: dict = {"calls": 0, "pages": 0}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=ebay_orders.TIMEOUT)
    try:
        listings = await fetch_active(client, sleep, stats)
    finally:
        if own_client:
            await client.aclose()
        msg = f"{CALL_NAME}: {stats['calls']} API call(s), {stats['pages']} page(s)"
        log.info(msg)
        print(msg)
    counts = record_snapshot(conn, listings, run_date, dry_run=dry_run)
    if not dry_run:
        conn.execute(
            "INSERT INTO listing_snapshot_runs (run_date, started_at, finished_at, items, pages, api_calls, "
            "new_items, changed, unchanged, gone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_date, started.isoformat(timespec="seconds"),
             datetime.now(timezone.utc).isoformat(timespec="seconds"), len(listings), stats["pages"],
             stats["calls"], counts["new"], counts["changed"], counts["unchanged"], counts["gone"]))
        conn.commit()
        counts["suggestions_linked"] = link_listings(conn)
    return {"skipped": False, "dry_run": dry_run, "run_date": run_date, "items": len(listings),
            "pages": stats["pages"], "api_calls": stats["calls"], **counts}


# ── Per-listing history ─────────────────────────────────────────────────

def _day(iso: str | None) -> date | None:
    return date.fromisoformat(iso[:10]) if iso else None


def latest_run_date(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(run_date) FROM listing_snapshot_runs").fetchone()[0]


def snapshot_runs(conn: sqlite3.Connection) -> dict:
    """How many snapshots have run, and the latest one (with its API call count)."""
    n = conn.execute("SELECT COUNT(*) FROM listing_snapshot_runs").fetchone()[0]
    last = conn.execute("SELECT run_date, finished_at, items, pages, api_calls, new_items, changed, gone "
                        "FROM listing_snapshot_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"runs": n, "last": dict(last) if last else None}


def listing_history(conn: sqlite3.Connection, today: date | None = None) -> dict[str, dict]:
    """item_id -> what happened to that listing:
    first / current price, every price change with its date, days listed, and
    outcome: "sold" (a sold_history row with this item number, on or after the
    listing start), "active" (in the latest snapshot), or "ended_unsold"
    (dropped out of ActiveList with no matching sale).

    Worked out fresh each time, so a sale imported after the listing vanished
    still turns "ended_unsold" into "sold"."""
    today = today or datetime.now(timezone.utc).date()
    latest = latest_run_date(conn)
    by_item: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM listing_snapshots ORDER BY item_id, snapshot_date"):
        by_item.setdefault(r["item_id"], []).append(r)
    if not by_item:
        return {}
    sales: dict[str, list] = {}
    for item_number, sold_date, price in conn.execute(
            "SELECT item_number, sold_date, price FROM sold_history WHERE item_number != '' ORDER BY sold_date, id"):
        if item_number in by_item:
            sales.setdefault(item_number, []).append((sold_date, price))

    out = {}
    for item_id, states in by_item.items():
        first, last = states[0], states[-1]
        start = _day(first["start_time"]) or _day(first["snapshot_date"])
        changes = [{"date": cur["snapshot_date"], "from": prev["current_price"], "to": cur["current_price"]}
                   for prev, cur in zip(states, states[1:])
                   if abs(cur["current_price"] - prev["current_price"]) > PRICE_TOLERANCE]
        last_seen = last["last_seen_date"]
        sale = next(((d, p) for d, p in sales.get(item_id, []) if _day(d) >= start), None)
        if sale:
            outcome, end = "sold", _day(sale[0])
            # the price on eBay when it sold: the last state first seen on or before the sale day
            listed_at_sale = next((s["current_price"] for s in reversed(states)
                                   if s["snapshot_date"] <= sale[0][:10]), first["current_price"])
        else:
            active = latest is not None and last_seen >= latest
            outcome, end, listed_at_sale = ("active", today, None) if active else ("ended_unsold", _day(last_seen), None)
        out[item_id] = {
            "item_id": item_id,
            "title": last["title"],
            "part_number_norm": first["part_number_norm"],
            "start_date": start.isoformat(),
            "first_seen": first["snapshot_date"],
            "last_seen": last_seen,
            "first_price": first["current_price"],
            "current_price": last["current_price"],
            "price_changes": changes,
            "best_offer_enabled": bool(last["best_offer_enabled"]),
            "outcome": outcome,
            "sold_date": sale[0][:10] if sale else None,
            "sold_price": sale[1] if sale else None,
            "listed_price_at_sale": listed_at_sale,
            "days_listed": max(0, (end - start).days),
            "days_to_sell": max(0, (end - start).days) if sale else None,
        }
    return out


# ── Suggestion <-> listing linking ──────────────────────────────────────

def link_listings(conn: sqlite3.Connection, window_days: int | None = None) -> int:
    """Link each unlinked suggestion to a listing of the same part number that
    started on or after the suggestion's day (within LISTING_LINK_WINDOW_DAYS),
    nearest start first. One listing per suggestion and vice versa; oldest
    suggestions are linked first. Stores the item ID on the suggestion."""
    window = cfg.LISTING_LINK_WINDOW_DAYS if window_days is None else window_days
    used = {r[0] for r in conn.execute(
        "SELECT listing_item_id FROM price_suggestions WHERE listing_item_id IS NOT NULL")}
    starts: dict[str, list] = {}
    for item_id, pn, start, first_seen in conn.execute(
            "SELECT item_id, part_number_norm, MIN(COALESCE(start_time, snapshot_date)), MIN(snapshot_date) "
            "FROM listing_snapshots WHERE part_number_norm != '' GROUP BY item_id"):
        starts.setdefault(pn, []).append((_day(start or first_seen), item_id))
    for lst in starts.values():
        lst.sort()
    linked = 0
    pending = conn.execute(
        "SELECT id, timestamp, part_number_norm FROM price_suggestions "
        "WHERE listing_item_id IS NULL AND part_number_norm != '' ORDER BY timestamp, id").fetchall()
    for sid, ts, pn in pending:
        day = _day(ts)
        for start, item_id in starts.get(pn, []):
            if start < day or item_id in used:
                continue
            if (start - day).days > window:
                break
            conn.execute("UPDATE price_suggestions SET listing_item_id = ? WHERE id = ?", (item_id, sid))
            used.add(item_id)
            linked += 1
            break
    conn.commit()
    return linked
