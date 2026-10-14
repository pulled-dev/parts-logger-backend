"""Active-listing snapshot (GetMyeBaySelling). Synthetic XML only; no live eBay calls."""

import asyncio
import re
from datetime import datetime, timezone

import httpx
import pytest

from pricing import ebay_orders, listings
from pricing.ebay_orders import AuthError

TOKEN = "v^1.1#i^1#SECRET_TOKEN_VALUE#r^1"
DAY1 = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
DAY3 = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_DEV_ID", "dev")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_AUTH_TOKEN", TOKEN)


# ── Synthetic GetMyeBaySelling XML ──────────────────────────────────────

def item_xml(item_id, title, price, ship="0.00", start="2026-09-20T10:00:00.000Z", best_offer=None,
             extra_ship=None):
    bo = f"<BestOfferDetails><BestOfferCount>0</BestOfferCount><BestOfferEnabled>{best_offer}</BestOfferEnabled>" \
         f"</BestOfferDetails>" if best_offer is not None else ""
    more = ""
    if extra_ship is not None:  # a pricier courier option listed first but at priority 2
        more = f"""<ShippingServiceOptions><ShippingService>UK_Parcelforce48</ShippingService>
          <ShippingServiceCost currencyID="GBP">{extra_ship}</ShippingServiceCost>
          <ShippingServicePriority>2</ShippingServicePriority></ShippingServiceOptions>"""
    return f"""
      <Item>
        <BuyItNowPrice currencyID="GBP">{price}</BuyItNowPrice>
        <ItemID>{item_id}</ItemID>
        <ListingDetails><StartTime>{start}</StartTime></ListingDetails>
        <SellingStatus><CurrentPrice currencyID="GBP">{price}</CurrentPrice></SellingStatus>
        <ShippingDetails>{more}<ShippingServiceOptions>
          <ShippingService>UK_RoyalMailSecondClassStandard</ShippingService>
          <ShippingServiceCost currencyID="GBP">{ship}</ShippingServiceCost>
          <ShippingServicePriority>1</ShippingServicePriority>
        </ShippingServiceOptions></ShippingDetails>
        <Title>{title}</Title>
        <QuantityAvailable>1</QuantityAvailable>{bo}
      </Item>"""


def page_xml(items, pages=1, entries=None):
    entries = len(items) if entries is None else entries
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-10-01T06:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1199</Version>
  <ActiveList>
    <ItemArray>{''.join(items)}</ItemArray>
    <PaginationResult><TotalNumberOfPages>{pages}</TotalNumberOfPages>
      <TotalNumberOfEntries>{entries}</TotalNumberOfEntries></PaginationResult>
  </ActiveList>
</GetMyeBaySellingResponse>"""


ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><ShortMessage>Auth token is invalid.</ShortMessage><ErrorCode>931</ErrorCode></Errors>
</GetMyeBaySellingResponse>"""

MOTOR = "VW Golf Mk7 Window Motor Front Right 5Q0 959 802 A"
PULL = "VW Polo 6R Bonnet Release Pull 3C2 823 533"


def transport(pages, calls):
    """pages: list of XML strings, served by <PageNumber>."""
    def handler(request: httpx.Request):
        calls.append(request)
        body = request.content.decode()
        page = int(re.search(r"<PageNumber>(\d+)</PageNumber>", body).group(1))
        return httpx.Response(200, text=pages[page - 1])
    return httpx.MockTransport(handler)


async def _nosleep(_):
    return None


def run_snapshot(conn, pages, now, calls=None, **kw):
    calls = [] if calls is None else calls

    async def go():
        async with httpx.AsyncClient(transport=transport(pages, calls)) as c:
            return await listings.snapshot(conn, client=c, now=now, sleep=_nosleep, **kw)
    return asyncio.run(go())


