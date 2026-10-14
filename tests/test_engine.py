from datetime import date, timedelta

import pytest

import pricing_config as cfg
from pricing import engine
from pricing.engine import recency_weight, round_price, suggest, weighted_median
from pricing.sales_import import import_rows

AS_OF = date(2026, 10, 14)
_seq = iter(range(1, 10_000))


def add_sales(conn, title, prices, days_ago=30):
    rows = []
    for p in prices:
        i = next(_seq)
        rows.append({"order_number": f"E-{i}", "sales_record_number": str(i), "item_number": str(i),
                     "title": title, "custom_label": "", "quantity": "1", "sold_for": str(p),
                     "postage": "0", "total_price": "",
                     "sale_date": (AS_OF - timedelta(days=days_ago)).isoformat(), "dispatched_date": ""})
    import_rows(conn, rows, "csv_clean")


def mkt(count, lq, median, lo=None, hi=None):
    return {"count": count, "lq": lq, "median": median, "min": lo or lq, "max": hi or median * 1.2}


GOLF_WM = "Volkswagen Golf Mk7 2014 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 A"


# ── Rounding / maths ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (24.40, 23.99), (24.60, 24.99), (24.99, 24.99), (25.00, 24.99), (9.2, 8.99),
    (49.70, 49.99), (50.20, 49.99), (87.0, 84.99), (88.0, 89.99), (84.99, 84.99),
    (212.0, 209.99), (0.30, 0.99),
])
def test_rounding_rule(raw, expected):
    assert round_price(raw) == expected


def test_rounding_none(monkeypatch):
    monkeypatch.setattr(cfg, "ROUNDING_RULE", "none")
    assert round_price(24.444) == 24.44


def test_weighted_median_and_recency():
    assert weighted_median([(10, 1), (20, 1), (30, 1)]) == 20
    assert weighted_median([(10, 1), (20, 1)]) == 15
    assert weighted_median([(10, 0.1), (100, 5)]) == 100
    assert recency_weight(AS_OF.isoformat(), AS_OF) == 1
    assert recency_weight((AS_OF - timedelta(days=cfg.OWN_RECENCY_HALF_LIFE_DAYS)).isoformat(), AS_OF) == pytest.approx(0.5)


# ── Blend branches ──────────────────────────────────────────────────────

def test_own_three_plus_sales_high(conn):
    add_sales(conn, GOLF_WM, [30, 32, 34, 36])
    r = suggest(conn, "5Q0 959 802 A", as_of=AS_OF)
    assert r["source"] == "own_sold" and r["match"] == "exact" and r["confidence"] == "high"
    assert r["suggested_price"] == 32.99  # weighted median 33 -> nearest .99
    assert r["own"]["count"] == 4 and r["own"]["last_sold"] == "2026-09-14"
    assert r["reason"].startswith("4 of our sales (median £33")
    assert r["check_price"] is False


def test_own_three_plus_with_close_market_no_nudge(conn):
    add_sales(conn, GOLF_WM, [30, 32, 34])
    r = suggest(conn, "5Q0959802A", market=mkt(8, 29, 33, 27, 40), as_of=AS_OF)
    assert r["source"] == "own_sold" and r["suggested_price"] == 31.99
    assert "8 live competitors (£27–£40)" in r["reason"]


def test_own_three_plus_divergent_market_nudged(conn):
    add_sales(conn, GOLF_WM, [40, 40, 40])
    r = suggest(conn, "5Q0959802A", market=mkt(6, 60, 70), as_of=AS_OF)
    # market median 70 is 75% above own 40 -> 40 + 0.3 * (65 - 40) = 47.5
    assert r["source"] == "own_sold+market"
    assert r["raw_price"] == 47.5 and r["suggested_price"] == 47.99
    assert "market 75% higher, nudged 30% toward it" in r["reason"]
    assert r["confidence"] == "high"


def test_one_or_two_own_sales_blend_50_50(conn):
    add_sales(conn, GOLF_WM, [40])
    r = suggest(conn, "5Q0959802A", market=mkt(4, 50, 60), as_of=AS_OF)
    # (40 + (50 + 60) / 2) / 2 = 47.5
    assert r["raw_price"] == 47.5 and r["source"] == "own_sold+market"
    assert r["confidence"] == "medium"  # only 4 comps -> not high


def test_one_or_two_own_sales_tight_big_market_is_high(conn):
    add_sales(conn, GOLF_WM, [40, 44])
    r = suggest(conn, "5Q0959802A", market=mkt(9, 41, 45), as_of=AS_OF)
    assert r["confidence"] == "high"


