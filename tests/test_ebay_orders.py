"""Trading API GetOrders sync. Synthetic XML fixtures only (see
fixtures/make_getorders_fixtures.py); no live eBay calls."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_path

from pricing import ebay_orders
from pricing.ebay_orders import (
    AuthError, missing_env, order_to_rows, parse_response, sync_orders, sync_window,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
TOKEN = "v^1.1#i^1#SECRET_TOKEN_VALUE#r^1"


def _xml(name):
    with open(fixture_path(name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_DEV_ID", "dev")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_AUTH_TOKEN", TOKEN)


def _paged_transport(calls, pages=("getorders_page1.xml", "getorders_page2.xml")):
    def handler(request: httpx.Request):
        calls.append(request)
        assert request.url == httpx.URL(ebay_orders.API_URL)
        assert request.headers["X-EBAY-API-CALL-NAME"] == "GetOrders"
        assert request.headers["X-EBAY-API-IAF-TOKEN"] == TOKEN
        assert request.headers["X-EBAY-API-SITEID"] == "3"
        assert request.headers["X-EBAY-API-COMPATIBILITY-LEVEL"] == "1199"
        page = int(re.search(r"<PageNumber>(\d+)</PageNumber>", request.content.decode()).group(1))
        return httpx.Response(200, text=_xml(pages[page - 1]))
    return httpx.MockTransport(handler)


async def _nosleep(_):
    return None


def _sync(conn, transport, **kw):
    async def go():
        async with httpx.AsyncClient(transport=transport) as c:
            return await sync_orders(conn, client=c, now=kw.pop("now", NOW), sleep=_nosleep, **kw)
    return asyncio.run(go())


def _all_text(conn):
    return " ".join(str(v) for t in ("sold_history", "price_suggestions", "market_snapshots")
                    for row in conn.execute(f"SELECT * FROM {t}") for v in row)


# ── Parsing ─────────────────────────────────────────────────────────────

def test_parse_pages_and_line_items():
    orders, more, pages = parse_response(_xml("getorders_page1.xml"))
    assert len(orders) == 2 and more is True and pages == 2
    rows = [r for o in orders for r in order_to_rows(o)]
    assert len(rows) == 3
    single = rows[0]
    assert single["order_number"] == "31-00001-00001" and single["item_number"] == "400000000001"
    assert single["transaction_id"] == "9000000001" and single["sales_record_number"] == "8001"
    assert single["sold_for"] == "29.99" and single["postage"] == "0.00"
    assert single["sale_date"] == "2026-10-10T09:00:00.000Z"
    # order-level £6.99 P&P shared across the two lines
    assert rows[1]["postage"] == "3.50" and rows[2]["postage"] == "3.50"
    assert rows[1]["quantity"] == "2"


def test_parse_strips_buyer_fields():
    orders, _, _ = parse_response(_xml("getorders_page1.xml"))
    for row in (r for o in orders for r in order_to_rows(o)):
        assert set(row) == set(ebay_orders.import_rows.__globals__["CANONICAL_FIELDS"])
        blob = " ".join(row.values())
        assert "PII_SENTINEL" not in blob and "example.invalid" not in blob


def test_cancelled_and_unpaid_excluded():
    orders, more, _ = parse_response(_xml("getorders_page2.xml"))
    assert more is False
    rows = [order_to_rows(o) for o in orders]
    assert rows[0] == [] and rows[1] == []          # cancelled, unpaid
    assert len(rows[2]) == 1 and rows[2][0]["postage"] == "4.50"  # ActualShippingCost wins


def test_auth_error_ack_raises_auth_error():
    with pytest.raises(AuthError):
        parse_response(_xml("getorders_auth_error.xml"))


# ── Sync ────────────────────────────────────────────────────────────────

def test_sync_paginates_imports_and_strips_pii(conn, configured):
    calls = []
    s = _sync(conn, _paged_transport(calls))
    assert len(calls) == 2  # page 1 then page 2
    pages = [re.search(r"<PageNumber>(\d+)", c.content.decode()).group(1) for c in calls]
    assert pages == ["1", "2"]
    assert s["orders_fetched"] == 5 and s["inserted"] == 4 and s["skipped"] is False
    text = _all_text(conn)
    assert "PII_SENTINEL" not in text and "example.invalid" not in text
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sold_history)")}
    assert not any(c.startswith(("buyer", "ship_to", "post_to")) for c in cols)
    srcs = {r[0] for r in conn.execute("SELECT DISTINCT source FROM sold_history")}
    assert srcs == {"trading_api"}
    txns = {r[0] for r in conn.execute("SELECT transaction_id FROM sold_history")}
    assert txns == {"9000000001", "9000000002", "9000000003", "9000000006"}


def test_sync_is_idempotent(conn, configured):
    _sync(conn, _paged_transport([]))
    again = _sync(conn, _paged_transport([]))
    assert again["inserted"] == 0 and again["duplicates"] == 4
    assert conn.execute("SELECT COUNT(*) FROM sold_history").fetchone()[0] == 4


def test_dedupe_on_item_and_transaction_id_across_sources(conn, configured):
    # Same sale already imported from a raw report with a different record number.
    from pricing.sales_import import import_rows
    import_rows(conn, [{"order_number": "31-00001-00001", "sales_record_number": "X-other",
                        "item_number": "400000000001", "transaction_id": "9000000001",
                        "title": "Golf WINDOW MOTOR 5Q0 959 802 A", "custom_label": "", "quantity": "1",
                        "sold_for": "29.99", "postage": "0", "total_price": "", "sale_date": "10-Oct-26",
                        "dispatched_date": ""}], "csv_raw")
    s = _sync(conn, _paged_transport([]))
    assert s["inserted"] == 3 and s["duplicates"] == 1


def test_window_newest_sale_minus_two_days(conn):
    conn.execute("INSERT INTO sold_history (price, sold_date, source, imported_at, order_number) "
                 "VALUES (10, '2026-10-01', 'csv_clean', 'x', 'o1')")
    start, end = sync_window(conn, NOW)
    assert start.date().isoformat() == "2026-09-29" and end == NOW


def test_window_capped_at_90_days(conn, configured):
    # Empty DB and a DB whose newest sale is old: both capped at 90 days back.
    start, _ = sync_window(conn, NOW)
    assert NOW - start <= timedelta(days=90)
    conn.execute("INSERT INTO sold_history (price, sold_date, source, imported_at, order_number) "
                 "VALUES (10, '2025-01-01', 'csv_clean', 'x', 'o1')")
    start, _ = sync_window(conn, NOW)
    assert NOW - start <= timedelta(days=90) and NOW - start > timedelta(days=89)
    calls = []
    _sync(conn, _paged_transport(calls))
    sent = re.search(r"<CreateTimeFrom>([^<]+)</CreateTimeFrom>", calls[0].content.decode()).group(1)
    sent_dt = datetime.strptime(sent, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    assert NOW - sent_dt <= timedelta(days=90)


# ── Config / failures ───────────────────────────────────────────────────

def test_missing_env_vars_skip_cleanly(conn, capsys):
    assert set(missing_env()) == {"EBAY_APP_ID", "EBAY_DEV_ID", "EBAY_CERT_ID", "EBAY_AUTH_TOKEN"}

    def boom(request):
        raise AssertionError("must not call eBay when not configured")

    s = _sync(conn, httpx.MockTransport(boom))
    assert s["skipped"] is True and "EBAY_AUTH_TOKEN" in s["reason"]
    assert "EBAY_AUTH_TOKEN" in capsys.readouterr().out


def test_missing_single_var(monkeypatch, configured):
    monkeypatch.delenv("EBAY_DEV_ID")
    assert missing_env() == ["EBAY_DEV_ID"]


def test_site_id_override(monkeypatch, configured):
    monkeypatch.setenv("EBAY_SITE_ID", "0")
    assert ebay_orders._headers()["X-EBAY-API-SITEID"] == "0"


def test_retries_5xx_and_timeouts_then_succeeds(conn, configured):
    seq = iter(["timeout", 503, "page1", "page2"])

    def handler(request):
        step = next(seq)
        if step == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if step == 503:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=_xml(f"getorders_{step}.xml"))

    s = _sync(conn, httpx.MockTransport(handler))
    assert s["inserted"] == 4


def test_gives_up_after_max_attempts(conn, configured):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(RuntimeError, match="502"):
        _sync(conn, httpx.MockTransport(handler))
    assert len(calls) == ebay_orders.MAX_ATTEMPTS


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="no"),
    httpx.Response(200, text=_xml("getorders_auth_error.xml")),
])
def test_auth_errors_not_retried_and_token_not_leaked(conn, configured, response, caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return response

    with pytest.raises(AuthError) as exc:
        _sync(conn, httpx.MockTransport(handler))
    assert len(calls) == 1
    assert TOKEN not in str(exc.value) and "SECRET_TOKEN_VALUE" not in caplog.text


def test_timeouts_configured():
    assert ebay_orders.TIMEOUT.connect == 3.0 and ebay_orders.TIMEOUT.read == 10.0


# ── Endpoint ────────────────────────────────────────────────────────────

def test_sync_endpoint_needs_admin_token(client):
    assert client.post("/price/sync-orders").status_code == 503  # admin token not set


def test_sync_endpoint_reports_missing_vars(client, monkeypatch):
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "s3cret")
    assert client.post("/price/sync-orders", headers={"X-Admin-Token": "wrong"}).status_code == 401
    r = client.post("/price/sync-orders", headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 503
    assert "EBAY_AUTH_TOKEN" in r.json()["error"] and "EBAY_DEV_ID" in r.json()["error"]


def test_doctype_rejected():
    with pytest.raises(RuntimeError, match="DOCTYPE"):
        parse_response('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><GetOrdersResponse/>')