def rows(conn, item_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM listing_snapshots WHERE item_id = ? ORDER BY snapshot_date", (item_id,))]


# ── Parsing ─────────────────────────────────────────────────────────────

def test_parse_item_price_includes_postage_and_part_number():
    found, pages = listings.parse_response(page_xml([
        item_xml("111", MOTOR, "24.99", ship="6.99", best_offer="true"),
        item_xml("222", PULL, "19.99"),
    ], pages=3))
    assert pages == 3
    a, b = found
    assert a["item_id"] == "111" and a["part_number_norm"] == "5Q0959802A"
    assert a["item_price"] == 24.99 and a["shipping"] == 6.99 and a["current_price"] == 31.98
    assert a["start_time"] == "2026-09-20T10:00:00.000Z" and a["best_offer_enabled"] == 1
    assert b["current_price"] == 19.99 and b["best_offer_enabled"] == 0 and b["part_number_norm"] == "3C2823533"


def test_parse_uses_priority_one_postage():
    (x,), _ = listings.parse_response(page_xml([item_xml("111", MOTOR, "20.00", ship="0.00", extra_ship="64.99")]))
    assert x["shipping"] == 0.0 and x["current_price"] == 20.0


def test_parse_best_offer_from_auto_accept_price():
    xml = page_xml([item_xml("111", MOTOR, "20.00").replace(
        "</ListingDetails>", "<BestOfferAutoAcceptPrice currencyID=\"GBP\">18.00</BestOfferAutoAcceptPrice>"
                             "</ListingDetails>")])
    (x,), _ = listings.parse_response(xml)
    assert x["best_offer_enabled"] == 1


def test_parse_no_active_list_and_auth_error():
    empty = """<?xml version="1.0"?><GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack></GetMyeBaySellingResponse>"""
    assert listings.parse_response(empty) == ([], 1)
    with pytest.raises(AuthError):
        listings.parse_response(ERROR_XML)


def test_request_is_read_only_active_list():
    body = listings.build_request(2)
    assert body.count("<GetMyeBaySellingRequest") == 1 and "<ActiveList>" in body
    assert "<PageNumber>2</PageNumber>" in body and "<EntriesPerPage>200</EntriesPerPage>" in body
    for verb in ("Add", "Revise", "Relist", "End", "Verify"):
        assert f"<{verb}" not in body


# ── Fetching: pagination, headers, call counting ────────────────────────

def test_snapshot_paginates_counts_calls_and_sends_only_getmyebayselling(configured, conn):
    calls = []
    pages = [page_xml([item_xml("111", MOTOR, "24.99")], pages=3),
             page_xml([item_xml("222", PULL, "19.99")], pages=3),
             page_xml([item_xml("333", "Audi A3 8P Glow Plug Relay 038 906 281 D", "5.99"),
                       item_xml("222", PULL, "19.99")], pages=3)]  # 222 shifted pages: counted once
    s = run_snapshot(conn, pages, DAY1, calls)
    assert s["items"] == 3 and s["pages"] == 3 and s["api_calls"] == 3 and s["new"] == 3
    assert len(calls) == 3
    for req in calls:
        assert req.url == httpx.URL(ebay_orders.API_URL)
        assert req.headers["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling"
        assert req.headers["X-EBAY-API-IAF-TOKEN"] == TOKEN and req.headers["X-EBAY-API-SITEID"] == "3"
        assert req.content.decode().count("<GetMyeBaySellingRequest") == 1
    run = conn.execute("SELECT * FROM listing_snapshot_runs").fetchone()
    assert run["items"] == 3 and run["api_calls"] == 3 and run["pages"] == 3 and run["run_date"] == "2026-10-01"


def test_snapshot_counts_retries_as_calls(configured, conn):
    calls, served = [], []

    def handler(request):
        calls.append(request)
        if not served:
            served.append(1)
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=page_xml([item_xml("111", MOTOR, "24.99")]))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await listings.snapshot(conn, client=c, now=DAY1, sleep=_nosleep)
    s = asyncio.run(go())
    assert s["api_calls"] == 2 and s["pages"] == 1 and len(calls) == 2


def test_snapshot_stops_at_page_cap(configured, conn, monkeypatch):
    monkeypatch.setattr(listings, "MAX_PAGES", 2)
    pages = [page_xml([item_xml(str(i), MOTOR, "20")], pages=5) for i in range(5)]
    with pytest.raises(RuntimeError, match="refusing a partial snapshot"):
        run_snapshot(conn, pages, DAY1)
    assert conn.execute("SELECT COUNT(*) FROM listing_snapshots").fetchone()[0] == 0


def test_failed_page_writes_nothing(configured, conn):
    """A run that fails part-way must not record anything, or every listing on
    the missing pages would look ended."""
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY1)
    with pytest.raises(AuthError):
        run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")], pages=2), ERROR_XML], DAY2)
    assert conn.execute("SELECT COUNT(*) FROM listing_snapshot_runs").fetchone()[0] == 1
    assert rows(conn, "111")[0]["last_seen_date"] == "2026-10-01"


def test_missing_env_skips_cleanly(conn):
    s = asyncio.run(listings.snapshot(conn))
    assert s["skipped"] is True and set(s["missing"]) == set(ebay_orders.REQUIRED_ENV)
    assert conn.execute("SELECT COUNT(*) FROM listing_snapshot_runs").fetchone()[0] == 0


def test_token_never_in_output(configured, conn, capsys):
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY1)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and "GetMyeBaySelling: 1 API call(s)" in out.out


# ── Storing: unchanged days, price changes, first / last seen ────────────

