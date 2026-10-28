"""
Pick lists: which parts to take off each car, and which to leave or scrap.

    python scripts/pick_list.py VEH171 [VEH170 ...]   one sheet + CSV per car
    python scripts/pick_list.py --recent 3            the 3 newest cars
    python scripts/pick_list.py --yard                every car on the yard + yard summary
    python scripts/pick_list.py --last-call           tier-2 parts on cars about to be crushed

Reads analytics/analytics.db (analytics_build.py + shelf_targets.py),
data/pricing.db (pricing engine sales, read-only, if present), part_types.yaml,
model_map.yaml, shelf_rules.yaml and pick_rules.yaml. Writes only to
outputs/pick_lists/. No BreakerPro access; no customer data (part types,
VEH refs, prices and dates only).

Per line:
  effective_qty = located + unlocated x unlocated_weight + phantom x phantom_weight
  shortfall     = max(0, shelf target - effective_qty)
  sell_prob     = P(Poisson demand over cover_months > effective stock ahead)
  net_value     = sell_prob x (expected_price - eBay fee - courier)
  value_per_min = net_value / (pull minutes + handling minutes)
Tiers: 1 PULL & LIST, 1b PULL FOR TRADE, 2 LEAVE ON CAR (until crush-by),
3 SCRAP / DON'T PULL. Every line carries a plain-English reason.
"""

import argparse
import csv
import html
import io
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]
from analytics_build import NOT_PARTS, PII_PATTERNS, load_model_map, normalise_model  # noqa: E402
from shelf_targets import (family_platforms, group_for, load_rules as load_shelf_rules,  # noqa: E402
                           load_types, never_pull_types)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "analytics" / "analytics.db"
PRICING_DB = ROOT / "data" / "pricing.db"
RULES_PATH = ROOT / "analytics" / "pick_rules.yaml"
EFFORT_CSV = ROOT / "analytics" / "part_effort.csv"
OUT_DIR = ROOT / "outputs" / "pick_lists"

TIERS = ("1", "1b", "2", "3")
TIER_NAMES = {"1": "PULL & LIST", "1b": "PULL FOR TRADE", "2": "LEAVE ON CAR", "3": "SCRAP / DON'T PULL"}
SIZES = ("S", "M", "L", "Pallet")

# Car areas for the printed sheet, in walk-round order.
AREAS = {
    "Front": """headlight_bracket headlight headlight_control_module fog_light washer_bottle bonnet_hinge
        bonnet_release bonnet wing_bracket arch_liner wing bumper_bracket grille bumper slam_panel scuttle_panel
        wiper_motor wiper_arm windscreen radar_sensor horn rad_pack indicator_light""",
    "Interior": """airbag_squib_slip_ring airbag_crash_sensor airbag_curtain airbag_knee airbag_seat airbag_steering
        airbag_passenger_dash airbag_control_module airbag_kit airbag_other headlight_switch interior_light stalk
        window_switch window_regulator window_motor door_glass mirror_switch rear_view_mirror door_mirror
        door_control_module door_lock door_handle_exterior door_handle_interior door_card speaker door_complete
        door_bare steering_wheel dashboard dash_clocks heater_flap_actuator heater_control_panel
        heater_blower_motor heater_matrix head_unit mmi_control display_screen air_vent glove_box centre_console
        seatbelt_buckle seatbelt seats sun_visor interior_trim pedal_accelerator pedal_clutch gear_linkage
        gearstick ignition_barrel_key central_control_unit gateway_module module_other usb_port switch_other
        handbrake_switch handbrake_lever steering_column steering_control_module""",
    "Engine bay": """gearbox_control_module gearbox_dsg gearbox_auto gearbox_manual ecu_engine engine_cover
        engine_complete turbo boost_solenoid intake_pipe air_filter_box throttle_body fuel_injection_pump
        fuel_filter_housing alternator starter_motor ac_compressor radiator power_steering_pump
        radiator_expansion_tank cooling_fan water_pump relay engine_sensor battery abs_pump
        brake_servo_master_cylinder fuse_box wiring_loom""",
    "Rear": """tail_light brake_light_high tailgate_lock tailgate quarter_glass fuel_flap_actuator fuel_flap
        boot_floor parking_sensor_module parking_sensor aerial camera gas_strut jack_toolkit wheel_spare
        fuel_pump fuel_pump_control_module""",
    "Underside": """gearbox_mount engine_mount fuel_tank adblue_tank dpf driveshaft hub suspension_corner lower_arm
        shock_strut subframe rear_axle brake_caliper wheel_trim wheel_steel wheel_alloy steering_rack""",
}
AREA_OF = {t: a for a, ids in AREAS.items() for t in ids.split()}

