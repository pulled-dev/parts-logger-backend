"""Ongoing sync of our eBay orders via the Fulfillment API (getOrders).

Needs a USER OAuth refresh token with scope
`https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly`, held in the
`EBAY_USER_REFRESH_TOKEN` env var. (The app-only client-credentials token used
for Browse is rejected by Fulfillment.) How to mint one is in HANDOFF.md.

Lookback (eBay docs): with no filter getOrders returns the last 90 days; with a
`creationdate` filter it reaches back up to 2 years. We sync from the latest
sale already in sold_history (minus a 3-day overlap; the dedupe key absorbs
repeats), or 90 days on first run. Older history comes from the CSV backfill.

Only order/line identifiers, title, money and dates are read. Buyer and
fulfilment-address fields in the response are never touched.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from pricing.sales_import import import_rows

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
ORDERS_URL = "https://api.ebay.com/sell/fulfillment/v1/order"
FULFILLMENT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
DEFAULT_LOOKBACK_DAYS = 90
OVERLAP_DAYS = 3
PAGE_LIMIT = 200


class NotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("EBAY_USER_REFRESH_TOKEN") and os.environ.get("EBAY_APP_ID")
                and os.environ.get("EBAY_CERT_ID"))


async def get_user_access_token(client: httpx.AsyncClient) -> str:
    refresh = os.environ.get("EBAY_USER_REFRESH_TOKEN", "")
    app_id, cert_id = os.environ.get("EBAY_APP_ID", ""), os.environ.get("EBAY_CERT_ID", "")
    if not (refresh and app_id and cert_id):
        raise NotConfigured("EBAY_USER_REFRESH_TOKEN / EBAY_APP_ID / EBAY_CERT_ID not set")
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    resp = await client.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh, "scope": FULFILLMENT_SCOPE},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"eBay user token refresh failed: HTTP {resp.status_code}")
    return resp.json()["access_token"]


def _money(obj) -> float:
    try:
        return float((obj or {}).get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def order_to_rows(order: dict) -> list[dict]:
    """Map one getOrders order to canonical importer rows (no buyer data).

    Cancelled or unpaid orders are skipped."""
    if (order.get("cancelStatus") or {}).get("cancelState") == "CANCELED":
        return []
    if order.get("orderPaymentStatus") not in (None, "PAID", "PARTIALLY_REFUNDED"):
        return []
    rows = []
    created = order.get("creationDate", "")
    for li in order.get("lineItems", []):
        qty = int(li.get("quantity") or 1)
        line_cost = _money(li.get("lineItemCost"))
        shipping = _money((li.get("deliveryCost") or {}).get("shippingCost"))
        rows.append({
            "order_number": order.get("orderId", ""),
            "sales_record_number": str(order.get("salesRecordReference", "") or ""),
            "item_number": str(li.get("legacyItemId", "") or ""),
            "title": li.get("title", ""),
            "custom_label": li.get("sku", "") or "",
            "quantity": str(qty),
            "sold_for": f"{line_cost / qty:.2f}",
            "postage": f"{shipping:.2f}",
            "total_price": "",
            "sale_date": created,
            "dispatched_date": "",
        })
    return rows


async def fetch_orders(client: httpx.AsyncClient, token: str, since: datetime) -> list[dict]:
    params = {
        "filter": f"creationdate:[{since.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..]",
        "limit": str(PAGE_LIMIT),
        "offset": "0",
    }
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}
    orders: list[dict] = []
    url: str | None = ORDERS_URL
    while url:
        resp = await client.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"getOrders failed: HTTP {resp.status_code}")
        data = resp.json()
        orders.extend(data.get("orders", []))
        url, params = data.get("next"), None  # `next` already carries the query
    return orders


def sync_start(conn: sqlite3.Connection, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    latest = conn.execute("SELECT MAX(sold_date) FROM sold_history").fetchone()[0]
    if latest:
        start = datetime.fromisoformat(latest).replace(tzinfo=timezone.utc) - timedelta(days=OVERLAP_DAYS)
        return max(start, now - timedelta(days=730))  # API hard limit: 2 years
    return floor


async def sync_orders(conn: sqlite3.Connection, client: httpx.AsyncClient | None = None,
                      dry_run: bool = False, now: datetime | None = None) -> dict:
    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        token = await get_user_access_token(client)
        since = sync_start(conn, now)
        orders = await fetch_orders(client, token, since)
    finally:
        if own_client:
            await client.aclose()
    rows = [r for o in orders for r in order_to_rows(o)]
    summary = import_rows(conn, rows, "fulfillment_api", dry_run=dry_run)
    summary["orders_fetched"] = len(orders)
    summary["since"] = since.isoformat()
    return summary