def test_unchanged_day_moves_last_seen_only(configured, conn):
    page = [page_xml([item_xml("111", MOTOR, "24.99")])]
    run_snapshot(conn, page, DAY1)
    s = run_snapshot(conn, page, DAY2)
    assert s["unchanged"] == 1 and s["new"] == 0
    (r,) = rows(conn, "111")
    assert r["snapshot_date"] == "2026-10-01" and r["last_seen_date"] == "2026-10-02"


def test_price_change_adds_a_row(configured, conn):
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY1)
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY2)
    s = run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "19.99")])], DAY3)
    assert s["changed"] == 1
    r = rows(conn, "111")
    assert [(x["snapshot_date"], x["last_seen_date"], x["current_price"]) for x in r] == [
        ("2026-10-01", "2026-10-02", 24.99), ("2026-10-03", "2026-10-03", 19.99)]


def test_postage_change_is_a_price_change(configured, conn):
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99", ship="0.00")])], DAY1)
    s = run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99", ship="6.99")])], DAY2)
    assert s["changed"] == 1 and [x["current_price"] for x in rows(conn, "111")] == [24.99, 31.98]


def test_same_day_rerun_keeps_one_row_per_day(configured, conn):
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY1)
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY2)
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "22.99")])], DAY2)
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "21.99")])], DAY2.replace(hour=18))
    r = rows(conn, "111")
    assert [(x["snapshot_date"], x["current_price"]) for x in r] == [("2026-10-01", 24.99), ("2026-10-02", 21.99)]


def test_gone_items_counted(configured, conn):
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99"), item_xml("222", PULL, "19.99")])], DAY1)
    s = run_snapshot(conn, [page_xml([item_xml("222", PULL, "19.99")])], DAY2)
    assert s["gone"] == 1 and rows(conn, "111")[0]["last_seen_date"] == "2026-10-01"


def test_dry_run_writes_nothing(configured, conn):
    s = run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "24.99")])], DAY1, dry_run=True)
    assert s["dry_run"] is True and s["new"] == 1
    assert conn.execute("SELECT COUNT(*) FROM listing_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM listing_snapshot_runs").fetchone()[0] == 0


# ── History: price changes, days listed, sold / ended unsold ────────────

from pricing.sales_import import import_rows  # noqa: E402

_n = iter(range(1, 10_000))


def sale(conn, item_id, title, price, day, postage="0"):
    i = next(_n)
    import_rows(conn, [{"order_number": f"L-{i}", "sales_record_number": str(i), "item_number": item_id,
                        "title": title, "custom_label": "", "quantity": "1", "sold_for": str(price),
                        "postage": postage, "total_price": "", "sale_date": day, "dispatched_date": ""}], "csv_clean")


def three_days(conn, day1_items, day2_items, day3_items):
    run_snapshot(conn, [page_xml(day1_items)], DAY1)
    run_snapshot(conn, [page_xml(day2_items)], DAY2)
    run_snapshot(conn, [page_xml(day3_items)], DAY3)


def test_history_price_changes_and_days_listed(configured, conn):
    three_days(conn, [item_xml("111", MOTOR, "29.99", start="2026-09-21T10:00:00.000Z")],
               [item_xml("111", MOTOR, "24.99", start="2026-09-21T10:00:00.000Z")],
               [item_xml("111", MOTOR, "27.99", start="2026-09-21T10:00:00.000Z")])
    h = listings.listing_history(conn, today=DAY3.date())["111"]
    assert h["first_price"] == 29.99 and h["current_price"] == 27.99
    assert h["price_changes"] == [{"date": "2026-10-02", "from": 29.99, "to": 24.99},
                                  {"date": "2026-10-03", "from": 24.99, "to": 27.99}]
    assert h["outcome"] == "active" and h["start_date"] == "2026-09-21" and h["days_listed"] == 12
    assert h["first_seen"] == "2026-10-01" and h["last_seen"] == "2026-10-03"


def test_title_only_change_is_not_a_price_change(configured, conn):
    three_days(conn, [item_xml("111", MOTOR, "29.99")], [item_xml("111", MOTOR + " Genuine", "29.99")],
               [item_xml("111", MOTOR + " Genuine", "29.99")])
    h = listings.listing_history(conn, today=DAY3.date())["111"]
    assert h["price_changes"] == [] and h["title"].endswith("Genuine")
    assert len(rows(conn, "111")) == 2


def test_ended_unsold_vs_sold(configured, conn):
    start = "2026-09-25T10:00:00.000Z"
    both = [item_xml("111", MOTOR, "29.99", start=start), item_xml("222", PULL, "19.99", start=start)]
    three_days(conn, both, both, [])  # both gone on day 3
    sale(conn, "222", PULL, 17.00, "2026-10-02", postage="0")  # 222 sold (Best Offer below 19.99)
    h = listings.listing_history(conn, today=DAY3.date())
    assert h["111"]["outcome"] == "ended_unsold" and h["111"]["sold_price"] is None
    assert h["111"]["days_listed"] == 7  # 25 Sep -> last seen 2 Oct
    s = h["222"]
    assert s["outcome"] == "sold" and s["sold_price"] == 17.0 and s["sold_date"] == "2026-10-02"
    assert s["days_to_sell"] == 7 and s["listed_price_at_sale"] == 19.99