# Gearbox-specific part types that do not fit a car with another transmission.
TRANSMISSION_EXCLUDES = {
    "MANUAL": {"gearbox_auto", "gearbox_dsg", "gearbox_control_module"},
    "SEMI AUTO": {"gearbox_manual", "gearbox_auto", "pedal_clutch"},
    "AUTOMATIC": {"gearbox_manual", "gearbox_dsg", "pedal_clutch"},
    "CVT": {"gearbox_manual", "gearbox_dsg", "pedal_clutch"},
}


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def load_rules(path: Path = RULES_PATH) -> dict:
    r = yaml.safe_load(path.read_text(encoding="utf-8"))
    shelf = load_shelf_rules(path.parent / r["shelf_rules"])
    r["shelf"] = {k: shelf[k] for k in ("min_floor", "cover_months", "max_cap")}
    r["shelf_never_pull"] = shelf
    for k in ("unlocated_weight", "phantom_weight", "phantom_min_age_days", "value_per_min_pull_threshold",
              "value_per_min_floor", "vat_rate", "ebay_fee_pct", "default_pull_minutes",
              "trade_demand_lookback_days", "trade_demand_min_sales", "crush_window_days", "last_call_days",
              "on_yard_grace_days", "weekly_capacity_minutes", "busy_car_tier1_lines", "demand_lookback_days",
              "demand_min_available_days"):
        assert isinstance(r.get(k), (int, float)) and r[k] >= 0, f"pick_rules.yaml: {k} must be a number >= 0"
    assert r["value_per_min_floor"] <= r["value_per_min_pull_threshold"], "pick_rules.yaml: floor above threshold"
    assert set(r["courier_default_gbp"]) == set(SIZES), "pick_rules.yaml: courier_default_gbp needs S/M/L/Pallet"
    assert r["default_size"] in SIZES
    assert all(v in SIZES for v in (r.get("size_by_type") or {}).values())
    r["pull_minutes_by_type"] = r.get("pull_minutes_by_type") or {}
    r["size_by_type"] = r.get("size_by_type") or {}
    r["always_pull"] = set(r.get("always_pull") or [])
    r["non_ebay_extra_types"] = set(r.get("non_ebay_extra_types") or [])
    return r


# --------------------------------------------------------------------------
# Pure maths (unit tested)
# --------------------------------------------------------------------------

def stock_position(located: int, unlocated: int, phantom: int, rules: dict, from_other_cars: int = 0) -> float:
    """Effective shelf stock. from_other_cars = the same part already on the pick
    list of a car due to be crushed earlier: it will be on the shelf, located."""
    return located + from_other_cars + unlocated * rules["unlocated_weight"] + phantom * rules["phantom_weight"]


def shortfall(target: float, effective: float) -> float:
    return max(0.0, (target or 0) - effective)


