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
- Flags any vehicle whose paint name and paint code are different colours
"""

import csv
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import urllib.request
import urllib.error
import json

# paint_codes / paint_config live in the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paint_codes import lookup_paint_name, paint_warning  # noqa: E402

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


def parse_paint_name(colour: str, code: str | None) -> str | None:
    """Extract the human paint NAME from a messy Colour string.

    Removes the paint code token (case-insensitive) and any parentheses,
    collapses whitespace, strips, and Title-Cases the remainder.
    Returns None if nothing is left.

    Examples:
        "LO41 FLAT BLACK"/LO41 -> "Flat Black"
        "Black (LC9X)"/LC9X    -> "Black"
        "Lava Grey Lz7l"/LZ7L  -> "Lava Grey"
        "Red"/None             -> "Red"
    """
    if not colour:
        return None
    text = colour
    if code:
        text = re.sub(re.escape(code), '', text, flags=re.IGNORECASE)
    text = text.replace('(', ' ').replace(')', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return None
    return text.title()


def parse_paint(colour: str) -> tuple[str, str]:
    """Paint code AND name from ONE Colour cell, as a pair: ("", "") if the
    cell is blank. If the cell has a code but no name text, the name comes
    from the paint code dictionary, so the two always describe the same
    colour."""
    code = parse_paint_code(colour) or ""
    name = parse_paint_name(colour, code or None) or ""
    if code and not name:
        name = lookup_paint_name(code) or ""
    return code, name


def _norm_code(value) -> str:
    return (value or "").strip().upper()


def _norm_name(value) -> str:
    return " ".join((value or "").split()).casefold()


def same_paint(code_a, name_a, code_b, name_b) -> bool:
    """True if two (code, name) pairs are the same paint (ignores case/spaces)."""
    return (_norm_code(code_a) == _norm_code(code_b)
            and _norm_name(name_a) == _norm_name(name_b))


def read_breakerpro_rows(csv_path):
    """Yield (row_number, row_dict) for every data row of a BreakerPro stock
    export. The first 3 lines are BreakerPro junk (title + blanks) and are
    skipped; the 4th is the real header. Rows are keyed by column name, so a
    row is never matched to a vehicle by its position in the file."""
    # utf-8-sig strips a BOM if BreakerPro/Excel adds one; errors='replace'
    # stops a stray non-UTF-8 character (e.g. from Excel re-saving) killing the run.
    with open(csv_path, encoding='utf-8-sig', errors='replace', newline='') as f:
        # BreakerPro CSVs have 3 garbage header rows before the real header
        for _ in range(3):
            f.readline()
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            yield i, row


def shared_identity_refs(rows) -> dict[str, set[str]]:
    """{REF: other refs in the same file whose row has the same VIN or reg}.

    A BreakerPro record cloned from another car keeps the other car's VIN,
    reg and colour (VEH120 was a copy of VEH89's Jetta row). Such a row can't
    be trusted for either ref."""
    owners = defaultdict(set)
    for _, row in rows:
        ref = (row.get("Stock Reference") or "").strip().upper()
        for col in ("Vin", "Reg"):
            value = (row.get(col) or "").strip().upper()
            if ref and value:
                owners[(col, value)].add(ref)
    shared = defaultdict(set)
    for refs in owners.values():
        if len(refs) > 1:
            for ref in refs:
                shared[ref] |= refs - {ref}
    return dict(shared)


def _first_word(value) -> str:
    words = (value or "").split()
    return words[0].casefold() if words else ""


def row_identity_problem(current: dict, row: dict, others=()) -> str | None:
    """Why this CSV row may describe a DIFFERENT car than the stored vehicle,
    or None if it looks like the same car. Checks: VIN/reg shared with another
    ref in the file, and make/model (first word) disagreeing with the record."""
    if others:
        return f"same VIN/reg as {', '.join(sorted(others))} in the CSV"
    for col, key in (("Make", "make"), ("Model", "model")):
        a, b = _first_word(current.get(key)), _first_word(row.get(col))
        if a and b and a != b:
            return f"{key} is {current.get(key)!r} here but {row.get(col)!r} in the CSV"
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


# Fields the importer is allowed to backfill on an EXISTING vehicle.
# make/model are excluded (they are required at create time and never blank),
# and everything else the API stores is left untouched. paint_code and
# paint_name are handled together by plan_paint_backfill(), never singly.
BACKFILL_FIELDS = ("year_range", "vin", "engine_code")


def _is_empty(value) -> bool:
    """A field counts as empty (backfillable) if it is None or blank string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def plan_paint_backfill(current: dict, csv_code: str, csv_name: str) -> tuple[dict, str | None]:
    """Decide what to write for paint on an EXISTING vehicle, as a pair.

    Returns (fields_to_patch, note). The rule: paint is only written when,
    afterwards, the vehicle's (paint_code, paint_name) is exactly this CSV
    row's pair, and only blank fields are ever filled. So a code is never
    paired with a name from somewhere else (how LC9X vehicles ended up
    "Reflex Silver"). If the stored paint differs from the CSV, nothing is
    written and `note` says so (fix those with scripts/fix_paint_mismatches.py).
    """
    if _is_empty(csv_code) and _is_empty(csv_name):
        return {}, None
    cur_code = current.get("paint_code") or ""
    cur_name = current.get("paint_name") or ""
    if same_paint(cur_code, cur_name, csv_code, csv_name):
        return {}, None

    code_ok = _is_empty(cur_code) or _norm_code(cur_code) == _norm_code(csv_code)
    name_ok = _is_empty(cur_name) or _norm_name(cur_name) == _norm_name(csv_name)
    if code_ok and name_ok:
        # Filling the blanks lands exactly on the CSV pair. Send both so the
        # server never auto-fills the name from a different source.
        return {"paint_code": csv_code, "paint_name": csv_name}, None

    return {}, (f"paint differs from CSV (has {cur_code or '-'} {cur_name or '-'}, "
                f"CSV {csv_code or '-'} {csv_name or '-'}), left unchanged")


def plan_backfill(current: dict, candidate: dict) -> tuple[dict, str | None]:
    """Fields to PATCH onto an existing vehicle (blanks only), plus an optional
    paint note. `candidate` holds the CSV row's values for BACKFILL_FIELDS and
    paint_code/paint_name."""
    to_fill = {}
    for field in BACKFILL_FIELDS:
        new_val = candidate.get(field)
        if _is_empty(new_val):
            continue  # nothing to write for this field
        if _is_empty(current.get(field)):
            to_fill[field] = new_val  # existing is blank → safe to fill
    paint, note = plan_paint_backfill(
        current, candidate.get("paint_code") or "", candidate.get("paint_name") or "")
    to_fill.update(paint)
    return to_fill, note


def post_vehicle(payload: dict, dry_run: bool = False) -> tuple[str, str]:
    """POST to /vehicles. Returns (status, message) where status is one of
    'created', 'exists', 'failed'. In dry_run mode, performs a read-only GET
    to determine whether the vehicle already exists — no writes are made."""
    if dry_run:
        exists = get_vehicle(payload["ref"]) is not None
        if exists:
            return "exists", "Already exists"
        return "created", "[dry-run] would create"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f"{API_BASE}/vehicles",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return "created", f"Created (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8') if e.fp else ""
        if e.code == 409 or "already exists" in body.lower() or "duplicate" in body.lower():
            return "exists", "Already exists"
        return "failed", f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return "failed", f"Error: {e}"


def get_vehicle(ref: str) -> dict | None:
    """GET /vehicles/{ref}. Returns the record dict, or None if 404/error."""
    req = urllib.request.Request(f"{API_BASE}/vehicles/{ref}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def patch_vehicle(ref: str, fields: dict) -> tuple[bool, str]:
    """PATCH /vehicles/{ref} with only the given fields. Returns (ok, message)."""
    data = json.dumps(fields).encode('utf-8')
    req = urllib.request.Request(
        f"{API_BASE}/vehicles/{ref}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return True, f"Patched (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8') if e.fp else ""
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return False, f"Error: {e}"


def backfill_vehicle(ref: str, candidate: dict, dry_run: bool,
                     row: dict | None = None, others=()) -> tuple[str, str]:
    """Fill ONLY blank fields on an existing vehicle. Never overwrites a value
    that is already set. Returns (status, message) where status is one of
    'updated', 'complete', 'held', 'failed'.

    `candidate` holds the values parsed from the CSV for BACKFILL_FIELDS and
    paint_code/paint_name. `row` is the raw CSV row and `others` the refs that
    share its VIN/reg: if the row looks like a different car, nothing is
    filled ('held').
    """
    current = get_vehicle(ref)
    if current is None:
        return "failed", "could not GET existing record"

    warn = paint_warning(current.get("paint_code"), current.get("paint_name"))
    problem = row_identity_problem(current, row or {}, others)
    if problem:
        return "held", (f"NOT filled, CSV row looks like another vehicle ({problem})"
                        + (f"; PAINT CHECK: {warn}" if warn else ""))

    to_fill, note = plan_backfill(current, candidate)
    extra = "".join(f"; {m}" for m in (note, f"PAINT CHECK: {warn}" if warn else None) if m)

    if not to_fill:
        return "complete", "already complete" + extra

    summary = ", ".join(f"{k}={v}" for k, v in to_fill.items())
    if dry_run:
        return "updated", f"[dry-run] would fill {summary}" + extra

    ok, msg = patch_vehicle(ref, to_fill)
    if ok:
        return "updated", f"filled {summary}" + extra
    return "failed", msg


def main():
    args = [a for a in sys.argv[1:]]
    fill_missing = "--fill-missing" in args
    dry_run = "--dry-run" in args
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) != 1:
        print("Usage: python import_breakerpro.py path/to/vehicles.csv "
              "[--fill-missing] [--dry-run]")
        sys.exit(1)

    csv_path = Path(positional[0])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    print(f"Reading {csv_path}...")
    print(f"Target API: {API_BASE}")
    print(f"Mode: {'fill-missing (backfill blanks on existing)' if fill_missing else 'create-only'}"
          f"{'  [DRY RUN — no writes]' if dry_run else ''}\n")

    stats = {"created": 0, "updated": 0, "complete": 0, "exists": 0,
             "held": 0, "failed": 0, "skipped": 0}
    failures = []
    paint_warnings = []
    held = []

    rows = list(read_breakerpro_rows(csv_path))
    shared = shared_identity_refs(rows)

    for i, row in rows:
        ref = (row.get("Stock Reference") or "").strip()
        make = (row.get("Make") or "").strip()
        model = (row.get("Model") or "").strip()
        year_raw = (row.get("Year Manufacture") or "").strip()
        colour = (row.get("Colour") or "").strip()
        engine_raw = (row.get("Engine") or "").strip()
        vin = (row.get("Vin") or "").strip()

        if not ref or not make or not model:
            stats["skipped"] += 1
            print(f"  [{i}] SKIP — missing ref/make/model: {row}")
            continue

        year = parse_year(year_raw)
        if not year:
            stats["skipped"] += 1
            print(f"  [{i}] SKIP {ref} — invalid year: {year_raw!r}")
            continue

        paint_code, paint_name = parse_paint(colour)
        engine_code = parse_engine_code(engine_raw)

        csv_warn = paint_warning(paint_code, paint_name)
        if csv_warn:
            paint_warnings.append((ref, f"BreakerPro Colour {colour!r}: {csv_warn}"))

        # NOTE: the API model (VehicleIn) accepts `year_range` (str) and
        # `vin`, and ignores unknown fields (extra="ignore"). Earlier this
        # script sent year_from/year_to, which were silently dropped — so
        # every vehicle imported with a NULL year. Map to year_range, and
        # capture the VIN, so imported vehicles are complete.
        payload = {
            "ref": ref,
            "make": make.upper().strip(),
            "model": model.strip(),
            "year_range": str(year),
            "paint_code": paint_code,
            "paint_name": paint_name,
            "engine_code": engine_code or "",
            "vin": vin or "",
            "is_active": True,
        }

        status, msg = post_vehicle(payload, dry_run=dry_run)

        # On an existing vehicle, optionally backfill only blank fields.
        if status == "exists" and fill_missing:
            candidate = {
                "year_range": str(year),
                "vin": vin or "",
                "paint_code": paint_code,
                "paint_name": paint_name,
                "engine_code": engine_code or "",
            }
            status, msg = backfill_vehicle(ref, candidate, dry_run, row=row,
                                           others=shared.get(ref.upper(), set()))
            if status == "held":
                held.append((ref, msg))
            if "PAINT CHECK: " in msg:
                paint_warnings.append((ref, "database: " + msg.split("PAINT CHECK: ", 1)[1]))

        label = {
            "created": "NEW ",
            "updated": "FILL",
            "complete": "DONE",
            "exists": "SKIP",
            "held": "HOLD",
            "failed": "FAIL",
        }.get(status, "????")

        if status == "created" and shared.get(ref.upper()):
            held.append((ref, "created, but its VIN/reg is also on "
                              f"{', '.join(sorted(shared[ref.upper()]))}: check in BreakerPro"))

        if status == "failed":
            stats["failed"] += 1
            failures.append((ref, msg))
            print(f"  [{i}] FAIL {ref:8} — {msg}")
        else:
            stats[status] += 1
            print(f"  [{i}] {label} {ref:8} {make:8} {model[:34]:34} "
                  f"{paint_code or '----':6} {engine_code or '----':6} — {msg}")

        # Be polite to the API
        time.sleep(0.05)

    print(f"\n--- Summary ---")
    print(f"  Created (new):          {stats['created']}")
    print(f"  Updated (blanks filled):{stats['updated']}")
    print(f"  Already complete:       {stats['complete']}")
    if not fill_missing:
        print(f"  Already exists:         {stats['exists']}")
    print(f"  Held (row looks wrong): {stats['held']}")
    print(f"  Skipped:                {stats['skipped']}")
    print(f"  Failed:                 {stats['failed']}")
    print(f"  Paint warnings:         {len(paint_warnings)}")

    if paint_warnings:
        print(f"\n--- Paint warnings (colour clash, or unverified code with no name) ---")
        for ref, msg in paint_warnings:
            print(f"  PAINT {ref}: {msg}")

    if held:
        print(f"\n--- Held: CSV row looks like a different vehicle (check BreakerPro) ---")
        for ref, msg in held:
            print(f"  HOLD {ref}: {msg}")

    if failures:
        print(f"\n--- Failures ---")
        for ref, msg in failures:
            print(f"  {ref}: {msg}")

    # Non-zero exit when anything failed, so the drop-folder runner
    # (vehicle-imports/Update Vehicles.bat) leaves the CSV in inbox to retry.
    # Paint warnings don't fail the run; they're printed for data entry.
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
