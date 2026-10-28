"""
Build the local analytics store and reports: what sells, at what price, how
fast, by part type and model.

    python scripts/analytics_build.py [--no-llm] [--seed 7]

1. Extract from BreakerPro, read-only (scripts/analytics_extract.py).
2. Classify every distinct part name (scripts/part_classifier.py): rules,
   then the Claude API for the rest (cached; skipped with --no-llm or when
   no credentials are found). Confidence < 0.7 -> analytics/review_queue.csv.
3. Write analytics/analytics.db (SQLite, git-ignored): part_name_map, sales,
   sales_excluded, vehicles, stock, llm_cache, llm_runs.
4. Write reports/analytics_YYYY-MM-DD.html + CSVs (git-ignored) and print
   the checks (mapping coverage, net reconciliation, 20-name spot check,
   API cost).

Nothing is sent anywhere except part names to the Claude API.
"""

import argparse
import csv
import html
import random
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

sys.path[:0] = [str(Path(__file__).resolve().parent)]
import part_classifier as pc  # noqa: E402
from analytics_extract import extract  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "analytics" / "analytics.db"
REVIEW_PATH = ROOT / "analytics" / "review_queue.csv"
REPORTS = ROOT / "reports"

PULL_FIRST_MAX_DAYS = 30
PULL_FIRST_MIN_PRICE = 40
DONT_PULL_COVER_MONTHS = 24  # "Don't pull": more cover than this, or no sales in 12 months
TYPE_MODEL_MIN_SALES = 5
NOT_PARTS = {"misc_unknown", "vehicle_listing"}  # never in the stop / pull lists
# Engines, gearboxes and doors often went as cash yard sales that were never
# invoiced (removed on the mobile app instead) before the cutover, so their
# sales are under-recorded and their cover overstated. Flag only; do not act.
YARD_SALE_BIAS_TYPES = {"engine_complete", "gearbox_manual", "gearbox_auto", "gearbox_dsg",
                        "door_bare", "door_complete"}
# Postage / courier charges typed in as a part line (normally they sit on
# separate DELIVERY lines, which are never counted as parts revenue).
POSTAGE_RX = re.compile(r"\b(POSTAGE|COURIER|SHIPPING|CARRIAGE|P ?& ?P|DELIVERY( CHARGE)?|PALLET)\b")

# Personal-data patterns for the output scan (emails, UK postcodes, UK phones).
PII_PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("UK postcode", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[ABD-HJLNP-UW-Z]{2}\b")),
    ("UK phone", re.compile(r"(?<!\d)(?:\+44 ?7\d{3}|07\d{3}|01\d{3}|02\d)[ -]?\d{3}[ -]?\d{3,4}(?!\d)")),
]

# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

MODEL_MAP_PATH = ROOT / "analytics" / "model_map.yaml"


def load_model_map(path: Path = MODEL_MAP_PATH) -> dict:
    mm = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entries in mm["models"].values():
        for e in entries:
            e["_rx"] = re.compile(e["match"])
            for g in e["generations"]:
                g["gen"] = str(g["gen"])  # YAML reads codes such as 121 as numbers
                g["_codes"] = [re.compile(rf"\b{re.escape(str(c))}\b") for c in g.get("codes") or []]
                g["_vin"] = {str(c).upper() for c in g.get("vin") or []}
    return mm


# VAG makers' VIN prefixes (WMI). Other makes (LEVC, Mercedes, MINI) are not decoded.
VAG_WMI = {"WVW", "WV1", "WV2", "WV3", "WAU", "WA1", "WUA", "TRU", "VSS", "TMB", "VWV", "WVG", "3VW", "AAV"}
# VIN position 10 -> model year (2001-2030 cycle; V-Y are 1997-2000 or 2027-2030).
_VIN_YEARS = dict(zip("123456789ABCDEFGHJKLMNPRSTVWXY", range(2001, 2031)))


def vin_info(r: dict) -> dict:
    """Decoder inputs from the VIN pieces the extract returns (never the full VIN)."""
    code, wmi = (r.get("vin_model_code") or "").upper(), (r.get("vin_wmi") or "").upper()
    if not r.get("vin_len"):
        return dict(status="no VIN")
    if r["vin_len"] != 17:
        return dict(status="VIN not 17 characters")
    if wmi not in VAG_WMI:
        return dict(status="not a VAG VIN")
    return dict(status="ok", code=code, year=_VIN_YEARS.get((r.get("vin_year_char") or "").upper()))


def _year(y):
    m = re.search(r"(19|20)\d{2}", str(y or ""))
    return int(m.group(0)) if m else None


def normalise_model(mm: dict, make, model, year=None, vin=None) -> dict:
    """('SEAT', 'IBIZA FR', '2014') -> make SEAT, model Ibiza, gen 6J, method year.

    method: vin (VAG model code at VIN positions 7-8), code (generation written
    in the string), year (inferred from the year of manufacture), single (model
    has one generation), gen unknown, unmapped (no model matched), no model
    (blank). vin: vin_info() output, or None."""
    mk = " ".join(str(make or "").upper().split())
    mk = mm["make_aliases"].get(mk, mk)
    m = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", str(model or "").upper())).strip()
    label = mm["make_labels"].get(mk, mk.title() or "Unknown")
    if not m:
        return dict(make=mk or None, model=None, gen=None, method="no model", family=f"{label} (no model)")
    entry = next((e for e in mm["models"].get(mk, []) if e["_rx"].search(m)), None)
    if entry is None:
        return dict(make=mk, model=None, gen=None, method="unmapped", family=f"{label} {m} (unmapped)")
    gens, gen, method = entry["generations"], None, None
    if vin and vin.get("status") == "ok":
        cands = [g for g in gens if vin["code"] in g["_vin"]]
        if len(cands) > 1:  # code shared by generations (Polo 6R/6C, Passat 3C, Tiguan 5N)
            named = [g for g in cands if any(rx.search(m) for rx in g["_codes"])]
            vy = vin.get("year")
            in_year = [g for g in cands if vy and g["years"][0] <= vy <= g["years"][1]]
            if len(named) == 1:  # the generation written in the name, if the VIN allows it
                cands = named
            elif in_year:  # else the VIN model year
                cands = in_year
            elif vy:  # model year just past a range (e.g. a 2024 Tiguan 5N): nearest
                cands = [min(cands, key=lambda g: min(abs(vy - g["years"][0]), abs(vy - g["years"][1])))]
        if len(cands) == 1:
            gen, method = cands[0], "vin"
    if gen is None:
        gen = next((g for g in gens if any(rx.search(m) for rx in g["_codes"])), None)
        method = "code" if gen else None
    if gen:
        pass
    elif (y := _year(year)) is not None:
        gen = next((g for g in gens if g["years"][0] <= y <= g["years"][1]), None)
        method = "year" if gen else None
    if gen is None and len(gens) == 1:
        gen, method = gens[0], "single"
    if gen is None:
        return dict(make=mk, model=entry["model"], gen=None, method="gen unknown",
                    family=f"{label} {entry['model']} (gen unknown)")
    shown = "" if gen["gen"] == entry["model"] else f" {gen['gen']}"
    return dict(make=mk, model=entry["model"], gen=gen["gen"], method=method,
                family=f"{label} {entry['model']}{shown}")


