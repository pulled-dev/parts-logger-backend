#!/usr/bin/env python3
"""
Pulled Apart - Vehicle Stock Quick Reference.

Step 3 of Update Vehicles.bat. Reads the newest BreakerPro CSV in done/ and:
  1. Builds the searchable quick reference page (all vehicles) and uploads it
     to pulledapart.co.uk at an unlisted, noindexed address.
  2. Writes a printable wall sheet of vehicles added since the last sheet
     (stock-sheets/Wall Sheet VEHxxx-VEHyyy.html - open it, Ctrl+P, landscape).
  3. Flags rows with a missing engine code or paint code so they can be
     fixed in BreakerPro.

Usage:
    python update_stock_sheets.py              # normal run
    python update_stock_sheets.py --no-upload  # build files only
    python update_stock_sheets.py --reprint-from 146   # redo wall sheet from VEH146

Stdlib only. Uses Windows OpenSSH (host alias pa-sg), same as update_website.py.
"""

import csv
import datetime
import glob
import html
import json
import os
import re
import secrets
import subprocess
import sys

SSH_EXE = r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP_EXE = r"C:\Windows\System32\OpenSSH\scp.exe"
SSH_HOST = "pa-sg"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
LIVE_PATH = "/home/customer/www/pulledapart.co.uk/public_html"
LIVE_URL = "https://pulledapart.co.uk"

HERE = os.path.dirname(os.path.abspath(__file__))
DONE_DIR = os.path.join(HERE, "done")
OUT_DIR = os.path.join(HERE, "stock-sheets")
STATE_FILE = os.path.join(OUT_DIR, "stock-sheets-state.json")

FIRST_UNPRINTED_DEFAULT = 146  # VEH1-145 are already on the wall

MAKE_FIX = {"seat": "SEAT", "levc": "LEVC", "vw": "Volkswagen", "skoda": "Skoda",
            "audi": "Audi", "volkswagen": "Volkswagen", "cupra": "Cupra",
            "mercedes": "Mercedes"}
PAINT_RE = re.compile(r"^\s*(L[A-Z0-9][0-9][A-Z0-9])\b\s*(.*)$", re.I)
ENGINE_CODE_RE = re.compile(r"\b[A-Z]{3,4}\b")
NOT_CODES = {"TDI", "TSI", "TFSI", "FSI", "GTI", "GTD", "MPI", "CDI", "HDI", "DSG"}


# ---------------------------------------------------------------- data

def find_csv():
    csvs = sorted(glob.glob(os.path.join(DONE_DIR, "*.csv")), key=os.path.getmtime)
    return csvs[-1] if csvs else None


def clean_colour(c):
    c = " ".join((c or "").split())
    m = PAINT_RE.match(c)
    if not m:
        return c, False
    code = m.group(1).upper()
    name = m.group(2).title()
    return (f"{code} {name}".strip()), True


