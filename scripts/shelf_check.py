"""
Per-car shelf check: what to pull from one vehicle.

    python scripts/shelf_check.py VEH130

Looks up the vehicle's model generation and platform in analytics/analytics.db
and writes reports/shelf_check_VEH130.html, a one-page A4 sheet listing every
part type with sales history for that fitment: status, in stock, target, gap,
median eBay net price and median days to sell. PULL first (by price x gap),
then LEAVE ON CAR, then NEVER. Run scripts/shelf_targets.py first.
"""

import argparse
import html
import re
import sqlite3
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from shelf_targets import DB_PATH, REPORTS, family_platforms, load_model_map  # noqa: E402

STATUSES = ("PULL", "LEAVE ON CAR", "NEVER")
SPLIT_AT = 34  # rows per column before the list flows into a second column

CSS = """
:root { --bg:#fff; --fg:#111; --muted:#555; --line:#bbb; --head:#e8e8e8; --pull:#0a6b2d; --sec:#f3f3f3; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --pull:#5fd38a; --sec:#1d1d1d; } }
:root[data-theme="dark"] { --bg:#141414; --fg:#eee; --muted:#aaa; --line:#444; --head:#262626; --pull:#5fd38a; --sec:#1d1d1d; }
@page { size:A4; margin:10mm; }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--fg); font:12px/1.3 system-ui, sans-serif; margin:16px; }
h1 { font-size:18px; margin:0 0 2px; }
.meta { color:var(--muted); margin:0 0 6px; }
.total { font-size:14px; font-weight:600; margin:4px 0 8px; padding:4px 8px; border:2px solid var(--fg); display:inline-block; }
.cols { display:flex; gap:4mm; align-items:flex-start; }
.cols > table { flex:1 1 0; min-width:0; }
table { border-collapse:collapse; width:100%; table-layout:fixed; }
th, td { border-bottom:1px solid var(--line); padding:1px 3px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
th { background:var(--head); text-align:right; font-size:10px; }
th:first-child, td:first-child { text-align:left; }
td { text-align:right; font-variant-numeric:tabular-nums; }
td:first-child { white-space:normal; overflow-wrap:anywhere; line-height:1.15; }
tr.sec td { background:var(--sec); font-weight:700; text-align:left; }
tr.PULL td:first-child { color:var(--pull); font-weight:600; }
.note { color:var(--muted); font-size:10px; margin-top:6px; }
@media print { body { margin:0; font-size:9px; line-height:1.2; } h1 { font-size:15px; } .total { font-size:12px; }
  th { font-size:8.5px; } .note { font-size:8px; } }
@media (max-width: 700px) { .cols { flex-direction:column; } .cols > table { width:100%; } }
"""

COLGROUP = ("<colgroup><col style='width:46%'><col style='width:9%'><col style='width:9%'>"
            "<col style='width:9%'><col style='width:15%'><col style='width:12%'></colgroup>")
HEAD = ("<thead><tr><th>Part type</th><th>Stock</th><th>Target</th><th>Gap</th>"
        "<th>eBay net</th><th>Days</th></tr></thead>")


def gbp(v):
    return "" if v is None else f"£{v:,.0f}" if v >= 100 else f"£{v:,.2f}"


