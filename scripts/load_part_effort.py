"""
Load the filled-in dismantler worksheet into analytics/analytics.db.

    python scripts/load_part_effort.py [--effort analytics/part_effort.csv]
                                       [--courier analytics/courier_cost.csv] [--db analytics/analytics.db]

Validates every row first; if anything is wrong it lists each problem (file,
line, value) and loads nothing. Blank cells are allowed (not filled in yet).

  part_effort.csv  part_type_id must be in analytics/part_types.yaml, once;
                   pull_minutes one of 5 / 15 / 30 / 60 / 120;
                   size one of S / M / L / Pallet (any case).
  courier_cost.csv size_class one of S / M / L / Pallet, once each;
                   typical_courier_cost_gbp a number >= 0 ("£" allowed).

Tables (replaced on each load): part_effort, courier_cost.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = ROOT / "analytics" / "part_types.yaml"

PULL_MINUTES = {"5", "15", "30", "60", "120"}
SIZES = {"s": "S", "m": "M", "l": "L", "pallet": "Pallet"}
EFFORT_REQUIRED = ["part_type_id", "pull_minutes", "size", "notes"]
COURIER_REQUIRED = ["size_class", "typical_courier_cost_gbp"]

SCHEMA = """
DROP TABLE IF EXISTS part_effort;
CREATE TABLE part_effort (part_type TEXT PRIMARY KEY, label TEXT, pull_minutes INTEGER, size TEXT,
    notes TEXT, loaded_at TEXT);
DROP TABLE IF EXISTS courier_cost;
CREATE TABLE courier_cost (size_class TEXT PRIMARY KEY, typical_cost_gbp REAL, loaded_at TEXT);
"""


def read_csv(path: Path, required: list[str], errors: list[str]):
    with path.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in required if c not in (rd.fieldnames or [])]
        if missing:
            errors.append(f"{path.name}: missing column(s) {missing}")
            return []
        return [(i, {k: (v or "").strip() for k, v in r.items() if k}) for i, r in enumerate(rd, start=2)]


def validate_effort(path: Path, labels: dict, errors: list[str]) -> list[tuple]:
    out, seen = [], {}
    for line, r in read_csv(path, EFFORT_REQUIRED, errors):
        tid, mins, size = r["part_type_id"], r["pull_minutes"], r["size"]
        where = f"{path.name} line {line}"
        if tid not in labels:
            errors.append(f"{where}: unknown part_type_id {tid!r}")
            continue
        if tid in seen:
            errors.append(f"{where}: {tid!r} already on line {seen[tid]}")
            continue
        seen[tid] = line
        if mins and mins not in PULL_MINUTES:
            errors.append(f"{where} ({tid}): pull_minutes {mins!r} is not one of 5 / 15 / 30 / 60 / 120")
        if size and size.lower() not in SIZES:
            errors.append(f"{where} ({tid}): size {size!r} is not one of S / M / L / Pallet")
        out.append((tid, labels[tid], int(mins) if mins in PULL_MINUTES else None,
                    SIZES.get(size.lower()) if size else None, r.get("notes") or None))
    return out


def validate_courier(path: Path, errors: list[str]) -> list[tuple]:
    out, seen = [], set()
    for line, r in read_csv(path, COURIER_REQUIRED, errors):
        where = f"{path.name} line {line}"
        sc, cost = r["size_class"], r["typical_courier_cost_gbp"].replace("£", "").replace(",", "").strip()
        if sc.lower() not in SIZES:
            errors.append(f"{where}: size_class {sc!r} is not one of S / M / L / Pallet")
            continue
        sc = SIZES[sc.lower()]
        if sc in seen:
            errors.append(f"{where}: size_class {sc} appears twice")
            continue
        seen.add(sc)
        val = None
        if cost:
            try:
                val = float(cost)
                if val < 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{where} ({sc}): cost {r['typical_courier_cost_gbp']!r} is not a number >= 0")
                continue
        out.append((sc, val))
    return out


def load(effort: Path, courier: Path | None, db_path: Path) -> int:
    types = yaml.safe_load(TAXONOMY.read_text(encoding="utf-8"))["types"]
    labels = {t["id"]: t["label"] for t in types}
    errors: list[str] = []
    eff = validate_effort(effort, labels, errors)
    cou = validate_courier(courier, errors) if courier and courier.exists() else []
    if errors:
        print(f"REJECTED: {len(errors)} problem(s); nothing loaded.")
        for e in errors:
            print(f"  {e}")
        return 1
    now = datetime.now().isoformat(timespec="seconds")
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO part_effort VALUES (?,?,?,?,?,?)", [(*r, now) for r in eff])
            db.executemany("INSERT INTO courier_cost VALUES (?,?,?)", [(*r, now) for r in cou])
    finally:
        db.close()
    filled = sum(1 for r in eff if r[2] is not None or r[3] is not None)
    print(f"Loaded {len(eff)} part types ({filled} with pull time or size filled in) and "
          f"{len(cou)} courier sizes into {db_path}")
    not_listed = len(labels) - len(eff)
    if not_listed:
        print(f"  note: {not_listed} part types in part_types.yaml are not in {effort.name}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--effort", type=Path, default=ROOT / "analytics" / "part_effort.csv")
    ap.add_argument("--courier", type=Path, default=ROOT / "analytics" / "courier_cost.csv")
    ap.add_argument("--db", type=Path, default=ROOT / "analytics" / "analytics.db")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(load(a.effort, a.courier, a.db))


if __name__ == "__main__":
    main()
