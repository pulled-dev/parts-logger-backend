"""
Auto-learning end-to-end test script.

Tests the full auto-learning cycle and edge cases without requiring a running server.
Run: python test_autolearn.py
"""

import json
import os
import sys
import shutil
import tempfile

# Point vag_lookup at a temporary test database
_ORIG_DB_PATH = None


def _setup_test_db(tmp_path: str) -> str:
    """Copy the real database to a temp location and redirect vag_lookup to use it."""
    import vag_lookup

    src = os.path.join(os.path.dirname(__file__), "vag_parts_db.json")
    dst = os.path.join(tmp_path, "vag_parts_db_test.json")
    shutil.copy(src, dst)

    # Redirect module to use test database
    global _ORIG_DB_PATH
    _ORIG_DB_PATH = vag_lookup.DB_PATH
    vag_lookup.DB_PATH = dst
    vag_lookup._db = None  # force reload from new path
    return dst


def _teardown_test_db():
    """Restore vag_lookup to use the real database."""
    import vag_lookup
    if _ORIG_DB_PATH:
        vag_lookup.DB_PATH = _ORIG_DB_PATH
        vag_lookup._db = None


PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        print(f"  [PASS] {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label}{': ' + detail if detail else ''}")
        FAIL += 1


def run_tests():
    from vag_lookup import lookup_part, save_learned, normalise, extract_middle_group

    print("\n=== 1. Normalisation ===")
    check("strips spaces and uppercases",
          normalise("6j3 837 401 aj") == "6J3837401AJ")
    check("handles already clean input",
          normalise("5NA945096E") == "5NA945096E")
    check("handles mixed case with spaces",
          normalise("5g0 927 225 d") == "5G0927225D")

    print("\n=== 2. Middle Group Extraction ===")
    check("6J3837401AJ -> 837401",  extract_middle_group("6J3837401AJ") == "837401")
    check("5Q0407272C  -> 407272",  extract_middle_group("5Q0407272C")  == "407272")
    check("5NA945096E  -> 945096",  extract_middle_group("5NA945096E")  == "945096")
    check("6R0820045G  -> 820045",  extract_middle_group("6R0820045G")  == "820045")
    check("1K0907719C  -> 907719",  extract_middle_group("1K0907719C")  == "907719")
    check("engine code returns None", extract_middle_group("CBZ") is None)

    print("\n=== 3. Exact Lookup (known BreakerPro parts) ===")
    r = lookup_part("6C0907379N")
    check("exact match found",          r is not None)
    check("source = database",          r and r["source"] == "database")
    check("confidence = high",          r and r["confidence"] == "high")
    check("breakerpro_price is set",    r and r.get("breakerpro_price") is not None)
    check("description is non-empty",   r and bool(r.get("description")))

    print("\n=== 4. Group-Level Lookup (cross-platform matching) ===")
    # 6R0820045G is in exact (Polo Mk5 heater control panel, group=820045)
    # A Golf Mk7 version with same group should match via groups
    r2 = lookup_part("5G0820045A")
    check("group match found",          r2 is not None)
    check("source = database",          r2 and r2["source"] == "database")
    check("confidence = medium",        r2 and r2["confidence"] == "medium")
    check("description contains part",  r2 and "HEATER" in r2["description"].upper())

    print("\n=== 5. Unknown Part Returns None ===")
    r3 = lookup_part("ZZZ000000Z")
    check("unknown part returns None",  r3 is None)

    print("\n=== 6. Part Number Variations (spaces, lowercase) ===")
    r4 = lookup_part("6c0 907 379 n")   # same as exact test but with spaces/lowercase
    check("spaces+lowercase handled",   r4 is not None and r4["source"] == "database")

    print("\n=== 7. Auto-Learning Cycle ===")
    fake_pn = "9Z9999999Z"   # definitely not in database
    r5 = lookup_part(fake_pn)
    check("unknown part not in DB initially", r5 is None)

    # Simulate Claude identifying the part and saving it
    save_learned(fake_pn, "Test Part Widget")

    # Look up again — should now come from learned
    r6 = lookup_part(fake_pn)
    check("learned part now found",     r6 is not None)
    check("source = learned",           r6 and r6["source"] == "learned")
    check("description matches saved",  r6 and r6["description"] == "Test Part Widget")

    print("\n=== 8. Learned Part Persists to Disk ===")
    # Reload from disk and check
    import vag_lookup
    vag_lookup.reload_db()
    r7 = lookup_part(fake_pn)
    check("learned entry survives reload", r7 is not None and r7["source"] == "learned")

    # Check JSON file on disk
    with open(vag_lookup.DB_PATH) as f:
        db_on_disk = json.load(f)
    check("entry in JSON on disk",       fake_pn.upper() in db_on_disk.get("learned", {}))

    print("\n=== 9. Edge Cases ===")
    check("N/A returns None",           lookup_part("N/A") is None)
    check("empty string returns None",  lookup_part("") is None)
    # Paint code — no digits, but also no 6-digit group — should return None
    check("paint code LC9X returns None", lookup_part("LC9X") is None)
    # Short engine code — no group extractable
    check("engine code CBZ returns None", lookup_part("CBZ") is None)


def main():
    tmp_dir = tempfile.mkdtemp(prefix="vag_test_")
    try:
        db_path = _setup_test_db(tmp_dir)
        print(f"Test database: {db_path}")
        run_tests()
    finally:
        _teardown_test_db()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    main()