def test_sale_imported_late_turns_ended_into_sold(configured, conn):
    three_days(conn, [item_xml("111", MOTOR, "29.99")], [], [])
    assert listings.listing_history(conn)["111"]["outcome"] == "ended_unsold"
    sale(conn, "111", MOTOR, 29.99, "2026-10-01")
    assert listings.listing_history(conn)["111"]["outcome"] == "sold"


def test_sale_of_same_item_before_listing_start_is_ignored(configured, conn):
    """An older sale under the same item number (before this listing started)
    isn't this listing's sale."""
    sale(conn, "111", MOTOR, 29.99, "2026-09-01")
    run_snapshot(conn, [page_xml([item_xml("111", MOTOR, "29.99", start="2026-09-20T10:00:00.000Z")])], DAY1)
    run_snapshot(conn, [page_xml([])], DAY2)
    assert listings.listing_history(conn)["111"]["outcome"] == "ended_unsold"


def test_sold_price_compared_at_the_price_in_force_on_sale_day(configured, conn):
    three_days(conn, [item_xml("111", MOTOR, "29.99")], [item_xml("111", MOTOR, "24.99")], [])
    sale(conn, "111", MOTOR, 24.99, "2026-10-02")
    h = listings.listing_history(conn)["111"]
    assert h["listed_price_at_sale"] == 24.99 and h["first_price"] == 29.99


# ── Suggestion <-> listing linking ──────────────────────────────────────

def suggestion(conn, pn, suggested, ts, final=None, current=None, mode="shadow"):
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, part_type, suggested, confidence, source, "
        "reason, mode, final_price, current_price) VALUES (?, ?, 'WINDOW MOTOR', ?, 'high', 'own_sold', 'r', ?, ?, ?)",
        (ts, pn, suggested, mode, final, current))
    conn.commit()
    return cur.lastrowid


def linked(conn, sid):
    return conn.execute("SELECT listing_item_id FROM price_suggestions WHERE id = ?", (sid,)).fetchone()[0]


def test_link_nearest_listing_on_or_after_suggestion(configured, conn):
    s1 = suggestion(conn, "5Q0959802A", 30, "2026-09-20T09:00:00+00:00")
    s2 = suggestion(conn, "5Q0959802A", 30, "2026-09-24T09:00:00+00:00")
    other = suggestion(conn, "3C2823533", 20, "2026-09-20T09:00:00+00:00")
    run_snapshot(conn, [page_xml([
        item_xml("old", MOTOR, "29.99", start="2026-09-10T10:00:00.000Z"),   # before both: never linked
        item_xml("near", MOTOR, "29.99", start="2026-09-21T10:00:00.000Z"),
        item_xml("far", MOTOR, "29.99", start="2026-09-26T10:00:00.000Z"),
    ])], DAY1)
    assert linked(conn, s1) == "near"   # nearest after s1
    assert linked(conn, s2) == "far"    # "near" started before s2 (and is taken)
    assert linked(conn, other) is None  # different part number


def test_link_same_day_and_one_to_one(configured, conn):
    s1 = suggestion(conn, "5Q0959802A", 30, "2026-09-21T15:00:00+00:00")
    s2 = suggestion(conn, "5Q0959802A", 30, "2026-09-21T16:00:00+00:00")
    run_snapshot(conn, [page_xml([item_xml("a", MOTOR, "29.99", start="2026-09-21T08:00:00.000Z")])], DAY1)
    assert linked(conn, s1) == "a" and linked(conn, s2) is None
    run_snapshot(conn, [page_xml([item_xml("a", MOTOR, "29.99", start="2026-09-21T08:00:00.000Z"),
                                  item_xml("b", MOTOR, "29.99", start="2026-09-30T08:00:00.000Z")])], DAY2)
    assert linked(conn, s1) == "a" and linked(conn, s2) == "b"   # existing link never moves


def test_link_window(configured, conn):
    sid = suggestion(conn, "5Q0959802A", 30, "2026-07-01T09:00:00+00:00")
    run_snapshot(conn, [page_xml([item_xml("late", MOTOR, "29.99", start="2026-09-21T08:00:00.000Z")])], DAY1)
    assert linked(conn, sid) is None  # 82 days later: outside the 60-day window
    assert listings.link_listings(conn, window_days=90) == 1 and linked(conn, sid) == "late"
