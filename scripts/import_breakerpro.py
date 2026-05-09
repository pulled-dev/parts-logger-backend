"""
BreakerPro CSV → Parts Logger v2 vehicle database import.

One-off script. Run locally. Reads BreakerPro stock export CSV,
extracts what we need, POSTs each vehicle to the live /vehicles endpoint.

Usage:
    python import_breakerpro.py path/to/vehicles.csv

What it does:
- Skips first 3 lines (BreakerPro header garbage)
- For each row: extracts ref, make, model, year, paint code, engine code
- POSTs to live API
- Reports successes, failures, and skipped rows with reasons
- Idempotent: if vehicle already exists, skips (won't error out)
"""

import csv
import re
import sys
import time
from pathlib import Path

import urllib.request
import urllib.error
import json

API_BASE = "https://web-production-7a1f0.up.railway.app"

# VAG paint codes follow patterns like LC9X, LY9T, LZ9Y, LX7W, LB9A, LS3H, LA3H
# 2 letters + 1 digit + 1 alphanumeric, OR sometimes 4 chars with mixed case
PAINT_CODE_PATTERN = re.compile(r'\b([LMW][A-Z][0-9][A-Z0-9])\b', re.IGNORECASE)

# Engine codes are 3-4 uppercase letters, often after a displacement value
# Examples: "1.6TDi CAY" → CAY, "2.0 TDI CRB" → CRB, "1.5 TFSI DPCA" → DPCA
ENGINE_CODE_PATTERN = re.compile(r'\b([A-Z]{3,4})\b')


def parse_paint_code(colour: str) -> str | None:
    """Extract a VAG paint code from messy colour strings."""
    if not colour:
        return None
    match = PAINT_CODE_PATTERN.search(colour)
    if match:
        return match.group(1).upper()
    return None


def parse_engine_code(engine: str) -> str | None:
    """Extract a 3-4 letter engine code from messy engine strings."""
    if not engine:
        return None
    # Skip pure numeric strings like "1390" or "1598"
    if engine.strip().isdigit():
        return None
    # Find all uppercase 3-4 letter sequences, return last one
    # (engine code typically comes after the displacement)
    matches = ENGINE_CODE_PATTERN.findall(engine.upper())
    # Filter out common false positives
    blacklist = {'TDI', 'TFSI', 'TSI', 'FSI', 'GTI', 'GTD', 'MPI', 'CR'}
    candidates = [m for m in matches if m not in blacklist]
    if candidates:
        return candidates[-1]  # Last match is usually the actual engine code
    return None


def parse_year(year_str: str) -> int | None:
    """Parse year, return None if invalid."""
    if not year_str:
        return None
    try:
        year = int(year_str.strip())
        if 1990 <= year <= 2030:
            return year
    except ValueError:
        pass
    return None


def post_vehicle(payload: dict) -> tuple[bool, str]:
    """POST to /vehicles endpoint. Returns (success, message)."""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f"{API_BASE}/vehicles",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            return True, f"Created (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8') if e.fp else ""
        if e.code == 409 or "already exists" in body.lower() or "duplicate" in body.lower():
            return True, "Already exists (skipped)"
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return False, f"Error: {e}"


def main():
    if len(sys.argv) != 2:
        print("Usage: python import_breakerpro.py path/to/vehicles.csv")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    print(f"Reading {csv_path}...")
    print(f"Posting to {API_BASE}/vehicles\n")

    with open(csv_path, encoding='utf-8') as f:
        # BreakerPro CSVs have 3 garbage header rows before the real header
        for _ in range(3):
            f.readline()
        reader = csv.DictReader(f)

        stats = {"created": 0, "exists": 0, "failed": 0, "skipped": 0}
        failures = []

        for i, row in enumerate(reader, start=1):
            ref = (row.get("Stock Reference") or "").strip()
            make = (row.get("Make") or "").strip()
            model = (row.get("Model") or "").strip()
            year_raw = (row.get("Year Manufacture") or "").strip()
            colour = (row.get("Colour") or "").strip()
            engine_raw = (row.get("Engine") or "").strip()

            if not ref or not make or not model:
                stats["skipped"] += 1
                print(f"  [{i}] SKIP — missing ref/make/model: {row}")
                continue

            year = parse_year(year_raw)
            if not year:
                stats["skipped"] += 1
                print(f"  [{i}] SKIP {ref} — invalid year: {year_raw!r}")
                continue

            paint_code = parse_paint_code(colour)
            engine_code = parse_engine_code(engine_raw)

            payload = {
                "ref": ref,
                "make": make.upper().strip(),
                "model": model.strip(),
                "year_from": year,
                "year_to": year,
                "paint_code": paint_code or "",
                "engine_code": engine_code or "",
                "is_active": True,
            }

            success, msg = post_vehicle(payload)
            if success:
                if "exists" in msg:
                    stats["exists"] += 1
                else:
                    stats["created"] += 1
                print(f"  [{i}] OK   {ref:8} {make:8} {model[:40]:40} {paint_code or '----':6} {engine_code or '----':6} — {msg}")
            else:
                stats["failed"] += 1
                failures.append((ref, msg))
                print(f"  [{i}] FAIL {ref:8} — {msg}")

            # Be polite to the API
            time.sleep(0.05)

    print(f"\n--- Summary ---")
    print(f"  Created:        {stats['created']}")
    print(f"  Already exists: {stats['exists']}")
    print(f"  Skipped:        {stats['skipped']}")
    print(f"  Failed:         {stats['failed']}")

    if failures:
        print(f"\n--- Failures ---")
        for ref, msg in failures:
            print(f"  {ref}: {msg}")


if __name__ == "__main__":
    main()
