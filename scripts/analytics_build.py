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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import part_classifier as pc  # noqa: E402
from analytics_extract import extract  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "analytics" / "analytics.db"
REVIEW_PATH = ROOT / "analytics" / "review_queue.csv"
REPORTS = ROOT / "reports"

PULL_FIRST_MAX_DAYS = 30
PULL_FIRST_MIN_PRICE = 40
STOP_PULLING_COVER_MONTHS = 12
TYPE_MODEL_MIN_SALES = 5
NOT_PARTS = {"misc_unknown", "vehicle_listing"}  # never in the stop / pull lists

# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

MAKES = {"VW": "VOLKSWAGEN", "VOLKSWAGON": "VOLKSWAGEN", "SKODA": "SKODA", "SEAT": "SEAT",
         "AUDI": "AUDI", "VOLKSWAGEN": "VOLKSWAGEN", "CUPRA": "CUPRA"}
MODEL_BASES = [
    "GOLF PLUS", "GOLF SPORTSVAN", "E GOLF", "GOLF", "POLO", "UP", "PASSAT CC", "PASSAT", "TIGUAN",
    "TOURAN", "SHARAN", "CADDY", "TRANSPORTER", "SCIROCCO", "EOS", "BEETLE", "JETTA", "T ROC",
    "T CROSS", "TOUAREG", "ARTEON", "ID 3", "ID3", "ID 4", "FOX", "LUPO", "BORA", "TAIGO",
    "A1", "A2", "A3", "A4", "A5", "A6", "A7", "Q2", "Q3", "Q5", "Q7", "TT",
    "IBIZA", "LEON", "ARONA", "ATECA", "ALHAMBRA", "MII", "TOLEDO", "ALTEA", "TARRACO", "FORMENTOR",
    "FABIA", "OCTAVIA", "SUPERB", "YETI", "CITIGO", "KAROQ", "KODIAQ", "RAPID", "ROOMSTER",
    "KAMIQ", "SCALA", "ENYAQ",
]
GEN_RX = re.compile(r"\b(MK ?\d{1,2}|\d[A-Z]{1,2}\d?|B\d{1,2}|8[A-Z])\b")
GEN_IGNORE = {"5DR", "3DR", "2DR", "4DR", "5D", "3D", "4X4", "2WD", "4WD"}


def model_family(make: str, model: str) -> str:
    """'VOLKSWAGEN', 'POLO MATCH 6R' -> 'VOLKSWAGEN POLO 6R'."""
    mk = MAKES.get((make or "").strip().upper(), (make or "").strip().upper()) or "UNKNOWN"
    m = re.sub(r"[^A-Z0-9 ]+", " ", (model or "").upper())
    m = re.sub(r"\s+", " ", m).strip()
    if not m:
        return f"{mk} (no model)"
    base = next((b for b in MODEL_BASES if re.search(rf"\b{b}\b", m)), m.split()[0])
    gens = [g.replace(" ", "") for g in GEN_RX.findall(m) if g.replace(" ", "") not in GEN_IGNORE]
    return " ".join([mk, base] + gens[:1])


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
    model_family TEXT, part_name TEXT, part_type TEXT, position TEXT, sale_date TEXT,
    net_price REAL, gross_price REAL, channel TEXT, added_date TEXT, added_source TEXT,
    days_to_sell INTEGER);
DROP TABLE IF EXISTS sales_excluded;
CREATE TABLE sales_excluded (reason TEXT, lines INTEGER, net_total REAL);
DROP TABLE IF EXISTS vehicles;
CREATE TABLE vehicles (veh_ref TEXT PRIMARY KEY, vehicle_id TEXT, make TEXT, model TEXT,
    model_family TEXT, year TEXT, engine TEXT, engine_code TEXT, fuel TEXT, has_vehicle_row INTEGER);
DROP TABLE IF EXISTS stock;
CREATE TABLE stock (part_id TEXT, veh_ref TEXT, part_name TEXT, part_type TEXT, position TEXT,
    price REAL, added_date TEXT, listing TEXT);
