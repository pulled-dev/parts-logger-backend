"""Part names on the Railway volume: one-time seed, corrections that always
win and survive a redeploy, the AI side guard, and the export log.

A tiny seed file stands in for vag_parts_db.json; no network (Claude and eBay
are stubbed)."""

import json

import pytest

import part_names
import vag_lookup

SEED = {
    "_meta": {"last_updated": "2026-03-07"},
    "exact": {
        "5Q0959802A": {"description": "WINDOW MOTOR", "breakerpro_price": 30.0, "vehicle": "Golf"},
        "1K0941431BB": {"description": "HEADLIGHT SWITCH", "breakerpro_price": 12.0, "vehicle": "Golf"},
    },
    "groups": {"941005": {"description": "HEADLIGHT", "avg_price": 80.0}},
    "learned": {},
}


def write_seed(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def names(tmp_db, tmp_path, monkeypatch):
    """Fresh part-name store seeded from SEED (not the 3,600-entry git file)."""
    monkeypatch.setattr(part_names, "SEED_PATH", write_seed(tmp_path / "seed.json", SEED))
    monkeypatch.setattr(vag_lookup, "_db", None)
    vag_lookup.reload_db()
    return tmp_path


def restart(monkeypatch, tmp_path, git_copy):
    """A Railway redeploy: new process (empty memory), a fresh git copy of
    vag_parts_db.json on disk, same volume."""
    monkeypatch.setattr(part_names, "SEED_PATH", write_seed(tmp_path / "seed-redeploy.json", git_copy))
    monkeypatch.setattr(vag_lookup, "_db", None)
    monkeypatch.setattr(vag_lookup, "_db_path", None)


@pytest.fixture
def api(names, monkeypatch):
    """main.app with real (non-mock) lookups; Claude and eBay stubbed."""
    from fastapi.testclient import TestClient

    import main

    claude = {"answer": None}

    async def fake_claude(_pn):
        return claude["answer"]

    async def no_ebay(_client, _pn, _desc):
        return main._NO_PRICING.copy(), ""

    monkeypatch.setattr(main, "USE_MOCK", False)
    monkeypatch.setattr(main, "identify_with_claude", fake_claude)
    monkeypatch.setattr(main, "_ebay_pricing", no_ebay)
    with TestClient(main.app) as c:
        c.claude = claude
        yield c


# ── One-time seed + redeploy ───────────────────────────────────────────

def test_first_boot_seeds_from_git_copy(names, conn):
    assert conn.execute("SELECT COUNT(*) FROM part_names WHERE section = 'exact'").fetchone()[0] == 2
    assert part_names.meta(conn)["seeded_at"]
    assert vag_lookup.lookup_part("5Q0 959 802 A")["description"] == "WINDOW MOTOR"


def test_redeploy_keeps_corrections_and_learned(names, monkeypatch, conn):
    vag_lookup.save_correction("5Q0959802A", "Window Motor Front OS")
    vag_lookup.save_learned("3C0000001", "Boot Release Handle")

    # The new deploy ships a git copy that knows nothing of either, and even
    # disagrees about an exact entry.
    git_copy = json.loads(json.dumps(SEED))
    git_copy["exact"]["5Q0959802A"]["description"] = "SOMETHING ELSE"
    git_copy["exact"]["9Z9999999"] = {"description": "NEW IN GIT"}
    restart(monkeypatch, names, git_copy)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:  # runs the startup hook, like a real boot
        stats = c.get("/db-stats").json()

    hit = vag_lookup.lookup_part("5Q0959802A")
    assert hit["description"] == "Window Motor Front OS" and hit["corrected"] is True
    assert hit["ai_description"] == "WINDOW MOTOR"           # stored copy, not the new git text
    assert vag_lookup.lookup_part("3C0000001")["description"] == "Boot Release Handle"
    assert vag_lookup.lookup_part("9Z9999999") is None       # git file not re-read
    assert stats["corrected_entries"] == 1 and stats["learned_entries"] == 1
    assert stats["storage"] == "sqlite"
    assert conn.execute("SELECT COUNT(*) FROM part_names_meta WHERE key = 'seeded_at'").fetchone()[0] == 1


def test_old_style_corrections_in_seed_become_corrections(tmp_db, tmp_path, monkeypatch):
    seed = json.loads(json.dumps(SEED))
    seed["exact"]["5Q0959802A"].update(corrected=True, description="Window Motor OSF")
    monkeypatch.setattr(part_names, "SEED_PATH", write_seed(tmp_path / "s.json", seed))
    monkeypatch.setattr(vag_lookup, "_db", None)
    assert vag_lookup.lookup_part("5Q0959802A")["corrected"] is True


# ── Corrections ────────────────────────────────────────────────────────

def test_stored_correction_auto_applied_to_repeat_part_number(api):
    first = api.post("/lookup", json={"part_number": "1K0941431BB"}).json()
    assert first["description"] == "HEADLIGHT SWITCH" and first["corrected"] is False

    r = api.post("/db-correct", json={"part_number": "1K0 941 431 BB",
                                      "corrected_description": "Headlight Switch With Fog",
                                      "ai_description": "HEADLIGHT SWITCH"}).json()
    assert r["status"] == "saved" and r["ai_description"] == "HEADLIGHT SWITCH"

    repeat = api.post("/lookup", json={"part_number": "1k0941431bb"}).json()
    assert repeat["description"] == "Headlight Switch With Fog"
    assert repeat["corrected"] is True and repeat["source"] == "corrected"
    assert repeat["ai_description"] == "HEADLIGHT SWITCH"


def test_latest_correction_wins_and_ai_value_kept(names, conn):
    vag_lookup.save_correction("5Q0959802A", "Window Motor A")
    vag_lookup.save_correction("5Q0959802A", "Window Motor B")
    hit = vag_lookup.lookup_part("5Q0959802A")
    assert hit["description"] == "Window Motor B" and hit["ai_description"] == "WINDOW MOTOR"
    row = conn.execute("SELECT * FROM part_corrections").fetchone()
    assert row["times_corrected"] == 2 and row["ai_description"] == "WINDOW MOTOR"
    log = [r[0] for r in conn.execute("SELECT description FROM part_correction_log ORDER BY id")]
    assert log == ["Window Motor A", "Window Motor B"]


def test_correcting_back_to_ai_value_removes_correction(names):
    vag_lookup.save_correction("5Q0959802A", "Window Motor A")
    assert vag_lookup.save_correction("5Q0959802A", "WINDOW MOTOR") is None
    assert vag_lookup.lookup_part("5Q0959802A")["corrected"] is False


def test_correction_for_a_part_the_ai_named(api):
    api.claude["answer"] = "Boot Lock Actuator"
    assert api.post("/lookup", json={"part_number": "5G6827505"}).json()["description"] == "Boot Lock Actuator"
    api.post("/db-correct", json={"part_number": "5G6827505", "corrected_description": "Tailgate Lock"})
    api.claude["answer"] = "SHOULD NOT BE ASKED"
    hit = api.post("/lookup", json={"part_number": "5G6827505"}).json()
    assert hit["description"] == "Tailgate Lock" and hit["ai_description"] == "Boot Lock Actuator"


def test_pricing_engine_uses_the_corrected_name(names, conn):
    from pricing.engine import resolve_part_type
    vag_lookup.save_correction("1K0941431BB", "Wiper Motor")
    assert resolve_part_type(conn, "1K0941431BB", None) == "WIPER MOTOR"


def test_empty_correction_rejected(api):
    assert api.post("/db-correct", json={"part_number": "5Q0959802A", "corrected_description": "  "}).status_code == 400


# ── Side guard (part_names_config.py) ──────────────────────────────────

@pytest.mark.parametrize("ai, expected", [
    ("Headlight Switch Passenger Side", "Headlight Switch"),
    ("Wiper Motor N/S", "Wiper Motor"),
    ("Heater Control Panel - Driver Side", "Heater Control Panel"),
    ("Seat Heating Switch Left", "Seat Heating Switch"),
    ("ABS Pump O/S", "ABS Pump"),
    ("Interior Mirror Right", "Interior Mirror"),
    ("Headlight Passenger Side", "Headlight Passenger Side"),
    ("Door Mirror O/S", "Door Mirror O/S"),
    ("Tail Light Left", "Tail Light Left"),
    ("Front Doors Right", "Front Doors Right"),
    ("Window Regulator Driver Side", "Window Regulator Driver Side"),
    ("Driveshaft N/S", "Driveshaft N/S"),
    ("Rear Hub Right", "Rear Hub Right"),
    ("Seat Belt OSF", "Seat Belt OSF"),
    ("Wing NSF", "Wing NSF"),
])
def test_side_guard(ai, expected):
    assert part_names.guard_ai_side(ai) == expected


def test_side_stripped_from_non_sided_ai_part(api, conn):
    api.claude["answer"] = "Headlight Switch Passenger Side"
    hit = api.post("/lookup", json={"part_number": "6R0941531F"}).json()
    assert hit["description"] == "Headlight Switch" and hit["ai_description"] == "Headlight Switch"
    learned = json.loads(conn.execute("SELECT data FROM part_names WHERE key = '6R0941531F'").fetchone()[0])
    assert learned["description"] == "Headlight Switch"
    assert learned["ai_raw_description"] == "Headlight Switch Passenger Side"  # kept for audit
    assert api.post("/lookup", json={"part_number": "6R0941531F"}).json()["description"] == "Headlight Switch"


def test_side_kept_on_sided_ai_part(api):
    api.claude["answer"] = "Headlight Passenger Side"
    assert api.post("/lookup", json={"part_number": "6R1941015B"}).json()["description"] == "Headlight Passenger Side"


def test_side_guard_never_touches_a_manual_correction(names):
    vag_lookup.save_correction("1K0941431BB", "Headlight Switch Passenger Side")
    assert vag_lookup.lookup_part("1K0941431BB")["description"] == "Headlight Switch Passenger Side"


def test_old_learned_entries_get_the_side_guard(names):
    vag_lookup.save_learned("6R0959565", "Mirror Adjust Switch N/S")
    assert vag_lookup.lookup_part("6R0959565")["description"] == "Mirror Adjust Switch"


# ── Export log + edit report ───────────────────────────────────────────

def export(client, export_id, at, rows):
    return client.post("/parts/exports", json={"export_id": export_id, "exported_at": at, "rows": rows})


def test_export_recorded_and_retry_is_idempotent(api, conn):
    rows = [{"part_number": "1K0941431BB", "ai_description": "HEADLIGHT SWITCH",
             "final_description": "Headlight Switch With Fog", "edited": True, "vehicle_ref": "VEH101"},
            {"part_number": "", "ai_description": None, "final_description": "OSF Door — dent",
             "edited": False, "vehicle_ref": "VEH101"}]
    first = export(api, "e1", "2026-10-15T09:00:00Z", rows).json()
    again = export(api, "e1", "2026-10-15T09:00:00Z", rows).json()
    assert first["inserted"] == 2 and again["inserted"] == 0 and again["already_had"] == 2
    got = conn.execute("SELECT part_number_norm, final_description, edited FROM export_log ORDER BY row_index").fetchall()
    assert [tuple(r) for r in got] == [("1K0941431BB", "Headlight Switch With Fog", 1), ("", "OSF Door — dent", 0)]


def test_edit_report_finds_exports_that_went_out_with_the_ai_name(api):
    api.post("/db-correct", json={"part_number": "1K0941431BB", "corrected_description": "Headlight Switch With Fog"})
    export(api, "late", "2999-01-01T00:00:00Z", [  # after the correction, but still the AI name: affected
        {"part_number": "1K0941431BB", "ai_description": "HEADLIGHT SWITCH",
         "final_description": "HEADLIGHT SWITCH", "vehicle_ref": "VEH1"},
        {"part_number": "5Q0959802A", "ai_description": "WINDOW MOTOR",  # never corrected: fine
         "final_description": "WINDOW MOTOR", "vehicle_ref": "VEH1"}])
    export(api, "good", "2999-01-02T00:00:00Z", [
        {"part_number": "1K0941431BB", "ai_description": "HEADLIGHT SWITCH",
         "final_description": "Headlight Switch With Fog", "edited": True, "vehicle_ref": "VEH2"}])
    export(api, "early", "2000-01-01T00:00:00Z", [  # before any correction existed: not counted
        {"part_number": "1K0941431BB", "ai_description": "HEADLIGHT SWITCH",
         "final_description": "HEADLIGHT SWITCH", "vehicle_ref": "VEH3"}])
    rep = api.get("/parts/edit-report").json()
    assert rep["counts"]["affected"] == 1 and rep["counts"]["export_rows_with_earlier_correction"] == 2
    assert rep["counts"]["corrections_stored"] == 1 and rep["counts"]["exports"] == 3
    (s,) = rep["sample"]
    assert s["vehicle_ref"] == "VEH1" and s["exported_description"] == "HEADLIGHT SWITCH"
    assert s["corrected_description"] == "Headlight Switch With Fog"


def test_edit_report_sample_capped_at_20(api):
    api.post("/db-correct", json={"part_number": "1K0941431BB", "corrected_description": "X"})
    rows = [{"part_number": "1K0941431BB", "ai_description": "HEADLIGHT SWITCH",
             "final_description": "HEADLIGHT SWITCH"}] * 25
    export(api, "many", "2999-01-01T00:00:00Z", rows)
    rep = api.get("/parts/edit-report").json()
    assert rep["counts"]["affected"] == 25 and len(rep["sample"]) == 20
