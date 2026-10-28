"""Pick list: unit tests on fixtures, plus integration / sanity / idempotency
checks against the local analytics store (skipped when it is not built)."""

import csv
import io
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts")]
import pick_list as pl  # noqa: E402

TODAY = date(2026, 10, 28)


@pytest.fixture(scope="module")
def rules():
    r = pl.load_rules()
    r["always_pull"] = set()
    return r


def ctx_fixture(rules, **over):
    """A one-car context with no real data: every number set by the test."""
    ctx = dict(
        today=TODAY, rules=rules,
        labels={"headlight": "Headlight", "airbag_steering": "Steering wheel airbag", "bonnet": "Bonnet",
                "tail_light": "Tail light"},
        level_of={"headlight": "generation", "airbag_steering": "generation", "bonnet": "generation",
                  "tail_light": "generation"},
        fam_plat={"VW Golf MK7": "MQB"}, never={"airbag_steering"},
        vehicles={"VEH1": dict(veh_ref="VEH1", model_family="VW Golf MK7", generation="MK7", year="2015",
                               engine_code="CXXA123 / CXXA", transmission="MANUAL", on_site_date="2026-10-20",
                               has_vehicle_row=1, model_method="vin")},
        shelf={}, pos={}, dont_pull=set(), courier={}, trade=Counter(), effort={}, engine_prices={},
        sold12=Counter(), avail={}, damage={})
    ctx.update(over)
    return ctx


def line(**kw):
    base = dict(part_type="headlight", non_ebay=False, trade_sales=0, effective_qty=0, dont_pull=False, expected_price=80.0,
                net_value=40.0, value_per_min=2.0, shortfall=1.0)
    base.update(kw)
    return base


# --- shortfall maths -----------------------------------------------------------

def test_effective_qty_weights(rules):
    r = dict(rules, unlocated_weight=0.5, phantom_weight=0)
    assert pl.stock_position(1, 1, 3, r) == 1.5
    assert pl.shortfall(2, pl.stock_position(1, 1, 3, r)) == 0.5
    assert pl.shortfall(2, pl.stock_position(0, 0, 5, r)) == 2      # phantoms count as nothing
    assert pl.shortfall(2, pl.stock_position(3, 0, 0, r)) == 0      # never negative
    r2 = dict(r, phantom_weight=0.25)
    assert pl.stock_position(0, 2, 4, r2) == 2.0


def test_parts_from_other_cars_count_as_located(rules):
    r = dict(rules, unlocated_weight=0.5, phantom_weight=0)
    assert pl.stock_position(0, 1, 0, r, from_other_cars=2) == 2.5


def test_yard_plan_does_not_fill_the_same_gap_twice(rules):
    shelf = {("headlight", "VW Golf MK7"): dict(target=1, monthly_rate=1.4, median_ebay_net=90.0, median_net=85.0,
                                                median_days=20, rate_level="own group", sold_12m=10)}
    car = dict(model_family="VW Golf MK7", generation="MK7", year="2015", engine_code="", transmission="MANUAL",
               has_vehicle_row=1, model_method="vin")
    vehicles = {"VEH1": dict(car, veh_ref="VEH1", on_site_date="2026-10-01"),
                "VEH2": dict(car, veh_ref="VEH2", on_site_date="2026-10-20")}
    ctx = ctx_fixture(rules, shelf=shelf, vehicles=vehicles, sold12=Counter({("headlight", "VW Golf MK7"): 14}),
                      avail={("headlight", "VW Golf MK7"): 300})
    plan = pl.yard_plan(ctx)
    assert plan["order"] == ["VEH1", "VEH2"]                       # soonest crush-by first
    first, second = ({l["part_type"]: l for l in c["lines"]} for c in plan["cars"])
    assert first["headlight"]["tier"] == "1"
    assert second["headlight"]["from_other_cars"] == 1 and second["headlight"]["tier"] != "1"


def test_sell_probability_monotone():
    p0, p_half, p1 = (pl.sell_probability(1.0, 3, e) for e in (0, 0.5, 1))
    assert 1 > p0 > p_half > p1 > 0
    assert pl.sell_probability(0, 3, 0) == 0
    assert abs(p0 - (1 - 2.718281828 ** -3)) < 1e-6                # P(N >= 1), N ~ Poisson(3)


def test_available_days_merges_overlaps():
    s, e = date(2026, 1, 1), date(2026, 1, 31)
    ints = [(date(2026, 1, 1), date(2026, 1, 11)), (date(2026, 1, 5), date(2026, 1, 15)),
            (date(2025, 12, 1), date(2025, 12, 5)), (date(2026, 1, 30), date(2026, 3, 1))]
    assert pl.available_days(ints, s, e) == 14 + 2