def _poisson_at_least(k: int, lam: float) -> float:
    """P(N >= k) for N ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, min(1.0, 1.0 - cdf))


def sell_probability(monthly_rate: float, cover_months: float, effective_ahead: float) -> float:
    """Chance this part sells within cover_months when effective_ahead parts are
    already on the shelf before it. Fractional stock (half-weight unlocated
    parts) interpolates between the two whole numbers either side."""
    lam = max(0.0, monthly_rate or 0) * cover_months
    whole = math.floor(effective_ahead)
    frac = effective_ahead - whole
    p_lo, p_hi = _poisson_at_least(whole + 1, lam), _poisson_at_least(whole + 2, lam)
    return (1 - frac) * p_lo + frac * p_hi


def available_days(intervals, start: date, end: date) -> int:
    """Days in [start, end] covered by at least one [added, gone) interval."""
    spans = sorted((max(a, start), min(b, end + timedelta(days=1))) for a, b in intervals)
    days, cur_s, cur_e = 0, None, None
    for a, b in spans:
        if b <= a:
            continue
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                days += (cur_e - cur_s).days
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        days += (cur_e - cur_s).days
    return days


def demand_rate(sold: int, avail: int, fallback: float, rules: dict) -> tuple[float, str]:
    """Monthly demand while in stock: (rate, basis)."""
    lo = rules["demand_min_available_days"]
    if sold == 0 and avail < lo:
        return fallback or 0.0, "shelf fallback rate"
    return sold / max(avail, lo) * 365.25 / 12, f"{sold} sold in {avail} days in stock"


def net_value(price, prob: float, courier: float, rules: dict) -> tuple[float, float]:
    """(net_value, fee). Fee and courier are only paid when the part sells."""
    if price is None or price <= 0:
        return 0.0, 0.0
    fee = rules["ebay_fee_pct"] / 100 * price * (1 + rules["vat_rate"])
    return prob * (price - fee - courier), fee


def band(vpm: float, rules: dict) -> str:
    if vpm >= rules["value_per_min_pull_threshold"]:
        return "High"
    return "Med" if vpm >= rules["value_per_min_floor"] else "Low"


def assign_tier(line: dict, rules: dict, today: date, crush_by) -> tuple[str, str]:
    """(tier, verdict). line needs: part_type, non_ebay, trade_sales, effective_qty, dont_pull,
    expected_price, net_value, value_per_min, shortfall."""
    thr, flr = rules["value_per_min_pull_threshold"], rules["value_per_min_floor"]
    if line["non_ebay"]:
        if line["trade_sales"] >= rules["trade_demand_min_sales"] and line["trade_sales"] > line["effective_qty"]:
            return "1b", f"{line['trade_sales']} phone/counter sale(s) in 12 months · PULL FOR TRADE"
        if line["trade_sales"] >= rules["trade_demand_min_sales"]:
            return "3", f"airbag: trade demand ({line['trade_sales']}) already covered by stock · depollution / scrap"
        return "3", "airbag: not for eBay, no phone/trade demand · depollution / scrap"
    if line["part_type"] in rules["always_pull"]:
        return "1", "always_pull override · PULL"
    if line["dont_pull"]:
        return "3", "on Don't-pull list · SCRAP"
    if line["expected_price"] is None or line["expected_price"] <= 0:
        return "3", "no price evidence · SCRAP"
    if line["net_value"] <= 0:
        return "3", "fees + courier eat the price · SCRAP"
    if line["value_per_min"] < flr:
        return "3", f"under £{flr:.2f}/min floor · SCRAP"
    if line["shortfall"] > 0 and line["value_per_min"] >= thr:
        return "1", "PULL"
    if crush_by is not None and today > crush_by:
        return "3", f"crush-by {crush_by:%d %b} passed · SCRAP"
    until = f" until {crush_by:%d %b}" if crush_by else ""
    if line["shortfall"] > 0:
        return "2", f"short on shelf but under £{thr:.2f}/min · LEAVE ON CAR{until}"
    return "2", f"shelf target met · LEAVE ON CAR{until}"


def reason_string(line: dict, verdict: str) -> str:
    extra = [f"{n} {w}" for n, w in ((line["unlocated"], "unlocated"), (line["phantom"], "phantom"),
                                      (line.get("from_other_cars", 0), "from other cars")) if n]
    shelf = f"Shelf {line['effective_qty']:g}/{line['target']}" + (f" ({', '.join(extra)})" if extra else "")
    return f"{shelf} · sells {line['monthly_rate']:.1f}/mo · £{line['value_per_min']:.2f}/min · {verdict}"


# --------------------------------------------------------------------------
# Context from the local stores
# --------------------------------------------------------------------------

def engine_code(raw) -> str:
    """'CGGBA89292 / CGGB' -> 'CGGB'; a bare code is kept."""
    s = str(raw or "").strip().upper()
    m = re.search(r"/\s*([A-Z0-9]{3,5})\s*$", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Z0-9]{3,5}", s) else ""


def _family_from_engine(mm, model: str, gen: str):
    """Pricing-engine model + generation ('IBIZA', 'MK5') -> analytics family ('SEAT Ibiza 6F')."""
    if not model:
        return None
    for mk in mm["models"]:
        n = normalise_model(mm, mk, f"{model} {gen}".strip())
        if n["gen"] is not None:
            return n["family"]
    return None


def load_engine_prices(today: date, rules: dict, mm, types, path: Path = PRICING_DB) -> dict:
    """(our part type, family) -> list of ex-VAT item prices from the pricing engine's
    sold_history: same model AND generation, its own lookback (no cross-model borrowing)."""
    if not path.exists():
        return {}
    import pricing_config as pcfg
    import part_classifier as pc
    since = (today - timedelta(days=pcfg.OWN_LOOKBACK_DAYS)).isoformat()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT raw_title, model, generation, item_price, price, shipping, sold_date "
                          "FROM sold_history WHERE channel = 'ebay' AND sold_date >= ? AND sold_date <= ?",
                          (since, today.isoformat())).fetchall()
    finally:
        db.close()
    fam_cache, out = {}, defaultdict(list)
    for title, model, gen, item, price, ship, _ in rows:
        hit = pc.classify_rules(title, types)
        if not hit:
            continue
        key = (model or "", gen or "")
        if key not in fam_cache:
            fam_cache[key] = _family_from_engine(mm, *key)
        fam = fam_cache[key]
        p = item if item is not None else (price or 0) - (ship or 0)
        if fam and p and p > 0:
            out[(hit[0], fam)].append(p / (1 + rules["vat_rate"]))
    return {k: v for k, v in out.items() if len(v) >= pcfg.PART_TYPE_MIN_SALES}


def load_effort(path: Path = EFFORT_CSV) -> dict:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {r["part_type_id"]: r for r in csv.DictReader(f)}


def load_context(today: date | None = None, db_path: Path = DB_PATH, pricing_path: Path = PRICING_DB) -> dict:
    today = today or date.today()
    rules, types, mm = load_rules(), load_types(), load_model_map()
    import part_classifier as pc
    ctypes = pc.load_taxonomy()
    labels = {t["id"]: t["label"] for t in types}
    level_of = {t["id"]: t["fitment_level"] for t in types}
    fam_plat = family_platforms(mm)
    never = never_pull_types(rules["shelf_never_pull"], list(labels))
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        vehicles = {r["veh_ref"]: dict(r) for r in db.execute(
            "SELECT veh_ref, model_family, norm_make, norm_model, generation, model_method, year, engine_code, "
            "transmission, on_site_date, has_vehicle_row FROM vehicles")}
        shelf = {(r["part_type"], r["fitment_group"]): dict(r) for r in db.execute(
            "SELECT part_type, fitment_group, target, monthly_rate, median_ebay_net, median_net, median_days, "
            "rate_level, sold_12m FROM shelf_targets")}
        stock = db.execute("SELECT veh_ref, part_type, location, added_date, listing FROM stock").fetchall()
        dont_pull = {r[0] for r in db.execute("SELECT part_type FROM dont_pull")}
        courier = {r[0]: r[1] for r in db.execute("SELECT size_class, typical_cost_gbp FROM courier_cost")}
        since = (today - timedelta(days=rules["trade_demand_lookback_days"])).isoformat()
        counter = db.execute("SELECT part_type, model_family FROM sales WHERE channel = 'counter' "
                             "AND sale_date >= ?", (since,)).fetchall()
        sold_rows = db.execute("SELECT part_type, model_family, added_date, sale_date FROM sales").fetchall()
    finally:
        db.close()

    # Stock position per (part type, fitment group), grouped exactly as shelf_targets does.
    pos = defaultdict(Counter)
    for s in stock:
        fam = vehicles.get(s["veh_ref"], {}).get("model_family") or "(no vehicle)"
        key = (s["part_type"], group_for(level_of.get(s["part_type"], "generation"), fam, fam_plat.get(fam)))
        no_loc = not (s["location"] or "").strip()
        age = (today - date.fromisoformat(s["added_date"][:10])).days if s["added_date"] else None
        if no_loc and age is not None and age >= rules["phantom_min_age_days"] and s["listing"] in ("unlisted", "dead"):
            pos[key]["phantom"] += 1
        elif s["listing"] == "dead":
            pos[key]["dead"] += 1      # not counted, as in shelf_targets
        elif no_loc:
            pos[key]["unlocated"] += 1
        else:
            pos[key]["located"] += 1

    # Demand: sales per day in stock, per group. In-stock intervals come from sold
    # parts [added, sold) and current live stock [added, today].
    d_start = today - timedelta(days=rules["demand_lookback_days"])
    intervals, sold12 = defaultdict(list), Counter()
    for r in sold_rows:
        fam = r["model_family"]
        if fam not in fam_plat or not r["sale_date"]:
            continue
        key = (r["part_type"], group_for(level_of.get(r["part_type"], "generation"), fam, fam_plat[fam]))
        sold_on = date.fromisoformat(r["sale_date"][:10])
        if r["added_date"]:
            intervals[key].append((date.fromisoformat(r["added_date"][:10]), sold_on))
        if d_start <= sold_on <= today:
            sold12[key] += 1
    for s in stock:
        fam = vehicles.get(s["veh_ref"], {}).get("model_family")
        if fam in fam_plat and s["added_date"] and s["listing"] != "dead":
            key = (s["part_type"], group_for(level_of.get(s["part_type"], "generation"), fam, fam_plat[fam]))
            intervals[key].append((date.fromisoformat(s["added_date"][:10]), today))
    avail = {k: available_days(v, d_start, today) for k, v in intervals.items()}

    trade = Counter()
    for r in counter:
        fam = r["model_family"]
        if fam in fam_plat:
            trade[(r["part_type"], group_for(level_of.get(r["part_type"], "generation"), fam, fam_plat[fam]))] += 1

    return dict(today=today, rules=rules, labels=labels, sold12=sold12, avail=avail, level_of=level_of, fam_plat=fam_plat, never=never,
                vehicles=vehicles, shelf=shelf, pos=pos, dont_pull=dont_pull, courier=courier, trade=trade,
                effort=load_effort(), engine_prices=load_engine_prices(today, rules, mm, ctypes, pricing_path),
                damage={})  # no damage zones are recorded anywhere yet


# --------------------------------------------------------------------------
# Lines per car
# --------------------------------------------------------------------------

def crush_by(v: dict, rules: dict):
    if not v.get("on_site_date"):
        return None
    return date.fromisoformat(v["on_site_date"]) + timedelta(days=rules["crush_window_days"])


def score_line(t: str, group: str, fam: str, ctx: dict, crush, today: date) -> dict:
    rules = ctx["rules"]
    p = ctx["pos"].get((t, group), Counter())
    loc, unl, ph = p["located"], p["unlocated"], p["phantom"]
    other = ctx.get("pending", {}).get((t, group), 0)
    eff = round(stock_position(loc, unl, ph, rules, other), 2)
    sh = ctx["shelf"].get((t, group))
    target = sh["target"] if sh else 0
    rate, rate_basis = demand_rate(ctx["sold12"].get((t, group), 0), ctx["avail"].get((t, group), 0),
                                   sh["monthly_rate"] if sh else 0.0, rules)
    eng = ctx["engine_prices"].get((t, fam))
    if eng:
        price, src = round(statistics.median(eng), 2), f"pricing engine ({len(eng)} {fam} sales)"
    elif sh and sh["median_ebay_net"] is not None:
        price, src = sh["median_ebay_net"], f"analytics eBay median ({sh['rate_level']})"
    elif sh and sh["median_net"] is not None:
        price, src = sh["median_net"], f"analytics median, all channels ({sh['rate_level']})"
    else:
        price, src = None, "no price evidence"
    eff_row = ctx["effort"].get(t) or {}
    pull_min = eff_row.get("pull_minutes") or ""
    effort_default = not str(pull_min).strip()
    pull_min = int(pull_min) if not effort_default else rules["pull_minutes_by_type"].get(t, rules["default_pull_minutes"])
    size = (eff_row.get("size") or "").strip()
    size_default = size not in SIZES
    size = rules["size_by_type"].get(t, rules["default_size"]) if size_default else size
    cc = ctx["courier"].get(size)
    courier_default = cc is None
    courier = rules["courier_default_gbp"][size] if courier_default else cc
    minutes = pull_min + sum(rules["handling_minutes"].values())
    prob = sell_probability(rate, rules["shelf"]["cover_months"], eff)
    nv, fee = net_value(price, prob, courier, rules)
    vpm = nv / minutes if minutes else 0.0
    line = dict(part_type=t, label=ctx["labels"][t], area=AREA_OF.get(t, "Interior"), fitment_level=ctx["level_of"][t],
                fitment_group=group, located=loc, unlocated=unl, phantom=ph, from_other_cars=other,
                effective_qty=eff, target=target,
                shortfall=round(shortfall(target, eff), 2), monthly_rate=round(rate, 3), rate_basis=rate_basis,
                sell_probability=round(prob, 3), expected_price=price, price_source=src, ebay_fee=round(fee, 2),
                size=size, size_is_default=size_default, courier_cost=courier, courier_is_default=courier_default,
                net_value=round(nv, 2), pull_minutes=pull_min, handling_minutes=sum(rules["handling_minutes"].values()),
                total_minutes=minutes, effort_is_default=effort_default, value_per_min=round(vpm, 2),
                non_ebay=t in ctx["never"] or t in rules["non_ebay_extra_types"],
                trade_sales=ctx["trade"].get((t, group), 0), dont_pull=t in ctx["dont_pull"],
                damage="CHECK CONDITION" if AREA_OF.get(t) in ctx["damage"].get("zones", ()) else "")
    line["band"] = band(line["value_per_min"], rules)
    line["tier"], verdict = assign_tier(line, rules, today, crush)
    line["reason"] = reason_string(line, verdict)
    return line


def car_lines(ctx: dict, veh_ref: str, pending=None) -> dict:
    """Score every part type that fits the car. pending: parts already on the pick
    lists of cars due to be crushed earlier (default: from the yard plan)."""
    if pending is None:
        pending = yard_plan(ctx)["before"].get(veh_ref, Counter())
    ctx = dict(ctx, pending=pending)
    v = ctx["vehicles"].get(veh_ref)
    if v is None:
        raise SystemExit(f"{veh_ref} not found in analytics.db")
    fam = v["model_family"]
    if fam not in ctx["fam_plat"]:
        raise SystemExit(f"{veh_ref}: generation unknown ({fam}); no fitment group")
    rules, today = ctx["rules"], ctx["today"]
    platform = ctx["fam_plat"][fam]
    crush = crush_by(v, rules)
    trans = (v.get("transmission") or "").split(" (")[0]
    skip = TRANSMISSION_EXCLUDES.get(trans, set())
    lines = []
    for t in sorted(ctx["labels"]):
        if t in NOT_PARTS or t in skip:
            continue
        group = group_for(ctx["level_of"][t], fam, platform)
        lines.append(score_line(t, group, fam, ctx, crush, today))
    lines.sort(key=lambda l: (list(AREAS).index(l["area"]), TIERS.index(l["tier"]), -l["value_per_min"], l["label"]))
    warnings = []
    if not ctx["damage"].get(veh_ref):
        warnings.append("No damage data recorded for this car: check the condition of every part before pulling.")
    defaults = sorted({w for l in lines if l["tier"] in ("1", "1b")
                       for w, on in (("pull minutes", l["effort_is_default"]), ("size", l["size_is_default"]),
                                     ("courier cost", l["courier_is_default"])) if on})
    if defaults:
        warnings.append(f"Default figures used for tier-1 lines: {', '.join(defaults)} (fill in part_effort.csv / "
                        "courier_cost.csv).")
    if crush is None:
        warnings.append("No on-site date: crush-by unknown, tier 2 has no expiry.")
    elif today > crush:
        warnings.append(f"Crush-by {crush:%d %b %Y} has passed: tier 2 is now scrap.")
    else:
        warnings.append(f"Crush-by {crush:%d %b %Y} ({(crush - today).days} days): tier 2 is valid until then.")
    n_other = sum(l["from_other_cars"] for l in lines)
    if n_other:
        warnings.append(f"Counts {n_other} part(s) already on the pick lists of cars due to be crushed earlier.")
    if skip:
        warnings.append(f"Transmission {trans.lower()}: {', '.join(sorted(ctx['labels'].get(s, s) for s in skip))} left off.")
    t1 = [l for l in lines if l["tier"] in ("1", "1b")]
    value = sum(l["net_value"] for l in t1)
    mins = sum(l["total_minutes"] for l in t1)
    return dict(v=v, family=fam, platform=platform, engine=engine_code(v.get("engine_code")),
                transmission=trans or "unknown", crush_by=crush, lines=lines, warnings=warnings,
                tier_counts={k: sum(l["tier"] == k for l in lines) for k in TIERS},
                tier1_value=round(value, 2), tier1_minutes=mins, cover_months=rules["shelf"]["cover_months"],
                per_hour=round(value / mins * 60, 2) if mins else 0.0)


def on_yard(v: dict, rules: dict, today: date) -> bool:
    c = crush_by(v, rules)
    return bool(v.get("has_vehicle_row")) and c is not None and today <= c + timedelta(days=rules["on_yard_grace_days"])


def yard_plan(ctx: dict) -> dict:
    """Cars on the yard in crush-by order (soonest first). Each car is scored with
    the tier-1 / 1b parts of the cars before it counted as stock, so two cars of the
    same model don't both fill the same gap. Cached on ctx."""
    if "_plan" in ctx:
        return ctx["_plan"]
    rules, today = ctx["rules"], ctx["today"]
    refs = sorted((r for r, v in ctx["vehicles"].items()
                   if on_yard(v, rules, today) and v["model_family"] in ctx["fam_plat"]),
                  key=lambda r: (crush_by(ctx["vehicles"][r], rules), int(r[3:])))
    pending, before, cars = Counter(), {}, []
    for ref in refs:
        before[ref] = Counter(pending)
        c = car_lines(ctx, ref, pending=before[ref])
        cars.append(c)
        for l in c["lines"]:
            if l["tier"] in ("1", "1b"):
                pending[(l["part_type"], l["fitment_group"])] += 1
    ctx["_plan"] = dict(order=refs, before=before, cars=cars, after=pending)
    return ctx["_plan"]


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