"""


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
        if use_llm and not pc.llm_available():
            log("Claude API: no credentials found (ANTHROPIC_API_KEY / ant profile) - LLM step skipped")
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
        db.executemany("INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?)",
                       [(ref, r["vehicle_id"], r["make"], r["model"], model_family(r["make"], r["model"]),
                         r["year"], r["engine"], r["engine_code"], r["fuel"], r["has_vehicle_row"])
                        for ref, r in veh.items()])

        # Sales.
        flags = {r["invoice_id"]: r for r in data["invoice_flags"]}
        inv_net = defaultdict(Decimal)
        for r in data["sale_lines"]:
            if r["net_price"] is not None:
                inv_net[r["invoice_id"]] += r["net_price"]
        excluded = defaultdict(lambda: [0, Decimal(0)])
        sales, neg_days = [], 0
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
            sale_dt = r["invoice_date"]
            if r["invoice_time"]:
                sale_dt = sale_dt.replace(hour=r["invoice_time"].hour, minute=r["invoice_time"].minute)
            added, src = r["added_date"], "AddedDate"
            if added is None:
                added, src = added_from_part_id(r["part_id"]), "Part ID"
            days = (sale_dt.date() - added.date()).days if added else None
            if days is not None and days < 0:
                neg_days += 1
                days = None
            gross = None
            if fl.get("gross_paid") is not None and inv_net[r["invoice_id"]]:
                gross = fl["gross_paid"] * net / inv_net[r["invoice_id"]]
            channel = "ebay" if fl.get("auto_raised") else "counter"
            make, model = r["make"], r["model"]
            if not model and r["veh_ref"] in veh:
                make, model = veh[r["veh_ref"]]["make"], veh[r["veh_ref"]]["model"]
            sales.append(dict(invoice_id=r["invoice_id"], part_id=r["part_id"], veh_ref=r["veh_ref"] or None,
                              make=make, model=model, model_family=model_family(make, model),
                              part_name=nm, part_type=ptype, position=pos, sale_date=sale_dt,
                              net_price=net, gross_price=gross, channel=channel, added_date=added,
                              added_source=src if added else None, days_to_sell=days))
        db.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(s["invoice_id"], s["part_id"], s["veh_ref"], s["make"], s["model"], s["model_family"],
                         s["part_name"], s["part_type"], s["position"], iso(s["sale_date"]), f2(s["net_price"]),
                         f2(s["gross_price"]), s["channel"], iso(s["added_date"]), s["added_source"],
                         s["days_to_sell"]) for s in sales])
        db.executemany("INSERT INTO sales_excluded VALUES (?,?,?)",
                       [(k, v[0], f2(v[1])) for k, v in excluded.items()])

        # Stock now.
        stock = []
        for r in data["stock"]:
            nm = name_key(r["part_name"])
            ptype, pos, _, _ = mapping.get(nm, ("misc_unknown", "", "", 0))
            stock.append(dict(r, part_type=ptype, position=pos))
        db.executemany("INSERT INTO stock VALUES (?,?,?,?,?,?,?,?)",
                       [(s["part_id"], s["veh_ref"], name_key(s["part_name"]), s["part_type"], s["position"],
                         f2(s["price"]), iso(s["added_date"]), s["listing"]) for s in stock])
        db.commit()
    finally:
        db.close()

    return dict(data=data, types=types, labels=labels, names=names, mapping=mapping, usage=usage,
                sales=sales, excluded=excluded, stock=stock, veh=veh, neg_days=neg_days)


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
    rev, refs, units = defaultdict(float), defaultdict(set), Counter()
    for s in sales:
        m = s["model_family"]
        rev[m] += float(s["net_price"])
        units[m] += 1
        if s["veh_ref"] and s["veh_ref"].startswith("VEH"):
            refs[m].add(s["veh_ref"])
    rows = [dict(model_family=m, revenue_net=rev[m], units_sold=units[m], veh_refs=len(refs[m]),
                 revenue_per_veh=(rev[m] / len(refs[m])) if refs[m] else None) for m in rev]
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
MODEL_COLS = [("model_family", "Model", ""), ("revenue_net", "Revenue (net)", "gbp0"),
              ("units_sold", "Parts sold", ""), ("veh_refs", "VEH refs", ""),
              ("revenue_per_veh", "Revenue per VEH ref", "gbp0")]


def write_outputs(b, today):
    REPORTS.mkdir(exist_ok=True)
    stamp = today.strftime("%Y-%m-%d")
    tm = type_metrics(b["sales"], b["stock"], datetime.combine(today, datetime.min.time()))
    tmm = type_model_metrics(b["sales"])
    by_total, by_veh = model_metrics(b["sales"])
    stop = sorted([r for r in tm if r["in_stock"] and r["months_cover"] is not None
                   and r["months_cover"] > STOP_PULLING_COVER_MONTHS and r["part_type"] not in NOT_PARTS],
                  key=lambda r: -r["months_cover"])
    pull = sorted([r for r in tm if r["median_days"] is not None and r["median_days"] < PULL_FIRST_MAX_DAYS
                   and r["ebay_median"] is not None and r["ebay_median"] > PULL_FIRST_MIN_PRICE
                   and r["part_type"] not in NOT_PARTS],
                  key=lambda r: (r["median_days"], -r["ebay_median"]))

    files = {
        f"analytics_part_types_{stamp}.csv": (tm, TYPE_COLS),
        f"analytics_type_model_{stamp}.csv": (tmm, TM_COLS),
        f"analytics_stop_pulling_{stamp}.csv": (stop, TYPE_COLS),
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
    parts.append(f"<h2>c) Stop pulling: more than {STOP_PULLING_COVER_MONTHS} months of cover ({len(stop)})</h2>")
    parts.append(html_table(stop, TYPE_COLS, b["labels"]))
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
    return dict(tm=tm, tmm=tmm, stop=stop, pull=pull, by_total=by_total, by_veh=by_veh, review=rq,
                html=REPORTS / f"analytics_{stamp}.html", csvs=list(files))


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
    print(f"\nStop pulling (> {STOP_PULLING_COVER_MONTHS} months cover): {len(out['stop'])}")
    for r in out["stop"]:
        print(f"  {r['part_type']:28} in stock {r['in_stock']:>4}  sold 12m {r['units_12m']:>3}  "
              f"cover {fmt(r['months_cover'], 'cover')}")
    print(f"\nPull first (median days < {PULL_FIRST_MAX_DAYS}, eBay median > £{PULL_FIRST_MIN_PRICE}): {len(out['pull'])}")
    for r in out["pull"]:
        print(f"  {r['part_type']:28} median days {fmt(r['median_days']):>5}  eBay median "
              f"{fmt(r['ebay_median'], 'gbp'):>8}  sold {r['units_sold']:>4}  in stock {r['in_stock']}")
    print(f"\nWrote {DB_PATH.relative_to(ROOT)}, {out['html'].relative_to(ROOT)} and {len(out['csvs'])} CSVs in reports/")


if __name__ == "__main__":
    main()