def test_demand_rate_uses_days_in_stock(rules):
    r = dict(rules, demand_min_available_days=60)
    rate, _ = pl.demand_rate(2, 61, 0.1, r)
    assert abs(rate - 2 / 61 * 365.25 / 12) < 1e-9
    assert pl.demand_rate(2, 10, 0.1, r)[0] == pytest.approx(2 / 60 * 365.25 / 12)   # floor on the window
    assert pl.demand_rate(0, 10, 0.4, r) == (0.4, "shelf fallback rate")            # no evidence: fallback
    assert pl.demand_rate(0, 300, 0.4, r)[0] == 0                                     # stocked, never sold


# --- tiers -----------------------------------------------------------------------

def test_airbags_never_tier1(rules):
    r = dict(rules, always_pull={"airbag_steering"})
    for trade in (0, 5):
        for kw in (dict(value_per_min=50, shortfall=3), dict(value_per_min=0, shortfall=0)):
            t, _ = pl.assign_tier(line(part_type="airbag_steering", non_ebay=True, trade_sales=trade, **kw),
                                  r, TODAY, None)
            assert t != "1"
            assert t == ("1b" if trade else "3")


def test_trade_pull_only_when_stock_does_not_cover_it(rules):
    ab = dict(part_type="airbag_steering", non_ebay=True)
    assert pl.assign_tier(line(trade_sales=2, effective_qty=1, **ab), rules, TODAY, None)[0] == "1b"
    t, why = pl.assign_tier(line(trade_sales=1, effective_qty=1.5, **ab), rules, TODAY, None)
    assert t == "3" and "covered by stock" in why


def test_dont_pull_never_tier1_or_2(rules):
    for kw in (dict(value_per_min=10, shortfall=2), dict(value_per_min=0.5, shortfall=0)):
        t, why = pl.assign_tier(line(dont_pull=True, **kw), rules, TODAY, None)
        assert t == "3" and "Don't-pull" in why


def test_always_pull_overrides(rules):
    r = dict(rules, always_pull={"headlight"})
    t, why = pl.assign_tier(line(value_per_min=0.01, shortfall=0, net_value=0.1), r, TODAY, None)
    assert t == "1" and "always_pull" in why


def test_tier2_expires_after_crush_date(rules):
    l2 = line(shortfall=0, value_per_min=0.5)
    crush = date(2026, 11, 1)
    assert pl.assign_tier(l2, rules, date(2026, 10, 31), crush)[0] == "2"
    assert pl.assign_tier(l2, rules, crush, crush)[0] == "2"
    t, why = pl.assign_tier(l2, rules, date(2026, 11, 2), crush)
    assert t == "3" and "crush-by" in why
    # tier 1 is not affected by the crush date
    assert pl.assign_tier(line(), rules, date(2026, 11, 2), crush)[0] == "1"


def test_tier_thresholds(rules):
    thr, flr = rules["value_per_min_pull_threshold"], rules["value_per_min_floor"]
    assert pl.assign_tier(line(value_per_min=thr, shortfall=0.5), rules, TODAY, None)[0] == "1"
    assert pl.assign_tier(line(value_per_min=thr - 0.01, shortfall=0.5), rules, TODAY, None)[0] == "2"
    assert pl.assign_tier(line(value_per_min=flr - 0.01, shortfall=2), rules, TODAY, None)[0] == "3"
    assert pl.assign_tier(line(net_value=0, value_per_min=0), rules, TODAY, None)[0] == "3"


@pytest.mark.parametrize("price", [None, 0, 0.0])
def test_zero_or_missing_price(rules, price):
    assert pl.net_value(price, 0.9, 7.5, rules) == (0.0, 0.0)
    t, why = pl.assign_tier(line(expected_price=price, net_value=0, value_per_min=0), rules, TODAY, None)
    assert t == "3" and "no price" in why


def test_score_line_without_any_data(rules):
    ctx = ctx_fixture(rules)
    ln = pl.score_line("headlight", "VW Golf MK7", "VW Golf MK7", ctx, None, TODAY)
    assert ln["tier"] == "3" and ln["expected_price"] is None and ln["net_value"] == 0
    assert ln["effort_is_default"] and ln["size_is_default"] and ln["courier_is_default"]
    assert ln["reason"].startswith("Shelf 0/0")


def test_reason_string_format(rules):
    shelf = {("headlight", "VW Golf MK7"): dict(target=2, monthly_rate=1.4, median_ebay_net=90.0, median_net=85.0,
                                                median_days=20, rate_level="own group", sold_12m=10)}
    pos = {("headlight", "VW Golf MK7"): Counter(unlocated=1, phantom=2)}
    ctx = ctx_fixture(rules, shelf=shelf, pos=pos, sold12=Counter({("headlight", "VW Golf MK7"): 14}),
                      avail={("headlight", "VW Golf MK7"): 300})
    ln = pl.score_line("headlight", "VW Golf MK7", "VW Golf MK7", ctx, None, TODAY)
    assert ln["effective_qty"] == 0.5 and ln["shortfall"] == 1.5
    assert ln["reason"].startswith("Shelf 0.5/2 (1 unlocated, 2 phantom) · sells 1.4/mo · £")
    assert ln["reason"].endswith("· PULL") and ln["tier"] == "1"


