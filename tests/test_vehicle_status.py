"""Vehicle yard status: migration, transitions, the bot's availability
endpoint, the BreakerPro On-site Date mapping and the logger page's status
change, end to end."""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time

import pytest

import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
NODE = shutil.which("node")
KEY = "test-bot-key-0123456789"


@pytest.fixture
def bot_key(monkeypatch):
    monkeypatch.setenv("BOT_API_KEY", KEY)
    return KEY


def _add(client, ref="VEH113", **extra):
    body = {"ref": ref, "make": "VOLKSWAGEN", "model": "Golf GT TDI",
            "year_range": "2016", "engine_code": "CRB", "paint_code": "LC9X",
            "paint_name": "Deep Black Pearl", "vin": "WVWZZZAUZGW000001"}
    body.update(extra)
    r = client.post("/vehicles", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _status(client, ref, **body):
    return client.patch(f"/vehicles/{ref}/status", json=body)


# ── Migration ───────────────────────────────────────────────────────────

OLD_SCHEMA = """
CREATE TABLE vehicles (
    ref TEXT PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL,
    year_range TEXT, paint_code TEXT, paint_name TEXT, engine_code TEXT,
    transmission TEXT, vin TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
)"""


def _old_db(path, with_is_active=True):
    schema = OLD_SCHEMA if with_is_active else OLD_SCHEMA.replace(
        ",\n    is_active INTEGER NOT NULL DEFAULT 1", "")
    c = sqlite3.connect(path)
    c.execute(schema)
    rows = [("VEH113", "AUDI", "A1", "2014", "LY9T", "Mythos Black", "CAY", "Manual",
             "WAUZZZ8X2EB115629", "hatch", "2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            ("VEH115", "SEAT", "Leon", None, None, None, None, None, None, None,
             "2026-03-01T00:00:00", "2026-03-01T00:00:00")]
    cols = "ref, make, model, year_range, paint_code, paint_name, engine_code, transmission, vin, notes, created_at, updated_at"
    c.executemany(f"INSERT INTO vehicles ({cols}) VALUES ({', '.join('?' * 12)})", rows)
    if with_is_active:
        c.execute("UPDATE vehicles SET is_active = 0 WHERE ref = 'VEH115'")
    c.commit()
    before = {r[0]: r for r in c.execute(f"SELECT {cols} FROM vehicles")}
    c.close()
    return cols, before


@pytest.mark.parametrize("with_is_active", [True, False])
def test_migration_keeps_every_existing_value_and_sets_unknown(tmp_path, monkeypatch, with_is_active):
    path = str(tmp_path / "old.db")
    cols, before = _old_db(path, with_is_active)
    monkeypatch.setenv("DB_PATH", path)
    db.init_db()
    db.init_db()  # idempotent: a second boot changes nothing

    c = sqlite3.connect(path)
    after = {r[0]: r for r in c.execute(f"SELECT {cols} FROM vehicles")}
    assert after == before
    new = c.execute("SELECT ref, status, status_changed_at, status_changed_by, damage_notes, arrived_at, is_active "
                    "FROM vehicles ORDER BY ref").fetchall()
    assert [r[:6] for r in new] == [("VEH113", "unknown", None, None, None, None),
                                    ("VEH115", "unknown", None, None, None, None)]
    assert [r[6] for r in new] == ([1, 0] if with_is_active else [1, 1])
    c.close()


def test_new_vehicle_starts_unknown(client):
    v = _add(client)
    assert v["status"] == "unknown"
    assert v["status_changed_at"] is None
    assert client.get("/vehicles").json()[0]["status"] == "unknown"


# ── Status transitions ──────────────────────────────────────────────────

STATUSES = ["on_yard", "stripped", "crushed", "unknown"]


@pytest.mark.parametrize("start", STATUSES)
@pytest.mark.parametrize("end", STATUSES)
def test_every_transition(client, start, end):
    _add(client)
    first = _status(client, "VEH113", status=start)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == start
    time.sleep(0.002)
    r = _status(client, "VEH113", status=end)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["status"] == end
    if start == end:
        # Re-tapping the same status keeps "since when".
        assert v["status_changed_at"] == first.json()["status_changed_at"]
    else:
        assert v["status_changed_at"] > (first.json()["status_changed_at"] or "")
    # Nothing else about the car moves.
    assert (v["make"], v["paint_code"], v["paint_name"], v["vin"], v["engine_code"]) == (
        "VOLKSWAGEN", "LC9X", "Deep Black Pearl", "WVWZZZAUZGW000001", "CRB")
    assert client.get("/vehicles/VEH113").json()["status"] == end


def test_lifecycle_stamps_time_who_and_damage(client):
    _add(client)
    r = _status(client, "veh113", status="on_yard", damage_notes="front-end hit, passenger side damaged",
                changed_by="Yard")
    v = r.json()
    assert (v["status"], v["damage_notes"], v["status_changed_by"]) == (
        "on_yard", "front-end hit, passenger side damaged", "Yard")
    assert v["status_changed_at"]

    # damage_notes not sent → kept
    v = _status(client, "VEH113", status="stripped").json()
    assert v["damage_notes"] == "front-end hit, passenger side damaged"
    assert v["status_changed_by"] is None

    # damage_notes sent empty → cleared
    v = _status(client, "VEH113", status="crushed", damage_notes="").json()
    assert v["status"] == "crushed" and v["damage_notes"] is None


def test_bad_status_and_unknown_ref(client):
    _add(client)
    assert _status(client, "VEH113", status="sold").status_code == 422
    assert _status(client, "VEH113").status_code == 422
    r = _status(client, "VEH999", status="crushed")
    assert r.status_code == 404 and r.json() == {"error": "vehicle not found"}
    assert client.get("/vehicles/VEH113").json()["status"] == "unknown"


def test_damage_notes_and_arrival_via_vehicle_patch(client):
    _add(client, arrived_at="2025-04-09", damage_notes="rear hit")
    v = client.get("/vehicles/VEH113").json()
    assert (v["arrived_at"], v["damage_notes"]) == ("2025-04-09", "rear hit")
    v = client.patch("/vehicles/VEH113", json={"damage_notes": None, "arrived_at": "2025-05-01"}).json()
    assert (v["arrived_at"], v["damage_notes"], v["status"]) == ("2025-05-01", None, "unknown")
    assert client.patch("/vehicles/VEH113", json={"arrived_at": "09/04/25"}).status_code == 422
    # A plain PATCH can't move the status (it has to go through /status).
    assert client.patch("/vehicles/VEH113", json={"status": "crushed"}).json()["status"] == "unknown"


# ── Availability (bot) endpoint ─────────────────────────────────────────

def test_availability_valid_ref(client, bot_key):
    _add(client)
    _status(client, "VEH113", status="stripped", damage_notes="front-end hit")
    r = client.get("/vehicles/veh113/availability", headers={"X-API-Key": bot_key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ref", "make", "model", "year", "engine_code", "paint_code",
                         "status", "status_changed_at", "damage_notes"}
    assert {k: v for k, v in body.items() if k != "status_changed_at"} == {
        "ref": "VEH113", "make": "VOLKSWAGEN", "model": "Golf GT TDI", "year": "2016",
        "engine_code": "CRB", "paint_code": "LC9X", "status": "stripped",
        "damage_notes": "front-end hit"}
    assert body["status_changed_at"]


def test_availability_unknown_ref_is_404(client, bot_key):
    r = client.get("/vehicles/VEH999/availability", headers={"X-API-Key": bot_key})
    assert r.status_code == 404
    assert r.json() == {"error": "no vehicle with ref 'VEH999'"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong"},
                                     {"X-API-Key": KEY + "x"}, {"Authorization": f"Bearer {KEY}"}])
def test_availability_rejects_missing_or_wrong_key(client, bot_key, headers):
    _add(client)
    r = client.get("/vehicles/VEH113/availability", headers=headers)
    assert r.status_code == 401
    assert "API key" in r.json()["error"]
    assert "VEH113" not in r.text  # nothing about the car leaks


def test_availability_fails_closed_without_configured_key(client, monkeypatch):
    monkeypatch.delenv("BOT_API_KEY", raising=False)
    _add(client)
    r = client.get("/vehicles/VEH113/availability", headers={"X-API-Key": ""})
    assert r.status_code == 503


def test_availability_still_answers_for_soft_deleted(client, bot_key):
    _add(client)
    _status(client, "VEH113", status="crushed")
    client.delete("/vehicles/VEH113")
    r = client.get("/vehicles/VEH113/availability", headers={"X-API-Key": bot_key})
    assert r.status_code == 200 and r.json()["status"] == "crushed"


def test_existing_endpoints_need_no_key(client, bot_key):
    _add(client)
    assert client.get("/vehicles").status_code == 200
    assert client.get("/vehicles/VEH113").status_code == 200
    assert client.patch("/vehicles/VEH113", json={"notes": "x"}).status_code == 200
    assert _status(client, "VEH113", status="on_yard").status_code == 200


# ── BreakerPro import: On-site Date → arrived_at ───────────────────────

def test_onsite_date_parsing():
    from scripts.import_breakerpro import parse_onsite_date
    assert parse_onsite_date("09/04/25") == "2025-04-09"
    assert parse_onsite_date(" 30/04/26 ") == "2026-04-30"
    assert parse_onsite_date("09/04/2025") == "2025-04-09"
    assert parse_onsite_date("") is None
    assert parse_onsite_date(None) is None
    assert parse_onsite_date("31/02/25") is None


def test_backfill_only_fills_blank_arrival():
    from scripts.import_breakerpro import plan_backfill
    fill, _ = plan_backfill({"arrived_at": None}, {"arrived_at": "2025-04-09"})
    assert fill == {"arrived_at": "2025-04-09"}
    fill, _ = plan_backfill({"arrived_at": "2025-01-01"}, {"arrived_at": "2025-04-09"})
    assert fill == {}
    # Status is never something the import writes.
    fill, _ = plan_backfill({"status": "unknown"}, {"status": "crushed"})
    assert "status" not in fill


# ── Frontend: status change round trip ─────────────────────────────────

def _src() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def _status_section() -> str:
    s = _src()
    a = s.index("// ── Vehicle status")
    return s[a:s.index("// ── Vehicles overlay", a)]


@pytest.fixture
def live_server(tmp_db):
    """The real app on 127.0.0.1, so the page's own fetch code can talk to it."""
    import uvicorn
    import main

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)


