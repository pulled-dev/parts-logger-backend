"""
Dismantler worksheet: how long each part type takes to pull and how big it is.

    python scripts/make_dismantler_worksheet.py

Reads analytics/part_types.yaml and analytics/analytics.db (local SQLite, built
by scripts/analytics_build.py). No BreakerPro access.

Writes:
  reports/dismantler_worksheet.html  print on A4; tick pull minutes and size by hand
  analytics/part_effort.csv          one row per part type; type the ticks in here,
                                     then run scripts/load_part_effort.py
  analytics/courier_cost.csv         size class -> typical courier cost (fill in)

Values already typed into the two CSVs are kept when this is run again.
Rows are grouped into sections; sections and the rows inside them are sorted
by units sold in the last 12 months, most first.
"""

import csv
import html
import sqlite3
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = ROOT / "analytics" / "part_types.yaml"
DB_PATH = ROOT / "analytics" / "analytics.db"
EFFORT_CSV = ROOT / "analytics" / "part_effort.csv"
COURIER_CSV = ROOT / "analytics" / "courier_cost.csv"
WORKSHEET = ROOT / "reports" / "dismantler_worksheet.html"

PULL_MINUTES = ["5", "15", "30", "60", "120"]
SIZES = ["S", "M", "L", "Pallet"]
EFFORT_COLS = ["part_type_id", "part_type", "section", "units_sold_12m", "median_ebay_price",
               "pull_minutes", "size", "notes"]
COURIER_COLS = ["size_class", "typical_courier_cost_gbp"]

# Section for every part type id in part_types.yaml (checked at startup).
SECTIONS = {
    "Engine bay": """gearbox_control_module gearbox_mount gearbox_dsg gearbox_auto gearbox_manual ecu_engine
        engine_mount engine_cover engine_complete turbo boost_solenoid intake_pipe air_filter_box throttle_body
        fuel_pump_control_module fuel_pump fuel_injection_pump fuel_filter_housing fuel_tank adblue_tank dpf
        alternator starter_motor ac_compressor radiator power_steering_pump radiator_expansion_tank rad_pack
        cooling_fan water_pump engine_sensor battery abs_pump brake_servo_master_cylinder washer_bottle
        wiper_motor gear_linkage horn relay""",
    "Body": """headlight_bracket headlight tail_light fog_light brake_light_high indicator_light wiper_arm
        quarter_glass windscreen door_glass door_mirror door_lock door_handle_exterior door_complete door_bare
        bonnet_hinge bonnet_release bonnet tailgate_lock tailgate wing_bracket arch_liner wing bumper_bracket
        parking_sensor grille bumper slam_panel scuttle_panel fuel_flap fuel_flap_actuator gas_strut
        window_regulator window_motor""",
    "Interior": """airbag_curtain airbag_knee airbag_seat airbag_steering airbag_passenger_dash airbag_kit
        airbag_other rear_view_mirror door_handle_interior door_card speaker steering_wheel steering_column
        dashboard air_vent glove_box centre_console seatbelt_buckle seatbelt seats sun_visor boot_floor
        interior_trim pedal_accelerator pedal_clutch gearstick handbrake_lever interior_light heater_matrix
        heater_blower_motor""",
    "Electrical": """airbag_squib_slip_ring airbag_crash_sensor airbag_control_module headlight_switch
        headlight_control_module stalk window_switch mirror_switch door_control_module parking_sensor_module
        wiring_loom handbrake_switch dash_clocks heater_control_panel heater_flap_actuator head_unit mmi_control
        display_screen ignition_barrel_key central_control_unit gateway_module radar_sensor camera fuse_box
        module_other aerial usb_port switch_other steering_control_module""",
    "Wheels / suspension": """steering_rack brake_caliper driveshaft hub suspension_corner lower_arm shock_strut
        subframe rear_axle wheel_trim wheel_spare wheel_steel wheel_alloy""",
    "Other": """jack_toolkit misc_unknown vehicle_listing""",
}


def section_of() -> dict:
    out = {}
    for sec, ids in SECTIONS.items():
        for i in ids.split():
            assert i not in out, f"{i} is in two sections"
            out[i] = sec
    return out