def test_damage_zone_marks_check_condition(rules):
    ctx = ctx_fixture(rules, damage={"zones": {"Front"}})
    assert pl.score_line("headlight", "VW Golf MK7", "VW Golf MK7", ctx, None, TODAY)["damage"] == "CHECK CONDITION"
    assert pl.score_line("tail_light", "VW Golf MK7", "VW Golf MK7", ctx, None, TODAY)["damage"] == ""


def test_car_level_warnings(rules):
    c = pl.car_lines(ctx_fixture(rules), "VEH1")
    assert any("No damage data" in w for w in c["warnings"])
    assert any("Crush-by 19 Dec 2026" in w for w in c["warnings"])
    assert c["engine"] == "CXXA"


def test_engine_code_parse():
    assert pl.engine_code("CGGBA89292 / CGGB") == "CGGB"
    assert pl.engine_code("CXXA") == "CXXA"
    assert pl.engine_code("") == "" and pl.engine_code(None) == ""


def test_area_map_covers_every_part_type():
    from shelf_targets import load_types
    ids = {t["id"] for t in load_types()} - pl.NOT_PARTS
    assert ids <= set(pl.AREA_OF), sorted(ids - set(pl.AREA_OF))
    assert len(pl.AREA_OF) == sum(len(v.split()) for v in pl.AREAS.values())   # no type in two areas


# --- integration / sanity / idempotency (local analytics store) -------------------

needs_db = pytest.mark.skipif(not pl.DB_PATH.exists(), reason="analytics.db not built")


@pytest.fixture(scope="module")
def live_ctx():
    return pl.load_context()


def _recent(ctx, n=3):
    cars = [(r, v) for r, v in ctx["vehicles"].items() if v["has_vehicle_row"]]
    return [r for r, _ in sorted(cars, key=lambda kv: (kv[1]["on_site_date"] or "", int(kv[0][3:])), reverse=True)[:n]]


@needs_db
def test_integration_three_newest_cars(live_ctx, tmp_path):
    for ref in _recent(live_ctx):
        c = pl.car_lines(live_ctx, ref)
        pl.write_car(c, live_ctx["today"], tmp_path)
        print("\n" + pl.summary_line(c))
        assert c["lines"] and all(l["reason"] and l["tier"] in pl.TIERS for l in c["lines"])
        assert all(l["tier"] != "1" for l in c["lines"] if l["non_ebay"])
        assert all(l["tier"] not in ("1", "2") for l in c["lines"]
                   if l["dont_pull"] and l["part_type"] not in live_ctx["rules"]["always_pull"])
        assert all(l["effective_qty"] == round(pl.stock_position(l["located"], l["unlocated"], l["phantom"],
                                                                 live_ctx["rules"], l["from_other_cars"]), 2)
                   for l in c["lines"])


@needs_db
def test_yard_summary_and_no_customer_data(live_ctx, tmp_path):
    y = pl.yard(live_ctx, tmp_path)
    dist = Counter()
    for r in y["rows"]:
        for k in pl.TIERS:
            dist[k] += r[f"tier_{k}"]
    print(f"\nyard: {len(y['rows'])} cars, tiers {dict(dist)}, tier-1 minutes {y['total_minutes']} "
          f"vs capacity {y['capacity']}")
    assert y["rows"] and dist["3"] > 0
    pl.last_call(live_ctx, tmp_path)
    files = sorted(tmp_path.glob("*.csv")) + sorted(tmp_path.glob("*.html"))
    for f in tmp_path.glob("*.csv"):
        header = next(csv.reader(io.StringIO(f.read_text(encoding="utf-8"))))
        bad = [c for c in header if any(w in c.lower() for w in pl.FORBIDDEN_COLUMN_WORDS)]
        assert not bad, (f.name, bad)
    assert pl.pii_scan(files) == []


@needs_db
def test_idempotent(live_ctx, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        ctx = pl.load_context(today=live_ctx["today"])
        for ref in _recent(ctx):
            pl.write_car(pl.car_lines(ctx, ref), ctx["today"], d)
        pl.yard(ctx, d)
        pl.last_call(ctx, d)
    names = sorted(p.name for p in a.iterdir())
    assert names == sorted(p.name for p in b.iterdir()) and names
    for n in names:
        assert (a / n).read_bytes() == (b / n).read_bytes(), n
