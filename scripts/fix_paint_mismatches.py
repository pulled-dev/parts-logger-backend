"""
Correct vehicles whose paint code/name don't match BreakerPro.

Uses the same checks as scripts/paint_audit.py. For every vehicle the audit
marks "update from CSV", it sets paint_code AND paint_name together to the
pair from that vehicle's latest BreakerPro row. Nothing else is touched.
Vehicles marked "check by hand" or "keep" are listed but never changed.

DRY RUN BY DEFAULT: it only reads and prints what it would change.
Writing needs --apply, and only after the dry-run output has been approved.

Usage:
    python scripts/fix_paint_mismatches.py            # dry run (same as --dry-run)
    python scripts/fix_paint_mismatches.py --dry-run
    python scripts/fix_paint_mismatches.py --apply    # writes (PATCH) to the live API
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_breakerpro import API_BASE, patch_vehicle  # noqa: E402
from paint_audit import audit, csv_files, fetch_vehicles, load_csv_paint  # noqa: E402


def plan_fixes(lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split audit lines into (fixes, manual). Each fix is
    {ref, before: (code, name), patch: {paint_code, paint_name}}."""
    fixes, manual = [], []
    for l in lines:
        if l["action"] == "update from CSV":
            fixes.append({
                "ref": l["ref"],
                "before": (l["db_paint_code"], l["db_paint_name"]),
                "patch": {"paint_code": l["proposed_paint_code"],
                          "paint_name": l["proposed_paint_name"]},
                "issues": l["issues"],
            })
        else:
            manual.append(l)
    return fixes, manual


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    if apply and "--dry-run" in args:
        print("Pick one: --dry-run or --apply.")
        sys.exit(2)

    print(f"Target API: {API_BASE}")
    print("Mode: APPLY (writes paint_code + paint_name)" if apply
          else "Mode: DRY RUN (read-only, nothing is changed)")
    print()

    files = csv_files()
    if not files:
        print("No BreakerPro CSVs found in vehicle-imports/done or inbox.")
        sys.exit(1)
    lines = audit(fetch_vehicles(), load_csv_paint(files))
    fixes, manual = plan_fixes(lines)

    verb = "Updating" if apply else "Would update"
    print(f"{verb} {len(fixes)} vehicle(s):")
    failed = []
    for f in fixes:
        bc, bn = f["before"]
        p = f["patch"]
        print(f"  {f['ref']:10} {bc or '-':5} {bn or '-':22} -> {p['paint_code'] or '-':5} "
              f"{p['paint_name'] or '-':22} [{f['issues']}]")
        if apply:
            ok, msg = patch_vehicle(f["ref"], p)
            if not ok:
                failed.append((f["ref"], msg))
                print(f"      FAILED: {msg}")
            time.sleep(0.05)

    if manual:
        print(f"\nNot changed ({len(manual)}):")
        for l in manual:
            print(f"  {l['ref']:10} {l['db_paint_code'] or '-':5} {l['db_paint_name'] or '-':22} "
                  f"{l['action']}: {l['warning'] or l['issues']}")

    if not apply:
        print("\nDry run only. Nothing was changed. Re-run with --apply once approved.")
    elif failed:
        print(f"\n{len(failed)} update(s) failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
