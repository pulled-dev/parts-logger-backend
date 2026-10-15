"""
Paint audit: which vehicles have the wrong paint name or code? READ-ONLY.

Compares every vehicle in the live database (GET /vehicles only, never
writes) with the BreakerPro stock exports in vehicle-imports/done (and
inbox), and lists each vehicle where:

  - the paint name and code are different colours (e.g. LC9X "Reflex Silver"),
  - the stored code or name differs from what the latest CSV would give, or
  - BreakerPro's own Colour text contradicts itself.

Usage:
    python scripts/paint_audit.py [--out path.csv]

Writes the full list as a CSV for data entry (default:
vehicle-imports/logs/paint-audit-<date>.csv, git-ignored) and prints counts.
scripts/fix_paint_mismatches.py uses the same logic to propose fixes.
"""

import csv
import json
import sys
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from db import normalise_ref  # noqa: E402
from import_breakerpro import (  # noqa: E402
    API_BASE, parse_paint, read_breakerpro_rows, row_identity_problem, same_paint,
    shared_identity_refs,
)
from paint_config import CONFIRMED_VEHICLE_PAINT  # noqa: E402
from paint_codes import (  # noqa: E402
    colour_families_in_name, colour_families_of_code, paint_family_conflict, paint_warning,
)

IMPORT_DIRS = (ROOT / "vehicle-imports" / "done", ROOT / "vehicle-imports" / "inbox")

AUDIT_COLUMNS = [
    "ref", "make", "model", "reg", "vin", "active",
    "db_paint_code", "db_paint_name",
    "csv_colour", "csv_paint_code", "csv_paint_name",
    "issues", "warning",
    "proposed_paint_code", "proposed_paint_name", "action", "csv_file", "csv_row",
]


def csv_files(dirs=IMPORT_DIRS) -> list[Path]:
    """BreakerPro exports, oldest first. Files in done/ are named
    <YYYY-MM-DD_HHMMSS>_..., so a name sort is a date sort; inbox files
    (not yet imported) count as newest."""
    files = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.csv")))
    return files


def load_csv_paint(files) -> dict[str, dict]:
    """{ref: paint info from the NEWEST file that contains that ref}.
    `others` lists refs whose row in that file has the same VIN or reg."""
    index = {}
    for path in files:
        rows = list(read_breakerpro_rows(path))
        shared = shared_identity_refs(rows)
        for row_no, row in rows:
            try:
                ref = normalise_ref(row.get("Stock Reference"))
            except ValueError:
                continue
            colour = (row.get("Colour") or "").strip()
            code, name = parse_paint(colour)
            index[ref] = {
                "colour": colour, "code": code, "name": name,
                "make": (row.get("Make") or "").strip(),
                "model": (row.get("Model") or "").strip(),
                "reg": (row.get("Reg") or "").strip(),
                "vin": (row.get("Vin") or "").strip(),
                "others": sorted(shared.get((row.get("Stock Reference") or "").strip().upper(), ())),
                "file": Path(path).name, "row": row_no,
            }
    return index


def _spelling_only(a: str, b: str) -> bool:
    """Same colour, just spelt differently (BreakerPro's "Relfex Silver"):
    nearly identical text and the same colour family."""
    if colour_families_in_name(a) != colour_families_in_name(b):
        return False
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= 0.85