def read_kept(path: Path, key: str, cols: list[str]) -> dict:
    """Hand-typed values from an existing CSV, keyed by `key`."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r[key]: {c: (r.get(c) or "").strip() for c in cols} for r in csv.DictReader(f) if r.get(key)}


def sales_by_type(today: date) -> dict:
    """part_type -> (units sold in the last 12 months, median eBay price paid per part, excl. postage)."""
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        since = (today - timedelta(days=365)).isoformat()
        units = dict(db.execute("SELECT part_type, COUNT(1) FROM sales WHERE sale_date >= ? GROUP BY part_type",
                                (since,)).fetchall())
        prices = {}
        for t, p in db.execute("SELECT part_type, gross_price FROM sales "
                               "WHERE channel = 'ebay' AND gross_price IS NOT NULL"):
            prices.setdefault(t, []).append(p)
    finally:
        db.close()
    return {t: (units.get(t, 0), statistics.median(prices[t]) if t in prices else None)
            for t in set(units) | set(prices)}


def build_rows(today: date):
    types = yaml.safe_load(TAXONOMY.read_text(encoding="utf-8"))["types"]
    sec = section_of()
    ids = [t["id"] for t in types]
    missing, extra = sorted(set(ids) - set(sec)), sorted(set(sec) - set(ids))
    if missing or extra:
        sys.exit(f"SECTIONS out of step with part_types.yaml. Not in a section: {missing}; unknown ids: {extra}")
    stats = sales_by_type(today)
    kept = read_kept(EFFORT_CSV, "part_type_id", ["pull_minutes", "size", "notes"])
    rows = []
    for t in types:
        units, med = stats.get(t["id"], (0, None))
        k = kept.get(t["id"], {})
        rows.append(dict(part_type_id=t["id"], part_type=t["label"], section=sec[t["id"]], units_sold_12m=units,
                         median_ebay_price="" if med is None else f"{med:.2f}",
                         pull_minutes=k.get("pull_minutes", ""), size=k.get("size", ""), notes=k.get("notes", "")))
    sec_units = {s: sum(r["units_sold_12m"] for r in rows if r["section"] == s) for s in SECTIONS}
    order = sorted(SECTIONS, key=lambda s: (s == "Other", -sec_units[s]))  # "Other" always last
    rows.sort(key=lambda r: (order.index(r["section"]), -r["units_sold_12m"], r["part_type"]))
    return rows, order, sec_units


def write_csvs(rows):
    with EFFORT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=EFFORT_COLS)
        w.writeheader()
        w.writerows(rows)
    kept = read_kept(COURIER_CSV, "size_class", ["typical_courier_cost_gbp"])
    with COURIER_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COURIER_COLS)
        for s in SIZES:
            w.writerow([s, kept.get(s, {}).get("typical_courier_cost_gbp", "")])
    return {s: kept.get(s, {}).get("typical_courier_cost_gbp", "") for s in SIZES}


def box(ticked: bool) -> str:
    return "<span class='box'>&#10003;</span>" if ticked else "<span class='box'></span>"


def write_html(rows, order, sec_units, courier, today):
    e = html.escape
    head = ("<tr><th class='name'>Part type</th><th class='num'>Sold<br>12m</th><th class='num'>eBay<br>median</th>"
            + "".join(f"<th class='tick{' grp' if i == 0 else ''}'>{m}</th>" for i, m in enumerate(PULL_MINUTES))
            + "".join(f"<th class='tick{' grp' if i == 0 else ''}'>{s}</th>" for i, s in enumerate(SIZES))
            + "<th class='notes'>Notes</th></tr>")
    body = []
    for s in order:
        rs = [r for r in rows if r["section"] == s]
        body.append(f"<tbody><tr class='sec'><th colspan='{3 + len(PULL_MINUTES) + len(SIZES) + 1}'>{e(s)} <span>({len(rs)} types, "
                    f"{sec_units[s]:,} sold in 12 months)</span></th></tr>")
        for r in rs:
            price = f"£{float(r['median_ebay_price']):,.0f}" if r["median_ebay_price"] else "–"
            body.append(
                f"<tr data-type='{e(r['part_type_id'])}'><td class='name'>{e(r['part_type'])}</td>"
                f"<td class='num'>{r['units_sold_12m'] or '–'}</td><td class='num'>{price}</td>"
                + "".join(f"<td class='tick{' grp' if i == 0 else ''}'>{box(r['pull_minutes'] == m)}</td>"
                          for i, m in enumerate(PULL_MINUTES))
                + "".join(f"<td class='tick{' grp' if i == 0 else ''}'>{box(r['size'].lower() == z.lower())}</td>"
                          for i, z in enumerate(SIZES))
                + f"<td class='notes'>{e(r['notes'])}</td></tr>")
        body.append("</tbody>")
    courier_cells = "".join(f"<td><b>{s}</b><span class='fill'>£ {e(courier[s])}</span></td>" for s in SIZES)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Dismantler Worksheet</title><style>
:root{{--bg:#fff;--fg:#000;--muted:#444;--line:#999;--soft:#ececec}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#151515;--fg:#eee;--muted:#aaa;--line:#555;--soft:#262626}}}}
:root[data-theme="dark"]{{--bg:#151515;--fg:#eee;--muted:#aaa;--line:#555;--soft:#262626}}
@page{{size:A4 portrait;margin:10mm}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);font:15px/1.25 system-ui,Arial,sans-serif;margin:0 auto;max-width:190mm;padding:16px}}
h1{{font-size:24px;margin:0 0 4px}} p.how{{margin:0 0 10px;font-size:14px;color:var(--muted)}}
.courier{{border:2px solid var(--fg);padding:6px 8px;margin:0 0 12px}}
.courier h2{{font-size:16px;margin:0 0 4px}} .courier table{{width:100%;border-collapse:collapse}}
.courier td{{font-size:16px;padding:4px 6px;width:25%}} .courier .fill{{display:inline-block;min-width:60%;
  border-bottom:1px solid var(--fg);margin-left:8px;padding:0 4px}}
table.ws{{border-collapse:collapse;width:100%;table-layout:fixed}}
.ws th,.ws td{{border:1px solid var(--line);padding:3px 4px;vertical-align:middle}}
.ws thead th{{font-size:13px;background:var(--soft)}} .ws thead{{display:table-header-group}}
.ws tr{{break-inside:avoid;page-break-inside:avoid}}
.ws td{{height:10mm}}
.ws th.name,.ws td.name{{width:52mm;text-align:left;font-size:15px}}
.ws .num{{width:13mm;text-align:right;font-variant-numeric:tabular-nums}}
.ws .tick{{width:8.5mm;text-align:center;padding:2px}} .ws th.tick{{font-size:12px}}
.ws .grp{{border-left:2px solid var(--fg)}} .ws .notes{{text-align:left}} .ws th.notes{{border-left:2px solid var(--fg)}}
.ws td.notes{{border-left:2px solid var(--fg)}}
.ws tr.sec th{{text-align:left;font-size:17px;background:var(--fg);color:var(--bg);padding:5px 6px;
  break-after:avoid;page-break-after:avoid}}
.ws tr.sec span{{font-size:13px;font-weight:normal}}
.box{{display:inline-block;width:5mm;height:5mm;border:1.5px solid var(--fg);line-height:5mm;font-size:14px}}
.colgroup-h{{font-size:12px}}
@media print{{body{{max-width:none;padding:0;font-size:14px}} :root{{--bg:#fff;--fg:#000;--muted:#333;--line:#888;--soft:#e6e6e6}}}}
</style></head><body>
<h1>Dismantler worksheet</h1>
<p class="how">Tick how long each part takes to pull (minutes) and its parcel size. Most-sold part types come first in
each section; do the top of each section first. Sold 12m = units sold in the last 12 months; eBay median = typical
price paid per part (excluding postage). Printed {today:%d %b %Y}, {len(rows)} part types.</p>
<div class="courier"><h2>Typical courier cost by size</h2><table><tr>{courier_cells}</tr></table></div>
<table class="ws"><colgroup><col style="width:52mm"><col style="width:12mm"><col style="width:13mm">
{'<col style="width:8.5mm">' * (len(PULL_MINUTES) + len(SIZES))}<col></colgroup>
<thead><tr class="colgroup-h"><th></th><th colspan="2"></th><th colspan="5" class="grp">Pull time (minutes)</th>
<th colspan="4" class="grp">Size</th><th class="notes"></th></tr>{head}</thead>
{''.join(body)}</table></body></html>"""
    WORKSHEET.parent.mkdir(exist_ok=True)
    WORKSHEET.write_text(doc, encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    today = date.today()
    rows, order, sec_units = build_rows(today)
    courier = write_csvs(rows)
    write_html(rows, order, sec_units, courier, today)
    print(f"{len(rows)} part types -> {WORKSHEET.relative_to(ROOT)}, {EFFORT_CSV.relative_to(ROOT)}, "
          f"{COURIER_CSV.relative_to(ROOT)}")
    print("Sections: " + ", ".join(f"{s} ({sec_units[s]})" for s in order))
    print("First 20 rows:")
    for i, r in enumerate(rows[:20], 1):
        print(f"  {i:2}. [{r['section']}] {r['part_type']} - sold 12m {r['units_sold_12m']}, "
              f"eBay median {r['median_ebay_price'] or '-'}")


if __name__ == "__main__":
    main()
