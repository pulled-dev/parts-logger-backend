"""
Read-only extract from BreakerPro for the analytics store. SELECT ONLY.

This module holds every query the analytics build sends to BreakerPro, so
the keyword check can be run on this file alone. SQLite writes live in
scripts/analytics_build.py.

Same guards as scripts/exceptions_report.py: every [bracketed] column must be
on ALLOWED_COLUMNS (no customer, delivery, PayPal or note columns) and exist
in the live schema; the login must not be able to write; per-query timeout.
[Transaction Details] is only read inside CASE/LIKE to classify SALESMAN,
PAYMENT, PAYPAL and VOID lines; it is never returned.
"""

import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from exceptions_report import ReportError, connect, load_env  # noqa: E402

TABLES = {"Stock", "Invoices", "StockDeleted"}
ALLOWED_COLUMNS = {
    "Part ID", "Vehicle ID", "Stock Reference", "Part Type", "Part Name", "Part Location",
    "Part Nett Sale", "Ebay_FixedPrice", "Ebay_Number", "Ebay_Status", "AddedDate",
    "Make", "Model", "Year Manufacture", "Engine", "VEH_ENGINECODE", "Fuel", "VIN",
    "Invoice ID", "Invoice Date", "Invoice Time", "Transaction Type",
    "Transaction Details", "Transaction Amount",
}
VEHICLE_ROW = "'Breaking For Spares'"
REF = "UPPER(LTRIM(RTRIM({a}.[Stock Reference])))"
# Only the VIN pieces the model decoder needs leave SQL Server: maker (WMI,
# positions 1-3), VAG model code (7-8) and model year (10). Never the full VIN.
VIN_PARTS = ("SUBSTRING(UPPER(LTRIM(RTRIM({a}.[VIN]))), 1, 3) AS vin_wmi, "
             "SUBSTRING(UPPER(LTRIM(RTRIM({a}.[VIN]))), 7, 2) AS vin_model_code, "
             "SUBSTRING(UPPER(LTRIM(RTRIM({a}.[VIN]))), 10, 1) AS vin_year_char, "
             "LEN(LTRIM(RTRIM({a}.[VIN]))) AS vin_len")
PRICE = ("COALESCE(TRY_CONVERT(decimal(10,2), NULLIF(LTRIM(RTRIM(s.[Ebay_FixedPrice])), '')),"
         " TRY_CONVERT(decimal(10,2), NULLIF(LTRIM(RTRIM(s.[Part Nett Sale])), '')))")

SQL_WHOAMI = """
SELECT SUSER_SNAME() AS login_name, IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin,
       IS_ROLEMEMBER('db_owner') AS is_owner, IS_ROLEMEMBER('db_denydatawriter') AS denies_write
"""

SQL_SCHEMA = """
SELECT t.name AS table_name, c.name AS column_name
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
WHERE t.name IN (?, ?, ?)
"""

# Every PART line ever invoiced (one row per part sold).
SQL_SALE_LINES = f"""
SELECT p.[Invoice ID] AS invoice_id, p.[Invoice Date] AS invoice_date, p.[Invoice Time] AS invoice_time,
       p.[Part ID] AS part_id, {REF.format(a='p')} AS veh_ref, p.[Vehicle ID] AS vehicle_id,
       p.[Part Name] AS part_name, p.[Part Type] AS bp_part_type,
       TRY_CONVERT(decimal(10,2), p.[Transaction Amount]) AS net_price,
       p.[Transaction Amount] AS net_raw,
       UPPER(LTRIM(RTRIM(p.[Make]))) AS make, UPPER(LTRIM(RTRIM(p.[Model]))) AS model,
       p.[Year Manufacture] AS year, p.[AddedDate] AS added_date
FROM [Invoices] p WHERE p.[Transaction Type] = 'PART'
"""

