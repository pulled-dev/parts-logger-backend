"""
BreakerPro stock exceptions report. READ-ONLY.

Reads the local BreakerPro SQL Server database with the breakerpro_readonly
login (SELECT only) and lists stock that needs attention:

  1. Sold but not picked (Pending Picking older than EXC_UNPICKED_HOURS)
  2. Dead listings (in stock, eBay item number, failed / removed status)
  3. On the shelf, unlisted (in stock, no eBay item number)
  4. No shelf location
  5. Priced under EXC_LOW_PRICE_GBP
  6. Aged stock (logged more than EXC_AGED_DAYS ago) with total shelf value
  7. Orphan parts (in stock, vehicle row missing)
  9. Mobile-app deletions, last 7 days (>= EXC_DELETION_CHECK_GBP flagged)
 10. Aged x location x listing grid, plus probable phantom stock
 11. Policy-removal pattern (eBay policy removals grouped by part name)
  8. TODO: live eBay listings with no Stock row (needs the eBay API)

Usage:
    python scripts/exceptions_report.py [--ref VEH113] [--out-dir reports]

Writes reports/exceptions_YYYY-MM-DD.csv and .html (git-ignored) and prints
counts. Status rules and schema notes: build-spec/BREAKERPRO-STOCK-READ.md.

Safety:
  - Every [bracketed] identifier in the SQL below must be on ALLOWED_COLUMNS
    (no customer columns) and must exist in the live schema, or the script
    stops before writing anything.
  - Refuses to run unless connected as a login without write rights.
  - Each query has a hard timeout (EXC_QUERY_TIMEOUT_S, default 5s).
"""

import argparse
import csv
import html
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "BREAKERPRO_SERVER": r".\SQLEXPRESS",
    "BREAKERPRO_DATABASE": "BreakerPro",
    "BREAKERPRO_USER": "breakerpro_readonly",
    "BREAKERPRO_PASSWORD_FILE": r"C:\Users\ACER\.secrets\breakerpro-readonly\password.txt",
    "BREAKERPRO_ODBC_DRIVER": "ODBC Driver 17 for SQL Server",
    "EXC_UNPICKED_HOURS": "24",
    "EXC_LOW_PRICE_GBP": "8",
    "EXC_AGED_DAYS": "180",
    "EXC_QUERY_TIMEOUT_S": "5",
    "EXC_DELETION_DAYS": "7",
    "EXC_DELETION_CHECK_GBP": "30",
    # From this date staff record sale vs damage separately; mobile-app
    # deletions before it mix yard sales and damaged parts.
    "EXC_MOBILE_REMOVAL_CUTOVER": "2026-10-28",
}


def load_env(path: Path = ROOT / ".env") -> dict:
    """DEFAULTS, then .env, then real environment variables (highest)."""
    cfg = dict(DEFAULTS)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in cfg:
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


# --------------------------------------------------------------------------
# SQL. Rules: bracket every BreakerPro table/column name, never bracket
# aliases, select only allowlisted columns. Thresholds are ? parameters.
# --------------------------------------------------------------------------

TABLES = {"Stock", "Invoices", "StockDeleted"}

# Everything the report may touch. No customer, address, delivery, PayPal or
# free-text note columns. [Transaction Details] is only used inside CASE/LIKE
# on STATUS/PAYMENT/VOID lines to classify them; it is never output.
ALLOWED_COLUMNS = {
    "Part ID", "Vehicle ID", "Stock Reference", "Part Type", "Part Name",
    "Part Location", "Part Nett Sale", "Ebay_FixedPrice", "Ebay_Number",
    "Ebay_Status", "AddedDate", "AddedBy", "DeletedDate", "DeletedBy", "DeletedReason",
    "Invoice ID", "Invoice Date", "Invoice Time", "Transaction Type",
    "Transaction Details", "Transaction Amount",
}

VEHICLE_ROW = "'Breaking For Spares'"

PRICE = ("COALESCE(TRY_CONVERT(decimal(10,2), NULLIF(LTRIM(RTRIM(s.[Ebay_FixedPrice])), '')),"
         " TRY_CONVERT(decimal(10,2), NULLIF(LTRIM(RTRIM(s.[Part Nett Sale])), '')))")

# Listing state for an in-stock part (see spec "How a part's status is derived").
LISTING_STATE = f"""
  CASE WHEN ISNULL(s.[Ebay_Number], '') = '' THEN 'unlisted'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(60)) LIKE 'Failed: This listing has been removed%' THEN 'dead: removed by eBay (policy)'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(60)) LIKE 'Failed: This item cannot be accessed%' THEN 'dead: listing deleted on eBay'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Failed%' THEN 'error: last eBay call failed'
       ELSE 'listed' END"""