CSV_COLS = ["veh_ref", "area", "tier", "tier_name", "part_type", "label", "fitment_level", "fitment_group",
            "located", "unlocated", "phantom", "from_other_cars", "effective_qty", "target", "shortfall", "monthly_rate",
            "rate_basis", "sell_probability", "expected_price", "price_source", "ebay_fee", "size", "size_is_default",
            "courier_cost", "courier_is_default", "net_value", "pull_minutes", "handling_minutes", "total_minutes",
            "effort_is_default", "value_per_min", "band", "trade_sales", "dont_pull", "damage", "reason"]
FORBIDDEN_COLUMN_WORDS = ("customer", "name_", "buyer", "address", "email", "phone", "postcode", "notes")


def _cell(v):
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v - round(v)) > 1e-9 or v == 0 else f"{v:.0f}"
    return "" if v is None else str(v)


def to_csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


CSS = """
:root { --bg:#fff; --fg:#111; --muted:#555; --line:#bbb; --head:#e8e8e8; --t1:#0a6b2d; --t2:#7a5a00; --t3:#777; --sec:#f2f2f2; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --t1:#5fd38a; --t2:#e0b84a; --t3:#999; --sec:#1d1d1d; } }
:root[data-theme="dark"] { --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --t1:#5fd38a; --t2:#e0b84a; --t3:#999; --sec:#1d1d1d; }
@page { size:A4; margin:10mm; }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--fg); font:13px/1.3 system-ui, sans-serif; margin:16px; }
h1 { font-size:20px; margin:0 0 4px; }
.meta { margin:0 0 6px; }
.box { border:2px solid var(--fg); padding:6px 10px; margin:6px 0 8px; display:flex; flex-wrap:wrap; gap:4px 18px; font-size:15px; font-weight:600; }
ul.warn { margin:4px 0 10px; padding-left:18px; color:var(--muted); font-size:12px; }
h2 { font-size:15px; margin:12px 0 2px; padding:3px 6px; background:var(--sec); break-after:avoid; }
.wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; table-layout:fixed; }
th, td { border-bottom:1px solid var(--line); padding:2px 3px; vertical-align:top; }
th { background:var(--head); font-size:10px; text-align:right; }
td { text-align:right; font-variant-numeric:tabular-nums; }
th.l, td.l { text-align:left; }
td.l .why { display:block; color:var(--muted); font-size:10px; }
tr { break-inside:avoid; }
.t1 td.tier, .t1b td.tier { color:var(--t1); font-weight:700; }
.t2 td.tier { color:var(--t2); font-weight:700; }
.t3 td { color:var(--t3); }
.tick { width:14px; height:14px; border:1.5px solid var(--fg); display:inline-block; }
.chk { color:#b00; font-weight:700; }
.scrap { font-size:11px; color:var(--t3); margin:3px 0 0; }
@media print { body { margin:0; font-size:11px; } .wrap { overflow:visible; } th { font-size:8.5px; }
  td.l .why { font-size:8.5px; } .box { font-size:13px; } }
"""
COLGROUP = ("<colgroup><col style='width:5mm'><col><col style='width:9mm'><col style='width:8mm'>"
            "<col style='width:8mm'><col style='width:8mm'><col style='width:12mm'><col style='width:9mm'>"
            "<col style='width:13mm'><col style='width:12mm'><col style='width:8mm'><col style='width:11mm'></colgroup>")
