"""Top up sold_history from eBay with the Trading API GetOrders call.

Uses the same long-lived token as the message bot (EBAY_AUTH_TOKEN, sent as the
X-EBAY-API-IAF-TOKEN header) plus EBAY_APP_ID / EBAY_DEV_ID / EBAY_CERT_ID and
EBAY_SITE_ID (default 3 = UK). GetOrders only reaches back 90 days, which is
fine: older history comes from the CSV import; this only tops it up.

Window: from the newest sale already in sold_history minus 2 days (overlap is
absorbed by the dedupe), never more than 90 days back, up to now.

Privacy: the XML is parsed and ONLY these fields are read per line item: item
number, title, sale price, quantity, postage, sale date, order number, sales
record number, transaction ID. Buyer name, user ID, email, address and phone
are never extracted, so they can't reach the DB or a log line.

Network: connect 3s / read 10s; timeouts and 5xx are retried with backoff;
auth failures are never retried. The token is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from pricing.sales_import import import_rows

log = logging.getLogger(__name__)

API_URL = "https://api.ebay.com/ws/api.dll"
COMPATIBILITY_LEVEL = "1199"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
REQUIRED_ENV = ("EBAY_APP_ID", "EBAY_DEV_ID", "EBAY_CERT_ID", "EBAY_AUTH_TOKEN")
MAX_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 2
ENTRIES_PER_PAGE = 100
MAX_PAGES = 50                       # safety stop (5,000 orders per sync)
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
MAX_ATTEMPTS = 3                     # 1 try + 2 retries on timeouts / 5xx
BACKOFF_BASE = 1.0                   # 1s, 2s
# Trading API error codes that mean the token/credentials are bad.
AUTH_ERROR_CODES = {"931", "932", "16110", "17470", "21916984", "21917053", "841"}


class NotConfigured(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


def missing_env() -> list[str]:
    return [v for v in REQUIRED_ENV if not os.environ.get(v)]


def is_configured() -> bool:
    return not missing_env()


def _headers() -> dict:
    return {
        "X-EBAY-API-SITEID": os.environ.get("EBAY_SITE_ID", "3") or "3",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "X-EBAY-API-APP-NAME": os.environ.get("EBAY_APP_ID", ""),
        "X-EBAY-API-DEV-NAME": os.environ.get("EBAY_DEV_ID", ""),
        "X-EBAY-API-CERT-NAME": os.environ.get("EBAY_CERT_ID", ""),
        "X-EBAY-API-CALL-NAME": "GetOrders",
        "X-EBAY-API-IAF-TOKEN": os.environ.get("EBAY_AUTH_TOKEN", ""),
        "Content-Type": "text/xml",
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_request(start: datetime, end: datetime, page: int) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <CreateTimeFrom>{_iso(start)}</CreateTimeFrom>
  <CreateTimeTo>{_iso(end)}</CreateTimeTo>
  <OrderRole>Seller</OrderRole>
  <OrderStatus>Completed</OrderStatus>
  <Pagination>
    <EntriesPerPage>{ENTRIES_PER_PAGE}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetOrdersRequest>"""


