"""
Shelf targets: how many of each part type to keep per fitment group, and the
gap to pull from incoming cars.

    python scripts/shelf_targets.py

Reads analytics/analytics.db (built by analytics_build.py), part_types.yaml
(fitment_level), model_map.yaml (platform) and shelf_rules.yaml. Writes the
shelf_targets table to analytics.db and reports/shelf_gaps.html. No BreakerPro
access; no customer data (part types, VEH refs, prices and dates only).

Fitment group: the model generation ("VW Golf MK7") for generation-level part
types, the platform ("MQB") for platform-level ones. A generation with no
platform in model_map.yaml stands alone.
"""

import fnmatch
import html
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from analytics_build import NOT_PARTS, PII_PATTERNS, load_model_map  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "analytics" / "analytics.db"
RULES_PATH = ROOT / "analytics" / "shelf_rules.yaml"
TYPES_PATH = ROOT / "analytics" / "part_types.yaml"
REPORTS = ROOT / "reports"
GAPS_HTML = REPORTS / "shelf_gaps.html"

STATUS_ORDER = {"PULL": 0, "LEAVE ON CAR": 1, "NEVER": 2}

SCHEMA = """
DROP TABLE IF EXISTS shelf_targets;
CREATE TABLE shelf_targets (part_type TEXT, label TEXT, fitment_level TEXT, fitment_group TEXT,
    platform TEXT, in_stock INTEGER, sold_12m INTEGER, sold_all INTEGER, rate_level TEXT,
    rate_basis_sales_12m INTEGER, monthly_rate REAL, target INTEGER, gap INTEGER,
    median_net REAL, median_ebay_net REAL, median_days INTEGER, gap_value REAL,
    status TEXT, reason TEXT, built_at TEXT, PRIMARY KEY (part_type, fitment_group));
"""


def load_rules(path: Path = RULES_PATH) -> dict:
    r = yaml.safe_load(path.read_text(encoding="utf-8"))
    for k in ("min_floor", "cover_months", "max_cap", "min_price", "min_sales_own"):
        assert isinstance(r.get(k), (int, float)) and r[k] >= 0, f"shelf_rules.yaml: {k} must be a number >= 0"
    assert r["min_floor"] <= r["max_cap"], "shelf_rules.yaml: min_floor is above max_cap"
    r["never_pull"] = r.get("never_pull") or []
    r["never_pull_except"] = r.get("never_pull_except") or []
    r["always_pull"] = r.get("always_pull") or []
    r["type_overrides"] = r.get("type_overrides") or {}
    return r


def load_types(path: Path = TYPES_PATH) -> list[dict]:
    types = yaml.safe_load(path.read_text(encoding="utf-8"))["types"]
    for t in types:
        t.setdefault("fitment_level", "generation")
        assert t["fitment_level"] in ("generation", "platform"), f"{t['id']}: bad fitment_level"
    return types


def never_pull_types(rules: dict, type_ids) -> set:
    hit = {t for t in type_ids if any(fnmatch.fnmatchcase(t, p) for p in rules["never_pull"])}
    return hit - {t for t in type_ids if any(fnmatch.fnmatchcase(t, p) for p in rules["never_pull_except"])}


def family_platforms(mm: dict) -> dict:
    """Family label as analytics_build.normalise_model writes it -> platform (or None)."""
    out = {}
    for mk, entries in mm["models"].items():
        label = mm["make_labels"].get(mk, mk.title())
        for e in entries:
            for g in e["generations"]:
                shown = "" if g["gen"] == e["model"] else f" {g['gen']}"
                out[f"{label} {e['model']}{shown}"] = g.get("platform") or None
    return out


def group_for(level: str, family: str, platform):
    return platform if level == "platform" and platform else family


def median(v):
    return statistics.median(v) if v else None