PARTS_IN_STOCK = f"[Stock] s WHERE s.[Part Type] <> {VEHICLE_ROW}"

SQL_WHOAMI = """
SELECT SUSER_SNAME() AS login_name, IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin,
       IS_ROLEMEMBER('db_owner') AS is_owner, IS_ROLEMEMBER('db_denydatawriter') AS denies_write
"""

SQL_SCHEMA = """
SELECT t.name AS table_name, c.name AS column_name
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
WHERE t.name IN (?, ?, ?)
"""

SQL_STOCK_SUMMARY = f"""
SELECT x.state, COUNT(*) AS parts FROM (
  SELECT {LISTING_STATE} AS state FROM {PARTS_IN_STOCK}
) x GROUP BY x.state
"""

SQL_REF_SUMMARY = f"""
SELECT x.state, COUNT(*) AS parts FROM (
  SELECT {LISTING_STATE} AS state FROM {PARTS_IN_STOCK}
    AND UPPER(LTRIM(RTRIM(s.[Stock Reference]))) = UPPER(?)
) x GROUP BY x.state
"""

# 1. Invoices whose only picking state is Pending Picking, not voided /
# refunded / cancelled, older than ? hours. Invoice ID + PART line fields only.
SQL_UNPICKED = """
WITH st AS (
  SELECT i.[Invoice ID] AS invoice,
         MAX(CASE WHEN i.[Transaction Details] LIKE 'Pending Picking%' THEN 1 ELSE 0 END) AS pending_pick,
         MAX(CASE WHEN i.[Transaction Details] LIKE 'Mark as Picked%'
                    OR i.[Transaction Details] LIKE 'Mark as Packed%'
                    OR i.[Transaction Details] LIKE 'Mark as Dispatched%'
                    OR i.[Transaction Details] LIKE 'Pending Packing%'
                    OR i.[Transaction Details] LIKE 'Pending Dispatch%' THEN 1 ELSE 0 END) AS later_stage
  FROM [Invoices] i WHERE i.[Transaction Type] = 'STATUS' GROUP BY i.[Invoice ID]
), cancelled AS (
  SELECT DISTINCT i.[Invoice ID] AS invoice FROM [Invoices] i
  WHERE i.[Transaction Type] = 'VOID'
     OR (i.[Transaction Type] = 'PAYMENT' AND (i.[Transaction Details] LIKE '%CANCEL%' OR i.[Transaction Details] LIKE '%REFUND%'))
), lines AS (
  SELECT p.[Invoice ID] AS invoice,
         DATEADD(minute, DATEDIFF(minute, 0, CAST(ISNULL(p.[Invoice Time], 0) AS time)),
                 CAST(CAST(p.[Invoice Date] AS date) AS datetime)) AS invoiced_at,
         p.[Part ID] AS part_id, p.[Part Name] AS part_name,
         UPPER(LTRIM(RTRIM(p.[Stock Reference]))) AS veh_ref, p.[Part Location] AS location
  FROM [Invoices] p WHERE p.[Transaction Type] = 'PART'
)
SELECT l.invoice, l.invoiced_at, l.part_id, l.part_name, l.veh_ref, l.location
FROM lines l JOIN st ON st.invoice = l.invoice
WHERE st.pending_pick = 1 AND st.later_stage = 0
  AND NOT EXISTS (SELECT 1 FROM cancelled c WHERE c.invoice = l.invoice)
  AND l.invoiced_at < DATEADD(hour, -?, GETDATE())
ORDER BY l.invoiced_at, l.invoice
"""

PART_COLUMNS = f"""
  s.[Part ID] AS part_id, UPPER(LTRIM(RTRIM(s.[Stock Reference]))) AS veh_ref,
  s.[Part Name] AS part_name, s.[Part Location] AS location, {PRICE} AS price,
  s.[Ebay_Number] AS ebay_number, s.[AddedDate] AS added_date, s.[AddedBy] AS added_by,
  {LISTING_STATE} AS listing_state"""

# 2. In stock with an item number but a failed / removed eBay status.
SQL_DEAD = f"""
SELECT {PART_COLUMNS} FROM {PARTS_IN_STOCK}
  AND ISNULL(s.[Ebay_Number], '') <> '' AND CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Failed%'
ORDER BY listing_state, veh_ref, part_name
"""

