"""Import our eBay sales (Seller Hub "All orders" reports) into sold_history.

Two input formats are accepted:
  * clean   - data/ebay-orders/ebay_sales_clean.csv (header on row 1, 12 columns)
  * raw     - a Seller Hub "All orders" download (junk first row, header on row 2,
              ~81 columns including buyer name/email/address/phone)

PII handling: columns are read through an ALLOWLIST (ALLOWED_COLUMNS). Anything
not on it - every Buyer*/Post To* column, notes, tax IDs etc. - is dropped as the
row is read, so it never reaches the DB, the API or a log line.

Price stored = Sold for + Postage and packaging (item + P&P). Seller Hub's own
"Total price" column is £0 / inconsistent on multi-item order lines (see
docs/pricing-recon.md), so it is not trusted.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime, timezone

from pricing.normalise import base_number, normalise
from pricing.titles import parse_title

# canonical field -> accepted header spellings (compared lower-cased, trimmed)
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "order_number": ("order number",),
    "sales_record_number": ("sales record number",),
    "item_number": ("item number",),
    "title": ("item title",),
    "custom_label": ("custom label",),
    "quantity": ("quantity",),
    "sold_for": ("sold for",),
    "postage": ("postage and packaging",),
    "total_price": ("total price",),
    "sale_date": ("sale date",),
    "dispatched_date": ("dispatched on date",),
    "transaction_id": ("transaction id",),
}
CANONICAL_FIELDS = tuple(ALLOWED_COLUMNS)
_HEADER_LOOKUP = {name: canon for canon, names in ALLOWED_COLUMNS.items() for name in names}

_DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%b-%d-%y", "%b-%d-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y")


class ImportFormatError(ValueError):
    pass


def parse_money(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace("£", "").replace("GBP", "").replace(",", "").strip()
    # Some exports mis-decode the pound sign; strip any leftover non-numeric prefix.
    s = s.lstrip("Â� ").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(value) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    if "T" in s and len(s) >= 10:  # ISO timestamp from the Fulfillment API
        s = s[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("could not decode CSV")


def read_orders_csv(source) -> tuple[list[dict], str]:
    """Read a clean or raw orders report. `source` is a path, bytes or str.

    Returns (rows, fmt) where each row holds ONLY CANONICAL_FIELDS keys and fmt
    is 'clean' or 'raw'."""
    if isinstance(source, bytes):
        text = _decode(source)
    elif isinstance(source, str) and "\n" in source:
        text = source
    else:
        with open(source, "rb") as fh:
            text = _decode(fh.read())

    all_rows = list(csv.reader(io.StringIO(text)))
    header_idx = None
    for i, row in enumerate(all_rows[:5]):
        lowered = {c.strip().lower() for c in row}
        if "order number" in lowered and "item number" in lowered:
            header_idx = i
            break
    if header_idx is None:
        raise ImportFormatError("no 'Order number' / 'Item number' header in the first 5 rows")

    header = all_rows[header_idx]
    fmt = "clean" if header_idx == 0 else "raw"
    # column index -> canonical name, allowlisted columns only
    keep = {}
    for idx, name in enumerate(header):
        canon = _HEADER_LOOKUP.get(name.strip().lower())
        if canon and canon not in keep.values():
            keep[idx] = canon

    out = []
    for raw in all_rows[header_idx + 1:]:
        row = {canon: (raw[idx].strip() if idx < len(raw) else "") for idx, canon in keep.items()}
        for f in CANONICAL_FIELDS:
            row.setdefault(f, "")
        out.append(row)
    return out, fmt


def sanitise_row(row: dict) -> dict:
    """Keep only allowlisted canonical keys (used on API-submitted rows too)."""
    return {f: ("" if row.get(f) is None else str(row.get(f)).strip()) for f in CANONICAL_FIELDS}


def build_record(row: dict, source: str, now: str | None = None) -> tuple[dict | None, str]:
    """Turn a canonical row into a sold_history record.

    Returns (record, "") or (None, skip_reason)."""
    row = sanitise_row(row)
    if not row["item_number"] or not row["title"]:
        return None, "no item"  # order-summary / footer rows in raw reports
    sold_for = parse_money(row["sold_for"])
    if sold_for is None or sold_for <= 0:
        return None, "no price"
    sold = parse_date(row["sale_date"])
    if sold is None:
        return None, "bad date"
    try:
        qty = max(1, int(float(row["quantity"] or 1)))
    except ValueError:
        qty = 1
    postage = parse_money(row["postage"]) or 0.0
    shipping = round(postage / qty, 2)
    pn = normalise(row["title"])
    parsed = parse_title(row["title"])
    dispatched = parse_date(row["dispatched_date"])
    record = {
        "part_number_norm": pn,
        "base_number": base_number(pn) if pn else "",
        "price": round(sold_for + shipping, 2),
        "shipping": shipping,
        "item_price": round(sold_for, 2),
        "sold_date": sold.isoformat(),
        "channel": "ebay",
        "source": source,
        "vehicle_ref": None,
        "raw_title": row["title"],
        "part_type": parsed["part_type"],
        "model": parsed["model"],
        "generation": parsed["generation"],
        "engine_group": parsed["engine_group"],
        "order_number": row["order_number"],
        "item_number": row["item_number"],
        "sales_record_number": row["sales_record_number"],
        "dispatched_date": dispatched.isoformat() if dispatched else None,
        "transaction_id": row["transaction_id"],
        "imported_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return record, ""


_INSERT_COLS = (
    "part_number_norm", "base_number", "price", "shipping", "item_price", "sold_date",
    "channel", "source", "vehicle_ref", "raw_title", "part_type", "model", "generation",
    "engine_group", "order_number", "item_number", "sales_record_number",
    "dispatched_date", "imported_at", "transaction_id",
)


def _key(rec: dict) -> tuple[str, str, str]:
    return (rec["order_number"], rec["item_number"], rec["sales_record_number"])


def import_rows(conn: sqlite3.Connection, rows: list[dict], source: str, dry_run: bool = False) -> dict:
    """Insert rows idempotently (dedupe on order + item + sales record number).

    In dry-run nothing is written; the summary says what *would* happen."""
    summary = {
        "read": len(rows), "inserted": 0, "duplicates": 0, "skipped": 0,
        "no_part_number": 0, "skip_reasons": {}, "dry_run": dry_run,
        "postage_free": 0, "postage_paid": 0, "date_min": None, "date_max": None,
    }
    seen: set[tuple[str, str, str]] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    sql = f"INSERT OR IGNORE INTO sold_history ({', '.join(_INSERT_COLS)}) VALUES ({placeholders})"
    for row in rows:
        rec, why = build_record(row, source, now)
        if rec is None:
            summary["skipped"] += 1
            summary["skip_reasons"][why] = summary["skip_reasons"].get(why, 0) + 1
            continue
        k = _key(rec)
        tk = ("txn", rec["item_number"], rec["transaction_id"]) if rec["transaction_id"] else None
        exists = k in seen or (tk is not None and tk in seen) or conn.execute(
            "SELECT 1 FROM sold_history WHERE order_number=? AND item_number=? AND sales_record_number=?", k
        ).fetchone() is not None or (tk is not None and conn.execute(
            "SELECT 1 FROM sold_history WHERE item_number=? AND transaction_id=?", tk[1:]
        ).fetchone() is not None)
        seen.add(k)
        if tk:
            seen.add(tk)
        if exists:
            summary["duplicates"] += 1
            continue
        if not dry_run:
            conn.execute(sql, tuple(rec[c] for c in _INSERT_COLS))
        summary["inserted"] += 1
        if not rec["part_number_norm"]:
            summary["no_part_number"] += 1
        summary["postage_free" if rec["shipping"] == 0 else "postage_paid"] += 1
        d = rec["sold_date"]
        summary["date_min"] = min(filter(None, [summary["date_min"], d]))
        summary["date_max"] = max(filter(None, [summary["date_max"], d]))
    if not dry_run:
        conn.commit()
    return summary


def format_summary(summary: dict, fmt: str = "") -> str:
    verb = "Would insert" if summary["dry_run"] else "Inserted"
    lines = [
        ("DRY RUN - nothing written" if summary["dry_run"] else "Import complete")
        + (f" ({fmt} format)" if fmt else ""),
        f"  Rows read:           {summary['read']}",
        f"  {verb + ':':<21}{summary['inserted']}",
        f"  Duplicates skipped:  {summary['duplicates']}",
        f"  Invalid rows:        {summary['skipped']}"
        + (f"  {summary['skip_reasons']}" if summary["skip_reasons"] else ""),
        f"  No part number:      {summary['no_part_number']} (still used for part-type pricing)",
        f"  Free P&P / paid P&P: {summary['postage_free']} / {summary['postage_paid']}",
    ]
    if summary["date_min"]:
        lines.append(f"  Sale dates:          {summary['date_min']} to {summary['date_max']}")
    return "\n".join(lines)