def build(today: date | None = None, log=print) -> dict:
    today = today or date.today()
    since = (today - timedelta(days=365)).isoformat()
    rules, types, mm = load_rules(), load_types(), load_model_map()
    labels = {t["id"]: t["label"] for t in types}
    level_of = {t["id"]: t["fitment_level"] for t in types}
    never = never_pull_types(rules, list(labels))
    for t in rules["always_pull"] + list(rules["type_overrides"]):
        assert t in labels, f"shelf_rules.yaml: unknown part type {t!r}"
    fam_plat = family_platforms(mm)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        veh = {r["veh_ref"]: r for r in db.execute("SELECT veh_ref, model_family FROM vehicles")}
        stock = db.execute("SELECT part_id, veh_ref, part_type, listing FROM stock").fetchall()
        sales = db.execute("SELECT part_type, model_family, sale_date, net_price, channel, days_to_sell "
                           "FROM sales").fetchall()

        # Group sizes for the fallbacks: generations the yard has handled, per platform.
        yard_fams = {r["model_family"] for r in veh.values() if r["model_family"] in fam_plat}
        gens_on_platform = Counter(fam_plat[f] for f in yard_fams if fam_plat[f])
        n_groups = {"generation": len(yard_fams),
                    "platform": len({group_for("platform", f, fam_plat[f]) for f in yard_fams})}

        # Stock: every live part counted once, in its vehicle's group.
        in_stock, counted, dead = Counter(), {}, 0
        for s in stock:
            if s["listing"] == "dead":
                dead += 1
                continue
            fam = veh[s["veh_ref"]]["model_family"] if s["veh_ref"] in veh else "(no vehicle)"
            key = (s["part_type"], group_for(level_of[s["part_type"]], fam, fam_plat.get(fam)))
            in_stock[key] += 1
            assert s["part_id"] not in counted, f"part {s['part_id']} counted twice"
            counted[s["part_id"]] = key

        # Sales, by group and by the fallback levels. Sales with no known
        # generation (hand-typed, "gen unknown") only count at type level.
        by_group, by_plat, by_type = defaultdict(list), defaultdict(list), defaultdict(list)
        for s in sales:
            t, fam = s["part_type"], s["model_family"]
            by_type[t].append(s)
            if fam not in fam_plat:
                continue
            by_group[(t, group_for(level_of[t], fam, fam_plat[fam]))].append(s)
            if level_of[t] == "generation" and fam_plat[fam]:
                by_plat[(t, fam_plat[fam])].append(s)

        def recent(rows):
            return [r for r in rows if (r["sale_date"] or "") >= since]

        rows = []
        built_at = datetime.now().isoformat(timespec="seconds")
        for (t, grp) in sorted(set(in_stock) | set(by_group)):
            lvl = level_of[t]
            fam_platform = fam_plat.get(grp) if lvl == "generation" else (grp if grp not in fam_plat else fam_plat[grp])
            own = by_group.get((t, grp), [])
            own12 = recent(own)
            # Rate source: own group, else platform (per generation), else type overall (per group).
            basis, rate_level, divisor = own, "own group", 1
            if len(own12) < rules["min_sales_own"]:
                plat_rows = by_plat.get((t, fam_platform), []) if lvl == "generation" and fam_platform else []
                if len(recent(plat_rows)) >= rules["min_sales_own"]:
                    basis, rate_level, divisor = plat_rows, "platform", max(gens_on_platform[fam_platform], 1)
                else:
                    basis, rate_level, divisor = by_type[t], "part type overall", max(n_groups[lvl], 1)
            basis12 = recent(basis)
            rate = len(basis12) / 12 / divisor
            ov = rules["type_overrides"].get(t, {})
            floor_, cap = ov.get("min_floor", rules["min_floor"]), ov.get("max_cap", rules["max_cap"])
            target = min(cap, max(floor_, math.ceil(round(rate * rules["cover_months"], 9))))
            med_net = median([float(r["net_price"]) for r in basis if r["net_price"] is not None])
            med_ebay = median([float(r["net_price"]) for r in basis
                               if r["channel"] == "ebay" and r["net_price"] is not None])
            med_days = median([r["days_to_sell"] for r in basis if r["days_to_sell"] is not None])
            stk = in_stock.get((t, grp), 0)
            if t in NOT_PARTS:
                status, reason = "NEVER", "not a single part type"
            elif t in never:
                status, reason = "NEVER", "never_pull rule"
            elif t in rules["always_pull"]:
                status, reason = "PULL", "always_pull rule"
            elif not own12:
                status, reason = "NEVER", "no sales in 12 months"
            elif target - stk > 0 and (med_net or 0) >= rules["min_price"]:
                status, reason = "PULL", "below target"
            elif target - stk > 0:
                status, reason = "LEAVE ON CAR", f"median price under £{rules['min_price']}"
            else:
                status, reason = "LEAVE ON CAR", "at or above target"
            if status == "NEVER":
                target = 0
            gap = target - stk
            price = med_ebay if med_ebay is not None else med_net
            rows.append(dict(part_type=t, label=labels[t], fitment_level=lvl, fitment_group=grp,
                             platform=fam_platform, in_stock=stk, sold_12m=len(own12), sold_all=len(own),
                             rate_level=rate_level, rate_basis_sales_12m=len(basis12),
                             monthly_rate=round(rate, 4), target=target, gap=gap,
                             median_net=round(med_net, 2) if med_net is not None else None,
                             median_ebay_net=round(med_ebay, 2) if med_ebay is not None else None,
                             median_days=round(med_days) if med_days is not None else None,
                             gap_value=round(max(gap, 0) * price, 2) if price is not None and status != "NEVER" else 0.0,
                             status=status, reason=reason, built_at=built_at))

        db.executescript(SCHEMA)
        cols = list(rows[0])
        db.executemany(f"INSERT INTO shelf_targets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [tuple(r[c] for c in cols) for r in rows])
        db.commit()
    finally:
        db.close()
    return dict(rows=rows, rules=rules, never=never, labels=labels, counted=counted, dead=dead,
                live_stock=len(stock) - dead, today=today)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

CSS = """
:root { --bg:#fff; --fg:#111; --muted:#555; --line:#ccc; --head:#eee; --pull:#0a6b2d; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --pull:#5fd38a; } }
:root[data-theme="dark"] { --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --pull:#5fd38a; }
body { background:var(--bg); color:var(--fg); font:14px/1.35 system-ui, sans-serif; margin:16px; }
h1 { font-size:20px; margin:0 0 4px; } p { margin:4px 0 10px; color:var(--muted); }
.wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { border-bottom:1px solid var(--line); padding:3px 6px; text-align:left; }
th { background:var(--head); position:sticky; top:0; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
.PULL { color:var(--pull); font-weight:600; }
@page { size:A4 landscape; margin:10mm; }
@media print { body { margin:0; font-size:10px; } th { position:static; } .wrap { overflow:visible; } }
"""


def gbp(v, dp=2):
    return "" if v is None else f"£{v:,.{dp}f}"


def gaps_html(b: dict) -> str:
    below = sorted((r for r in b["rows"] if r["gap"] > 0 and r["status"] != "NEVER"),
                   key=lambda r: (-r["gap_value"], r["label"], r["fitment_group"]))
    n_pull = sum(r["status"] == "PULL" for r in below)
    tot = sum(r["gap_value"] for r in below if r["status"] == "PULL")
    ru = b["rules"]
    body = []
    for r in below:
        body.append(
            f"<tr><td>{html.escape(r['label'])}</td><td>{html.escape(r['fitment_group'])}</td>"
            f"<td class='{r['status'].split()[0]}'>{r['status']}</td><td class=n>{r['in_stock']}</td>"
            f"<td class=n>{r['target']}</td><td class=n>{r['gap']}</td><td class=n>{r['sold_12m']}</td>"
            f"<td>{html.escape(r['rate_level'])}</td><td class=n>{gbp(r['median_ebay_net'] if r['median_ebay_net'] is not None else r['median_net'])}</td>"
            f"<td class=n>{'' if r['median_days'] is None else r['median_days']}</td>"
            f"<td class=n>{gbp(r['gap_value'], 0)}</td></tr>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Shelf gaps</title>
<style>{CSS}</style></head><body>
<h1>Shelf gaps: {len(below)} fitment groups below target</h1>
<p>{b['today']:%d %b %Y}. {n_pull} to PULL, gap value {gbp(tot, 0)} (gap &times; median eBay net price);
the rest are below target but under £{ru['min_price']} median, so LEAVE ON CAR.
Target = between {ru['min_floor']} and {ru['max_cap']}, {ru['cover_months']} months of sales. In stock excludes dead listings.
"Rate from" says where the sales rate came from when the group sold fewer than {ru['min_sales_own']} in 12 months.</p>
<div class=wrap><table><thead><tr><th>Part type</th><th>Fitment</th><th>Status</th><th class=n>In stock</th>
<th class=n>Target</th><th class=n>Gap</th><th class=n>Sold 12m</th><th>Rate from</th><th class=n>Median eBay net</th>
<th class=n>Days to sell</th><th class=n>Gap value</th></tr></thead><tbody>
{''.join(body)}
</tbody></table></div></body></html>
"""


def pii_hits(paths) -> list:
    hits = []
    for f in paths:
        txt = Path(f).read_text(encoding="utf-8")
        hits += [(Path(f).name, label) for label, rx in PII_PATTERNS for _ in rx.finditer(txt)]
    return hits


def checks(b: dict) -> bool:
    rows, ru, ok = b["rows"], b["rules"], True

    def say(name, passed, detail):
        nonlocal ok
        ok &= passed
        print(f"CHECK {name}: {detail}  -> {'PASS' if passed else 'FAIL'}")

    keys = Counter((r["part_type"], r["fitment_group"]) for r in rows)
    in_rows = {k: r["in_stock"] for r in rows for k in [(r["part_type"], r["fitment_group"])]}
    per_key = Counter(b["counted"].values())
    total = sum(r["in_stock"] for r in rows)
    say("stock counted once", max(keys.values()) == 1 and per_key == +Counter(in_rows)
        and len(b["counted"]) == b["live_stock"] == total,
        f"{b['live_stock']} live parts (excl. vehicle rows, {b['dead']} dead listings), "
        f"{len(b['counted'])} placed in a row, sum of in_stock {total}")
    bad = [r for r in rows if r["status"] != "NEVER" and not (
        ru["type_overrides"].get(r["part_type"], {}).get("min_floor", ru["min_floor"]) <= r["target"]
        <= ru["type_overrides"].get(r["part_type"], {}).get("max_cap", ru["max_cap"]))]
    say("target within floor and cap", not bad, f"{len(bad)} rows outside (NEVER rows excluded)")
    nev = [r for r in rows if r["part_type"] in b["never"]]
    say("never_pull types all NEVER", all(r["status"] == "NEVER" for r in nev),
        f"{len(nev)} rows over {len(b['never'])} types")
    airbags = {t for t in b["labels"] if t.startswith("airbag_") and t != "airbag_squib_slip_ring"}
    say("airbag types covered", airbags <= b["never"], f"never_pull matches {len(b['never'])} types")
    for t in sorted(b["never"]):
        print(f"    never_pull: {t:24} {b['labels'][t]}")
    outs = [GAPS_HTML] + sorted(REPORTS.glob("shelf_check_*.html"))
    hits = pii_hits(outs)
    say("personal-data scan", not hits, f"{len(outs)} files, {len(hits)} hits")
    for h in hits[:20]:
        print(f"    {h[0]}: {h[1]}")  # the match itself is not printed
    return ok


def main():
    b = build()
    REPORTS.mkdir(exist_ok=True)
    GAPS_HTML.write_text(gaps_html(b), encoding="utf-8")
    rows = b["rows"]
    st = Counter(r["status"] for r in rows)
    below = [r for r in rows if r["gap"] > 0 and r["status"] != "NEVER"]
    print(f"shelf_targets: {len(rows)} rows  ({', '.join(f'{k} {v}' for k, v in sorted(st.items()))})")
    print(f"rate level: {dict(Counter(r['rate_level'] for r in rows))}")
    print(f"below target: {len(below)} fitment groups ({sum(r['status'] == 'PULL' for r in below)} PULL, "
          f"gap value £{sum(r['gap_value'] for r in below if r['status'] == 'PULL'):,.0f})")
    print(f"wrote {GAPS_HTML.relative_to(ROOT)}")
    sys.exit(0 if checks(b) else 1)


if __name__ == "__main__":
    main()