HEAD = ("<thead><tr><th></th><th class=l>Part type · reason</th><th>Tier</th><th>Loc</th><th>Unloc</th>"
        "<th>Phant</th><th>Eff / tgt</th><th>Sells /mo</th><th>Price</th><th>Net</th><th>Mins</th>"
        "<th>£/min</th></tr></thead>")


def _gbp(v, dp=2):
    return "" if v is None else f"£{v:,.{dp}f}"


def render_car(c: dict, today: date) -> str:
    v, body = c["v"], []
    for area in AREAS:
        rows = [l for l in c["lines"] if l["area"] == area]
        keep = [l for l in rows if l["tier"] != "3"]
        scrap = [l for l in rows if l["tier"] == "3"]
        if not rows:
            continue
        body.append(f"<h2>{area} · {sum(l['tier'] in ('1', '1b') for l in rows)} to pull</h2>")
        if keep:
            trs = []
            for l in keep:
                chk = " <span class=chk>CHECK CONDITION</span>" if l["damage"] else ""
                trs.append(
                    f"<tr class='t{l['tier']}'><td><span class=tick></span></td>"
                    f"<td class=l>{html.escape(l['label'])}{chk}<span class=why>{html.escape(l['reason'])}</span></td>"
                    f"<td class=tier>{l['tier']}</td><td>{l['located']}</td><td>{l['unlocated']}</td>"
                    f"<td>{l['phantom']}</td><td>{l['effective_qty']:g}/{l['target']}</td>"
                    f"<td>{l['monthly_rate']:.1f}</td><td>{_gbp(l['expected_price'])}</td>"
                    f"<td>{_gbp(l['net_value'])}</td><td>{l['total_minutes']}{'*' if l['effort_is_default'] else ''}</td>"
                    f"<td>{l['value_per_min']:.2f}</td></tr>")
            body.append(f"<div class=wrap><table>{COLGROUP}{HEAD}<tbody>{''.join(trs)}</tbody></table></div>")
        if scrap:
            body.append(f"<p class=scrap><b>Scrap / don't pull ({len(scrap)}):</b> "
                        + html.escape(", ".join(l["label"] for l in scrap)) + "</p>")
    tc = c["tier_counts"]
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in c["warnings"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pick list {v['veh_ref']}</title>
<style>{CSS}</style></head><body>
<h1>Pick list: {v['veh_ref']} · {html.escape(c['family'])}</h1>
<p class=meta>Generation {html.escape(str(v['generation']))} · platform {html.escape(c['platform'] or 'not set')} ·
engine {html.escape(c['engine'] or 'unknown')} · gearbox {html.escape(c['transmission'].lower())} (BreakerPro has no gearbox code) ·
year {html.escape(str(v['year'] or '?'))} · {today:%d %b %Y}</p>
<div class=box><span>Tier 1: {tc['1']} + {tc['1b']} trade</span><span>Est. value {_gbp(c['tier1_value'], 0)}</span>
<span>{c['tier1_minutes']} min</span><span>{_gbp(c['per_hour'], 0)}/hour</span>
<span>Leave on car {tc['2']} · scrap {tc['3']}</span></div>
<ul class=warn>{warn}</ul>
{''.join(body)}
<p class=scrap>Tier 1 PULL &amp; LIST · 1b PULL FOR TRADE · 2 LEAVE ON CAR until crush-by · 3 scrap / don't pull.
Eff = located + unlocated &times; ½ + phantom &times; 0. Price ex VAT. Net = chance it sells in
{c['cover_months']:g} months &times; (price − eBay fee − courier). Mins = pull + handling; * = default pull time.</p>
</body></html>
"""


def write_car(c: dict, today: date, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = c["v"]["veh_ref"]
    rows = [dict(l, veh_ref=ref, tier_name=TIER_NAMES[l["tier"]]) for l in c["lines"]]
    p_csv, p_html = out_dir / f"pick_list_{ref}.csv", out_dir / f"pick_list_{ref}.html"
    p_csv.write_text(to_csv(rows, CSV_COLS), encoding="utf-8", newline="")
    p_html.write_text(render_car(c, today), encoding="utf-8", newline="")
    return p_csv, p_html


YARD_COLS = ["veh_ref", "model", "on_site_date", "crush_by", "days_to_crush", "tier_1", "tier_1b", "tier_2",
             "tier_3", "tier1_minutes", "tier1_value", "per_hour", "busy"]


def yard(ctx: dict, out_dir: Path = OUT_DIR) -> dict:
    rules, today = ctx["rules"], ctx["today"]
    plan = yard_plan(ctx)
    cars, rows = plan["cars"], []
    for c in cars:
        ref = c["v"]["veh_ref"]
        write_car(c, today, out_dir)
        tc = c["tier_counts"]
        rows.append(dict(veh_ref=ref, model=c["family"], on_site_date=c["v"]["on_site_date"],
                         crush_by=c["crush_by"].isoformat(), days_to_crush=(c["crush_by"] - today).days,
                         tier_1=tc["1"], tier_1b=tc["1b"], tier_2=tc["2"], tier_3=tc["3"],
                         tier1_minutes=c["tier1_minutes"], tier1_value=c["tier1_value"], per_hour=c["per_hour"],
                         busy="over %d tier-1 lines" % rules["busy_car_tier1_lines"]
                         if tc["1"] > rules["busy_car_tier1_lines"] else ""))
    total_min = sum(r["tier1_minutes"] for r in rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "yard_summary.csv").write_text(to_csv(rows, YARD_COLS), encoding="utf-8", newline="")
    trs = "".join(
        f"<tr><td class=l>{r['veh_ref']}</td><td class=l>{html.escape(r['model'])}</td><td>{r['crush_by']}</td>"
        f"<td>{r['days_to_crush']}</td><td>{r['tier_1']}</td><td>{r['tier_1b']}</td><td>{r['tier_2']}</td>"
        f"<td>{r['tier_3']}</td><td>{r['tier1_minutes']}</td><td>{_gbp(r['tier1_value'], 0)}</td>"
        f"<td>{_gbp(r['per_hour'], 0)}</td><td class=l>{r['busy']}</td></tr>" for r in rows)
    cap = rules["weekly_capacity_minutes"]
    (out_dir / "yard_summary.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Yard summary</title>
<style>{CSS}</style></head><body><h1>Yard summary · {today:%d %b %Y}</h1>
<div class=box><span>{len(rows)} cars on yard</span><span>Tier-1 pulling {total_min:,} min</span>
<span>Capacity {cap:,} min/week</span><span>{total_min / cap:.1f} weeks of work</span></div>
<p class=meta>On yard = vehicle row in BreakerPro Stock and not more than {rules['on_yard_grace_days']} days past
crush-by (on-site + {rules['crush_window_days']} days); BreakerPro does not record crushing. Cars are planned in
crush-by order: parts on an earlier car's list count as stock for the later ones.</p>
<div class=wrap><table><thead><tr><th class=l>VEH</th><th class=l>Model</th><th>Crush-by</th><th>Days</th>
<th>T1</th><th>T1b</th><th>T2</th><th>T3</th><th>T1 min</th><th>T1 value</th><th>£/hour</th><th class=l>Flag</th>
</tr></thead><tbody>{trs}</tbody></table></div></body></html>
""", encoding="utf-8", newline="")
    return dict(rows=rows, cars=cars, total_minutes=total_min, capacity=cap)


