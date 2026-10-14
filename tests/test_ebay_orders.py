import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from conftest import load_json

from pricing import ebay_orders
from pricing.ebay_orders import NotConfigured, order_to_rows, sync_orders, sync_start


def _transport(calls):
    p1, p2 = load_json("fulfillment_orders_page1.json"), load_json("fulfillment_orders_page2.json")
    token = load_json("ebay_user_token.json")

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            assert b"grant_type=refresh_token" in request.content
            assert b"sell.fulfillment.readonly" in request.content
            return httpx.Response(200, json=token)
        assert request.headers["Authorization"] == "Bearer FAKE_USER_TOKEN"
        if request.url.params.get("offset") == "2":
            return httpx.Response(200, json=p2)
        return httpx.Response(200, json=p1)

    return httpx.MockTransport(handler)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_USER_REFRESH_TOKEN", "refresh")


def test_order_to_rows_maps_lines_and_ignores_buyer():
    order = load_json("fulfillment_orders_page1.json")["orders"][1]
    rows = order_to_rows(order)
    assert len(rows) == 2
    wiper = rows[1]
    assert wiper["sold_for"] == "20.00"  # line cost 40.00 over qty 2
    assert wiper["postage"] == "6.99"
    assert wiper["order_number"] == "21-00002-00002" and wiper["sales_record_number"] == "7002"
    assert not any("PII_SENTINEL" in str(v) or "example.invalid" in str(v) for r in rows for v in r.values())


def test_cancelled_and_unpaid_orders_skipped():
    p2 = load_json("fulfillment_orders_page2.json")
    assert [order_to_rows(o) for o in p2["orders"]] == [[], []]


def test_sync_paginates_and_imports(conn, configured):
    calls = []
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    async def run():
        async with httpx.AsyncClient(transport=_transport(calls)) as client:
            return await sync_orders(conn, client=client, now=now)

    summary = asyncio.run(run())
    assert summary["orders_fetched"] == 4
    assert summary["inserted"] == 3
    assert len(calls) == 3  # token + 2 pages
    first_orders_call = calls[1]
    assert "creationdate:[2026-07-16" in first_orders_call.url.params["filter"]  # 90-day first run
    text = " ".join(str(v) for r in conn.execute("SELECT * FROM sold_history") for v in r)
    assert "PII_SENTINEL" not in text and "example.invalid" not in text
    # Re-running is idempotent.
    calls.clear()
    summary = asyncio.run(run())
    assert summary["inserted"] == 0 and summary["duplicates"] == 3


def test_sync_start_uses_latest_sale_with_overlap(conn):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert sync_start(conn, now).date().isoformat() == "2026-07-16"
    conn.execute("INSERT INTO sold_history (price, sold_date, source, imported_at, order_number) "
                 "VALUES (10, '2026-10-01', 'csv_clean', 'x', 'o1')")
    assert sync_start(conn, now).date().isoformat() == "2026-09-28"


def test_not_configured_raises(conn):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
            await sync_orders(conn, client=c)

    with pytest.raises(NotConfigured):
        asyncio.run(run())
    assert ebay_orders.is_configured() is False


def test_sync_endpoint_reports_not_configured(client, monkeypatch):
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "s3cret")
    r = client.post("/price/sync-orders", headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 503
    assert "EBAY_USER_REFRESH_TOKEN" in r.json()["error"]


def test_token_refresh_failure_is_reported(conn, configured):
    async def run():
        t = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_grant"}))
        async with httpx.AsyncClient(transport=t) as c:
            await sync_orders(conn, client=c)

    with pytest.raises(RuntimeError, match="token refresh failed"):
        asyncio.run(run())
