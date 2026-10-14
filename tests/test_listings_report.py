"""/price/report with real eBay listings (listing_snapshots). Hand-built
snapshot rows; no network.

Scenario (today = 31 Dec 2026):
  A  5Q0959802A  engine 30, current 25, logger exported 30.
     Listed 2 Sep at 34.99 (re-priced UP, away from the engine), Best Offer on,
     sold 12 Sep for 32.00 -> 10 days, 8.5% below the listed price.
  B  3C2823533   engine 20, current 15, logger exported 15.
     Listed 3 Sep at 15, raised to 22 on 10 Oct (toward the engine), still
     listed: 119 days unsold, 10% above the engine.
  C  1K0905851   engine 60, no export. Listed 5 Sep at 59.99, gone after
     20 Sep with no sale -> ended unsold.
  D  6R0905851B  logger exported 44.99, never seen on eBay.
  U1 1K0951605C  a listing with no suggestion: sold at its listed price.
"""

import csv
import io
from datetime import date

import pytest

from pricing import feedback, listings
from pricing.sales_import import import_rows

TODAY = date(2026, 12, 31)
_n = iter(range(1, 10_000))


def sale(conn, item_id, title, price, day):
    i = next(_n)
    import_rows(conn, [{"order_number": f"R-{i}", "sales_record_number": str(i), "item_number": item_id,
                        "title": title, "custom_label": "", "quantity": "1", "sold_for": str(price),
                        "postage": "0", "total_price": "", "sale_date": day, "dispatched_date": ""}], "csv_clean")


def state(conn, item_id, title, pn, price, start, first, last, best_offer=0):
    conn.execute(
        "INSERT INTO listing_snapshots (item_id, title, part_number_norm, current_price, item_price, shipping, "
        "start_time, snapshot_date, last_seen_date, best_offer_enabled) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (item_id, title, pn, price, price, f"{start}T09:00:00.000Z", first, last, best_offer))


def run(conn, day):
    conn.execute("INSERT INTO listing_snapshot_runs (run_date, started_at, finished_at, items, pages, api_calls, "
                 "new_items, changed, unchanged, gone) VALUES (?, ?, ?, 3, 1, 1, 0, 0, 3, 0)",
                 (day, f"{day}T02:00:00+00:00", f"{day}T02:00:05+00:00"))


def suggestion(conn, pn, engine, current=None, final=None, ts="2026-09-01T10:00:00+00:00"):
    conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, part_type, suggested, confidence, source, "
        "reason, mode, current_price, final_price) VALUES (?, ?, 'PART', ?, 'high', 'own_sold', 'r', 'shadow', ?, ?)",
        (ts, pn, engine, current, final))


@pytest.fixture
def scenario(conn):
    suggestion(conn, "5Q0959802A", 30, current=25, final=30)
    suggestion(conn, "3C2823533", 20, current=15, final=15)
    suggestion(conn, "1K0905851", 60)
    suggestion(conn, "6R0905851B", 45, current=44.99, final=44.99)
    state(conn, "A1", "Golf Window Motor 5Q0 959 802 A", "5Q0959802A", 34.99, "2026-09-02",
          "2026-09-02", "2026-09-11", best_offer=1)
    state(conn, "B1", "Polo Bonnet Pull 3C2 823 533", "3C2823533", 15, "2026-09-03", "2026-09-03", "2026-10-09")
    state(conn, "B1", "Polo Bonnet Pull 3C2 823 533", "3C2823533", 22, "2026-09-03", "2026-10-10", "2026-12-31")
    state(conn, "C1", "Octavia Ignition Barrel 1K0 905 851", "1K0905851", 59.99, "2026-09-05",
          "2026-09-05", "2026-09-20")
    state(conn, "U1", "Fabia Alarm Siren 1K0 951 605 C", "1K0951605C", 17.99, "2026-08-01",
          "2026-08-01", "2026-08-14")
    for day in ("2026-09-20", "2026-12-30", "2026-12-31"):
        run(conn, day)
    conn.commit()
    sale(conn, "A1", "Golf Window Motor 5Q0 959 802 A", 32.00, "2026-09-12")
    sale(conn, "U1", "Fabia Alarm Siren 1K0 951 605 C", 17.99, "2026-08-15")
    return conn


def test_listing_history_outcomes(scenario):
    h = listings.listing_history(scenario, TODAY)
    assert {k: v["outcome"] for k, v in h.items()} == {"A1": "sold", "B1": "active", "C1": "ended_unsold",
                                                        "U1": "sold"}
    assert h["B1"]["days_listed"] == 119 and h["C1"]["days_listed"] == 15 and h["A1"]["days_to_sell"] == 10