# 3. In stock, no item number. unlisted_reason refines why.
SQL_UNLISTED = f"""
SELECT {PART_COLUMNS},
  CASE WHEN s.[Ebay_Status] IS NULL THEN 'never listed'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Batch%' THEN 'queued in a listing batch'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Success Ended%' THEN 'listing ended'
       WHEN CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Fail%' THEN 'listing attempt failed'
       ELSE 'other' END AS unlisted_reason
FROM {PARTS_IN_STOCK} AND ISNULL(s.[Ebay_Number], '') = ''
ORDER BY veh_ref, part_name
"""

# 4. No shelf location.
SQL_NO_LOCATION = f"""
SELECT {PART_COLUMNS} FROM {PARTS_IN_STOCK} AND ISNULL(LTRIM(RTRIM(s.[Part Location])), '') = ''
ORDER BY veh_ref, part_name
"""

# 5. Priced under ? GBP.
SQL_LOW_PRICE = f"""
SELECT {PART_COLUMNS} FROM {PARTS_IN_STOCK} AND {PRICE} < ?
ORDER BY price, veh_ref
"""

# 6. Logged more than ? days ago (AddedDate: matches the Part ID date and the
# audit log for every part checked, see spec part 2 a).
SQL_AGED = f"""
SELECT {PART_COLUMNS}, DATEDIFF(day, s.[AddedDate], GETDATE()) AS age_days
FROM {PARTS_IN_STOCK} AND s.[AddedDate] < DATEADD(day, -?, GETDATE())
ORDER BY s.[AddedDate], veh_ref
"""

# 7. In stock, no vehicle row in Stock for the same Vehicle ID.
SQL_ORPHANS = f"""
SELECT {PART_COLUMNS},
  CASE WHEN EXISTS (SELECT 1 FROM [Invoices] i WHERE i.[Transaction Type] = 'PART'
                      AND i.[Part Type] = {VEHICLE_ROW} AND i.[Vehicle ID] = s.[Vehicle ID])
         THEN 'vehicle row sold (breaking-for-spares ad bought)'
       WHEN EXISTS (SELECT 1 FROM [StockDeleted] d WHERE d.[Part Type] = {VEHICLE_ROW} AND d.[Vehicle ID] = s.[Vehicle ID])
         THEN 'vehicle row removed'
       ELSE 'no vehicle row on record' END AS orphan_reason
FROM {PARTS_IN_STOCK}
  AND NOT EXISTS (SELECT 1 FROM [Stock] v WHERE v.[Part Type] = {VEHICLE_ROW} AND v.[Vehicle ID] = s.[Vehicle ID])
ORDER BY veh_ref, part_name
"""

# 9. Parts removed through the mobile app in the last ? days. Before the
# cutover this reason mixes yard sales and damaged parts; it is not split.
SQL_MOBILE_DELETIONS = f"""
SELECT s.[Part ID] AS part_id, UPPER(LTRIM(RTRIM(s.[Stock Reference]))) AS veh_ref,
       s.[Part Name] AS part_name, {PRICE} AS price,
       s.[DeletedDate] AS deleted_date, UPPER(s.[DeletedBy]) AS deleted_by
FROM [StockDeleted] s
WHERE s.[DeletedReason] LIKE 'Deleted Manually by Mobile App%'
  AND s.[DeletedDate] >= DATEADD(day, -?, GETDATE())
ORDER BY s.[DeletedDate] DESC
"""

# 10. Age band x located x listing for every in-stock part. "dead" = any
# failed eBay status with an item number (same set as section 2).
AGE_DAYS = "DATEDIFF(day, s.[AddedDate], GETDATE())"
AGE_BAND = f"""
  CASE WHEN {AGE_DAYS} < 30 THEN '1: <30d' WHEN {AGE_DAYS} < 90 THEN '2: 30-90d'
       WHEN {AGE_DAYS} < 180 THEN '3: 90-180d' WHEN {AGE_DAYS} <= 365 THEN '4: 180-365d'
       ELSE '5: >365d' END"""
LOCATED = "CASE WHEN ISNULL(LTRIM(RTRIM(s.[Part Location])), '') = '' THEN 'no location' ELSE 'located' END"
LISTING3 = """
  CASE WHEN x.state = 'unlisted' THEN 'unlisted' WHEN x.state = 'listed' THEN 'listed' ELSE 'dead' END"""