# One row per invoice: channel signals, void/refund flag, gross paid and the
# DELIVERY (postage/courier) charge. Paid = parts + VAT + delivery; VAT is on
# the parts only, so the parts' gross share is paid minus delivery.
SQL_INVOICE_FLAGS = """
SELECT i.[Invoice ID] AS invoice_id,
  MAX(CASE WHEN i.[Transaction Type] = 'SALESMAN' AND i.[Transaction Details] LIKE '%BY AUTO' THEN 1 ELSE 0 END) AS auto_raised,
  MAX(CASE WHEN i.[Transaction Type] = 'PAYPAL' THEN 1 ELSE 0 END) AS paypal_import,
  MAX(CASE WHEN i.[Transaction Type] = 'PAYMENT' AND i.[Transaction Details] = 'Cash' THEN 1 ELSE 0 END) AS cash,
  MAX(CASE WHEN i.[Transaction Type] = 'VOID'
             OR (i.[Transaction Type] = 'PAYMENT' AND (i.[Transaction Details] LIKE '%CANCEL%'
                                                      OR i.[Transaction Details] LIKE '%REFUND%'))
           THEN 1 ELSE 0 END) AS voided,
  -SUM(CASE WHEN i.[Transaction Type] = 'PAYMENT' AND i.[Transaction Details] IN ('Pay Pal', 'Cash')
            THEN TRY_CONVERT(decimal(10,2), i.[Transaction Amount]) END) AS gross_paid,
  SUM(CASE WHEN i.[Transaction Type] = 'DELIVERY'
           THEN TRY_CONVERT(decimal(10,2), i.[Transaction Amount]) END) AS delivery
FROM [Invoices] i GROUP BY i.[Invoice ID]
"""

# Check total for the reconciliation test.
SQL_PART_TOTAL = """
SELECT COUNT(1) AS lines, SUM(TRY_CONVERT(decimal(12,2), i.[Transaction Amount])) AS net_total,
       SUM(CASE WHEN TRY_CONVERT(decimal(12,2), i.[Transaction Amount]) IS NULL THEN 1 ELSE 0 END) AS unparseable
FROM [Invoices] i WHERE i.[Transaction Type] = 'PART'
"""

# Parts in stock now (vehicle rows excluded).
SQL_STOCK = f"""
SELECT s.[Part ID] AS part_id, {REF.format(a='s')} AS veh_ref, s.[Part Name] AS part_name,
       LTRIM(RTRIM(s.[Part Location])) AS location, {PRICE} AS price, s.[AddedDate] AS added_date,
       CASE WHEN ISNULL(s.[Ebay_Number], '') = '' THEN 'unlisted'
            WHEN CAST(s.[Ebay_Status] AS nvarchar(20)) LIKE 'Failed%' THEN 'dead'
            ELSE 'listed' END AS listing
FROM [Stock] s WHERE s.[Part Type] <> {VEHICLE_ROW}
"""

# Distinct part names in StockDeleted (Stock and Invoices names come from the
# two queries above).
SQL_REMOVED_NAMES = f"""
SELECT s.[Part Name] AS part_name, COUNT(1) AS n
FROM [StockDeleted] s WHERE s.[Part Type] <> {VEHICLE_ROW} GROUP BY s.[Part Name]
"""