NAME_MAKES = {"VOLKSWAGEN": "VOLKSWAGEN", "VW": "VOLKSWAGEN", "VOLKSWAGON": "VOLKSWAGEN",
              "VOLKWAGEN": "VOLKSWAGEN", "VOLKSWGAEN": "VOLKSWAGEN", "AUDI": "AUDI", "SEAT": "SEAT",
              "SKODA": "SKODA", "LEVC": "LEVC", "MINI": "MINI", "MERCEDES": "MERCEDES"}


def model_from_name(mm: dict, name: str) -> dict:
    """Model from a hand-typed (MISC) part name, e.g. 'VOLKSWAGEN GOLF MK7 FRONT BUMPER'.

    Whole-word matches only. Models flagged name_needs_make count only when the
    make is written too ('PICK UP' is not a VW up!). A model whose match sits
    inside a longer one is dropped (GOLF inside GOLF PLUS). Two different models
    in one name -> ambiguous, not guessed. Generation: a code in the name, else
    the year(s) in it (all must agree), else single-generation models."""
    n = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper())).strip()
    words = set(n.split())
    makes = {NAME_MAKES[w] for w in words if w in NAME_MAKES}
    hits = []
    for mk, entries in mm["models"].items():
        for e in entries:
            if e.get("name_needs_make") and mk not in makes:
                continue
            m = e["_rx"].search(n)
            if m:
                hits.append((mk, e, m.span()))
    hits = [h for h in hits if not any(o is not h and o[2][0] <= h[2][0] and h[2][1] <= o[2][1]
                                       and o[2] != h[2] for o in hits)]
    if makes and any(h[0] in makes for h in hits):
        hits = [h for h in hits if h[0] in makes]
    models = {(h[0], h[1]["model"]) for h in hits}
    if len(models) != 1:
        return dict(make=None, model=None, gen=None, family="Unknown (no model)",
                    method="name ambiguous" if models else "name no model")
    mk, e, _ = hits[0]
    label = mm["make_labels"].get(mk, mk.title())
    gens = e["generations"]
    gen, method = next((g for g in gens if any(rx.search(n) for rx in g["_codes"])), None), "name code"
    if gen is None:
        years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", n)]
        years += [2000 + int(a) for a, b in re.findall(r"\b(\d{2}) (\d{2})\b", n)
                  if 0 <= int(a) <= 30 and 0 <= int(b) <= 30 and int(a) <= int(b)]
        years += [2000 + int(b) for a, b in re.findall(r"\b(\d{2}) (\d{2})\b", n)
                  if 0 <= int(a) <= 30 and 0 <= int(b) <= 30 and int(a) <= int(b)]
        found = {g["gen"] for y in years for g in gens if g["years"][0] <= y <= g["years"][1]}
        if len(found) == 1:
            gen, method = next(g for g in gens if g["gen"] in found), "name year"
    if gen is None and len(gens) == 1:
        gen, method = gens[0], "name single"
    if gen is None:
        return dict(make=mk, model=e["model"], gen=None, method="name gen unknown",
                    family=f"{label} {e['model']} (gen unknown)")
    shown = "" if gen["gen"] == e["model"] else f" {gen['gen']}"
    return dict(make=mk, model=e["model"], gen=gen["gen"], method=method, family=f"{label} {e['model']}{shown}")


def name_key(name) -> str:
    return " ".join(str(name or "").upper().split())


def added_from_part_id(part_id: str):
    """Part ID '1659-070726-…' -> 2026-07-07 (DDMMYY segment)."""
    m = re.match(r"^\d+-(?:MISC)?(\d{2})(\d{2})(\d{2})", part_id or "")
    if not m:
        return None
    try:
        return datetime(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def pct(values, q):
    """Percentile with linear interpolation (q in 0..100); None for empty."""
    v = sorted(values)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q / 100
    f, c = int(k), min(int(k) + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


# --------------------------------------------------------------------------
# SQLite (analytics store only)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (raw_name TEXT PRIMARY KEY, part_type TEXT, position TEXT,
    confidence REAL, model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS llm_runs (run_at TEXT, model TEXT, names INTEGER, batches INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL);
DROP TABLE IF EXISTS part_name_map;
CREATE TABLE part_name_map (raw_name TEXT PRIMARY KEY, part_type TEXT, position TEXT, method TEXT,
    confidence REAL, rows_stock INTEGER, rows_sold INTEGER, rows_deleted INTEGER);
DROP TABLE IF EXISTS sales;
CREATE TABLE sales (invoice_id TEXT, part_id TEXT, veh_ref TEXT, make TEXT, model TEXT,
    model_family TEXT, model_method TEXT, part_name TEXT, part_type TEXT, position TEXT, sale_date TEXT,
    net_price REAL, gross_price REAL, channel TEXT, added_date TEXT, added_source TEXT,
    days_to_sell INTEGER);
DROP TABLE IF EXISTS sales_excluded;
CREATE TABLE sales_excluded (reason TEXT, lines INTEGER, net_total REAL);
DROP TABLE IF EXISTS misc_demand;
CREATE TABLE misc_demand (part_type TEXT, model TEXT, count INTEGER, net_revenue REAL);
DROP TABLE IF EXISTS unknown_vehicle_sales;
CREATE TABLE unknown_vehicle_sales (grp TEXT, invoice_id TEXT, part_id TEXT, part_name TEXT,
    sale_date TEXT, net_price REAL);
DROP TABLE IF EXISTS vehicles;
CREATE TABLE vehicles (veh_ref TEXT PRIMARY KEY, vehicle_id TEXT, make TEXT, model TEXT,
    model_family TEXT, norm_make TEXT, norm_model TEXT, generation TEXT, model_method TEXT,
    vin_model_code TEXT, vin_status TEXT, generation_without_vin TEXT, year TEXT, engine TEXT, engine_code TEXT, fuel TEXT, has_vehicle_row INTEGER,
    on_site_date TEXT, transmission TEXT);
DROP TABLE IF EXISTS dont_pull;
CREATE TABLE dont_pull (part_type TEXT PRIMARY KEY, in_stock INTEGER, units_12m INTEGER, months_cover REAL,
    reason TEXT);
DROP TABLE IF EXISTS stock;
CREATE TABLE stock (part_id TEXT, veh_ref TEXT, part_name TEXT, part_type TEXT, position TEXT,
    location TEXT, price REAL, added_date TEXT, listing TEXT);
"""


def on_site_iso(v):
    """BreakerPro [On-Site Date] text 'dd/mm/yy' (sometimes with a time) -> 'yyyy-mm-dd', else None."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b", str(v or ""))
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y + 2000 if y < 100 else y, mo, d).isoformat()
    except ValueError:
        return None