def audit_vehicle(vehicle: dict, csv_info: dict | None) -> dict | None:
    """One audit line for a vehicle, or None if its paint looks right."""
    code = vehicle.get("paint_code") or ""
    name = vehicle.get("paint_name") or ""
    issues = []
    warning = paint_warning(code, name)
    if paint_family_conflict(code, name):
        issues.append("code/name colour clash")
    elif warning:
        issues.append("unverified code, no name")

    proposed = None
    action = None
    confirmed = CONFIRMED_VEHICLE_PAINT.get(vehicle.get("ref"))
    problem = csv_info and row_identity_problem(
        vehicle, {"Make": csv_info.get("make"), "Model": csv_info.get("model")},
        csv_info.get("others"))
    if problem:
        issues.append(f"CSV row looks like another vehicle ({problem})")

    if confirmed:
        # Checked by hand: beats the CSV, whatever the CSV says.
        if same_paint(code, name, *confirmed):
            return None
        issues.append("differs from hand-confirmed paint")
        proposed, action = confirmed, "update (confirmed by hand)"
    elif problem:
        action = "check by hand (CSV row looks like another vehicle)"
    elif csv_info and csv_info["code"]:
        if (code or "").strip().upper() != csv_info["code"]:
            issues.append("code differs from CSV")
        if not same_paint("", name, "", csv_info["name"]):
            if code.strip().upper() == csv_info["code"] and _spelling_only(name, csv_info["name"]):
                issues.append("name spelt differently in CSV")
                action = "keep (spelling difference only)"
            else:
                issues.append("name differs from CSV")
        if paint_family_conflict(csv_info["code"], csv_info["name"]):
            issues.append("BreakerPro colour contradicts itself")
        if action is None and not same_paint(code, name, csv_info["code"], csv_info["name"]):
            proposed = (csv_info["code"], csv_info["name"])
    elif csv_info and csv_info["name"] and not same_paint("", name, "", csv_info["name"]):
        # BreakerPro has a plain colour ("Silver") but no code. A code stored
        # here was typed in the app and is more specific: never remove it.
        # Only a clash of colours needs a look.
        have = set(colour_families_of_code(code)) | colour_families_in_name(name)
        want = colour_families_in_name(csv_info["name"])
        if not code:
            issues.append("name differs from CSV")
            proposed = ("", csv_info["name"])
        elif have and want and not have & want:
            issues.append("colour differs from CSV (CSV has no code)")
            action = "check by hand (CSV has a colour but no code)"

    if not issues:
        return None

    if action:
        if not action.startswith("update"):
            proposed = None
    elif proposed and "BreakerPro colour contradicts itself" not in issues:
        action = "update from CSV"
    elif proposed:
        action = "check by hand (BreakerPro text is inconsistent)"
        proposed = None
    else:
        action = "check by hand (no CSV colour for this vehicle)"

    csv_info = csv_info or {}
    return {
        "ref": vehicle.get("ref"),
        "make": vehicle.get("make") or "",
        "model": vehicle.get("model") or "",
        "reg": csv_info.get("reg", ""),
        "vin": vehicle.get("vin") or "",
        "active": "yes" if vehicle.get("is_active", True) else "no",
        "db_paint_code": code,
        "db_paint_name": name,
        "csv_colour": csv_info.get("colour", ""),
        "csv_paint_code": csv_info.get("code", ""),
        "csv_paint_name": csv_info.get("name", ""),
        "issues": "; ".join(issues),
        "warning": warning or "",
        "proposed_paint_code": proposed[0] if proposed else "",
        "proposed_paint_name": proposed[1] if proposed else "",
        "action": action,
        "csv_file": csv_info.get("file", ""),
        "csv_row": csv_info.get("row", ""),
    }


def audit(vehicles: list[dict], csv_index: dict[str, dict]) -> list[dict]:
    lines = []
    for v in sorted(vehicles, key=lambda v: v.get("ref") or ""):
        line = audit_vehicle(v, csv_index.get(v.get("ref")))
        if line:
            lines.append(line)
    return lines


def fetch_vehicles() -> list[dict]:
    """All vehicles, including soft-deleted. GET only."""
    req = urllib.request.Request(f"{API_BASE}/vehicles?include_inactive=true", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def write_audit_csv(lines: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        w.writeheader()
        w.writerows(lines)


def summarise(vehicles: list[dict], lines: list[dict]) -> str:
    def n(tag):
        return sum(1 for l in lines if tag in l["issues"])
    return "\n".join([
        f"Vehicles checked:                {len(vehicles)}",
        f"Vehicles with a paint problem:   {len(lines)}",
        f"  code/name colour clash:        {n('colour clash')}",
        f"  unverified code, no name:      {n('unverified code')}",
        f"  code differs from CSV:         {n('code differs')}",
        f"  name differs from CSV:         {n('name differs')}",
        f"  CSV colour only, clashes:      {n('CSV has no code')}",
        f"  spelling difference only:      {n('spelt differently')}",
        f"  BreakerPro text inconsistent:  {n('contradicts itself')}",
        f"  CSV row is another vehicle:    {n('looks like another vehicle')}",
        f"  differs from confirmed paint:  {n('hand-confirmed')}",
        f"Fixable from CSV:                {sum(1 for l in lines if l['action'] == 'update from CSV')}",
        f"Fixable from confirmed paint:    {sum(1 for l in lines if l['action'] == 'update (confirmed by hand)')}",
        f"Keep as is (spelling only):      {sum(1 for l in lines if l['action'].startswith('keep'))}",
        f"Need a manual check:             {sum(1 for l in lines if l['action'].startswith('check by hand'))}",
    ])


def main():
    args = sys.argv[1:]
    out = None
    if "--out" in args:
        out = Path(args[args.index("--out") + 1])
    if out is None:
        out = ROOT / "vehicle-imports" / "logs" / f"paint-audit-{datetime.now():%Y-%m-%d_%H%M%S}.csv"

    files = csv_files()
    if not files:
        print("No BreakerPro CSVs found in vehicle-imports/done or inbox.")
        sys.exit(1)
    print(f"Reading live vehicles (read-only) from {API_BASE} ...")
    vehicles = fetch_vehicles()
    csv_index = load_csv_paint(files)
    print(f"BreakerPro CSVs: {len(files)} (newest: {files[-1].name}), {len(csv_index)} refs\n")

    lines = audit(vehicles, csv_index)
    write_audit_csv(lines, out)
    print(summarise(vehicles, lines))
    print(f"\nFull list: {out}")


if __name__ == "__main__":
    main()