def test_one_or_two_own_sales_no_market(conn):
    add_sales(conn, GOLF_WM, [40, 50])
    r = suggest(conn, "5Q0959802A", market=mkt(2, 10, 12), as_of=AS_OF)
    assert r["source"] == "own_sold" and r["confidence"] == "medium"
    assert r["raw_price"] == 45


def test_market_only(conn):
    r = suggest(conn, "1K0959653D", market=mkt(5, 20, 24, 18, 30), as_of=AS_OF)
    assert r["source"] == "market" and r["match"] == "market"
    assert r["raw_price"] == 22 and r["suggested_price"] == 21.99
    assert r["confidence"] == "high"  # 5 comps, spread (24-20)/24 = 17%
    assert r["own"] is None and r["market"]["count"] == 5


def test_market_only_loose_spread_medium(conn):
    r = suggest(conn, "1K0959653D", market=mkt(7, 10, 40), as_of=AS_OF)
    assert r["confidence"] == "medium"


def test_base_number_match_is_medium(conn):
    add_sales(conn, "Volkswagen Golf Mk7 2014 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 B", [30, 31, 32])
    r = suggest(conn, "5Q0959802A", as_of=AS_OF)
    assert r["match"] == "base" and r["confidence"] == "medium"
    assert "index letter differs" in r["reason"]


def test_exact_preferred_over_base_when_enough(conn):
    add_sales(conn, GOLF_WM, [30, 30, 30])
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR 5Q0 959 802 B", [90, 90, 90])
    r = suggest(conn, "5Q0959802A", as_of=AS_OF)
    assert r["match"] == "exact" and r["raw_price"] == 30


def test_sales_outside_lookback_ignored(conn):
    add_sales(conn, GOLF_WM, [30, 30, 30], days_ago=cfg.OWN_LOOKBACK_DAYS + 5)
    r = suggest(conn, "5Q0959802A", car={"model": "GOLF", "generation": "MK7"}, as_of=AS_OF)
    assert r["own"] is None


def test_no_peeking_at_same_day_sale(conn):
    add_sales(conn, GOLF_WM, [30, 30, 30], days_ago=0)
    r = suggest(conn, "5Q0959802A", as_of=AS_OF)
    assert r["own"] is None


# ── Fallbacks ───────────────────────────────────────────────────────────

def test_part_type_fallback_medium_with_five(conn):
    for i, p in enumerate([20, 22, 24, 26, 28]):
        add_sales(conn, f"Volkswagen Golf Mk7 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 80{i} C", [p])
    r = suggest(conn, "5G0 959 999", part_type="WINDOW MOTOR (FRONT DRIVER SIDE)",
                car={"model": "GOLF", "generation": "MK7"}, as_of=AS_OF)
    assert r["source"] == "part_type" and r["match"] == "part_type"
    assert r["raw_price"] == 24 and r["confidence"] == "medium"
    assert "5 of our Golf MK7 window motor front sales" in r["reason"]


def test_part_type_fallback_low_with_two(conn):
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 801 C", [20])
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 803 C", [30])
    r = suggest(conn, "5G0959999", part_type="WINDOW MOTOR FRONT", car={"model": "GOLF", "generation": "MK7"},
                as_of=AS_OF)
    assert r["source"] == "part_type" and r["confidence"] == "low" and r["check_price"] is True
    assert r["reason"].endswith("check price")


