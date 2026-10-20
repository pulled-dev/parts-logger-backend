"""Paint code/name integrity: pair-only updates, BreakerPro row alignment,
the colour-family guard, the audit and the dry-run correction plan.

Background: VEH153 is LC9X (a black) but showed "Reflex Silver", because an
early import sent the code without a name and the server filled the name from
a wrong dictionary entry (paint_codes.PAINT_CODES["LC9X"])."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import import_breakerpro as ib  # noqa: E402
import paint_audit  # noqa: E402
from fix_paint_mismatches import plan_fixes  # noqa: E402
from paint_codes import (  # noqa: E402
    PAINT_CODES, colour_families_in_name, lookup_paint_name, paint_family_conflict,
)

HEADER = ("Vehicle Id,Make,Model,Stock Reference,Engine,Style,Year Manufacture,"
          "Colour,Reg,Vin,Miles,Fuel,Originator,On-site Date,VAT Status,Vehicle Id")


def _row(ref, model, colour, vin, reg="AB12CDE"):
    return (f"VH-1,Volkswagen,{model},{ref},1.2 CGP,Hatchback 5 Door,2014,"
            f"{colour},{reg},{vin},1000,PETROL,,29/08/26,Global,VH-1")


def _write_breakerpro(path, rows, junk=("Stock - Stock Reference  - 121026 112723", "", "")):
    path.write_text("\n".join(list(junk) + [HEADER] + rows) + "\n", encoding="utf-8")
    return path


# ── 1. Colour-family guard ──────────────────────────────────────────────

def test_guard_flags_lc9x_with_silver_name():
    warn = paint_family_conflict("LC9X", "Reflex Silver")
    assert warn and "LC9X" in warn and "black" in warn and "silver" in warn


def test_guard_is_case_insensitive_on_code():
    assert paint_family_conflict("lc9x", "reflex silver")


def test_guard_does_not_flag_unknown_code():
    assert "LQ1Z" not in PAINT_CODES
    assert paint_family_conflict("LQ1Z", "Reflex Silver") is None


def test_guard_does_not_flag_matching_or_colourless_names():
    assert paint_family_conflict("LC9X", "Deep Black") is None
    assert paint_family_conflict("LC9X", "Deep Black Pearl") is None
    assert paint_family_conflict("LC5F", "Blue Graphite") is None   # two families, one matches
    assert paint_family_conflict("LA7T", "Sharkskin") is None       # no colour word
    assert paint_family_conflict("LC9X", "") is None
    assert paint_family_conflict("", "Reflex Silver") is None
    assert paint_family_conflict(None, None) is None


def test_name_family_matches_whole_words_only():
    assert colour_families_in_name("Fred Grey") == {"grey"}       # "red" inside "Fred" ignored
    assert colour_families_in_name("Blue Graphite") == {"blue", "grey"}


def test_dictionary_never_contradicts_family_table():
    """The bug's source: PAINT_CODES had LC9X → "Reflex Silver". Every name
    the server can auto-fill must agree with paint_config."""
    bad = {c: n for c, n in PAINT_CODES.items() if paint_family_conflict(c, n)}
    assert bad == {}


def test_dictionary_corrected_entries():
    assert lookup_paint_name("LC9X") == "Deep Black Pearl"
    assert lookup_paint_name("LZ9Y") == "Phantom Black"
    assert lookup_paint_name("LC9A") == "Pure White"


# ── 2. Code and name always move as a pair ──────────────────────────────

def test_parse_paint_returns_pair_from_one_cell():
    assert ib.parse_paint("LC9X Deep Black") == ("LC9X", "Deep Black")
    assert ib.parse_paint("Black (LC9X)") == ("LC9X", "Black")
    assert ib.parse_paint("LC9X") == ("LC9X", "Deep Black Pearl")   # name from fixed dictionary
    assert ib.parse_paint("Silver") == ("", "Silver")
    assert ib.parse_paint("") == ("", "")


@pytest.mark.parametrize("cur_code", ["", "LC9X", "LA7W"])
@pytest.mark.parametrize("cur_name", ["", "Deep Black", "Reflex Silver"])
@pytest.mark.parametrize("csv_pair", [("LC9X", "Deep Black"), ("", "Black"), ("LC9X", "")])
def test_backfill_writes_both_or_neither(cur_code, cur_name, csv_pair):
    current = {"paint_code": cur_code, "paint_name": cur_name}
    fill, _note = ib.plan_paint_backfill(current, *csv_pair)
    assert set(fill) in (set(), {"paint_code", "paint_name"})
    if fill:
        # The result is exactly the CSV row's pair, and nothing set was overwritten.
        assert (fill["paint_code"], fill["paint_name"]) == csv_pair
        assert cur_code in ("", csv_pair[0]) and cur_name.casefold() in ("", csv_pair[1].casefold())


def test_backfill_fills_blank_name_for_same_code():
    fill, note = ib.plan_paint_backfill({"paint_code": "LC9X", "paint_name": ""}, "LC9X", "Deep Black")
    assert fill == {"paint_code": "LC9X", "paint_name": "Deep Black"} and note is None


def test_backfill_never_pairs_new_name_with_old_code():
    """Cause (a): an old code kept and a name filled from another row."""
    fill, note = ib.plan_paint_backfill({"paint_code": "LA7W", "paint_name": ""}, "LC9X", "Deep Black")
    assert fill == {}
    assert "differs from CSV" in note


def test_backfill_leaves_set_pair_alone_and_reports():
    fill, note = ib.plan_paint_backfill(
        {"paint_code": "LC9X", "paint_name": "Reflex Silver"}, "LC9X", "Deep Black")
    assert fill == {} and "Reflex Silver" in note


def test_backfill_vehicle_patch_carries_pair(monkeypatch):
    sent = {}
    monkeypatch.setattr(ib, "get_vehicle", lambda ref: {
        "ref": ref, "paint_code": None, "paint_name": None,
        "year_range": "2014", "vin": "", "engine_code": "CGP"})
    monkeypatch.setattr(ib, "patch_vehicle", lambda ref, f: (sent.update(f), (True, "ok"))[1])
    status, _ = ib.backfill_vehicle("VEH153", {
        "year_range": "2014", "vin": "WVW1", "engine_code": "CGP",
        "paint_code": "LC9X", "paint_name": "Deep Black"}, dry_run=False)
    assert status == "updated"
    assert sent == {"vin": "WVW1", "paint_code": "LC9X", "paint_name": "Deep Black"}


def test_backfill_vehicle_reports_paint_check(monkeypatch):
    monkeypatch.setattr(ib, "get_vehicle", lambda ref: {
        "ref": ref, "paint_code": "LC9X", "paint_name": "Reflex Silver",
        "year_range": "2014", "vin": "WVW1", "engine_code": "CGP"})
    monkeypatch.setattr(ib, "patch_vehicle", lambda *a: pytest.fail("must not write"))
    status, msg = ib.backfill_vehicle("VEH153", {
        "year_range": "2014", "vin": "WVW1", "engine_code": "CGP",
        "paint_code": "LC9X", "paint_name": "Deep Black"}, dry_run=False)
    assert status == "complete" and "PAINT CHECK:" in msg


def test_patch_new_code_without_name_drops_old_name(tmp_db):
    import db
    db.create_vehicle({"ref": "VEH900", "make": "VW", "model": "Polo",
                       "paint_code": "LA7W", "paint_name": "Reflex Silver"})
    rec = db.update_vehicle("VEH900", {"paint_code": "LC9X"})
    assert (rec["paint_code"], rec["paint_name"]) == ("LC9X", "Deep Black Pearl")
    rec = db.update_vehicle("VEH900", {"paint_code": "LQ1Z"})      # unknown code
    assert (rec["paint_code"], rec["paint_name"]) == ("LQ1Z", None)


def test_patch_same_code_or_explicit_name_keeps_name(tmp_db):
    import db
    db.create_vehicle({"ref": "VEH901", "make": "VW", "model": "Polo",
                       "paint_code": "LC9X", "paint_name": "Deep Black"})
    assert db.update_vehicle("VEH901", {"paint_code": "lc9x"})["paint_name"] == "Deep Black"
    assert db.update_vehicle("VEH901", {"vin": "X"})["paint_name"] == "Deep Black"
    rec = db.update_vehicle("VEH901", {"paint_code": "LB9A", "paint_name": "Candy White"})
    assert rec["paint_name"] == "Candy White"


# ── 3. Row alignment: 3 junk header rows, VEH gaps ──────────────────────

def test_rows_align_after_junk_header_and_gaps(tmp_path):
    path = _write_breakerpro(tmp_path / "stock.csv", [
        _row("VEH150", "Golf Mk7", "LA7W Reflex Silver", "VIN150"),
        _row("VEH154", "Golf Mk7 Dsg", "LO41 FLAT BLACK", "VIN154"),   # 151-153 missing, out of order
        _row("veh153", "Polo 6r", "LC9X Deep Black", "VIN153"),        # lowercase ref
        _row("VEH160", "Polo 6r", "", "VIN160"),
    ])
    rows = list(ib.read_breakerpro_rows(path))
    assert [n for n, _ in rows] == [1, 2, 3, 4]
    assert rows[0][1]["Stock Reference"] == "VEH150"           # header is line 4, not data
    idx = paint_audit.load_csv_paint([path])
    assert set(idx) == {"VEH150", "VEH153", "VEH154", "VEH160"}
    assert (idx["VEH153"]["code"], idx["VEH153"]["name"], idx["VEH153"]["vin"]) == \
        ("LC9X", "Deep Black", "VIN153")
    assert (idx["VEH154"]["code"], idx["VEH154"]["vin"]) == ("LO41", "VIN154")
    assert (idx["VEH160"]["code"], idx["VEH160"]["name"]) == ("", "")


def test_junk_rows_with_commas_do_not_shift_columns(tmp_path):
    path = _write_breakerpro(tmp_path / "stock.csv",
                             [_row("VEH153", "Polo 6r", "LC9X Deep Black", "VIN153")],
                             junk=("Stock, Report", "Printed,12/10/26,", ","))
    (_, row), = ib.read_breakerpro_rows(path)
    assert row["Stock Reference"] == "VEH153" and row["Colour"] == "LC9X Deep Black"


def test_newest_csv_wins(tmp_path):
    old = _write_breakerpro(tmp_path / "2026-09-01_old.csv",
                            [_row("VEH153", "Polo 6r", "Silver", "VIN153")])
    new = _write_breakerpro(tmp_path / "2026-10-12_new.csv",
                            [_row("VEH153", "Polo 6r", "LC9X Deep Black", "VIN153")])
    idx = paint_audit.load_csv_paint([old, new])
    assert idx["VEH153"]["code"] == "LC9X" and idx["VEH153"]["file"] == new.name


# ── 4. Audit + dry-run correction plan ──────────────────────────────────

def _csv(code, name, colour=None):
    return {"code": code, "name": name, "colour": colour or f"{code} {name}".strip(),
            "reg": "GJ14VPZ", "vin": "", "file": "x.csv", "row": 1}


def test_audit_veh153_proposes_csv_pair():
    v = {"ref": "VEH153", "paint_code": "LC9X", "paint_name": "Reflex Silver", "is_active": True}
    line = paint_audit.audit_vehicle(v, _csv("LC9X", "Deep Black"))
    assert "colour clash" in line["issues"] and "name differs" in line["issues"]
    assert (line["proposed_paint_code"], line["proposed_paint_name"]) == ("LC9X", "Deep Black")
    assert line["action"] == "update from CSV"


def test_audit_ignores_case_only_differences_and_matches():
    v = {"ref": "VEH1", "paint_code": "LB9A", "paint_name": "candy white"}
    assert paint_audit.audit_vehicle(v, _csv("LB9A", "Candy White")) is None


def test_audit_blank_csv_colour_never_proposes_blanking():
    v = {"ref": "VEH2", "paint_code": "LC9X", "paint_name": "Reflex Silver"}
    line = paint_audit.audit_vehicle(v, _csv("", "", colour=""))
    assert line["proposed_paint_code"] == "" and line["action"].startswith("check by hand")


def test_audit_not_in_csv_only_flagged_on_clash():
    assert paint_audit.audit_vehicle({"ref": "VEH3", "paint_code": "LB9A", "paint_name": "Candy White"}, None) is None
    line = paint_audit.audit_vehicle({"ref": "VEH4", "paint_code": "LZ9Y", "paint_name": "Pure White"}, None)
    assert line and line["action"].startswith("check by hand")


def test_plan_fixes_always_patches_code_and_name_together():
    vehicles = [
        {"ref": "VEH153", "paint_code": "LC9X", "paint_name": "Reflex Silver"},
        {"ref": "VEH200", "paint_code": "", "paint_name": "LY9C Ibis white"},
        {"ref": "VEH201", "paint_code": "LZ9Y", "paint_name": "Pure White"},   # not in CSV
    ]
    index = {"VEH153": _csv("LC9X", "Deep Black"), "VEH200": _csv("LY9C", "Ibis White")}
    fixes, manual = plan_fixes(paint_audit.audit(vehicles, index))
    assert [f["ref"] for f in fixes] == ["VEH153", "VEH200"]
    assert all(set(f["patch"]) == {"paint_code", "paint_name"} for f in fixes)
    assert fixes[1]["patch"] == {"paint_code": "LY9C", "paint_name": "Ibis White"}
    assert [m["ref"] for m in manual] == ["VEH201"]


# ── 5. API and logger show the warning ──────────────────────────────────

def test_api_returns_paint_warning(client):
    r = client.post("/vehicles", json={"ref": "VEH153", "make": "VW", "model": "Polo",
                                       "paint_code": "LC9X", "paint_name": "Reflex Silver"})
    assert r.status_code == 201 and "LC9X" in r.json()["paint_warning"]
    assert "black" in client.get("/vehicles/VEH153").json()["paint_warning"]
    listed = {v["ref"]: v for v in client.get("/vehicles").json()}
    assert listed["VEH153"]["paint_warning"]
    ok = client.patch("/vehicles/VEH153", json={"paint_name": "Deep Black"}).json()
    assert ok["paint_warning"] is None


def test_api_unknown_code_has_no_warning(client):
    r = client.post("/vehicles", json={"ref": "VEH154", "make": "VW", "model": "Golf",
                                       "paint_code": "LQ1Z", "paint_name": "Reflex Silver"})
    assert r.json()["paint_warning"] is None


def test_logger_renders_paint_warning():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as fh:
        src = fh.read()
    # vehicle list row and body-panel paint line both show it
    assert src.count("v.paint_warning ?") + src.count("if (v.paint_warning)") >= 2
    assert src.count('class="paint-check-warn"') >= 2
    assert "escHtml(v.paint_warning)" in src


def test_audit_never_removes_a_code_breakerpro_lacks():
    """VEH1: app has LA7W Reflex Silver, BreakerPro only "Silver" → keep."""
    v = {"ref": "VEH1", "paint_code": "LA7W", "paint_name": "Reflex Silver"}
    assert paint_audit.audit_vehicle(v, _csv("", "Silver")) is None
    clash = paint_audit.audit_vehicle(
        {"ref": "VEH5", "paint_code": "LC9X", "paint_name": "Deep Black"}, _csv("", "Silver"))
    assert clash["proposed_paint_code"] == "" and clash["action"].startswith("check by hand")


def test_audit_keeps_name_when_csv_only_misspells_it():
    v = {"ref": "VEH65", "paint_code": "LA7W", "paint_name": "Reflex Silver"}
    line = paint_audit.audit_vehicle(v, _csv("LA7W", "Relfex Silver"))
    assert line["action"] == "keep (spelling difference only)"
    assert line["proposed_paint_name"] == ""
    assert plan_fixes([line]) == ([], [line])


def test_audit_real_name_difference_same_family_is_updated():
    v = {"ref": "VEH104", "paint_code": "LZ7G", "paint_name": "Reflex Silver"}
    line = paint_audit.audit_vehicle(v, _csv("LZ7G", "Floret Silver"))
    assert line["action"] == "update from CSV"


# ── 6. Cloned BreakerPro rows (VEH120 was a copy of VEH89's Jetta row) ──

def _clone_csv(tmp_path):
    jetta = ("VH-{id},Volkswagen,Jetta,{ref},2.0 TDI BKD,Saloon 4 Door,2008,LC5F Blue Graphite,"
             "AB08HDL,WVWZZZ1KZ8M150209,153148,DIESEL,,12/11/25,Global,VH-{id}")
    return _write_breakerpro(tmp_path / "2026-10-12_stock.csv", [
        _row("VEH88", "Golf Mk7", "LA7W Reflex Silver", "VIN88", reg="AA11AAA"),
        jetta.format(id="121125", ref="veh89"),
        jetta.format(id="160426", ref="veh120"),
        _row("VEH121", "Polo 6r", "LC9X Deep Black", "VIN121", reg="BB22BBB"),
    ])


def test_shared_identity_refs_finds_cloned_rows(tmp_path):
    shared = ib.shared_identity_refs(ib.read_breakerpro_rows(_clone_csv(tmp_path)))
    assert shared == {"VEH89": {"VEH120"}, "VEH120": {"VEH89"}}


def test_row_identity_problem():
    polo = {"make": "VOLKSWAGEN", "model": "Polo 6R"}
    assert "Jetta" in ib.row_identity_problem(polo, {"Make": "Volkswagen", "Model": "Jetta"})
    assert "VEH89" in ib.row_identity_problem(polo, {"Make": "Volkswagen", "Model": "Polo 6r"}, {"VEH89"})
    golf = {"make": "VOLKSWAGEN", "model": "Golf Mk7"}
    assert ib.row_identity_problem(golf, {"Make": "Volkswagen", "Model": "Golf Mk7 Dsg"}) is None
    assert ib.row_identity_problem({"make": "", "model": None}, {"Make": "VW", "Model": "Up"}) is None


def test_backfill_holds_row_from_another_vehicle(monkeypatch):
    """VEH120 (Polo) got the Jetta's VIN and year from the cloned row."""
    monkeypatch.setattr(ib, "get_vehicle", lambda ref: {
        "ref": ref, "make": "VOLKSWAGEN", "model": "Polo 6R", "year_range": None,
        "vin": None, "engine_code": "CGP", "paint_code": None, "paint_name": None})
    monkeypatch.setattr(ib, "patch_vehicle", lambda *a: pytest.fail("must not write"))
    status, msg = ib.backfill_vehicle(
        "VEH120", {"year_range": "2008", "vin": "WVWZZZ1KZ8M150209", "engine_code": "BKD",
                   "paint_code": "LC5F", "paint_name": "Blue Graphite"},
        dry_run=False, row={"Make": "Volkswagen", "Model": "Jetta"}, others={"VEH89"})
    assert status == "held" and "another vehicle" in msg