def f2(x):
    return None if x is None else float(x)


def iso(d):
    return d.strftime("%Y-%m-%d %H:%M") if d else None


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def classify_all(db, names: dict, types, use_llm: bool, log):
    """names: key -> Counter(source->rows). Fills part_name_map; returns (map, llm_usage)."""
    cached = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "SELECT raw_name, part_type, position, confidence FROM llm_cache")}
    mapping, pending = {}, []
    for nm in names:
        hit = pc.classify_rules(nm, types)
        if hit:
            mapping[nm] = (hit[0], pc.position(nm), "rule", hit[1])
        elif nm in cached:
            t, pos, conf = cached[nm]
            mapping[nm] = (t, pos or pc.position(nm), "llm", conf)
        else:
            pending.append(nm)
    usage = None
    reuse = sum(1 for m in mapping.values() if m[2] == "llm")
    log(f"Claude API cache: {reuse} names answered from llm_cache (not re-sent); "
        f"{len(pending)} names have no rule and no cached answer")
    if pending and use_llm:
        log(f"Claude API: classifying {len(pending)} names in batches of {pc.LLM_BATCH} ({pc.LLM_MODEL})")
        results, usage = pc.classify_llm(sorted(pending), types, log=log)
        now = datetime.now().isoformat(timespec="seconds")
        db.executemany("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?,?)",
                       [(nm, t, pos, conf, pc.LLM_MODEL, now) for nm, (t, pos, conf) in results.items()])
        db.execute("INSERT INTO llm_runs VALUES (?,?,?,?,?,?,?,?,?)",
                   (now, pc.LLM_MODEL, len(pending), usage["batches"], usage["input"], usage["output"],
                    usage["cache_read"], usage["cache_write"], usage["cost_usd"]))
        for nm, (t, pos, conf) in results.items():
            mapping[nm] = (t, pos or pc.position(nm), "llm", conf)
        pending = [nm for nm in pending if nm not in results]
    for nm in pending:  # no rule, no cache, no API answer
        mapping[nm] = ("misc_unknown", pc.position(nm), "unclassified", 0.0)
    db.executemany("INSERT INTO part_name_map VALUES (?,?,?,?,?,?,?,?)",
                   [(nm, t, pos, meth, conf, names[nm]["stock"], names[nm]["sold"], names[nm]["deleted"])
                    for nm, (t, pos, meth, conf) in mapping.items()])
    return mapping, usage