def test_no_cross_model_breakerpro_group_average(conn):
    # 723523 (accelerator pedal) has an all-models avg_price (9.99) in
    # vag_parts_db.json. That cross-model average must never be used as a price.
    r = suggest(conn, "9Z9 723 523 X", as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["confidence"] == "low"
    assert r["suggested_price"] is None and r["raw_price"] is None
    assert "BreakerPro" not in r["reason"]


def test_no_category_defaults(conn):
    assert not hasattr(cfg, "CATEGORY_DEFAULTS")  # pinned per-type prices are gone
    r = suggest(conn, "9Z9 111 111", part_type="widget thing", as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["suggested_price"] is None


def test_no_cross_model_part_type_borrowing(conn):
    # Plenty of A3 8V washer-bottle sales must not price a Polo 6R washer bottle.
    add_sales(conn, "Audi A3 8V WASHER BOTTLE", [14, 16, 18])
    r = suggest(conn, "9Z9 111 112", part_type="Washer Bottle", car={"model": "POLO", "generation": "6R"},
                as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["suggested_price"] is None
    assert "any model" not in r["reason"]
    assert "fewer than 2 Polo 6R washer bottle sales, so no price is suggested" in r["reason"]


def test_no_cross_generation_part_type_borrowing(conn):
    add_sales(conn, "Volkswagen Golf Mk6 WINDOW MOTOR (FRONT) 5K0 959 801 C", [20])
    add_sales(conn, "Volkswagen Golf Mk6 WINDOW MOTOR (FRONT) 5K0 959 803 C", [30])
    r = suggest(conn, "5G0959999", part_type="WINDOW MOTOR FRONT", car={"model": "GOLF", "generation": "MK7"},
                as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["suggested_price"] is None  # Mk6 sales never price a Mk7


def test_part_type_needs_generation(conn):
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 801 C", [20])
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 803 C", [30])
    r = suggest(conn, "5G0959999", part_type="WINDOW MOTOR FRONT", car={"model": "GOLF"}, as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["suggested_price"] is None
    assert "no vehicle model/generation" in r["reason"]


def test_no_flat_default_without_evidence(conn):
    r = suggest(conn, "9Z9 111 113", as_of=AS_OF)
    assert r["source"] == "no_evidence" and r["suggested_price"] is None and r["raw_price"] is None
    assert r["confidence"] == "low" and r["check_price"] is True
    assert r["reason"].endswith("check price")
    assert not hasattr(cfg, "DEFAULT_PRICE")  # the flat default is gone, not just unused


def test_thin_market_mentioned_in_fallback(conn):
    r = suggest(conn, "9Z9 111 113", market=mkt(2, 10, 12), as_of=AS_OF)
    assert "only 2 live competitors" in r["reason"]


# ── Floor ───────────────────────────────────────────────────────────────

def test_floor_applied(conn):
    add_sales(conn, GOLF_WM, [3, 4, 5])
    r = suggest(conn, "5Q0959802A", as_of=AS_OF)
    assert r["suggested_price"] == cfg.MIN_LIST_PRICE
    assert "minimum" in r["reason"]


def test_floor_per_part_type(conn, monkeypatch):
    monkeypatch.setitem(cfg.MIN_LIST_PRICE_BY_TYPE, "WINDOW MOTOR FRONT", 39.99)
    add_sales(conn, GOLF_WM, [30, 30, 30])
    assert suggest(conn, "5Q0959802A", as_of=AS_OF)["suggested_price"] == 39.99


# ── Endpoint ────────────────────────────────────────────────────────────

def test_suggest_endpoint_shape(client, conn):
    add_sales(conn, GOLF_WM, [30, 32, 34, 36])
    r = client.get("/price/suggest", params={"part_number": "5q0 959 802 a"})
    assert r.status_code == 200
    body = r.json()
    for key in ("suggested_price", "confidence", "reason", "own", "market", "match", "source"):
        assert key in body
    assert body["confidence"] == "high" and body["market"] is None  # no eBay keys in tests


def test_suggest_endpoint_uses_vehicle_ref(client, conn):
    client.post("/vehicles", json={"ref": "VEH900", "make": "Volkswagen", "model": "Golf Mk7", "engine_code": "CLHA"})
    for t, p in (("Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 801 C", 20),
                 ("Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 803 C", 30)):
        add_sales(conn, t, [p])
    r = client.get("/price/suggest", params={"part_number": "5G0959999", "vehicle_ref": "900",
                                             "part_type": "Window Motor Front"}).json()
    assert r["source"] == "part_type" and r["raw_price"] == 25


def test_suggest_endpoint_rejects_junk(client):
    assert client.get("/price/suggest", params={"part_number": "hello"}).status_code == 400


def test_part_type_prefers_what_we_sold_over_description(conn):
    # /lookup would say "WINDOW MOTOR OS"; our titles index it as "WINDOW MOTOR FRONT".
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 A", [30])
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 B", [34])
    r = suggest(conn, "5G0 959 802 F", part_type="WINDOW MOTOR OS",
                car={"model": "GOLF", "generation": "MK7"}, as_of=AS_OF)
    assert r["part_type"] == "WINDOW MOTOR FRONT"
    assert r["source"] == "part_type" and r["raw_price"] == 32


def test_reason_only_mentions_live_listings_when_market_checked(conn):
    assert suggest(conn, "9Z9 111 113", as_of=AS_OF)["reason"].startswith("no sales of this number")
    r = suggest(conn, "9Z9 111 113", market={"count": 0, "lq": None, "median": None, "min": None, "max": None},
                as_of=AS_OF)
    assert r["reason"].startswith("no sales or usable live listings")


def test_part_type_resolution_does_not_peek(conn):
    add_sales(conn, "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT) 5Q0 959 802 A", [30], days_ago=0)
    r = suggest(conn, "5G0 959 802 F", as_of=AS_OF)  # the only evidence is on as_of itself
    assert r["part_type"] != "WINDOW MOTOR FRONT"


# ── Variant-sensitive categories ────────────────────────────────────────

@pytest.mark.parametrize("ptype,expected", [
    ("HEADLIGHT", True), ("REAR LIGHT ON BODY", True), ("AIRBAG MODULE", True), ("ENGINE ECU", True),
    ("CENTRAL CONTROL UNIT", True), ("SPEEDO CLUSTER", True), ("STEERING WHEEL", True),
    ("INFOTAINMENT SCREEN", True), ("SAT NAV UNIT", True),
    ("HEADLIGHT SWITCH", False), ("STEERING WHEEL TRIM", False), ("REAR LIGHT BULB HOLDER", False),
    ("WINDOW MOTOR FRONT", False), ("AIRBAG SQUIB/SLIP RING", False), ("", False),
])
def test_is_variant_sensitive(ptype, expected):
    assert engine.is_variant_sensitive(ptype) is expected


def test_variant_sensitive_fallback_gives_no_price_only_range(conn):
    add_sales(conn, "Volkswagen Golf Mk7 HEADLIGHT (DRIVER SIDE) 5G1 941 006", [80])
    add_sales(conn, "Volkswagen Golf Mk7 HEADLIGHT (PASSENGER SIDE) 5G1 941 005", [150])
    add_sales(conn, "Volkswagen Golf Mk6 HEADLIGHT (PASSENGER SIDE) 5K1 941 005", [40])  # other gen: excluded
    r = suggest(conn, "5G1 941 999 B", part_type="Headlight", car={"model": "GOLF", "generation": "MK7"},
                as_of=AS_OF)
    assert r["suggested_price"] is None and r["raw_price"] is None
    assert r["confidence"] == "low" and r["check_price"] is True and r["source"] == "variant_check"
    assert r["group_range"] == {"count": 2, "min": 80.0, "max": 150.0, "median": 115.0, "group": "Golf MK7 HEADLIGHT"}
    assert "reference only" in r["reason"] and r["reason"].endswith("check price")


def test_variant_sensitive_without_group(conn):
    r = suggest(conn, "5G1 941 999 B", part_type="Headlight", car={"model": "GOLF", "generation": "MK7"},
                as_of=AS_OF)
    assert r["suggested_price"] is None and r["group_range"] is None
    assert "no Golf MK7 headlight sales to compare against" in r["reason"]


def test_variant_sensitive_still_priced_from_exact_sales(conn):
    add_sales(conn, "Volkswagen Golf Mk7 HEADLIGHT (DRIVER SIDE) 5G1 941 006", [80, 85, 90])
    r = suggest(conn, "5G1 941 006", as_of=AS_OF)
    assert r["source"] == "own_sold" and r["suggested_price"] == 84.99


def test_variant_sensitive_still_priced_from_market(conn):
    r = suggest(conn, "5G1 941 999 B", part_type="Headlight", market=mkt(6, 90, 100), as_of=AS_OF)
    assert r["source"] == "market" and r["suggested_price"] is not None


def test_variant_list_is_configurable(conn, monkeypatch):
    assert suggest(conn, "9Z9 111 112", part_type="Parcel Shelf", as_of=AS_OF)["source"] == "no_evidence"
    monkeypatch.setattr(cfg, "VARIANT_SENSITIVE_PART_TYPES", cfg.VARIANT_SENSITIVE_PART_TYPES + ("PARCEL SHELF",))
    r = suggest(conn, "9Z9 111 112", part_type="Parcel Shelf", as_of=AS_OF)
    assert r["suggested_price"] is None and r["source"] == "variant_check"


def test_suggest_endpoint_no_price_shape(client):
    r = client.get("/price/suggest", params={"part_number": "5G1941999B", "part_type": "Headlight"}).json()
    assert r["suggested_price"] is None and r["confidence"] == "low" and "group_range" in r


def test_suggest_endpoint_no_evidence_shape(client):
    # What the live logger receives when there's no real evidence: no price,
    # so it keeps the current-method price and shows Low / Check price.
    r = client.get("/price/suggest", params={"part_number": "9Z9111113"}).json()
    assert r["suggested_price"] is None and r["source"] == "no_evidence"
    assert r["confidence"] == "low" and r["check_price"] is True and r["suggestion_id"] is not None