def test_report_uses_real_listed_price(scenario):
    rep = feedback.report(scenario, today=TODAY)
    assert rep["listing"]["listed_price_source"] == {"ebay": 3, "logger_export": 1}
    # the "final" (listed) price judged against the sale is eBay's 34.99, not the exported 30
    assert rep["after_sale"]["final"] == {"n": 1, "median_abs_pct_error": 9.3, "pct_within_15": 100.0,
                                          "median_bias_pct": 9.3}
    (item,) = rep["sold_items"]
    assert item["final_price"] == 34.99 and item["logger_final_price"] == 30
    assert item["listed_price_source"] == "ebay" and item["listing_item_id"] == "A1"
    assert item["sold_price"] == 32.0 and item["days_to_sell"] == 10


def test_report_ebay_listings_section(scenario):
    e = feedback.report(scenario, today=TODAY)["ebay_listings"]
    assert e["snapshots"]["runs"] == 3 and e["snapshots"]["last"]["run_date"] == "2026-12-31"
    assert e["tracked_listings"] == {"total": 4, "active": 1, "sold": 2, "ended_unsold": 1, "with_price_change": 1}
    assert e["linked_suggestions"] == 3

    vs = e["vs_sold_price"]
    assert vs["n_sold"] == 1 and vs["median_days_to_sell"] == 10
    assert vs["current"]["median_abs_pct_error"] == 21.9      # 25 vs 32
    assert vs["engine"]["median_abs_pct_error"] == 6.2        # 30 vs 32
    assert vs["first_listed"]["median_abs_pct_error"] == 9.3  # 34.99 vs 32

    at_listing = e["repriced_after_logging"]["logger_to_first_listed"]
    assert at_listing == {"compared": 2, "repriced": 1, "repriced_pct": 50.0, "up": 1, "down": 0,
                          "median_change_pct": 16.6, "toward_engine": 0, "away_from_engine": 1,
                          "no_engine_price": 0}
    listed = e["repriced_after_logging"]["while_listed"]
    assert listed == {"compared": 3, "repriced": 1, "repriced_pct": 33.3, "up": 1, "down": 0,
                      "median_change_pct": 46.7, "toward_engine": 1, "away_from_engine": 0, "no_engine_price": 0}

    assert e["first_listed_vs_engine"] == {"above_engine": 1, "at_engine": 1, "below_engine": 1}

    st = e["sell_through"]
    assert st["all"] == {"listings": 3, "eligible_14d": 3, "sold_within_14d_pct": 33.3, "eligible_30d": 3,
                         "sold_within_30d_pct": 33.3, "eligible_60d": 3, "sold_within_60d_pct": 33.3}
    assert st["above_engine"]["sold_within_14d_pct"] == 100.0   # A
    assert st["below_engine"]["sold_within_60d_pct"] == 0.0     # B
    assert st["at_engine"]["sold_within_60d_pct"] == 0.0        # C
    assert st["no_engine_price"]["listings"] == 0

    assert e["unsold_over_60_days"] == {"count": 1, "still_listed": 1, "ended": 0, "with_engine_price": 1,
                                        "median_pct_vs_engine": 10.0, "above_engine": 1, "at_engine": 0,
                                        "below_engine": 0}
    assert e["best_offer"] == {"sold": 2, "sold_below_listed": 1, "sold_below_listed_pct": 50.0,
                               "median_discount_pct": 8.5, "best_offer_enabled_listings": 1}


def test_sell_through_waits_until_listing_old_enough(scenario):
    st = feedback.report(scenario, today=date(2026, 9, 30))["ebay_listings"]["sell_through"]["all"]
    # on 30 Sep: A (28 days), B (27), C (25) count for 14 days, none yet for 30 or 60
    assert st["eligible_14d"] == 3 and st["eligible_30d"] == 0 and st["sold_within_30d_pct"] is None


def test_report_csv_has_listing_columns(scenario):
    rows = list(csv.DictReader(io.StringIO(feedback.report_csv(scenario, today=TODAY))))
    a = next(r for r in rows if r["part_number"] == "5Q0959802A")
    assert a["final_price"] == "30.0" and a["listed_price"] == "34.99" and a["listed_price_source"] == "ebay"
    assert a["listing_item_id"] == "A1" and a["listing_outcome"] == "sold" and a["sold_price"] == "32.0"
    assert a["final_abs_pct_error"] == "6.2" and a["listed_abs_pct_error"] == "9.3"
    b = next(r for r in rows if r["part_number"] == "3C2823533")
    assert b["ebay_first_listed_price"] == "15.0" and b["ebay_current_price"] == "22.0"
    assert b["ebay_price_changes"] == "1" and b["listing_days_listed"] == "119"
    d = next(r for r in rows if r["part_number"] == "6R0905851B")
    assert d["listed_price"] == "44.99" and d["listed_price_source"] == "logger_export" and d["listing_item_id"] == ""


def test_report_endpoint_with_no_listings(client):
    e = client.get("/price/report").json()["ebay_listings"]
    assert e["tracked_listings"]["total"] == 0 and e["linked_suggestions"] == 0
    assert e["snapshots"] == {"runs": 0, "last": None}
    assert e["best_offer"]["median_discount_pct"] is None