def test_importer_dry_run_reports_held_rows(tmp_path, monkeypatch, capsys):
    records = {
        "VEH88": {"make": "VOLKSWAGEN", "model": "Golf Mk7", "paint_code": "LA7W",
                  "paint_name": "Reflex Silver", "year_range": "2014", "vin": "VIN88", "engine_code": "CGP",
                      "arrived_at": "2026-08-29"},
        "VEH89": {"make": "VOLKSWAGEN", "model": "Jetta", "paint_code": "LC5F",
                  "paint_name": "Blue Graphite", "year_range": "2008",
                  "vin": "WVWZZZ1KZ8M150209", "engine_code": "BKD"},
        "VEH120": {"make": "VOLKSWAGEN", "model": "Polo 6R", "paint_code": "LD5Q",
                   "paint_name": "Atlantic Blue", "year_range": None, "vin": None, "engine_code": "CGP"},
        "VEH121": {"make": "VOLKSWAGEN", "model": "Polo 6r", "paint_code": "LC9X",
                   "paint_name": "Deep Black", "year_range": "2014", "vin": "VIN121", "engine_code": "CGP",
                       "arrived_at": "2026-08-29"},
    }
    monkeypatch.setattr(ib, "get_vehicle", lambda ref: records.get(ref.upper()))
    monkeypatch.setattr(ib, "patch_vehicle", lambda *a: pytest.fail("dry run must not write"))
    monkeypatch.setattr(ib.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["x", str(_clone_csv(tmp_path)), "--fill-missing", "--dry-run"])
    with pytest.raises(SystemExit) as e:
        ib.main()
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "Held (row looks wrong): 2" in out
    assert "HOLD veh120" in out and "HOLD veh89" in out
    assert "would fill" not in out          # VEH120's blank VIN/year are NOT filled from the Jetta