SQL_AGE_GRID = f"""
SELECT x.band, x.located, {LISTING3} AS listing, COUNT(1) AS parts, SUM(x.price) AS value
FROM (SELECT {AGE_BAND} AS band, {LOCATED} AS located, {LISTING_STATE} AS state, {PRICE} AS price
      FROM {PARTS_IN_STOCK}) x
GROUP BY x.band, x.located, {LISTING3}
"""

# 10b. Probable phantom stock: 180+ days, no location, unlisted or dead.
SQL_PHANTOM = f"""
SELECT {PART_COLUMNS}, {AGE_DAYS} AS age_days
FROM {PARTS_IN_STOCK}
  AND {AGE_DAYS} >= 180
  AND ISNULL(LTRIM(RTRIM(s.[Part Location])), '') = ''
  AND (ISNULL(s.[Ebay_Number], '') = '' OR CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Failed%')
ORDER BY TRY_CONVERT(int, SUBSTRING(UPPER(LTRIM(RTRIM(s.[Stock Reference]))), 4, 10)), veh_ref, price DESC
"""

ALL_SQL = [SQL_AGE_GRID, SQL_PHANTOM, SQL_STOCK_SUMMARY, SQL_REF_SUMMARY, SQL_UNPICKED, SQL_DEAD, SQL_UNLISTED,
           SQL_NO_LOCATION, SQL_LOW_PRICE, SQL_AGED, SQL_ORPHANS, SQL_MOBILE_DELETIONS]

EBAY_TODO = """\
Needs eBay API access (no new credentials in this phase). BreakerPro lists
through the Trading API, so its listings are not visible to the Sell
Inventory API. Plan:
  1. Trading API GetMyeBaySelling (ActiveList, paginated 200/page) or
     GetSellerList (EndTimeFrom=now, EndTimeTo=now+31d, DetailLevel=ReturnAll):
     returns ItemID + SKU (Custom Label) for every live listing.
     Or Sell Feed API: createInventoryTask / LMS_ACTIVE_INVENTORY_REPORT
     (OAuth scope sell.inventory.readonly) for one bulk file.
  2. Compare ItemIDs with Stock.[Ebay_Number] (and SKU with [Part ID]).
  3. Report live ItemIDs with no Stock row (part sold/removed but listing still
     up: risk of selling something we no longer have), and Stock rows whose
     item number is not live.
The existing EBAY_* settings in pricing/ebay_orders.py are a Trading API
token and could be reused once approved."""

# --------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    "section", "section_name", "veh_ref", "part_id", "part_name", "location",
    "price", "ebay_number", "status", "invoice", "invoice_date", "added_date",
    "added_by", "age_days", "deleted_date", "deleted_by", "flag", "count",
]
# Belt and braces: none of these may ever appear in an output column name.
FORBIDDEN_OUTPUT = ("customer", "business", "address", "add1", "town", "postcode",
                    "email", "phone", "paypal", "delivery", "comment", "note", "buyer")

SECTIONS = {
    1: "Sold but not picked",
    2: "Dead listings",
    3: "On the shelf, unlisted",
    4: "No shelf location",
    5: "Priced under threshold",
    6: "Aged stock",
    7: "Orphan parts (vehicle row missing)",
    8: "Live eBay listing with no Stock row (TODO)",
    9: "Mobile-app deletions, last 7 days",
    10: "Aged x location x listing; probable phantom stock",
    11: "Policy-removal pattern",
}
REPORTED = (1, 2, 3, 4, 5, 6, 7, 9, 10, 11)

CRUSHED_TODO = ("TODO: Parts Logger vehicle status (on_yard / stripped / crushed) lives on the Railway API "
                "(GET /vehicles, /availability). The local vehicles.db is a 2-row test copy with no status "
                "column, so crushed cars cannot be read locally without going off the PC.")

# Section 11: words that describe position/side rather than the part itself.
POSITION_WORDS = {
    "driver", "drivers", "passenger", "passengers", "side", "front", "rear", "left", "right",
    "lh", "rh", "ns", "os", "n/s", "o/s", "nsf", "osf", "nsr", "osr", "ds", "ps", "upper", "lower",
    "inner", "outer", "top", "bottom", "centre", "center", "middle", "on", "body", "in", "the",
    "srs",
}


def part_group(name: str) -> str:
    """Simple keyword key: lowercase, airbag spellings merged ('air bag',
    'a bag', 'abag'), side/position words dropped, word order ignored.
    'CURTAIN SRS ABAG (PASSENGER SIDE)' -> 'airbag curtain'."""
    n = re.sub(r"\b[no]/s\b", " ", (name or "").lower())
    n = re.sub(r"\b(?:air\s*bag|a\s*bag)s?\b", "airbag", n)
    words = re.sub(r"[^a-z0-9]+", " ", n).split()
    kept = sorted({w for w in words if w not in POSITION_WORDS})
    return " ".join(kept) or "(blank)"


