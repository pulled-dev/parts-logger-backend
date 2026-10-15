"""Unverified paint codes: their (unchecked) dictionary names are never
auto-filled. The vehicle keeps a blank name and shows "Check paint"."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import import_breakerpro as ib  # noqa: E402
import paint_audit  # noqa: E402
from paint_codes import PAINT_CODES, is_unverified_code, lookup_paint_name, paint_warning  # noqa: E402
from paint_config import PAINT_CODE_FAMILIES, UNVERIFIED_PAINT_CODES  # noqa: E402

UNVERIFIED = {"B4B4", "LB9Y", "LX7Z", "LH7Z", "LD5R", "LA3W", "LB5K", "LA5W", "LD7W",
              "LB7Z", "LD8U", "LB6V", "LP5C", "LD6P", "LR5W", "LB5W", "LZ7H"}


def test_the_17_unverified_codes():
    assert UNVERIFIED_PAINT_CODES == UNVERIFIED
    assert UNVERIFIED <= set(PAINT_CODES)              # all still listed for the dropdown
    assert not UNVERIFIED & set(PAINT_CODE_FAMILIES)   # a checked code must leave the list


@pytest.mark.parametrize("code", sorted(UNVERIFIED))
def test_lookup_never_returns_unverified_name(code):
    assert lookup_paint_name(code) is None
    assert lookup_paint_name(code.lower()) is None
    assert is_unverified_code(code)


def test_verified_codes_still_resolve():
    assert lookup_paint_name("LC9X") == "Deep Black Pearl"
    assert lookup_paint_name("LD5Q") == "Shadow Blue"
    assert not is_unverified_code("LC9X") and not is_unverified_code(None)


def test_paint_warning_for_unverified_code():
    assert "LX7Z" in paint_warning("LX7Z", None)
    assert "verified" in paint_warning("lx7z", "  ")
    assert paint_warning("LX7Z", "Graphite Grey") is None    # a person typed a name: fine
    assert paint_warning("LQ1Z", None) is None               # unknown code: never flagged
    assert paint_warning("LC9X", None) is None               # verified code, blank name
    assert "black" in paint_warning("LC9X", "Reflex Silver") # clash still reported


def test_create_with_unverified_code_leaves_name_blank(tmp_db):
    import db
    rec = db.create_vehicle({"ref": "VEH950", "make": "VW", "model": "Golf", "paint_code": "LX7Z"})
    assert (rec["paint_code"], rec["paint_name"]) == ("LX7Z", None)
    rec = db.create_vehicle({"ref": "VEH951", "make": "VW", "model": "Golf", "paint_code": "LC9X"})
    assert rec["paint_name"] == "Deep Black Pearl"


def test_patch_to_unverified_code_clears_old_name(tmp_db):
    import db
    db.create_vehicle({"ref": "VEH952", "make": "VW", "model": "Golf",
                       "paint_code": "LC9X", "paint_name": "Deep Black"})
    rec = db.update_vehicle("VEH952", {"paint_code": "LB7Z"})
    assert (rec["paint_code"], rec["paint_name"]) == ("LB7Z", None)


def test_api_shows_check_paint_for_unverified_code(client):
    r = client.post("/vehicles", json={"ref": "VEH953", "make": "VW", "model": "Golf",
                                       "paint_code": "LX7Z"})
    body = r.json()
    assert r.status_code == 201 and body["paint_name"] is None
    assert "LX7Z" in body["paint_warning"]
    assert "LX7Z" in client.get("/vehicles/VEH953").json()["paint_warning"]
    fixed = client.patch("/vehicles/VEH953", json={"paint_name": "Graphite Grey"}).json()
    assert fixed["paint_warning"] is None


def test_paint_codes_endpoint_hides_unverified_names(client):
    codes = {c["code"]: c for c in client.get("/paint-codes").json()}
    assert codes["LX7Z"] == {"code": "LX7Z", "name": "", "verified": False}
    assert codes["LC9X"] == {"code": "LC9X", "name": "Deep Black Pearl", "verified": True}
    assert set(codes) == set(PAINT_CODES)


def test_lookup_panel_does_not_fill_unverified_name(client):
    client.post("/vehicles", json={"ref": "VEH954", "make": "VW", "model": "Golf",
                                   "paint_code": "LX7Z"})
    from categories import CATEGORIES
    painted = next(k for k, c in CATEGORIES.items() if c.get("use_paint_code"))
    r = client.get("/lookup-panel", params={"ref": "VEH954", "category": painted})
    assert r.status_code == 200, r.text
    assert r.json()["vehicle"]["paint_code"] == "LX7Z"
    assert not r.json()["vehicle"]["paint_name"]


def test_importer_code_only_cell_gives_blank_name():
    assert ib.parse_paint("LX7Z") == ("LX7Z", "")
    # A name written in the same BreakerPro cell is that row's own text: kept.
    assert ib.parse_paint("LX7Z Graphite Grey") == ("LX7Z", "Graphite Grey")


def test_audit_lists_unverified_code_without_name():
    line = paint_audit.audit_vehicle({"ref": "VEH955", "paint_code": "LX7Z", "paint_name": None}, None)
    assert line["issues"] == "unverified code, no name"
    assert line["action"].startswith("check by hand") and line["proposed_paint_code"] == ""