def _node(tmp_path, body: str) -> dict:
    assert NODE, "Node.js is required for the logger page checks"
    script = tmp_path / "status.js"
    script.write_text(_status_section() + "\n" + body, encoding="utf-8")
    res = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_page_status_change_round_trip(tmp_path, live_server, bot_key):
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        _add(c)
        _add(c, ref="VEH115", make="SEAT", model="Leon")
    out = _node(tmp_path, f"""
const window = {{}};
const cfg = {{ url: {json.dumps(live_server)} }};
(async () => {{
  window.PA_VEHICLES = await (await fetch(cfg.url + '/vehicles')).json();
  const before = vehStatusBadge(window.PA_VEHICLES.find(v => v.ref === 'VEH113'));
  const saved = await setVehicleStatus('VEH113', 'stripped', {{ damage_notes: 'front-end hit' }});
  const cached = window.PA_VEHICLES.find(v => v.ref === 'VEH113');
  let error = null;
  try {{ await setVehicleStatus('VEH999', 'crushed'); }} catch (e) {{ error = e.message; }}
  console.log(JSON.stringify({{
    before, after: vehStatusBadge(cached), saved: saved.status, cachedDamage: cached.damage_notes,
    buttons: vehStatusButtons(cached), yard: yardVehicles(window.PA_VEHICLES).map(v => v.ref), error,
  }}));
}})();
""")
    assert out["before"] == '<span class="vs-badge vs-unknown">Unknown</span>'
    assert out["after"] == '<span class="vs-badge vs-stripped">Stripped</span>'
    assert out["saved"] == "stripped" and out["cachedDamage"] == "front-end hit"
    assert 'class="vs-btn vs-stripped on" data-status="stripped" aria-pressed="true"' in out["buttons"]
    assert 'data-status="on_yard" aria-pressed="false"' in out["buttons"]
    assert out["yard"] == ["VEH115"]  # stripped car is off the yard list
    assert out["error"] == "vehicle not found"

    import urllib.request
    req = urllib.request.Request(live_server + "/vehicles/VEH113/availability", headers={"X-API-Key": bot_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        avail = json.loads(resp.read())
    assert (avail["status"], avail["damage_notes"]) == ("stripped", "front-end hit")


def test_yard_view_lists_on_yard_and_unknown_newest_first(tmp_path):
    out = _node(tmp_path, """
const cars = [
  { ref: 'A', status: 'on_yard',  arrived_at: '2025-01-01', created_at: '2026-10-01' },
  { ref: 'B', status: 'unknown',  arrived_at: '2026-04-30', created_at: '2026-09-01' },
  { ref: 'C', status: 'stripped', arrived_at: '2026-05-01', created_at: '2026-09-01' },
  { ref: 'D', status: 'crushed',  arrived_at: '2026-06-01', created_at: '2026-09-01' },
  { ref: 'E', status: 'on_yard',  arrived_at: null,         created_at: '2026-10-19' },
  { ref: 'F',                     arrived_at: null,         created_at: '2026-10-20' },
];
console.log(JSON.stringify({ yard: yardVehicles(cars).map(v => v.ref), cars: cars.map(v => v.ref),
  odd: vehStatusBadge({ status: '<b>' }) }));
""")
    assert out["yard"] == ["B", "A", "F", "E"]
    assert out["cars"] == ["A", "B", "C", "D", "E", "F"]  # input not reordered
    assert out["odd"] == '<span class="vs-badge vs-unknown">Unknown</span>'


def test_page_has_status_controls():
    s = _src()
    for needle in ('class="veh-tab" data-view="yard"', 'id="veh-status-btns"',
                   'id="veh-damage-notes"', 'id="veh-arrived-at"',
                   "damage_notes:  document.getElementById('veh-damage-notes')",
                   ".vs-on_yard", ".vs-stripped", ".vs-crushed", ".vs-unknown"):
        assert needle in s, needle