def sync_window(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(from, to): newest sale minus 2 days, capped at 90 days back."""
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=MAX_LOOKBACK_DAYS) + timedelta(minutes=5)  # stay inside eBay's limit
    latest = conn.execute("SELECT MAX(sold_date) FROM sold_history").fetchone()[0]
    if latest:
        start = datetime.fromisoformat(latest).replace(tzinfo=timezone.utc) - timedelta(days=OVERLAP_DAYS)
        return max(start, floor), now
    return floor, now


# ── Parsing (allowlist only) ─────────────────────────────────────────────

def _text(el, path: str) -> str:
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _money(el, path: str) -> float:
    try:
        return float(_text(el, path) or 0)
    except ValueError:
        return 0.0


def _order_is_sale(order) -> bool:
    """Completed, paid and not cancelled."""
    if _text(order, "e:OrderStatus") != "Completed":
        return False
    if _text(order, "e:CancelStatus") not in ("", "NotApplicable", "CancelRejected", "CancelClosedNoRefund"):
        return False
    if not _text(order, "e:PaidTime"):
        return False
    payment = _text(order, "e:CheckoutStatus/e:eBayPaymentStatus")
    return payment in ("", "NoPaymentFailure")


def order_to_rows(order) -> list[dict]:
    """One canonical importer row per transaction (line item). Only allowlisted
    fields are read; buyer details are never touched."""
    if not _order_is_sale(order):
        return []
    transactions = order.findall("e:TransactionArray/e:Transaction", NS)
    if not transactions:
        return []
    order_shipping = _money(order, "e:ShippingServiceSelected/e:ShippingServiceCost")
    created = _text(order, "e:CreatedTime")
    rows = []
    for t in transactions:
        qty = max(1, int(_text(t, "e:QuantityPurchased") or 1))
        if t.find("e:ActualShippingCost", NS) is not None:
            shipping = _money(t, "e:ActualShippingCost")
        else:
            shipping = order_shipping / len(transactions)  # order-level P&P shared across lines
        rows.append({
            "order_number": _text(order, "e:OrderID"),
            "sales_record_number": _text(t, "e:ShippingDetails/e:SellingManagerSalesRecordNumber")
            or _text(order, "e:ShippingDetails/e:SellingManagerSalesRecordNumber"),
            "item_number": _text(t, "e:Item/e:ItemID"),
            "transaction_id": _text(t, "e:TransactionID"),
            "title": _text(t, "e:Item/e:Title"),
            "custom_label": "",
            "quantity": str(qty),
            "sold_for": f"{_money(t, 'e:TransactionPrice'):.2f}",
            "postage": f"{shipping:.2f}",  # line total; the importer spreads it over the quantity
            "total_price": "",
            "sale_date": _text(t, "e:CreatedDate") or created,
            "dispatched_date": "",
        })
    return rows


def parse_response(xml_text: str) -> tuple[list, bool, int]:
    """(orders, has_more, total_pages). Raises AuthError / RuntimeError on Ack Failure."""
    if "<!DOCTYPE" in xml_text[:1000].upper():  # eBay never sends one; refuse entity tricks
        raise RuntimeError("GetOrders response contained a DOCTYPE; refusing to parse")
    root = ET.fromstring(xml_text)
    ack = _text(root, "e:Ack")
    if ack == "Failure":
        codes = [(_text(e, "e:ErrorCode"), _text(e, "e:ShortMessage")) for e in root.findall("e:Errors", NS)]
        if any(code in AUTH_ERROR_CODES for code, _ in codes):
            raise AuthError(f"eBay rejected the token (error {', '.join(c for c, _ in codes)})")
        raise RuntimeError(f"GetOrders failed: {'; '.join(f'{c} {m}' for c, m in codes)}")
    orders = root.findall("e:OrderArray/e:Order", NS)
    has_more = _text(root, "e:HasMoreOrders").lower() == "true"
    try:
        pages = int(_text(root, "e:PaginationResult/e:TotalNumberOfPages") or 1)
    except ValueError:
        pages = 1
    return orders, has_more, pages


# ── HTTP with retries ────────────────────────────────────────────────────

async def _post(client: httpx.AsyncClient, body: str, sleep=asyncio.sleep) -> str:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            resp = await client.post(API_URL, headers=_headers(), content=body.encode("utf-8"), timeout=TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if last:
                raise RuntimeError(f"GetOrders network failure after {attempt} attempts: {type(e).__name__}")
            log.warning("GetOrders %s (attempt %d), retrying", type(e).__name__, attempt)
            await sleep(BACKOFF_BASE * 2 ** (attempt - 1))
            continue
        if resp.status_code in (401, 403):
            raise AuthError(f"eBay rejected the token (HTTP {resp.status_code})")
        if resp.status_code >= 500:
            if last:
                raise RuntimeError(f"GetOrders HTTP {resp.status_code} after {attempt} attempts")
            log.warning("GetOrders HTTP %d (attempt %d), retrying", resp.status_code, attempt)
            await sleep(BACKOFF_BASE * 2 ** (attempt - 1))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"GetOrders HTTP {resp.status_code}")
        return resp.text
    raise RuntimeError("GetOrders exhausted retries")  # unreachable


async def fetch_orders(client: httpx.AsyncClient, start: datetime, end: datetime, sleep=asyncio.sleep) -> list:
    orders, page = [], 1
    while page <= MAX_PAGES:
        found, has_more, pages = parse_response(await _post(client, build_request(start, end, page), sleep))
        orders.extend(found)
        if not has_more and page >= pages:
            break
        page += 1
    return orders


async def sync_orders(conn: sqlite3.Connection, client: httpx.AsyncClient | None = None, dry_run: bool = False,
                      now: datetime | None = None, sleep=asyncio.sleep) -> dict:
    """Fetch recent orders and add new sales. Missing env vars -> logged and
    skipped (returns {"skipped": True, ...}); never raises for that."""
    missing = missing_env()
    if missing:
        msg = f"eBay order sync skipped: missing env var(s) {', '.join(missing)}"
        log.warning(msg)
        print(msg)
        return {"skipped": True, "reason": msg, "missing": missing}
    start, end = sync_window(conn, now)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        orders = await fetch_orders(client, start, end, sleep)
    finally:
        if own_client:
            await client.aclose()
    rows = [r for o in orders for r in order_to_rows(o)]
    summary = import_rows(conn, rows, "trading_api", dry_run=dry_run)
    summary.update({"skipped": False, "orders_fetched": len(orders), "since": start.isoformat(),
                    "until": end.isoformat()})
    return summary
