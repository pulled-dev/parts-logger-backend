"""
Build/Update VAG Parts Database from BreakerPro CSV exports.

Usage:
    python build_db.py input.csv [input2.csv ...] [--db vag_parts_db.json]
    python build_db.py --dir ./exports/ [--db vag_parts_db.json]

If the database file already exists, new entries are MERGED in.
Existing entries are never overwritten — they are considered verified.
"""

import argparse
import json
import os
import sys
from datetime import date

from breakerpro_parser import parse_csv, parse_directory, deduplicate, build_groups

DB_DEFAULT = os.path.join(os.path.dirname(__file__), "vag_parts_db.json")

_EMPTY_DB = {
    "_meta": {
        "version": "1.0",
        "description": "VAG part number lookup database built from BreakerPro export history",
        "source": "BreakerPro CSV exports from Pulled Apart Ltd",
        "last_updated": "",
        "total_exact_entries": 0,
        "total_group_entries": 0,
        "total_learned_entries": 0,
    },
    "exact": {},
    "groups": {},
    "learned": {},
}


def load_existing_db(db_path: str) -> dict:
    """Load existing database or return an empty template."""
    if os.path.exists(db_path):
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)
    import copy
    return copy.deepcopy(_EMPTY_DB)


def save_db(db: dict, db_path: str):
    """Write database to disk with readable formatting."""
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def build_db(csv_files=None, csv_dir=None, db_path=DB_DEFAULT):
    """
    Parse CSVs, build part mappings, and merge into the database file.

    Args:
        csv_files: List of CSV file paths to parse.
        csv_dir:   Directory containing CSV files to parse.
        db_path:   Path to the JSON database file.
    """
    all_parts = []

    if csv_dir:
        all_parts.extend(parse_directory(csv_dir))

    for filepath in (csv_files or []):
        print(f"Parsing: {filepath}...")
        results = parse_csv(filepath)
        print(f"  -> {len(results)} parts extracted")
        all_parts.extend(results)

    if not all_parts:
        print("No parts found. Provide CSV files or use --dir.")
        return

    print(f"\nTotal raw parts extracted: {len(all_parts)}")

    # Deduplicate and build group mappings
    new_exact_raw = deduplicate(all_parts)
    print(f"Unique part numbers from CSVs: {len(new_exact_raw)}")

    new_groups = build_groups(new_exact_raw)
    print(f"Unique middle groups: {len(new_groups)}")

    # Load existing database (or start fresh)
    db = load_existing_db(db_path)
    print(f"\nExisting database: {len(db.get('exact', {}))} exact, "
          f"{len(db.get('groups', {}))} groups, "
          f"{len(db.get('learned', {}))} learned")

    # Merge exact entries — existing entries take priority (already verified)
    added_exact = 0
    for pn, entry in new_exact_raw.items():
        if pn not in db["exact"]:
            db["exact"][pn] = {
                "description": entry["description"],
                "breakerpro_price": entry["price"],
                "vehicle": entry["vehicle"],
            }
            added_exact += 1

    # Merge group entries — existing entries take priority
    added_groups = 0
    for group, entry in new_groups.items():
        if group not in db["groups"]:
            db["groups"][group] = {
                "description": entry["description"],
                "avg_price": entry["avg_price"],
            }
            added_groups += 1

    # Update metadata
    db.setdefault("_meta", {}).update({
        "last_updated": date.today().isoformat(),
        "total_exact_entries": len(db["exact"]),
        "total_group_entries": len(db["groups"]),
        "total_learned_entries": len(db.get("learned", {})),
    })

    save_db(db, db_path)

    print(f"\nDatabase saved to: {db_path}")
    print(f"  Exact entries added:  {added_exact}")
    print(f"  Group entries added:  {added_groups}")
    print(f"  Total exact:          {len(db['exact'])}")
    print(f"  Total groups:         {len(db['groups'])}")
    print(f"  Learned entries:      {len(db.get('learned', {}))}")


def main():
    parser = argparse.ArgumentParser(
        description="Build/update VAG parts database from BreakerPro CSV exports"
    )
    parser.add_argument("files", nargs="*", help="CSV file(s) to parse")
    parser.add_argument("--dir", help="Directory containing CSV files")
    parser.add_argument("--db", default=DB_DEFAULT, help="Database JSON file path")
    args = parser.parse_args()

    if not args.files and not args.dir:
        parser.print_help()
        sys.exit(1)

    build_db(csv_files=args.files, csv_dir=args.dir, db_path=args.db)


if __name__ == "__main__":
    main()
