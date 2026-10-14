import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import load_json

from pricing import market
from pricing.market import filter_listings, get_market, iqr_filter, listing_total, search_filter, summarise

PN = "6R2880201B"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setitem(market._token_cache, "token", None)
    monkeypatch.setitem(market._token_cache, "expires", 0.0)


def _transport(calls, by_query):
    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json=load_json("ebay_app_token.json"))
        assert request.url.path == "/buy/browse/v1/item_summary/search"
        assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
        return httpx.Response(200, json=load_json(by_query.get(request.url.params["q"], "browse_search_empty.json")))
    return httpx.MockTransport(handler)


def _run(conn, calls, by_query, **kw):
    async def go():
        async with httpx.AsyncClient(transport=_transport(calls, by_query)) as c:
            return await get_market(conn, PN, client=c, now=kw.pop("now", NOW), **kw)
    return asyncio.run(go())


def _items():
    return load_json("browse_search_6R2880201B.json")["itemSummaries"]


def _by_seller(listings):
    return {l["seller"] for l in listings}


def test_search_filter_has_spec_rules():
    f = search_filter()
    assert "conditionIds:{3000}" in f
    assert "itemLocationCountry:GB" in f
    assert "buyingOptions:{FIXED_PRICE|BEST_OFFER}" in f
    assert "excludeSellers:{pulled_apart_ltd|singlesorsets}" in f


def test_listing_total_uses_cheapest_shipping():
    items = {i["itemId"][3:15]: i for i in _items()}
    assert listing_total(items["110000000002"]) == 46.98   # 39.99 + 6.99
    assert listing_total(items["110000000003"]) == 49.99   # free beats 9.99
    assert listing_total(items["110000000006"]) == 47.50   # no shipping info


def test_own_sellers_excluded_case_insensitive():
    kept = filter_listings(_items(), PN)
    sellers = {s.lower() for s in _by_seller(kept)}
    assert "pulled_apart_ltd" not in sellers and "singlesorsets" not in sellers


def test_new_condition_overseas_auction_currency_and_irrelevant_excluded():
    kept_ids = {l["item_id"][3:15] for l in filter_listings(_items(), PN)}
    for excluded in ("110000000011", "110000000012", "110000000013", "110000000014", "110000000015"):
        assert excluded not in kept_ids
    assert len(kept_ids) == 8  # 7 good comps + the £499 outlier (removed later by IQR)


def test_outlier_removed_by_iqr():
    prices = [l["total"] for l in filter_listings(_items(), PN)]
    assert 499.0 in prices
    stats = summarise(prices)
    assert stats["count"] == 7 and stats["dropped"] == 1
    assert stats["max"] == 55.0 and stats["min"] == 45.0
    assert stats["median"] == 47.5
    assert stats["lq"] == pytest.approx(46.985, abs=0.01)


def test_iqr_small_samples_untouched():
    assert iqr_filter([10, 500, 20]) == [10, 20, 500]
    assert summarise([])["count"] == 0


def test_get_market_fetches_and_caches(conn, configured):
    calls = []
    first = _run(conn, calls, {PN: "browse_search_6R2880201B.json"})
    assert first["count"] == 7 and first["median"] == 47.5 and first["cached"] is False
    assert len(calls) == 2  # token + one search
    calls.clear()
    second = _run(conn, calls, {PN: "browse_search_6R2880201B.json"}, now=NOW + timedelta(days=6))
    assert second["cached"] is True and second["median"] == 47.5
    assert calls == []  # served from market_snapshots, no API call
    calls.clear()
    _run(conn, calls, {PN: "browse_search_6R2880201B.json"}, now=NOW + timedelta(days=8))
    assert len(calls) >= 1  # cache expired after MARKET_CACHE_DAYS


def test_zero_results_falls_back_to_spaced_then_caches_zero(conn, configured):
    calls = []
    res = _run(conn, calls, {})
    queries = [c.url.params["q"] for c in calls if "q" in c.url.params]
    assert queries == [PN, "6R2 880 201 B"]
    assert res["count"] == 0 and res["median"] is None
    row = conn.execute("SELECT count FROM market_snapshots WHERE part_number_norm=?", (PN,)).fetchone()
    assert row[0] == 0


def test_spaced_fallback_used_when_unspaced_finds_nothing(conn, configured):
    calls = []
    res = _run(conn, calls, {"6R2 880 201 B": "browse_search_6R2880201B.json"})
    assert res["count"] == 7


def test_not_configured_returns_none_without_calls(conn):
    calls = []
    assert _run(conn, calls, {}) is None
    assert calls == []


def test_api_failure_returns_none(conn, configured):
    async def go():
        t = httpx.MockTransport(lambda r: httpx.Response(500))
        async with httpx.AsyncClient(transport=t) as c:
            return await get_market(conn, PN, client=c, now=NOW)
    assert asyncio.run(go()) is None
    assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 0
