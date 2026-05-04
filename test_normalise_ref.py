"""Tests for normalise_ref() — Phase 2 Task 0."""

from db import normalise_ref

# ── Happy paths ────────────────────────────────────────────────────
assert normalise_ref("1234") == "VEH1234", f'got {normalise_ref("1234")}'
assert normalise_ref("VEH1234") == "VEH1234", f'got {normalise_ref("VEH1234")}'
assert normalise_ref("veh1234") == "VEH1234", f'got {normalise_ref("veh1234")}'
assert normalise_ref(" VEH1234 ") == "VEH1234", f'got {normalise_ref(" VEH1234 ")}'
assert normalise_ref("VEH 1234") == "VEH1234", f'got {normalise_ref("VEH 1234")}'
assert normalise_ref("VEH-1234") == "VEH1234", f'got {normalise_ref("VEH-1234")}'
assert normalise_ref("VEG1234") == "VEHVEG1234", f'got {normalise_ref("VEG1234")}'

# ── Error cases ────────────────────────────────────────────────────
import traceback

for bad_input, label in [(None, "None"), ("", "empty string"), ("   ", "whitespace"), ("VEH", "bare VEH")]:
    try:
        normalise_ref(bad_input)
        raise AssertionError(f"Expected ValueError for {label}, got no exception")
    except ValueError:
        pass  # expected
    except AssertionError:
        raise

print("ALL PASS")