LAST_CALL_COLS = ["veh_ref", "model", "crush_by", "days_to_crush", "label", "located", "unlocated", "phantom",
                  "from_other_cars",
                  "effective_qty", "target", "shortfall", "net_value", "total_minutes", "value_per_min",
                  "decision", "reason"]


def last_call(ctx: dict, out_dir: Path = OUT_DIR) -> list[dict]:
    """Tier-2 lines on cars within last_call_days of crush-by (or past it but still
    on the yard), re-scored against current stock: PULL NOW or SCRAP."""
    rules, today = ctx["rules"], ctx["today"]
    plan = yard_plan(ctx)
    # Current stock plus every tier-1 / 1b part planned across the yard, plus the
    # last-call parts pulled so far: what the shelf will hold when this car goes.
    pending, out = Counter(plan["after"]), []
    for ref in plan["order"]:
        v = ctx["vehicles"][ref]
        c_by = crush_by(v, rules)
        if (c_by - today).days > rules["last_call_days"]:
            continue
        # Tier-2 lines as scored before crush-by, then re-scored against that stock.
        was = car_lines(dict(ctx, today=min(today, c_by)), ref, pending=plan["before"][ref])
        fam, crush_ctx = v["model_family"], dict(ctx, pending=pending)
        for l0 in was["lines"]:
            if l0["tier"] != "2":
                continue
            l = score_line(l0["part_type"], l0["fitment_group"], fam, crush_ctx, None, min(today, c_by))
            keep = l["value_per_min"] >= rules["value_per_min_floor"] and l["net_value"] > 0
            decision = "PULL NOW" if keep else "SCRAP"
            if keep:
                pending[(l["part_type"], l["fitment_group"])] += 1
            out.append(dict(l, veh_ref=ref, model=fam, crush_by=c_by.isoformat(),
                            days_to_crush=(c_by - today).days, decision=decision,
                            reason=reason_string(l, f"last call · {decision}")))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "last_call.csv").write_text(to_csv(out, LAST_CALL_COLS), encoding="utf-8", newline="")
    return out