def build(use_llm: bool, log=print):
    data = extract(log=log)
    types = pc.load_taxonomy()
    labels = {t["id"]: t["label"] for t in types}

    # Distinct names across Stock, Invoices PART lines and StockDeleted.
    names = defaultdict(Counter)
    for r in data["sale_lines"]:
        names[name_key(r["part_name"])]["sold"] += 1
    for r in data["stock"]:
        names[name_key(r["part_name"])]["stock"] += 1
    for r in data["deleted_names"]:
        names[name_key(r["part_name"])]["deleted"] += r["n"]
    names.pop("", None)

    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    try:
        db.executescript(SCHEMA)
        if use_llm:
            ok, why = pc.llm_available()
            if not ok:
                log(f"Claude API step SKIPPED: {why}. Rules and cached answers only; "
                    "names with neither are left unclassified and go to the review queue.")
                use_llm = False
        mapping, usage = classify_all(db, names, types, use_llm, log)

        # Vehicles: vehicle row if present, else the most common make/model per ref.
        veh, best = {}, {}
        for r in data["vehicles"]:
            ref = r["veh_ref"]
            if r["has_vehicle_row"]:
                veh[ref] = r
            elif ref not in veh and (ref not in best or r["n"] > best[ref]["n"]):
                best[ref] = r
        for ref, r in best.items():
            veh.setdefault(ref, r)
        mm = load_model_map()
        for r in veh.values():
            r["vin"] = vin_info(r)
            r["norm_no_vin"] = normalise_model(mm, r["make"], r["model"], r["year"])
            r["norm"] = normalise_model(mm, r["make"], r["model"], r["year"], vin=r["vin"])
        db.executemany("INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(ref, r["vehicle_id"], r["make"], r["model"], r["norm"]["family"], r["norm"]["make"],
                         r["norm"]["model"], r["norm"]["gen"], r["norm"]["method"], r["vin"].get("code"),
                         r["vin"]["status"], r["norm_no_vin"]["gen"],
                         r["year"], r["engine"], r["engine_code"], r["fuel"], r["has_vehicle_row"],
                         on_site_iso(r.get("on_site")), r.get("transmission") or None)
                        for ref, r in veh.items()])

        # Sales.
        flags = {r["invoice_id"]: r for r in data["invoice_flags"]}
        inv_net = defaultdict(Decimal)
        for r in data["sale_lines"]:
            if r["net_price"] is not None:
                inv_net[r["invoice_id"]] += r["net_price"]
        excluded = defaultdict(lambda: [0, Decimal(0)])
        sales, neg_days, unknown_veh = [], 0, []
        for r in data["sale_lines"]:
            nm = name_key(r["part_name"])
            ptype, pos, _, _ = mapping.get(nm, ("misc_unknown", "", "", 0))
            fl = flags.get(r["invoice_id"], {})
            net = r["net_price"]
            if net is None:
                excluded["amount not a number"][0] += 1
                continue
            if fl.get("voided"):
                excluded["voided / refunded / cancelled"][0] += 1
                excluded["voided / refunded / cancelled"][1] += net
                continue
            if ptype == "vehicle_listing" or (r["bp_part_type"] or "") == "Breaking For Spares":
                excluded["breaking-for-spares ad (not a part)"][0] += 1
                excluded["breaking-for-spares ad (not a part)"][1] += net
                continue
            if POSTAGE_RX.search(nm):
                excluded["postage / courier typed as a part line"][0] += 1
                excluded["postage / courier typed as a part line"][1] += net
                if not r["make"] and not r["model"]:
                    unknown_veh.append(dict(r, grp="postage / courier"))
                continue
            sale_dt = r["invoice_date"]
            if r["invoice_time"]:
                sale_dt = sale_dt.replace(hour=r["invoice_time"].hour, minute=r["invoice_time"].minute)
            added, src = r["added_date"], "AddedDate"
            if added is not None and added.year < 2000:  # 1900-01-01 placeholder on hand-typed parts
                added = None
            if added is None:
                added, src = added_from_part_id(r["part_id"]), "Part ID"
            days = (sale_dt.date() - added.date()).days if added else None
            if days is not None and days < 0:
                neg_days += 1
                days = None
            # Gross = this line's share of (paid - delivery): VAT is on parts only,
            # so this is the part price the buyer paid, without postage.
            gross = gross_with_post = None
            if fl.get("gross_paid") is not None and inv_net[r["invoice_id"]]:
                share = net / inv_net[r["invoice_id"]]
                gross_with_post = fl["gross_paid"] * share
                gross = (fl["gross_paid"] - (fl.get("delivery") or 0)) * share
            channel = "ebay" if fl.get("auto_raised") else "counter"
            make, model, year = r["make"], r["model"], r["year"]
            v = veh.get(r["veh_ref"])
            if v and (not model or not _year(year)):
                if not model:
                    make, model = v["make"], v["model"]
                year = year if _year(year) else v["year"]
            if v and v["norm"]["method"] not in ("unmapped", "no model"):
                norm = v["norm"]  # the vehicle's model, VIN-decoded where possible
            else:
                norm = normalise_model(mm, make, model, year)
            if not make and not model:
                is_misc = "-MISC" in (r["part_id"] or "")
                unknown_veh.append(dict(r, grp="manually typed part (MISC ID)" if is_misc else "other"))
                if is_misc:
                    norm = model_from_name(mm, nm)
            sales.append(dict(invoice_id=r["invoice_id"], part_id=r["part_id"], veh_ref=r["veh_ref"] or None,
                              make=make, model=model, model_family=norm["family"], model_method=norm["method"],
                              gross_with_post=gross_with_post,
                              part_name=nm, part_type=ptype, position=pos, sale_date=sale_dt,
                              net_price=net, gross_price=gross, channel=channel, added_date=added,
                              added_source=src if added else None, days_to_sell=days))
        db.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(s["invoice_id"], s["part_id"], s["veh_ref"], s["make"], s["model"], s["model_family"],
                         s["model_method"], s["part_name"], s["part_type"], s["position"], iso(s["sale_date"]), f2(s["net_price"]),
                         f2(s["gross_price"]), s["channel"], iso(s["added_date"]), s["added_source"],
                         s["days_to_sell"]) for s in sales])
        db.executemany("INSERT INTO sales_excluded VALUES (?,?,?)",
                       [(k, v[0], f2(v[1])) for k, v in excluded.items()])
        demand = defaultdict(lambda: [0, Decimal(0)])
        for s_ in sales:
            if "-MISC" in (s_["part_id"] or "") and not s_["veh_ref"]:
                demand[(s_["part_type"], s_["model_family"])][0] += 1
                demand[(s_["part_type"], s_["model_family"])][1] += s_["net_price"]
        db.executemany("INSERT INTO misc_demand VALUES (?,?,?,?)",
                       [(t, m, n, f2(v)) for (t, m), (n, v) in demand.items()])
        db.executemany("INSERT INTO unknown_vehicle_sales VALUES (?,?,?,?,?,?)",
                       [(u["grp"], u["invoice_id"], u["part_id"], name_key(u["part_name"]),
                         iso(u["invoice_date"]), f2(u["net_price"])) for u in unknown_veh])

        # Stock now.
        stock = []
        for r in data["stock"]:
            nm = name_key(r["part_name"])
            ptype, pos, _, _ = mapping.get(nm, ("misc_unknown", "", "", 0))
            stock.append(dict(r, part_type=ptype, position=pos))
        db.executemany("INSERT INTO stock VALUES (?,?,?,?,?,?,?,?,?)",
                       [(s["part_id"], s["veh_ref"], name_key(s["part_name"]), s["part_type"], s["position"],
                         s["location"] or None, f2(s["price"]), iso(s["added_date"]), s["listing"]) for s in stock])
        db.commit()
    finally:
        db.close()

    return dict(data=data, types=types, labels=labels, names=names, mapping=mapping, usage=usage,
                sales=sales, excluded=excluded, stock=stock, veh=veh, neg_days=neg_days, unknown_veh=unknown_veh,
                demand=demand)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def type_metrics(sales, stock, today):
    since = today - timedelta(days=365)
    by_type = defaultdict(list)
    for s in sales:
        by_type[s["part_type"]].append(s)
    stock_n = Counter(s["part_type"] for s in stock)
    stock_val = defaultdict(float)
    for s in stock:
        stock_val[s["part_type"]] += f2(s["price"]) or 0
    rows = []
    for t in set(by_type) | set(stock_n):
        ss = by_type.get(t, [])
        ebay = [float(s["gross_price"]) for s in ss if s["channel"] == "ebay" and s["gross_price"] is not None]
        counter = [float(s["gross_price"]) for s in ss if s["channel"] == "counter" and s["gross_price"] is not None]
        days = [s["days_to_sell"] for s in ss if s["days_to_sell"] is not None]
        sold_12m = sum(1 for s in ss if s["sale_date"] >= since)
        monthly = sold_12m / 12
        cover = (stock_n[t] / monthly) if monthly else (float("inf") if stock_n[t] else None)
        rows.append(dict(
            part_type=t, units_sold=len(ss), units_12m=sold_12m,
            revenue_net=sum(float(s["net_price"]) for s in ss),
            ebay_median=pct(ebay, 50), ebay_p25=pct(ebay, 25), ebay_p75=pct(ebay, 75), ebay_n=len(ebay),
            counter_median=pct(counter, 50), counter_n=len(counter),
            median_days=pct(days, 50),
            pct_30=(sum(d <= 30 for d in days) / len(days)) if days else None,
            pct_90=(sum(d <= 90 for d in days) / len(days)) if days else None,
            pct_180=(sum(d <= 180 for d in days) / len(days)) if days else None,
            in_stock=stock_n[t], stock_value=stock_val[t], months_cover=cover))
    return sorted(rows, key=lambda r: -r["revenue_net"])


def type_model_metrics(sales):
    groups = defaultdict(list)
    for s in sales:
        groups[(s["part_type"], s["model_family"])].append(s)
    rows = []
    for (t, m), ss in groups.items():
        if len(ss) < TYPE_MODEL_MIN_SALES:
            continue
        ebay = [float(s["gross_price"]) for s in ss if s["channel"] == "ebay" and s["gross_price"] is not None]
        counter = [float(s["gross_price"]) for s in ss if s["channel"] == "counter" and s["gross_price"] is not None]
        days = [s["days_to_sell"] for s in ss if s["days_to_sell"] is not None]
        rows.append(dict(part_type=t, model_family=m, units_sold=len(ss),
                         revenue_net=sum(float(s["net_price"]) for s in ss),
                         ebay_median=pct(ebay, 50), ebay_p25=pct(ebay, 25), ebay_p75=pct(ebay, 75),
                         counter_median=pct(counter, 50), median_days=pct(days, 50),
                         pct_30=(sum(d <= 30 for d in days) / len(days)) if days else None,
                         pct_90=(sum(d <= 90 for d in days) / len(days)) if days else None,
                         pct_180=(sum(d <= 180 for d in days) / len(days)) if days else None))
    return sorted(rows, key=lambda r: (-r["units_sold"], r["part_type"]))