def check(veh_ref: str) -> dict:
    veh_ref = veh_ref.strip().upper()
    if not re.fullmatch(r"VEH\d+", veh_ref):
        raise SystemExit(f"{veh_ref!r} is not a VEH ref (e.g. VEH130)")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        v = db.execute("SELECT veh_ref, model_family, generation, model_method, year FROM vehicles "
                       "WHERE veh_ref = ?", (veh_ref,)).fetchone()
        if v is None:
            raise SystemExit(f"{veh_ref} not found in analytics.db (rerun analytics_build.py?)")
        fam = v["model_family"]
        fam_plat = family_platforms(load_model_map())
        if fam not in fam_plat:
            raise SystemExit(f"{veh_ref}: model generation not known ({fam}); no fitment group")
        platform = fam_plat[fam]
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM shelf_targets WHERE sold_all > 0 AND "
            "((fitment_level = 'generation' AND fitment_group = :fam) OR "
            " (fitment_level = 'platform' AND fitment_group = :grp))",
            dict(fam=fam, grp=platform or fam))]
    finally:
        db.close()
    for r in rows:
        r["price"] = r["median_ebay_net"] if r["median_ebay_net"] is not None else r["median_net"]
    rows.sort(key=lambda r: (STATUSES.index(r["status"]),
                             -((r["price"] or 0) * max(r["gap"], 0)) if r["status"] == "PULL" else 0,
                             r["label"]))
    pull = [r for r in rows if r["status"] == "PULL"]
    return dict(v=dict(v), family=fam, platform=platform, rows=rows,
                n_pull=len(pull), pull_value=sum(r["price"] or 0 for r in pull))


def render(c: dict) -> str:
    lines = []
    for st in STATUSES:
        grp = [r for r in c["rows"] if r["status"] == st]
        if not grp:
            continue
        lines.append(f"<tr class=sec><td colspan=6>{st} ({len(grp)})</td></tr>")
        for r in grp:
            plat = " ▲" if r["fitment_level"] == "platform" and c["platform"] else ""
            lines.append(f"<tr class='{st.split()[0]}'><td title='{html.escape(r['label'])}'>{html.escape(r['label'])}{plat}</td>"
                         f"<td>{r['in_stock']}</td><td>{r['target'] if st != 'NEVER' else ''}</td>"
                         f"<td>{r['gap'] if st != 'NEVER' else ''}</td><td>{gbp(r['price'])}</td>"
                         f"<td>{'' if r['median_days'] is None else r['median_days']}</td></tr>")
    # Flow into two columns; never leave a section header alone at the bottom of column 1.
    half = (len(lines) + 1) // 2 if len(lines) > SPLIT_AT else len(lines)
    if half < len(lines) and lines[half - 1].startswith("<tr class=sec>"):
        half -= 1
    tables = "".join(f"<table>{COLGROUP}{HEAD}<tbody>{''.join(part)}</tbody></table>"
                     for part in (lines[:half], lines[half:]) if part)
    v = c["v"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Shelf check {v['veh_ref']}</title>
<style>{CSS}</style></head><body>
<h1>Shelf check: {v['veh_ref']} &middot; {html.escape(c['family'])}</h1>
<p class=meta>Year {html.escape(str(v['year'] or '?'))} &middot; generation from {html.escape(v['model_method'])}
&middot; platform {html.escape(c['platform'] or 'not set')} &middot; {len(c['rows'])} part types with sales history</p>
<div class=total>PULL {c['n_pull']} parts &middot; expected value {gbp(c['pull_value'])}</div>
<div class=cols>{tables}</div>
<p class=note>Stock / target / gap are for the whole fitment group (▲ = platform-wide part, counted across
{html.escape(c['platform'] or '')}). eBay net = median eBay price ex VAT, no postage (all sales if none on eBay).
Days = median days to sell. Expected value = one of each PULL part at that price. PULL is sorted by price &times; gap.</p>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("veh_ref")
    a = ap.parse_args()
    c = check(a.veh_ref)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"shelf_check_{c['v']['veh_ref']}.html"
    out.write_text(render(c), encoding="utf-8")
    print(f"{c['v']['veh_ref']} {c['family']} (platform {c['platform'] or 'not set'}): "
          f"{len(c['rows'])} part types, PULL {c['n_pull']} worth £{c['pull_value']:,.2f}")
    for r in c["rows"]:
        if r["status"] == "PULL":
            print(f"   PULL  {r['label'][:44]:44} stock {r['in_stock']} target {r['target']} gap {r['gap']} "
                  f"{gbp(r['price']):>8}  {'' if r['median_days'] is None else r['median_days']} days")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
