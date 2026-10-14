"""Import our eBay sales into the pricing engine's sold_history table.

Accepts the cleaned file (data/ebay-orders/ebay_sales_clean.csv) or a raw Seller
Hub "All orders" download. Buyer/post-to columns are dropped as the file is read,
so they never leave this PC.

Examples (run from the repo root):

    # Check what would happen, touching nothing:
    python scripts/import_ebay_sales.py --dry-run

    # Load into a local SQLite file (default data/pricing.db, git-ignored):
    python scripts/import_ebay_sales.py

    # Load the newest orders report in Downloads into the LIVE Railway DB:
    set PRICING_ADMIN_TOKEN=...   (PowerShell: $env:PRICING_ADMIN_TOKEN="...")
    python scripts/import_ebay_sales.py --from-downloads --api https://web-production-7a1f0.up.railway.app

Re-running is safe: rows are deduped on order number + item number + sales record number.
Never point this at anything under C:\\AutoManagement\\ (BreakerPro's folder).
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pricing.history import part_type_index  # noqa: E402
from pricing.sales_import import (  # noqa: E402
    ImportFormatError, format_summary, import_rows, read_orders_csv,
)
from pricing.store import init_pricing_db  # noqa: E402

DEFAULT_FILE = os.path.join(ROOT, "data", "ebay-orders", "ebay_sales_clean.csv")
DEFAULT_DB = os.path.join(ROOT, "data", "pricing.db")
BATCH = 500
FORBIDDEN_ROOT = os.path.normcase(os.path.abspath("C:\\AutoManagement"))


def _forbidden(path: str) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return p == FORBIDDEN_ROOT or p.startswith(FORBIDDEN_ROOT + os.sep)


def newest_download() -> str | None:
    """Newest CSV in ~/Downloads that looks like an eBay orders report."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    for path in sorted(glob.glob(os.path.join(downloads, "*.csv")), key=os.path.getmtime, reverse=True):
        try:
            read_orders_csv(path)
            return path
        except (ImportFormatError, OSError, UnicodeError):
            continue
    return None


def _merge(total: dict | None, part: dict) -> dict:
    if total is None:
        return dict(part)
    for k in ("read", "inserted", "duplicates", "skipped", "no_part_number", "postage_free", "postage_paid"):
        total[k] += part[k]
    for why, n in part["skip_reasons"].items():
        total["skip_reasons"][why] = total["skip_reasons"].get(why, 0) + n
    for k, fn in (("date_min", min), ("date_max", max)):
        vals = [v for v in (total[k], part[k]) if v]
        total[k] = fn(vals) if vals else None
    return total


def post_to_api(api: str, token: str, rows: list[dict], source: str, dry_run: bool) -> dict:
    import httpx

    total = None
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(rows), BATCH):
            resp = client.post(
                api.rstrip("/") + "/price/sold-history/import",
                headers={"X-Admin-Token": token},
                json={"rows": rows[i:i + BATCH], "source": source, "dry_run": dry_run},
            )
            if resp.status_code != 200:
                raise SystemExit(f"API error {resp.status_code}: {resp.text[:300]}")
            total = _merge(total, resp.json())
    return total or {"read": 0, "inserted": 0, "duplicates": 0, "skipped": 0, "no_part_number": 0,
                     "skip_reasons": {}, "dry_run": dry_run, "postage_free": 0, "postage_paid": 0,
                     "date_min": None, "date_max": None}


def _open_read_only(db_path: str) -> sqlite3.Connection:
    """Dry run: compare against an existing DB without writing to it. If there's
    no DB (or no sold_history table yet) use an empty in-memory one."""
    if os.path.exists(db_path):
        uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sold_history'"
        ).fetchone()
        if has:
            return conn
        conn.close()
    conn = sqlite3.connect(":memory:")
    init_pricing_db(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", nargs="?", help=f"orders CSV (default: {DEFAULT_FILE})")
    ap.add_argument("--from-downloads", action="store_true", help="use the newest orders report in ~/Downloads")
    ap.add_argument("--dry-run", action="store_true", help="read and report only; write nothing")
    ap.add_argument("--db", default=DEFAULT_DB, help="local SQLite file to write (default data/pricing.db)")
    ap.add_argument("--api", help="POST to this Parts Logger API instead of a local DB")
    ap.add_argument("--token", default=os.environ.get("PRICING_ADMIN_TOKEN", ""),
                    help="admin token for --api (default: PRICING_ADMIN_TOKEN env var)")
    ap.add_argument("--show-index", type=int, default=0, metavar="N",
                    help="after a local import, print the N biggest part-type groups")
    args = ap.parse_args(argv)

    path = args.file or (newest_download() if args.from_downloads else DEFAULT_FILE)
    if not path:
        print("No eBay orders report found in Downloads.")
        return 1
    if _forbidden(path) or (not args.api and _forbidden(args.db)):
        print("Refusing to touch C:\\AutoManagement\\ (BreakerPro's folder).")
        return 1
    try:
        rows, fmt = read_orders_csv(path)
    except (ImportFormatError, OSError) as e:
        print(f"Can't read {path}: {e}")
        return 1
    source = "csv_clean" if fmt == "clean" else "csv_raw"
    print(f"Read {len(rows)} rows from {os.path.basename(path)} ({fmt} format; buyer columns dropped)")

    if args.api:
        if not args.token:
            print("--api needs an admin token (--token or PRICING_ADMIN_TOKEN).")
            return 1
        summary = post_to_api(args.api, args.token, rows, source, args.dry_run)
        print(format_summary(summary, fmt))
        return 0

    if args.dry_run:
        conn = _open_read_only(args.db)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
        conn = sqlite3.connect(args.db)
        init_pricing_db(conn)
    try:
        summary = import_rows(conn, rows, source, dry_run=args.dry_run)
        print(format_summary(summary, fmt))
        if args.show_index and not args.dry_run:
            print(f"\nTop {args.show_index} part-type groups:")
            for g in part_type_index(conn, min_sales=2)[: args.show_index]:
                label = " ".join(x for x in (g["model"], g["generation"], g["engine_group"]) if x)
                print(f"  {g['count']:>4}  GBP {g['median']:>7.2f}  {label} - {g['part_type']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