class ReportError(SystemExit):
    pass


def sql_identifiers() -> set[str]:
    return {m for sql in ALL_SQL for m in re.findall(r"\[([^\]]+)\]", sql)}


def check_allowlist() -> None:
    idents = sql_identifiers() - TABLES
    bad = sorted(idents - ALLOWED_COLUMNS)
    if bad:
        raise ReportError(f"REFUSING TO RUN: SQL uses columns not on the allowlist: {bad}")
    for col in OUTPUT_COLUMNS:
        if any(f in col.lower() for f in FORBIDDEN_OUTPUT):
            raise ReportError(f"REFUSING TO RUN: output column {col!r} looks like personal data")


def check_schema(cur) -> None:
    """Every table and [bracketed] column used must exist, or stop."""
    cur.execute(SQL_SCHEMA, *sorted(TABLES))
    live = defaultdict(set)
    for t, c in cur.fetchall():
        live[t].add(c)
    missing_tables = sorted(TABLES - set(live))
    all_cols = set().union(*live.values()) if live else set()
    missing_cols = sorted((sql_identifiers() - TABLES) - all_cols)
    if missing_tables or missing_cols:
        raise ReportError(
            "SCHEMA CHECK FAILED. BreakerPro's database no longer matches this script "
            "(a BreakerPro upgrade?). No report written.\n"
            f"  missing tables:  {missing_tables or '-'}\n"
            f"  missing columns: {missing_cols or '-'}\n"
            "  Check build-spec/BREAKERPRO-STOCK-READ.md and adjust the queries.")


def connect(cfg):
    import pyodbc
    pw_file = Path(cfg["BREAKERPRO_PASSWORD_FILE"])
    if not pw_file.is_file():
        raise ReportError(f"Password file not found: {pw_file}")
    conn = pyodbc.connect(
        f"DRIVER={{{cfg['BREAKERPRO_ODBC_DRIVER']}}};SERVER={cfg['BREAKERPRO_SERVER']};"
        f"DATABASE={cfg['BREAKERPRO_DATABASE']};UID={cfg['BREAKERPRO_USER']};"
        f"PWD={{{pw_file.read_text(encoding='utf-8').strip()}}};"
        "ApplicationIntent=ReadOnly;Encrypt=no;APP=parts-logger-exceptions-report",
        autocommit=True, readonly=True, timeout=5)
    conn.timeout = int(cfg["EXC_QUERY_TIMEOUT_S"])  # per-query timeout, seconds
    return conn