# Vehicles: the vehicle row where there is one, else make/model from any part
# row (Stock, StockDeleted, Invoices) carrying that stock ref.
SQL_VEHICLES = f"""
WITH v AS (
  SELECT {REF.format(a='s')} AS veh_ref, s.[Vehicle ID] AS vehicle_id,
         UPPER(LTRIM(RTRIM(s.[Make]))) AS make, UPPER(LTRIM(RTRIM(s.[Model]))) AS model,
         s.[Year Manufacture] AS year, s.[Engine] AS engine, s.[VEH_ENGINECODE] AS engine_code,
         s.[Fuel] AS fuel, {VIN_PARTS.format(a='s')}, 1 AS has_vehicle_row
  FROM [Stock] s WHERE s.[Part Type] = {VEHICLE_ROW}
), parts AS (
  SELECT {REF.format(a='s')} AS veh_ref, UPPER(LTRIM(RTRIM(s.[Make]))) AS make, UPPER(LTRIM(RTRIM(s.[Model]))) AS model,
         s.[Year Manufacture] AS year, s.[Engine] AS engine, s.[VEH_ENGINECODE] AS engine_code, s.[Fuel] AS fuel
  FROM [Stock] s WHERE s.[Part Type] <> {VEHICLE_ROW}
  UNION ALL
  SELECT {REF.format(a='s')}, UPPER(LTRIM(RTRIM(s.[Make]))), UPPER(LTRIM(RTRIM(s.[Model]))),
         s.[Year Manufacture], s.[Engine], s.[VEH_ENGINECODE], s.[Fuel]
  FROM [StockDeleted] s
  UNION ALL
  SELECT {REF.format(a='s')}, UPPER(LTRIM(RTRIM(s.[Make]))), UPPER(LTRIM(RTRIM(s.[Model]))),
         s.[Year Manufacture], s.[Engine], s.[VEH_ENGINECODE], s.[Fuel]
  FROM [Invoices] s WHERE s.[Transaction Type] = 'PART'
)
SELECT v.veh_ref, v.vehicle_id, v.make, v.model, v.year, v.engine, v.engine_code, v.fuel,
       v.vin_wmi, v.vin_model_code, v.vin_year_char, v.vin_len, v.has_vehicle_row, 1 AS n
FROM v
UNION ALL
SELECT p.veh_ref, NULL, p.make, p.model, p.year, p.engine, p.engine_code, p.fuel,
       NULL, NULL, NULL, NULL, 0, COUNT(1)
FROM parts p
WHERE p.veh_ref LIKE 'VEH%' AND NOT EXISTS (SELECT 1 FROM v WHERE v.veh_ref = p.veh_ref)
GROUP BY p.veh_ref, p.make, p.model, p.year, p.engine, p.engine_code, p.fuel
"""
# For refs without a vehicle row, each make/model/... combination comes back
# with its count; the build keeps the most common one per ref.

ALL_SQL = [SQL_SALE_LINES, SQL_INVOICE_FLAGS, SQL_PART_TOTAL, SQL_STOCK, SQL_REMOVED_NAMES, SQL_VEHICLES]


def _identifiers():
    return {m for sql in ALL_SQL for m in re.findall(r"\[([^\]]+)\]", sql)}


def _check_allowlist():
    bad = sorted((_identifiers() - TABLES) - ALLOWED_COLUMNS)
    if bad:
        raise ReportError(f"REFUSING TO RUN: SQL uses columns not on the allowlist: {bad}")


def _check_schema(cur):
    cur.execute(SQL_SCHEMA, *sorted(TABLES))
    live = defaultdict(set)
    for t, c in cur.fetchall():
        live[t].add(c)
    missing_t = sorted(TABLES - set(live))
    missing_c = sorted((_identifiers() - TABLES) - set().union(*live.values()))
    if missing_t or missing_c:
        raise ReportError("SCHEMA CHECK FAILED. BreakerPro no longer matches the analytics extract "
                          f"(a BreakerPro upgrade?). Nothing written.\n  missing tables: {missing_t or '-'}\n"
                          f"  missing columns: {missing_c or '-'}")


def extract(log=print) -> dict:
    """Run every query once and return plain Python rows (dicts)."""
    _check_allowlist()
    cfg = load_env()
    limit = float(cfg["EXC_QUERY_TIMEOUT_S"])
    conn = connect(cfg)
    out, timings = {}, {}
    try:
        cur = conn.cursor()
        cur.execute(SQL_WHOAMI)
        who = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        if who["is_sysadmin"] or who["is_owner"] or not who["denies_write"]:
            raise ReportError(f"REFUSING TO RUN: login {who['login_name']!r} can write.")
        _check_schema(cur)
        for key, sql in [("sale_lines", SQL_SALE_LINES), ("invoice_flags", SQL_INVOICE_FLAGS),
                         ("part_total", SQL_PART_TOTAL), ("stock", SQL_STOCK),
                         ("deleted_names", SQL_REMOVED_NAMES), ("vehicles", SQL_VEHICLES)]:
            t0 = time.perf_counter()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            out[key] = [dict(zip(cols, r)) for r in cur.fetchall()]
            timings[key] = time.perf_counter() - t0
        cur.close()
    finally:
        conn.close()
    slow = {k: round(v, 2) for k, v in timings.items() if v > limit}
    if slow:
        raise ReportError(f"Queries over {limit}s (rework them): {slow}")
    out["timings"] = timings
    out["login"] = who["login_name"]
    log("BreakerPro extract: " + ", ".join(f"{k} {len(out[k])} rows {timings[k]:.2f}s"
                                           for k in timings))
    return out