def model_metrics(sales):
    rev, rev_veh, refs, units = defaultdict(float), defaultdict(float), defaultdict(set), Counter()
    for s in sales:
        m = s["model_family"]
        rev[m] += float(s["net_price"])
        units[m] += 1
        if s["veh_ref"] and s["veh_ref"].startswith("VEH"):
            refs[m].add(s["veh_ref"])
            rev_veh[m] += float(s["net_price"])
    # Revenue per VEH ref counts only parts sold off a VEH ref; hand-typed (MISC)
    # sales have no vehicle, so they are in the total but not the per-vehicle figure.
    rows = [dict(model_family=m, revenue_net=rev[m], units_sold=units[m], veh_refs=len(refs[m]),
                 revenue_per_veh=(rev_veh[m] / len(refs[m])) if refs[m] else None) for m in rev]
    by_total = sorted(rows, key=lambda r: -r["revenue_net"])[:20]
    by_veh = sorted([r for r in rows if r["revenue_per_veh"] is not None],
                    key=lambda r: -r["revenue_per_veh"])[:20]
    return by_total, by_veh


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def fmt(v, kind=""):
    if v is None:
        return ""
    if kind == "gbp":
        return f"£{v:,.2f}"
    if kind == "gbp0":
        return f"£{v:,.0f}"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "cover":
        return "no sales 12m" if v == float("inf") else f"{v:.1f}"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def write_csv(path, rows, cols):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c for c, _, _ in cols])
        for r in rows:
            w.writerow([fmt(r.get(c), k) if k in ("cover",) else
                        ("" if r.get(c) is None else (round(r[c], 4) if isinstance(r[c], float) else r[c]))
                        for c, _, k in cols])