def pii_scan(paths) -> list:
    hits = []
    for f in paths:
        txt = Path(f).read_text(encoding="utf-8")
        hits += [(Path(f).name, label) for label, rx in PII_PATTERNS for _ in rx.finditer(txt)]
    return hits


def summary_line(c: dict) -> str:
    tc = c["tier_counts"]
    return (f"{c['v']['veh_ref']} {c['family']} (engine {c['engine'] or '?'}, {c['transmission'].lower()}, "
            f"crush-by {c['crush_by']}): tier 1 {tc['1']}, 1b {tc['1b']}, 2 {tc['2']}, 3 {tc['3']} · "
            f"tier-1 value £{c['tier1_value']:,.2f} in {c['tier1_minutes']} min = £{c['per_hour']:,.2f}/hour")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("veh_refs", nargs="*")
    ap.add_argument("--recent", type=int, metavar="N", help="the N most recently added cars")
    ap.add_argument("--yard", action="store_true", help="every car on the yard + yard summary")
    ap.add_argument("--last-call", action="store_true", help="tier-2 lines on cars near crush-by")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ctx = load_context()
    refs = [r.strip().upper() for r in a.veh_refs]
    if a.recent:
        refs += [r for r, _ in sorted(((r, v) for r, v in ctx["vehicles"].items() if v["has_vehicle_row"]),
                                      key=lambda kv: (kv[1]["on_site_date"] or "", int(kv[0][3:])),
                                      reverse=True)[:a.recent]]
    for ref in refs:
        c = car_lines(ctx, ref)
        p_csv, p_html = write_car(c, ctx["today"])
        print(summary_line(c))
        for l in c["lines"]:
            if l["tier"] in ("1", "1b"):
                print(f"    T{l['tier']:2} {l['area']:10} {l['label'][:40]:40} {l['reason']}")
        print(f"    wrote {p_html.relative_to(ROOT)} and .csv")
    if a.yard:
        y = yard(ctx)
        dist = Counter()
        for r in y["rows"]:
            for k in TIERS:
                dist[k] += r[f"tier_{k}"]
        print(f"Yard: {len(y['rows'])} cars on yard; tier totals {dict(dist)}; tier-1 pulling {y['total_minutes']:,} min "
              f"vs capacity {y['capacity']:,} min/week; busy cars: "
              f"{[r['veh_ref'] for r in y['rows'] if r['busy']] or 'none'}")
    if a.last_call:
        lc = last_call(ctx)
        print(f"Last call: {len(lc)} tier-2 lines on cars within {ctx['rules']['last_call_days']} days of crush-by "
              f"({Counter(l['decision'] for l in lc)})")
    if not (refs or a.yard or a.last_call):
        ap.print_help()


if __name__ == "__main__":
    main()