def test_audit_veh120_uses_confirmed_paint_not_cloned_row(tmp_path):
    idx = paint_audit.load_csv_paint([_clone_csv(tmp_path)])
    assert idx["VEH120"]["others"] == ["VEH89"] and idx["VEH120"]["code"] == "LC5F"
    v = {"ref": "VEH120", "make": "VOLKSWAGEN", "model": "Polo 6R",
         "paint_code": "LD5Q", "paint_name": "Atlantic Blue"}
    line = paint_audit.audit_vehicle(v, idx["VEH120"])
    assert (line["proposed_paint_code"], line["proposed_paint_name"]) == ("LD5Q", "Shadow Blue")
    assert line["action"] == "update (confirmed by hand)"
    assert "another vehicle" in line["issues"]
    fixes, _ = plan_fixes([line])
    assert fixes[0]["patch"] == {"paint_code": "LD5Q", "paint_name": "Shadow Blue"}
    done = dict(v, paint_name="Shadow Blue")
    assert paint_audit.audit_vehicle(done, idx["VEH120"]) is None


def test_audit_cloned_row_without_confirmation_is_never_used(tmp_path):
    idx = paint_audit.load_csv_paint([_clone_csv(tmp_path)])
    v = {"ref": "VEH89", "make": "VOLKSWAGEN", "model": "Jetta",
         "paint_code": "LB9A", "paint_name": "Candy White"}
    line = paint_audit.audit_vehicle(v, idx["VEH89"])
    assert line["action"].startswith("check by hand") and line["proposed_paint_code"] == ""
    assert plan_fixes([line])[0] == []


def test_ld5q_is_shadow_blue():
    assert lookup_paint_name("LD5Q") == "Shadow Blue"
    assert paint_family_conflict("LD5Q", "Shadow Blue") is None