def run(cur, sql, *params, timings, label):
    t0 = time.perf_counter()
    cur.execute(sql, *params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    timings[label] = time.perf_counter() - t0
    return rows


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    return str(v).strip()


def out_row(section, r, **extra):
    row = {k: "" for k in OUTPUT_COLUMNS}
    row |= {"section": section, "section_name": SECTIONS[section]}
    for k in OUTPUT_COLUMNS:
        if k in r:
            row[k] = fmt(r[k])
    for k, v in extra.items():
        assert k in OUTPUT_COLUMNS, k
        row[k] = fmt(v)
    return row


def iso_week(d):
    if not d:
        return "(no date)"
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def money(rows):
    return sum((r["price"] or Decimal(0)) for r in rows)


def build(cfg, ref=None):
    check_allowlist()
    timings = {}
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        who = run(cur, SQL_WHOAMI, timings=timings, label="whoami")[0]
        if who["is_sysadmin"] or who["is_owner"] or not who["denies_write"]:
            raise ReportError(f"REFUSING TO RUN: login {who['login_name']!r} can write. "
                              "Use the breakerpro_readonly login.")
        check_schema(cur)
        timings["schema"] = 0.0

        summary = {r["state"]: r["parts"] for r in run(cur, SQL_STOCK_SUMMARY, timings=timings, label="summary")}
        ref_summary = None
        if ref:
            ref_summary = {r["state"]: r["parts"] for r in
                           run(cur, SQL_REF_SUMMARY, ref, timings=timings, label="ref summary")}
        s1 = run(cur, SQL_UNPICKED, int(cfg["EXC_UNPICKED_HOURS"]), timings=timings, label="1 unpicked")
        s2 = run(cur, SQL_DEAD, timings=timings, label="2 dead")
        s3 = run(cur, SQL_UNLISTED, timings=timings, label="3 unlisted")
        s4 = run(cur, SQL_NO_LOCATION, timings=timings, label="4 no location")
        s5 = run(cur, SQL_LOW_PRICE, Decimal(cfg["EXC_LOW_PRICE_GBP"]), timings=timings, label="5 low price")
        s6 = run(cur, SQL_AGED, int(cfg["EXC_AGED_DAYS"]), timings=timings, label="6 aged")
        s7 = run(cur, SQL_ORPHANS, timings=timings, label="7 orphans")
        s9 = run(cur, SQL_MOBILE_DELETIONS, int(cfg["EXC_DELETION_DAYS"]), timings=timings, label="9 deletions")
        grid = run(cur, SQL_AGE_GRID, timings=timings, label="10 grid")
        s10 = run(cur, SQL_PHANTOM, timings=timings, label="10 phantom")
        cur.close()
    finally:
        conn.close()

    limit = float(cfg["EXC_QUERY_TIMEOUT_S"])
    slow = {k: v for k, v in timings.items() if v > limit}
    if slow:
        raise ReportError(f"Queries over {limit}s (rework them): {slow}")

    rows = []
    rows += [out_row(1, r, invoice=r["invoice"], invoice_date=r["invoiced_at"], status="Pending Picking")
             for r in s1]
    rows += [out_row(2, r, status=r["listing_state"]) for r in s2]
    rows += [out_row(3, r, status=r["unlisted_reason"]) for r in s3]
    rows += [out_row(4, r, status=r["listing_state"]) for r in s4]
    rows += [out_row(5, r, status=r["listing_state"]) for r in s5]
    rows += [out_row(6, r, status=r["listing_state"]) for r in s6]
    rows += [out_row(7, r, status=r["orphan_reason"]) for r in s7]
    check_gbp = Decimal(cfg["EXC_DELETION_CHECK_GBP"])
    cutover = datetime.strptime(cfg["EXC_MOBILE_REMOVAL_CUTOVER"], "%Y-%m-%d")
    for r in s9:
        period = "mixed yard sales/damaged, pre-cutover" if r["deleted_date"] < cutover else "post-cutover"
        flag = "check: sale or damage?" if (r["price"] or 0) >= check_gbp else ""
        rows.append(out_row(9, r, status=period, flag=flag))

    for g in sorted(grid, key=lambda g: (g["band"], g["located"], g["listing"])):
        rows.append(out_row(10, {}, status=f"{g['band']} | {g['located']} | {g['listing']}",
                            count=g["parts"], price=g["value"], flag="grid cell"))
    rows += [out_row(10, r, status=r["listing_state"], flag="probable phantom stock") for r in s10]
    s11 = [r for r in s2 if r["listing_state"].startswith("dead: removed by eBay")]
    for r in s11:
        rows.append(out_row(11, r, status=part_group(r["part_name"])))
    phantom_refs = Counter(r["veh_ref"] for r in s10)
    phantom_value = defaultdict(Decimal)
    for r in s10:
        phantom_value[r["veh_ref"]] += r["price"] or Decimal(0)

    groups = {
        2: [("By state", Counter(r["listing_state"] for r in s2))],
        3: [("By VEH ref", Counter(r["veh_ref"] for r in s3)),
            ("By reason", Counter(r["unlisted_reason"] for r in s3))],
        4: [("By VEH ref", Counter(r["veh_ref"] for r in s4)),
            ("By creator (AddedBy)", Counter((r["added_by"] or "(blank)").upper() for r in s4)),
            ("By week added", Counter(iso_week(r["added_date"]) for r in s4))],
        6: [("By VEH ref", Counter(r["veh_ref"] for r in s6))],
        7: [("By VEH ref", Counter(r["veh_ref"] for r in s7)),
            ("By reason", Counter(r["orphan_reason"] for r in s7))],
        9: [("By user", Counter(r["deleted_by"] or "(blank)" for r in s9))],
        10: [("Phantom by VEH ref", phantom_refs),
             ("Phantom by creator (AddedBy)", Counter((r["added_by"] or "(blank)").upper() for r in s10))],
        11: [("Part group (top 15)", Counter(part_group(r["part_name"]) for r in s11))],
    }
    counts = {1: len(s1), 2: len(s2), 3: len(s3), 4: len(s4), 5: len(s5), 6: len(s6), 7: len(s7),
              9: len(s9), 10: len(s10), 11: len(s11)}
    extras = {
        6: f"Total shelf value: £{money(s6):,.2f}",
        5: f"Threshold: under £{cfg['EXC_LOW_PRICE_GBP']}",
        1: (f"{len({r['invoice'] for r in s1})} invoices; Pending Picking for over {cfg['EXC_UNPICKED_HOURS']}h; "
            "voided/refunded/cancelled excluded"),
        10: (f"Probable phantom stock (180+ days, no location, unlisted or dead listing): {len(s10)} parts, "
             f"£{money(s10):,.2f} across {len(phantom_refs)} VEH refs. Crushed cross-check: {CRUSHED_TODO}"),
        11: (f"{len(s11)} parts removed by eBay for policy, in {len(set(part_group(r['part_name']) for r in s11))} "
             "groups (lowercased, airbag spellings merged, side/position words dropped, word order ignored)"),
        9: (f"{sum(1 for r in s9 if (r['price'] or 0) >= check_gbp)} flagged at £{cfg['EXC_DELETION_CHECK_GBP']}+; "
            f"last listed price total £{money(s9):,.2f}; cutover {cfg['EXC_MOBILE_REMOVAL_CUTOVER']}"),
    }
    return dict(rows=rows, counts=counts, groups=groups, extras=extras, summary=summary, grid=grid,
                phantom_value=phantom_value,
                ref=ref, ref_summary=ref_summary, timings=timings, who=who["login_name"])


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def write_html(path, rep, cfg):
    e = html.escape
    in_stock = sum(rep["summary"].values())
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Exceptions</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--head:#f4f4f4;--accent:#b4441c}}
@media (prefers-color-scheme: dark){{:root{{--bg:#161616;--fg:#eee;--muted:#999;--line:#333;--head:#222;--accent:#f08a5d}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.muted{{color:var(--muted)}} .n{{color:var(--accent);font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}} th,td{{border:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top}}
th{{background:var(--head)}} .wrap{{overflow-x:auto}} details{{margin:6px 0}} .grid{{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}} .grid table{{width:auto}}
pre{{white-space:pre-wrap;background:var(--head);padding:8px}}
</style></head><body>
<h1>Stock exceptions: {date.today():%d %b %Y}</h1>
<p class="muted">Read-only from BreakerPro ({e(cfg['BREAKERPRO_SERVER'])}/{e(cfg['BREAKERPRO_DATABASE'])}) as {e(rep['who'])}, generated {datetime.now():%H:%M}.
In-stock parts: <b>{in_stock:,}</b> &middot; """ + " &middot; ".join(
        f"{e(k)}: <b>{v:,}</b>" for k, v in sorted(rep["summary"].items())) + "</p>"]
    if rep["ref"]:
        parts.append(f"<p>{e(rep['ref'].upper())}: " + ", ".join(
            f"{e(k)} {v}" for k, v in sorted(rep["ref_summary"].items())) + "</p>")
    parts.append("<table><tr><th>#</th><th>Section</th><th>Count</th></tr>" + "".join(
        f"<tr><td>{n}</td><td><a href='#s{n}'>{e(SECTIONS[n])}</a></td><td>{rep['counts'].get(n, 'TODO')}</td></tr>"
        for n in sorted(SECTIONS)) + "</table>")

    show = {1: ["invoice", "invoice_date", "part_id", "part_name", "veh_ref", "location"],
            2: ["veh_ref", "part_id", "part_name", "location", "price", "ebay_number", "status"],
            3: ["veh_ref", "part_id", "part_name", "location", "price", "status", "added_date"],
            4: ["veh_ref", "part_id", "part_name", "price", "status", "added_by", "added_date"],
            5: ["veh_ref", "part_id", "part_name", "location", "price", "status"],
            6: ["veh_ref", "part_id", "part_name", "location", "price", "added_date", "status"],
            7: ["veh_ref", "part_id", "part_name", "location", "price", "status"],
            9: ["deleted_date", "deleted_by", "veh_ref", "part_id", "part_name", "price", "flag", "status"],
            10: ["veh_ref", "part_id", "part_name", "price", "added_by", "added_date", "age_days", "status"],
            11: ["status", "veh_ref", "part_id", "part_name", "price", "ebay_number"]}
    def grid_html():
        cols = [(loc, lst) for loc in ("located", "no location") for lst in ("listed", "unlisted", "dead")]
        bands = sorted({g["band"] for g in rep["grid"]})

        def td(gs):
            n = sum(g["parts"] for g in gs)
            v = sum((g["value"] or Decimal(0)) for g in gs)
            return f"<td>{n:,}<br><span class='muted'>£{v:,.0f}</span></td>" if n else "<td class='muted'>-</td>"

        out = ["<div class='wrap'><table><tr><th rowspan=2>Age (AddedDate)</th>"
               "<th colspan=3>Located</th><th colspan=3>No location</th><th rowspan=2>Total</th></tr><tr>" +
               "".join(f"<th>{e(lst)}</th>" for _, lst in cols) + "</tr>"]
        for b in bands:
            out.append(f"<tr><th>{e(b[3:])}</th>" +
                       "".join(td([g for g in rep["grid"] if (g["band"], g["located"], g["listing"]) == (b, lo, li)])
                               for lo, li in cols) +
                       td([g for g in rep["grid"] if g["band"] == b]) + "</tr>")
        out.append("<tr><th>Total</th>" +
                   "".join(td([g for g in rep["grid"] if (g["located"], g["listing"]) == c]) for c in cols) +
                   td(rep["grid"]) + "</tr></table></div>")
        return "".join(out)

    def section_html(n):
        rows = [r for r in rep["rows"] if r["section"] == n and r["flag"] != "grid cell"]
        parts.append(f"<h2 id='s{n}'>{n}. {e(SECTIONS[n])} <span class='n'>{len(rows):,}</span></h2>")
        if n == 10:
            parts.append("<p class='muted'>Every in-stock part (vehicle rows excluded): count and total price per cell.</p>")
            parts.append(grid_html())
        if n in rep["extras"]:
            parts.append(f"<p class='muted'>{e(rep['extras'][n])}</p>")
        if n in rep["groups"]:
            parts.append("<div class='grid'>")
            for title, ctr in rep["groups"][n]:
                limit = 15 if title.startswith("Part group") else 25
                if title == "By week added":
                    top = sorted(ctr.items(), reverse=True)[:limit]
                elif title == "Phantom by VEH ref":
                    top = sorted(ctr.items(), key=lambda kv: (-rep["phantom_value"][kv[0]], kv[0]))[:limit]
                else:
                    top = ctr.most_common(limit)
                shown = sum(v for _, v in top)
                if title == "Phantom by VEH ref":
                    top = [(f"{k} (£{rep['phantom_value'][k]:,.0f})", v) for k, v in top]
                parts.append(f"<table><tr><th>{e(title)}</th><th>n</th></tr>" + "".join(
                    f"<tr><td>{e(str(k))}</td><td>{v}</td></tr>" for k, v in top) +
                    (f"<tr><td class='muted' colspan=2>+{len(ctr) - limit} more ({sum(ctr.values()) - shown} rows)</td></tr>"
                     if len(ctr) > limit else "") +
                    "</table>")
            parts.append("</div>")
        cols = show[n]
        table = ("<div class='wrap'><table><tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>" +
                 "".join("<tr>" + "".join(f"<td>{e(r[c])}</td>" for c in cols) + "</tr>" for r in rows) +
                 "</table></div>")
        parts.append(table if len(rows) <= 60 else f"<details><summary>Show all {len(rows):,} rows</summary>{table}</details>")

    for n in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11):
        section_html(n)
    parts.append(f"<h2 id='s8'>8. {e(SECTIONS[8])}</h2><pre>{e(EBAY_TODO)}</pre>")
    parts.append("<p class='muted'>Query times: " + ", ".join(
        f"{e(k)} {v:.2f}s" for k, v in rep["timings"].items()) + "</p></body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", help="also print listing-state counts for this VEH ref")
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # £ on the Windows console
    cfg = load_env()
    t0 = time.perf_counter()
    rep = build(cfg, args.ref)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"exceptions_{date.today():%Y-%m-%d}"
    write_csv(out / f"{stem}.csv", rep["rows"])
    write_html(out / f"{stem}.html", rep, cfg)

    print(f"In-stock parts: {sum(rep['summary'].values())}  {dict(sorted(rep['summary'].items()))}")
    if args.ref:
        print(f"{args.ref.upper()}: {dict(sorted(rep['ref_summary'].items()))}")
    for n in REPORTED:
        line = f"  {n}. {SECTIONS[n]:<36} {rep['counts'][n]:>5}"
        print(line + (f"   ({rep['extras'][n]})" if n in rep["extras"] else ""))
    print(f"  8. {SECTIONS[8]:<36}  TODO")
    print("Query times: " + ", ".join(f"{k} {v:.2f}s" for k, v in rep["timings"].items()))
    print(f"Total {time.perf_counter() - t0:.1f}s -> {out / stem}.csv / .html")


if __name__ == "__main__":
    main()
