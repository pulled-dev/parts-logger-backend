"""Job B: /price/suggest stays fast. Fake slow eBay clients only (no live calls)."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import load_json

import pricing_config as cfg
from pricing import feedback, market
from pricing.sales_import import import_rows

PN = "6R2880201B"
_seq = iter(range(1, 10_000))


def seed_sales(conn, pn_spaced="6R2 880 201 B", prices=(40, 42, 44), title="VW Polo 6R AIRBAG"):
    rows = []
    for p in prices:
        i = next(_seq)
        rows.append({"order_number": f"S-{i}", "sales_record_number": str(i), "item_number": str(i),
                     "title": f"{title} {pn_spaced}", "custom_label": "", "quantity": "1", "sold_for": str(p),
                     "postage": "0", "total_price": "", "sale_date": (datetime.now(timezone.utc).date()
                                                                       - timedelta(days=30)).isoformat(),
                     "dispatched_date": ""})
    import_rows(conn, rows, "csv_clean")


@pytest.fixture
def ebay_keys(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setitem(market._token_cache, "token", None)
    monkeypatch.setitem(market._token_cache, "expires", 0.0)
    market._inflight.clear()
    yield
    market._inflight.clear()


@pytest.fixture
def slow_ebay(monkeypatch, ebay_keys):
    """Fake eBay: Browse searches take `state['delay']` seconds."""
    state = {"delay": 0.0, "searches": [], "tokens": 0}

    async def handler(request: httpx.Request):
        if request.url.path == "/identity/v1/oauth2/token":
            state["tokens"] += 1
            return httpx.Response(200, json=load_json("ebay_app_token.json"))
        state["searches"].append(request.url.params["q"])
        await asyncio.sleep(state["delay"])
        return httpx.Response(200, json=load_json("browse_search_6R2880201B.json"))

    monkeypatch.setattr(market, "client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return state


def _wait_for_snapshot(conn, pn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if conn.execute("SELECT 1 FROM market_snapshots WHERE part_number_norm=?", (pn,)).fetchone():
            return True
        time.sleep(0.05)
    return False


def test_config_defaults():
    assert cfg.MARKET_TIME_BUDGET_SECONDS == 3
    assert cfg.MARKET_PREWARM_MAX_PER_NIGHT == 300
    assert market.TIMEOUT.connect == 3.0 and market.TIMEOUT.read == 5.0


def test_over_budget_returns_in_about_3s_with_price_then_caches(client, conn, slow_ebay):
    seed_sales(conn)
    slow_ebay["delay"] = 4.5  # eBay slower than the 3s budget
    t0 = time.perf_counter()
    r = client.get("/price/suggest", params={"part_number": PN}).json()
    elapsed = time.perf_counter() - t0
    assert 2.8 <= elapsed < 4.0, elapsed
    assert r["market_status"] == "timeout"
    assert r["suggested_price"] is not None and r["source"] == "own_sold"  # a price, from own sales
    assert "live prices still loading" in r["reason"]
    assert r["market"] is None
    # The lookup finishes in the background and lands in the cache...
    assert _wait_for_snapshot(conn, PN)
    # ...so the next request is served from cache, fast, with market data.
    t0 = time.perf_counter()
    r2 = client.get("/price/suggest", params={"part_number": PN}).json()
    assert time.perf_counter() - t0 < 1.0
    assert r2["market_status"] == "cache" and r2["market"]["count"] == 7
    assert "still loading" not in r2["reason"]
    assert slow_ebay["searches"] == [PN]  # exactly one eBay search in total


def test_within_budget_is_live(client, conn, slow_ebay):
    seed_sales(conn)
    r = client.get("/price/suggest", params={"part_number": PN}).json()
    assert r["market_status"] == "live" and r["market"]["count"] == 7


def test_no_duplicate_background_lookups(client, conn, slow_ebay, monkeypatch):
    monkeypatch.setattr(cfg, "MARKET_TIME_BUDGET_SECONDS", 0.2)
    slow_ebay["delay"] = 1.5
    for _ in range(3):  # three loggers add the same part while eBay is slow
        r = client.get("/price/suggest", params={"part_number": PN}).json()
        assert r["market_status"] == "timeout"
    assert _wait_for_snapshot(conn, PN)
    assert slow_ebay["searches"] == [PN]
    assert conn.execute("SELECT COUNT(*) FROM market_snapshots WHERE part_number_norm=?", (PN,)).fetchone()[0] == 1


def test_ensure_refresh_reuses_running_task(slow_ebay, tmp_db):
    slow_ebay["delay"] = 0.3

    async def go():
        a = market.ensure_refresh(PN)
        b = market.ensure_refresh(PN)
        assert a is b and market.inflight_count() == 1
        await a
        assert market.inflight_count() == 0
        c = market.ensure_refresh(PN)  # finished -> a new lookup may start
        assert c is not a
        await c

    asyncio.run(go())
    assert slow_ebay["searches"] == [PN, PN]


def test_unconfigured_market_is_instant(client, conn):
    seed_sales(conn)
    t0 = time.perf_counter()
    r = client.get("/price/suggest", params={"part_number": PN}).json()
    assert time.perf_counter() - t0 < 1.0 and r["market_status"] == "unconfigured"


# ── Nightly pre-warm ────────────────────────────────────────────────────

def _snapshot(conn, pn, age_days):
    when = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    conn.execute("INSERT INTO market_snapshots (part_number_norm, fetched_at, count) VALUES (?, ?, 0)", (pn, when))
    conn.commit()


def test_prewarm_most_sold_first_respects_cap_and_age(conn, slow_ebay):
    seed_sales(conn, "1K0 959 653 D", prices=(1, 2, 3, 4, 5), title="SLIP RING")   # 5 sales, fresh snapshot
    seed_sales(conn, "6R2 880 201 B", prices=(1, 2, 3, 4))                         # 4 sales, stale snapshot
    seed_sales(conn, "5Q0 959 802 A", prices=(1, 2, 3), title="WINDOW MOTOR")      # 3 sales, none
    seed_sales(conn, "3C2 823 533", prices=(1, 2), title="BONNET HANDLE")          # 2 sales, none
    _snapshot(conn, "1K0959653D", age_days=2)
    _snapshot(conn, "6R2880201B", age_days=cfg.MARKET_CACHE_DAYS + 1)
    s = asyncio.run(market.prewarm(conn, max_lookups=2))
    assert s["refreshed"] == 2 and s["already_fresh"] == 1 and s["skipped"] is False
    # fresh one skipped; next two most-sold refreshed; the 4th is over the cap
    assert slow_ebay["searches"] == ["6R2880201B", "5Q0959802A"]


def test_prewarm_default_cap_is_config(conn, slow_ebay, monkeypatch):
    monkeypatch.setattr(cfg, "MARKET_PREWARM_MAX_PER_NIGHT", 1)
    seed_sales(conn, "5Q0 959 802 A", prices=(1, 2), title="WINDOW MOTOR")
    seed_sales(conn, "3C2 823 533", prices=(1,), title="BONNET HANDLE")
    s = asyncio.run(market.prewarm(conn))
    assert s["cap"] == 1 and len(slow_ebay["searches"]) == 1


def test_prewarm_skips_cleanly_without_keys(conn, capsys):
    seed_sales(conn)
    s = asyncio.run(market.prewarm(conn))
    assert s["skipped"] is True and s["refreshed"] == 0
    assert "EBAY_APP_ID" in capsys.readouterr().out


def test_prewarm_endpoint_admin_only(client, monkeypatch):
    assert client.post("/price/prewarm").status_code == 503
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "t")
    assert client.post("/price/prewarm", headers={"X-Admin-Token": "x"}).status_code == 401
    assert client.post("/price/prewarm", headers={"X-Admin-Token": "t"}).json()["skipped"] is True


# ── Timing in the log + report ─────────────────────────────────────────

def test_percentile():
    assert feedback.percentile(list(range(1, 101)), 95) == 95
    assert feedback.percentile([10], 95) == 10
    assert feedback.percentile([], 95) is None


def test_duration_and_market_status_logged_and_reported(client, conn):
    seed_sales(conn)
    for _ in range(3):
        client.get("/price/suggest", params={"part_number": PN})
    rows = conn.execute("SELECT duration_ms, market_status FROM price_suggestions").fetchall()
    assert len(rows) == 3 and all(r[0] is not None and r[0] >= 0 for r in rows)
    assert {r[1] for r in rows} == {"unconfigured"}
    conn.executemany("UPDATE price_suggestions SET duration_ms=? WHERE id=?", [(100, 1), (200, 2), (3100, 3)])
    conn.commit()
    rt = client.get("/price/report").json()["response_time"]
    assert rt == {"n": 3, "median_ms": 200, "p95_ms": 3100, "market_status": {"unconfigured": 3}}