def load_vehicles(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for _ in range(3):
            f.readline()
        rows = list(csv.DictReader(f))
    vehicles, odd = {}, []
    for r in rows:
        raw = (r.get("Stock Reference") or "").strip()
        m = re.fullmatch(r"veh\s*0*(\d+)", raw, re.I)
        if not m:
            if raw:
                odd.append(raw)
            continue
        n = int(m.group(1))
        make = " ".join((r.get("Make") or "").split())
        make = MAKE_FIX.get(make.lower(), make)
        colour, has_paint = clean_colour(r.get("Colour"))
        engine = " ".join((r.get("Engine") or "").split())
        vehicles[n] = {
            "n": n,
            "ref": f"VEH{n}",
            "make": make,
            "model": " ".join((r.get("Model") or "").split()),
            "year": (r.get("Year Manufacture") or "").strip(),
            "engine": engine,
            "colour": colour,
            "has_paint": has_paint,
            "has_engine_code": any(t not in NOT_CODES
                                   for t in ENGINE_CODE_RE.findall(engine.upper())),
        }
    return [vehicles[k] for k in sorted(vehicles)], odd


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- pages

E = html.escape


def mobile_page(vehicles, stamp):
    data = json.dumps([[v["n"], v["make"], v["model"], v["year"], v["engine"], v["colour"]]
                       for v in vehicles], ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Vehicle Stock - Quick Reference</title>
<style>
:root{{--ink:#1A1A1A;--red:#E31E24;--bg:#F5F5F5;--card:#fff;--line:#E3E3E3;--mute:#666}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.35 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}}
header{{position:sticky;top:0;background:var(--ink);color:#fff;padding:12px 16px;z-index:2}}
h1{{margin:0 0 8px;font-size:17px;font-weight:700}}
h1 span{{color:var(--red)}}
input{{width:100%;padding:12px 14px;font-size:17px;border:0;border-radius:8px}}
.meta{{font-size:12px;color:#bbb;margin-top:6px}}
main{{padding:10px 16px 40px;max-width:820px;margin:0 auto}}
.v{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--red);border-radius:8px;padding:10px 12px;margin:8px 0}}
.top{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.ref{{font-weight:800;font-size:18px}}
.mm{{font-weight:600}}
.yr{{color:var(--mute);margin-left:auto}}
.d{{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin-top:4px;font-size:15px}}
.d b{{color:var(--mute);font-weight:500}}
.none{{text-align:center;color:var(--mute);padding:30px 0}}
</style></head>
<body>
<header>
<h1>PULLED <span>APART</span> &middot; Vehicle Stock</h1>
<input id="q" type="search" placeholder="Search VEH no., make, model, engine, colour..." autocomplete="off" autofocus>
<div class="meta" id="count"></div>
</header>
<main id="list"></main>
<script>
const V={data};
const UPDATED={json.dumps(stamp)};
const list=document.getElementById('list'),q=document.getElementById('q'),count=document.getElementById('count');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
const idx=V.map(v=>({{v,t:(v[1]+' '+v[2]+' '+v[3]+' '+v[4]+' '+v[5]).toLowerCase()}}));
function card(v){{return '<div class="v"><div class="top"><span class="ref">VEH'+v[0]+'</span><span class="mm">'+esc(v[1])+' '+esc(v[2])+'</span><span class="yr">'+esc(v[3])+'</span></div><div class="d"><b>Engine</b><span>'+esc(v[4]||'-')+'</span><b>Colour</b><span>'+esc(v[5]||'-')+'</span></div></div>';}}
function run(){{
  const toks=q.value.toLowerCase().trim().split(/\\s+/).filter(Boolean);
  const hits=idx.filter(({{v,t}})=>toks.every(k=>{{
    const m=k.match(/^(?:veh)?0*(\\d{{1,3}})$/);
    if(m)return +m[1]===v[0];
    return t.includes(k);
  }})).map(x=>x.v);
  list.innerHTML=hits.length?hits.map(card).join(''):'<div class="none">No vehicles match.</div>';
  count.textContent=hits.length+' of '+V.length+' vehicles \\u00b7 updated '+UPDATED;
}}
q.addEventListener('input',run);run();
</script>
</body></html>
"""


def wall_sheet(vehicles, stamp):
    rows = "\n".join(
        f"<tr><td class=r>{E(v['ref'])}</td><td>{E(v['make'])}</td><td>{E(v['model'])}</td>"
        f"<td class=c>{E(v['year'])}</td><td>{E(v['engine'])}</td><td>{E(v['colour'])}</td></tr>"
        for v in vehicles)
    rng = f"{vehicles[0]['ref']} - {vehicles[-1]['ref']}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Vehicle Stock {rng}</title>
<style>
@page{{size:A4 landscape;margin:12mm}}
body{{font:13pt Calibri,Arial,sans-serif;color:#000;margin:20px}}
.bar{{margin-bottom:12px}} .bar button{{font-size:14px;padding:8px 14px}}
.bar p{{font:12px Arial;color:#555;margin:6px 0 0}}
table{{width:100%;border-collapse:collapse}}
thead{{display:table-header-group}}
th{{background:#333;color:#fff;text-align:left;padding:5px 8px;font-weight:600}}
td{{border:1px solid #999;padding:4px 8px}}
tr:nth-child(even) td{{background:#eee}}
td.r{{font-weight:700;white-space:nowrap}} td.c,th.c{{text-align:center}}
tr{{page-break-inside:avoid}}
@media print{{.bar{{display:none}} body{{margin:0}} *{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
</style></head><body>
<div class="bar"><button onclick="print()">Print</button>
<p>{rng} &middot; {len(vehicles)} vehicles &middot; from BreakerPro export {E(stamp)}. Print landscape, A4.</p></div>
<table><thead><tr><th>VEH</th><th>Make</th><th>Model</th><th class=c>Year</th><th>Engine (size + code)</th><th>Colour / Paint code</th></tr></thead>
<tbody>
{rows}
</tbody></table>
</body></html>
"""


# ---------------------------------------------------------------- upload

def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timed out"
    except FileNotFoundError:
        return 1, "", f"not found: {cmd[0]}"


def upload(local_file, slug):
    remote_dir = f"{LIVE_PATH}/{slug}"
    rc, _, err = run([SSH_EXE] + SSH_OPTS + [SSH_HOST, f"mkdir -p {remote_dir}"])
    if rc:
        return False, f"mkdir failed: {err}"
    rc, _, err = run([SCP_EXE] + SSH_OPTS + [local_file, f"{SSH_HOST}:{remote_dir}/index.html"], 120)
    if rc:
        return False, f"upload failed: {err}"
    run([SSH_EXE] + SSH_OPTS + [SSH_HOST, f"wp sg purge --path={LIVE_PATH}"], 60)
    url = f"{LIVE_URL}/{slug}/"
    rc, out, _ = run([SSH_EXE] + SSH_OPTS + [SSH_HOST,
                      f'curl -s -o /dev/null -w "%{{http_code}}" "{url}?v={secrets.token_hex(3)}"'], 30)
    if out != "200":
        return False, f"uploaded but {url} returned {out or 'no response'}"
    return True, url


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    no_upload = "--no-upload" in args
    reprint = None
    if "--reprint-from" in args:
        reprint = int(re.sub(r"\D", "", args[args.index("--reprint-from") + 1]))

    print("")
    print("================================================")
    print("  Updating Vehicle Stock quick reference")
    print("================================================")

    path = find_csv()
    if not path:
        print("  ** No CSV in done/ - run the import first.")
        return 1
    stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d %b %Y %H:%M")
    vehicles, odd = load_vehicles(path)
    if not vehicles:
        print(f"  ** No vehicles read from {os.path.basename(path)}")
        return 1
    print(f"  CSV: {os.path.basename(path)}")
    print(f"  Vehicles: {len(vehicles)} (VEH{vehicles[0]['n']} - VEH{vehicles[-1]['n']})")

    os.makedirs(OUT_DIR, exist_ok=True)
    state = load_state()
    if "slug" not in state:
        state["slug"] = "stock-ref-" + secrets.token_hex(4)
    first = reprint if reprint is not None else state.get("next_to_print", FIRST_UNPRINTED_DEFAULT)

    # 1. quick reference page (all vehicles)
    page = os.path.join(OUT_DIR, "Vehicle Stock - Quick Reference.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(mobile_page(vehicles, stamp))
    print(f"  Quick reference saved: stock-sheets\\{os.path.basename(page)}")

    # 2. wall sheet for new vehicles
    new = [v for v in vehicles if v["n"] >= first]
    if new:
        name = f"Wall Sheet {new[0]['ref']}-{new[-1]['ref']}.html"
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(wall_sheet(new, stamp))
        print(f"  NEW wall sheet ({len(new)} vehicles): stock-sheets\\{name}")
        print("     open it and press Print (A4 landscape)")
        state["next_to_print"] = new[-1]["n"] + 1
    else:
        print(f"  No new vehicles since VEH{first - 1} - no wall sheet needed.")

    # 3. data warnings
    gaps = [i for i in range(vehicles[0]["n"], vehicles[-1]["n"] + 1)
            if i not in {v["n"] for v in vehicles}
            and i not in state.get("deleted_refs", [])]
    warn = [(v["ref"], "no paint code") for v in new if not v["has_paint"]]
    warn += [(v["ref"], "no engine code") for v in new if not v["has_engine_code"]]
    if warn or odd or gaps:
        print("")
        print("  Check in BreakerPro:")
        for ref, why in sorted(warn, key=lambda w: int(w[0][3:])):
            print(f"    {ref}: {why}")
        for o in odd:
            print(f"    Stock Reference not in VEHnnn format: {o!r} (left out)")
        if gaps:
            print(f"    Missing from export: {', '.join('VEH%d' % g for g in gaps)}")

    # 4. upload
    print("")
    ok = True
    if no_upload:
        print("  Upload skipped (--no-upload).")
    else:
        print("  Uploading to pulledapart.co.uk ...")
        ok, msg = upload(page, state["slug"])
        if ok:
            state["url"] = msg
            print(f"  Live: {msg}")
        else:
            print(f"  ** Website upload problem: {msg}")
            print("  ** The local copy above is still up to date.")

    save_state(state)
    if state.get("url"):
        print(f"  Phone bookmark: {state['url']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