def html_table(rows, cols, labels=None):
    e = html.escape
    head = "".join(f"<th>{e(h)}</th>" for _, h, _ in cols)
    body = []
    for r in rows:
        cells = []
        for c, _, k in cols:
            v = r.get(c)
            if c == "part_type" and labels:
                cells.append(f"<td title='{e(labels.get(v, ''))}'>{e(str(v))}</td>")
            else:
                cells.append(f"<td class='{'num' if k or isinstance(v, (int, float)) else ''}'>{e(fmt(v, k))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='wrap'><table><tr>{head}</tr>{''.join(body)}</table></div>"


TYPE_COLS = [("part_type", "Part type", ""), ("units_sold", "Sold", ""), ("units_12m", "Sold 12m", ""),
             ("revenue_net", "Revenue (net)", "gbp0"), ("ebay_median", "eBay median", "gbp"),
             ("ebay_p25", "eBay P25", "gbp"), ("ebay_p75", "eBay P75", "gbp"),
             ("counter_median", "Counter median", "gbp"), ("median_days", "Median days to sell", ""),
             ("pct_30", "≤30d", "pct"), ("pct_90", "≤90d", "pct"), ("pct_180", "≤180d", "pct"),
             ("in_stock", "In stock", ""), ("months_cover", "Months of cover", "cover")]
TM_COLS = [("part_type", "Part type", ""), ("model_family", "Model", ""), ("units_sold", "Sold", ""),
           ("revenue_net", "Revenue (net)", "gbp0"), ("ebay_median", "eBay median", "gbp"),
           ("ebay_p25", "eBay P25", "gbp"), ("ebay_p75", "eBay P75", "gbp"),
           ("counter_median", "Counter median", "gbp"), ("median_days", "Median days", ""),
           ("pct_30", "≤30d", "pct"), ("pct_90", "≤90d", "pct"), ("pct_180", "≤180d", "pct")]
CLEARANCE_COLS = [("veh_ref", "VEH ref", ""), ("part_id", "Part ID", ""), ("part_name", "Part name", ""),
                  ("part_type", "Part type", ""), ("location", "Location", ""), ("price", "Price", "gbp"),
                  ("age_days", "Age (days)", ""), ("listing", "Listing", ""), ("yard_sale_bias", "Flag", "")]
UNKNOWN_COLS = [("group", "Group", ""), ("invoice_id", "Invoice", ""), ("part_id", "Part ID", ""),
                ("part_name", "Part name", ""), ("sale_date", "Sale date", ""), ("net_price", "Net", "gbp")]
DEMAND_COLS = [("part_type", "Part type", ""), ("model", "Model", ""), ("count", "Count", ""),
               ("net_revenue", "Net revenue", "gbp")]
MODEL_COLS = [("model_family", "Model", ""), ("revenue_net", "Revenue (net)", "gbp0"),
              ("units_sold", "Parts sold", ""), ("veh_refs", "VEH refs", ""),
              ("revenue_per_veh", "Revenue per VEH ref", "gbp0")]


def dont_pull_html(stop, labels, today, n_parts):
    """One printable page for the yard: what not to take off the cars."""
    e = html.escape
    rows = []
    for r in stop:
        cover = "no sales in 12 months" if r["units_12m"] == 0 else f"{r['months_cover']:.0f} months"
        flag = ("<span class='flag'>yard-sale bias</span>" if r["yard_sale_bias"] else "")
        rows.append(f"<tr><td>{e(labels.get(r['part_type'], r['part_type']))} {flag}</td>"
                    f"<td class='num'>{e(cover)}</td><td class='num'>{r['in_stock']}</td></tr>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Don't Pull List</title><style>
:root{{--bg:#fff;--fg:#111;--muted:#555;--line:#ccc;--flag:#8a5a00;--flagbg:#fff3d6}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#151515;--fg:#eee;--muted:#aaa;--line:#3a3a3a;--flag:#ffcc66;--flagbg:#3a2e10}}}}
:root[data-theme="dark"]{{--bg:#151515;--fg:#eee;--muted:#aaa;--line:#3a3a3a;--flag:#ffcc66;--flagbg:#3a2e10}}
body{{background:var(--bg);color:var(--fg);font:13px/1.3 system-ui,sans-serif;margin:0 auto;max-width:760px;padding:12px 16px}}
h1{{font-size:20px;margin:0}} p{{margin:4px 0 8px;color:var(--muted)}}
table{{border-collapse:collapse;width:100%}} th,td{{border-bottom:1px solid var(--line);padding:2px 6px;text-align:left}}
th{{font-size:12px;color:var(--muted)}} td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.flag{{font-size:11px;color:var(--flag);background:var(--flagbg);border-radius:3px;padding:0 4px;white-space:nowrap}}
@media print{{body{{font-size:10.5px;max-width:none;padding:0}} h1{{font-size:16px}} td,th{{padding:1px 4px}}
 :root{{--bg:#fff;--fg:#000;--muted:#444;--line:#bbb;--flag:#000;--flagbg:#eee}}}}
</style></head><body>
<h1>Don't pull: {today:%d %b %Y}</h1>
<p>Leave these on the car. We already hold more than {DONT_PULL_COVER_MONTHS} months of sales in stock, or none sold
in the last 12 months. {len(stop)} part types, {sum(r['in_stock'] for r in stop):,} parts on the shelf
({n_parts:,} on the clearance list). <b>yard-sale bias</b>: engines, gearboxes and doors were often sold for cash in
the yard without an invoice before 28 Oct 2026, so their real sales are higher than shown; check before skipping.</p>
<table><tr><th>Part type</th><th class="num">Cover</th><th class="num">In stock</th></tr>
{''.join(rows)}</table></body></html>"""


def write_outputs(b, today):
    REPORTS.mkdir(exist_ok=True)
    stamp = today.strftime("%Y-%m-%d")
    dont_pull_path = REPORTS / f"dont_pull_{stamp}.html"
    tm = type_metrics(b["sales"], b["stock"], datetime.combine(today, datetime.min.time()))
    tmm = type_model_metrics(b["sales"])
    by_total, by_veh = model_metrics(b["sales"])
    for r in tm:
        r["yard_sale_bias"] = "yard-sale bias" if r["part_type"] in YARD_SALE_BIAS_TYPES else ""
    stop = sorted([r for r in tm if r["in_stock"] and r["part_type"] not in NOT_PARTS
                   and (r["units_12m"] == 0 or r["months_cover"] > DONT_PULL_COVER_MONTHS)],
                  key=lambda r: (-r["months_cover"], -r["in_stock"]))
    stop_types = {r["part_type"] for r in stop}
    db = sqlite3.connect(DB_PATH)
    try:
        db.executemany("INSERT INTO dont_pull VALUES (?,?,?,?,?)", [
            (r["part_type"], r["in_stock"], r["units_12m"],
             None if r["months_cover"] == float("inf") else round(r["months_cover"], 1),
             "no sales in 12 months" if r["units_12m"] == 0 else f"over {DONT_PULL_COVER_MONTHS} months cover")
            for r in stop])
        db.commit()
    finally:
        db.close()
    clearance = sorted([dict(veh_ref=s["veh_ref"], part_id=s["part_id"], part_name=name_key(s["part_name"]),
                             part_type=s["part_type"], location=s["location"] or "", price=f2(s["price"]),
                             age_days=(today - s["added_date"].date()).days if s["added_date"] else None,
                             listing=s["listing"],
                             yard_sale_bias="yard-sale bias" if s["part_type"] in YARD_SALE_BIAS_TYPES else "")
                        for s in b["stock"] if s["part_type"] in stop_types],
                       key=lambda r: (r["part_type"], -(r["age_days"] or 0)))
    pull = sorted([r for r in tm if r["median_days"] is not None and r["median_days"] < PULL_FIRST_MAX_DAYS
                   and r["ebay_median"] is not None and r["ebay_median"] > PULL_FIRST_MIN_PRICE
                   and r["part_type"] not in NOT_PARTS],
                  key=lambda r: (r["median_days"], -r["ebay_median"]))

    files = {
        f"analytics_part_types_{stamp}.csv": (tm, TYPE_COLS),
        f"analytics_type_model_{stamp}.csv": (tmm, TM_COLS),
        f"analytics_dont_pull_{stamp}.csv": (stop, TYPE_COLS + [("yard_sale_bias", "Flag", "")]),
        f"analytics_dont_pull_clearance_{stamp}.csv": (clearance, CLEARANCE_COLS),
        f"analytics_misc_demand_{stamp}.csv": (
            sorted([dict(part_type=t, model=m, count=n, net_revenue=f2(v)) for (t, m), (n, v) in b["demand"].items()],
                   key=lambda r: (-r["count"], -r["net_revenue"])), DEMAND_COLS),
        f"analytics_unknown_vehicle_sales_{stamp}.csv": (
            [dict(group=u["grp"], invoice_id=u["invoice_id"], part_id=u["part_id"],
                  part_name=name_key(u["part_name"]), sale_date=iso(u["invoice_date"]),
                  net_price=f2(u["net_price"])) for u in b["unknown_veh"]], UNKNOWN_COLS),
        f"analytics_pull_first_{stamp}.csv": (pull, TYPE_COLS),
        f"analytics_models_by_revenue_{stamp}.csv": (by_total, MODEL_COLS),
        f"analytics_models_by_revenue_per_veh_{stamp}.csv": (by_veh, MODEL_COLS),
    }
    for fn, (rows, cols) in files.items():
        write_csv(REPORTS / fn, rows, cols)

    # Review queue: names with confidence < 0.7 (names, mapping and row counts only).
    rq = sorted([(nm, *m) for nm, m in b["mapping"].items() if m[3] < pc.REVIEW_BELOW],
                key=lambda x: -(b["names"][x[0]]["sold"] + b["names"][x[0]]["stock"]))
    with REVIEW_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["raw_name", "part_type", "position", "method", "confidence", "rows_sold", "rows_stock",
                    "rows_deleted", "correct_part_type"])
        for nm, t, pos, meth, conf in rq:
            w.writerow([nm, t, pos, meth, conf, b["names"][nm]["sold"], b["names"][nm]["stock"],
                        b["names"][nm]["deleted"], ""])

    e = html.escape
    n_sales = len(b["sales"])
    exc = "; ".join(f"{k}: {v[0]} lines (£{v[1]:,.2f} net)" for k, v in b["excluded"].items())
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Parts Analytics</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--head:#f4f4f4;--accent:#b4441c}}
@media (prefers-color-scheme: dark){{:root{{--bg:#161616;--fg:#eee;--muted:#999;--line:#333;--head:#222;--accent:#f08a5d}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1200px;padding:16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.muted{{color:var(--muted)}} table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}}
th,td{{border:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top}} th{{background:var(--head)}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}} .wrap{{overflow-x:auto}} details{{margin:6px 0}}
</style></head><body>
<h1>Parts analytics: {today:%d %b %Y}</h1>
<p class="muted">From BreakerPro (read-only). {n_sales:,} parts sold since {min(s['sale_date'] for s in b['sales']):%b %Y}
(excluded: {e(exc)}). Prices are gross paid; revenue is net of VAT. eBay = auto-raised PayPal-import invoices,
counter = staff-raised. Days to sell = sale date minus logging date (AddedDate). Percent sold within 30/90/180 days is
of parts that have sold. Months of cover = parts in stock ÷ (units sold in the last 12 months ÷ 12).</p>"""]
    parts.append(f"<h2>c) Don't pull: more than {DONT_PULL_COVER_MONTHS} months of cover, or no sales in "
                 f"12 months ({len(stop)})</h2><p class='muted'>One-page version for the yard: "
                 f"{e(dont_pull_path.name)}. Clearance list of the parts in stock: "
                 f"analytics_dont_pull_clearance_{stamp}.csv.</p>")
    parts.append(html_table(stop, TYPE_COLS + [("yard_sale_bias", "Flag", "")], b["labels"]))
    parts.append(f"<h2>d) Pull first: median days to sell &lt; {PULL_FIRST_MAX_DAYS} and eBay median &gt; "
                 f"£{PULL_FIRST_MIN_PRICE} ({len(pull)})</h2>")
    parts.append(html_table(pull, TYPE_COLS, b["labels"]))
    parts.append(f"<h2>a) By part type ({len(tm)})</h2>")
    parts.append(html_table(tm, TYPE_COLS, b["labels"]))
    parts.append(f"<h2>b) By part type × model, {TYPE_MODEL_MIN_SALES}+ sales ({len(tmm)})</h2>")
    parts.append(f"<details><summary>Show {len(tmm)} rows</summary>{html_table(tmm, TM_COLS, b['labels'])}</details>")
    parts.append("<h2>e) Top 20 models by parts revenue</h2>" + html_table(by_total, MODEL_COLS))
    parts.append("<h2>e) Top 20 models by revenue per VEH ref</h2>"
                 "<p class='muted'>Revenue only; vehicle cost is not recorded in BreakerPro.</p>"
                 + html_table(by_veh, MODEL_COLS))
    (REPORTS / f"analytics_{stamp}.html").write_text("\n".join(parts) + "</body></html>", encoding="utf-8")
    dont_pull_path.write_text(dont_pull_html(stop, b["labels"], today, len(clearance)), encoding="utf-8")
    return dict(tm=tm, tmm=tmm, stop=stop, pull=pull, by_total=by_total, by_veh=by_veh, review=rq,
                clearance=clearance, html=REPORTS / f"analytics_{stamp}.html", dont_pull=dont_pull_path,
                csvs=list(files))


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def checks(b, out, seed):
    sales, mapping = b["sales"], b["mapping"]
    sold_hi = sum(1 for s in sales if mapping.get(s["part_name"], (0, 0, 0, 0))[3] >= pc.REVIEW_BELOW
                  and s["part_type"] != "misc_unknown")
    cov = sold_hi / len(sales) if sales else 0
    print(f"\nCHECK mapping coverage: {sold_hi}/{len(sales)} sold units mapped with confidence >= "
          f"{pc.REVIEW_BELOW} = {cov:.1%}  -> {'PASS' if cov >= 0.9 else 'FAIL'} (need >= 90%)")
    methods = Counter(m[2] for m in mapping.values())
    print(f"  distinct names {len(mapping)}: {dict(methods)}; review queue {len(out['review'])} names "
          f"-> {REVIEW_PATH.relative_to(ROOT)}")

    tot = b["data"]["part_total"][0]
    kept = sum(s["net_price"] for s in sales)
    excl = sum(v[1] for v in b["excluded"].values())
    unparse = b["excluded"].get("amount not a number", [0])[0]
    diff = tot["net_total"] - (kept + excl)
    print(f"CHECK net reconciliation: Invoices PART total £{tot['net_total']:,.2f} ({tot['lines']} lines, "
          f"{tot['unparseable']} unparseable) = sales £{kept:,.2f} + excluded £{excl:,.2f}; "
          f"difference £{diff:,.2f}  -> {'PASS' if abs(diff) < Decimal('0.01') and unparse == tot['unparseable'] else 'FAIL'}")
    print("  excluded: " + "; ".join(f"{k} {v[0]} lines £{v[1]:,.2f}" for k, v in b["excluded"].items()))
    print(f"  days_to_sell: {sum(1 for s in sales if s['days_to_sell'] is not None)} known, "
          f"{b['neg_days']} negative (set to unknown); added_date source "
          f"{dict(Counter(s['added_source'] for s in sales))}")

    # Unknown-vehicle sales breakdown (no make/model on the invoice line).
    uv = b["unknown_veh"]
    grp = defaultdict(lambda: [0, Decimal(0)])
    for u in uv:
        grp[u["grp"]][0] += 1
        grp[u["grp"]][1] += u["net_price"] or 0
    print(f"\nUNKNOWN-VEHICLE SALES: {len(uv)} lines, £{sum(g[1] for g in grp.values()):,.2f} net")
    for g in ("postage / courier", "manually typed part (MISC ID)", "other"):
        n, t = grp.get(g, (0, Decimal(0)))
        print(f"  {g:32} {n:>4} lines  £{t:>10,.2f}")
    sample = random.Random(seed).sample(uv, min(20, len(uv)))
    print("  20 sample part names:")
    for u in sample:
        print(f"    [{u['grp'][:6]}] {name_key(u['part_name'])[:80]:80} £{u['net_price']:,.2f}")
    with_post = [float(s["gross_with_post"]) for s in sales if s["channel"] == "ebay" and s["gross_with_post"] is not None]
    without = [float(s["gross_price"]) for s in sales if s["channel"] == "ebay" and s["gross_price"] is not None]
    print(f"  postage change: net parts revenue unchanged (DELIVERY lines were never in it; postage typed as a part: "
          f"{b['excluded'].get('postage / courier typed as a part line', [0])[0]} lines). eBay gross price per part "
          f"now excludes postage: total £{sum(with_post):,.2f} -> £{sum(without):,.2f} "
          f"(-£{sum(with_post) - sum(without):,.2f}); median eBay part £{pct(with_post, 50):,.2f} -> £{pct(without, 50):,.2f}")

    misc = [s for s in sales if "-MISC" in (s["part_id"] or "") and not s["veh_ref"]]
    mc = Counter(s["model_method"] for s in misc)
    mapped = [s for s in misc if s["model_method"].startswith("name") and s["model_method"] not in
              ("name ambiguous", "name no model")]
    print(f"  hand-typed (MISC) lines with a model from the part name: {len(mapped)}/{len(misc)} "
          f"(£{sum(float(s['net_price']) for s in mapped):,.2f}); {dict(mc)}")
    for s in [s for s in misc if s["model_method"] in ("name ambiguous", "name no model")]:
        print(f"    no model: {s['part_name'][:80]}  ({s['model_method']})")
    print(f"  MISC demand table: {len(b['demand'])} part type x model rows; top 10:")
    for (t, m), (n, v) in sorted(b["demand"].items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:10]:
        print(f"    {t:24} {m:26} {n:>3}  £{v:,.2f}")

    # VIN decoder.
    vs = Counter(v["vin"]["status"] for v in b["veh"].values())
    by_vin = [v for v in b["veh"].values() if v["norm"]["method"] == "vin"]
    changed = [(ref, v) for ref, v in b["veh"].items() if v["norm"]["gen"] != v["norm_no_vin"]["gen"]]
    unknown_codes = Counter((v["norm"]["model"], v["vin"]["code"]) for v in b["veh"].values()
                            if v["vin"]["status"] == "ok" and v["norm"]["method"] != "vin"
                            and v["norm"]["model"] is not None)
    print(f"\nVIN DECODER: VIN status {dict(vs)}; generation set from VIN for {len(by_vin)} vehicles; "
          f"{len(changed)} vehicles changed generation because of it")
    for ref, v in sorted(changed):
        print(f"    CHANGED {ref:7} {v['make']!s:10} {v['model']!s:34} year {v['year']!s:5} "
              f"VIN code {v['vin'].get('code')} (MY {v['vin'].get('year')}): "
              f"{v['norm_no_vin']['gen']} ({v['norm_no_vin']['method']}) -> {v['norm']['gen']}")
    for (mdl, code), n in sorted(unknown_codes.items(), key=str):
        print(f"    VIN code not decoded: {mdl} code {code} ({n} vehicles) - fell back to name/year")

    # Model normalisation.
    vm = Counter(v["norm"]["method"] for v in b["veh"].values())
    bad = sorted({(v["make"], v["model"], v["year"], v["norm"]["method"]) for v in b["veh"].values()
                  if v["norm"]["method"] in ("unmapped", "gen unknown", "no model")}, key=str)
    print(f"\nCHECK model normalisation: {len(b['veh'])} vehicles; method {dict(vm)}; "
          f"{len(bad)} unmapped or without a generation, listed below  -> PASS (every vehicle mapped or listed)")
    for mk, md, yr, meth in bad:
        print(f"    UNMAPPED  {mk!s:12} {md!s:40} year {yr!s:6} ({meth})")
    sm = Counter(s["model_method"] for s in sales)
    print(f"  sales by model method: {dict(sm)}")
    unm = Counter((s["make"], s["model"]) for s in sales if s["model_method"] in ("unmapped", "gen unknown"))
    for (mk, md), n in unm.most_common():
        print(f"    sales UNMAPPED  {mk!s:12} {md!s:40} {n} lines")

    n_stop = len(out["stop"])
    print(f"\nCHECK Don't pull list: {n_stop} types  -> {'PASS' if 40 <= n_stop <= 60 else 'OUTSIDE 40-60'}; "
          f"clearance CSV {len(out['clearance'])} parts")

    # Personal-data scan over every file this build wrote.
    files = [REPORTS / f for f in out["csvs"]] + [out["html"], out["dont_pull"], REVIEW_PATH]
    hits = []
    for f in files:
        txt = f.read_text(encoding="utf-8-sig")
        for label, rx in PII_PATTERNS:
            hits += [(f.name, label, m.group(0)) for m in rx.finditer(txt)]
    print(f"\nCHECK personal-data scan: {len(files)} files, {len(hits)} hits  -> {'PASS' if not hits else 'FAIL'}")
    for h in hits[:20]:
        print(f"    {h[0]}: {h[1]}")  # the match itself is not printed

    rnd = random.Random(seed)
    sample = rnd.sample(sorted(mapping), 20)
    print(f"\nSPOT CHECK (20 random distinct names, seed {seed}):")
    for nm in sample:
        t, pos, meth, conf = mapping[nm]
        print(f"  {nm[:60]:60} -> {t:28} pos={pos or '-':16} {meth:5} {conf:.2f}")

    u = b["usage"]
    if u:
        print(f"\nCLAUDE API COST: {u['batches']} batches, {u['input']:,} in / {u['output']:,} out tokens "
              f"(cache read {u['cache_read']:,}, write {u['cache_write']:,}) = ${u['cost_usd']:.4f} "
              f"({pc.LLM_MODEL}); refused {u['refused']}, failed {u['failed']}")
    else:
        print("\nCLAUDE API COST: $0.00 (no API calls this run)")
    return cov


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-llm", action="store_true", help="rules + cache only, no API calls")
    ap.add_argument("--seed", type=int, default=7, help="seed for the 20-name spot check")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    today = date.today()
    b = build(use_llm=not args.no_llm)
    out = write_outputs(b, today)
    checks(b, out, args.seed)
    print(f"\nTop 10 part types by revenue (net):")
    for r in out["tm"][:10]:
        print(f"  {r['part_type']:28} £{r['revenue_net']:>10,.0f}  sold {r['units_sold']:>4}  "
              f"eBay median {fmt(r['ebay_median'], 'gbp'):>8}  median days {fmt(r['median_days']):>6}  "
              f"in stock {r['in_stock']:>4}  cover {fmt(r['months_cover'], 'cover')}")
    print(f"\nTop 10 models by revenue per VEH ref (normalised):")
    for r in out["by_veh"][:10]:
        print(f"  {r['model_family']:28} £{r['revenue_per_veh']:>8,.0f} per vehicle  ({r['veh_refs']} vehicles, "
              f"£{r['revenue_net']:,.0f}, {r['units_sold']} parts)")
    print(f"\nDon't pull (> {DONT_PULL_COVER_MONTHS} months cover or no sales in 12 months): {len(out['stop'])}")
    for r in out["stop"]:
        print(f"  {r['part_type']:28} in stock {r['in_stock']:>4}  sold 12m {r['units_12m']:>3}  "
              f"cover {fmt(r['months_cover'], 'cover'):>13}  {r['yard_sale_bias']}")
    print(f"\nPull first (median days < {PULL_FIRST_MAX_DAYS}, eBay median > £{PULL_FIRST_MIN_PRICE}): {len(out['pull'])}")
    for r in out["pull"]:
        print(f"  {r['part_type']:28} median days {fmt(r['median_days']):>5}  eBay median "
              f"{fmt(r['ebay_median'], 'gbp'):>8}  sold {r['units_sold']:>4}  in stock {r['in_stock']}")
    print(f"\nWrote {DB_PATH.relative_to(ROOT)}, {out['html'].relative_to(ROOT)}, "
          f"{out['dont_pull'].relative_to(ROOT)} and {len(out['csvs'])} CSVs in reports/")


if __name__ == "__main__":
    main()
