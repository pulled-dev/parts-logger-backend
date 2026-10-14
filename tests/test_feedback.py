import csv
import io

from pricing import feedback
from pricing.sales_import import import_rows

_n = iter(range(1, 10_000))


def sale(conn, title, price, day):
    i = next(_n)
    import_rows(conn, [{"order_number": f"F-{i}", "sales_record_number": str(i), "item_number": str(i),
                        "title": title, "custom_label": "", "quantity": "1", "sold_for": str(price),
                        "postage": "0", "total_price": "", "sale_date": day, "dispatched_date": ""}], "csv_clean")


def suggestion(conn, pn, suggested, conf="high", ts="2026-09-01T10:00:00+00:00", part_type="WINDOW MOTOR FRONT",
               mode="live"):
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, part_type, suggested, confidence, source, reason,"
        " mode) VALUES (?, ?, ?, ?, ?, 'own_sold', 'r', ?)", (ts, pn, part_type, suggested, conf, mode))
    conn.commit()
    return cur.lastrowid


def test_suggest_endpoint_logs_and_returns_id(client, conn):
    r = client.get("/price/suggest", params={"part_number": "5Q0959802A", "vehicle_ref": "123"}).json()
    sid = r["suggestion_id"]
    row = conn.execute("SELECT * FROM price_suggestions WHERE id=?", (sid,)).fetchone()
    assert row["part_number_norm"] == "5Q0959802A" and row["vehicle_ref"] == "VEH123"
    assert row["suggested"] == r["suggested_price"] and row["confidence"] == r["confidence"]
    assert row["final_price"] is None and row["overridden"] is None


def test_suggest_endpoint_log_false(client, conn):
    r = client.get("/price/suggest", params={"part_number": "5Q0959802A", "log": "false"}).json()
    assert r["suggestion_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM price_suggestions").fetchone()[0] == 0


def test_record_finals_sets_overridden(client, conn):
    a = suggestion(conn, "5Q0959802A", 29.99)
    b = suggestion(conn, "5Q0959802A", 29.99)
    r = client.post("/price/suggestions/final", json={"items": [
        {"suggestion_id": a, "final_price": 29.99},
        {"suggestion_id": b, "final_price": 34.99},
        {"suggestion_id": 99999, "final_price": 1},
    ]}).json()
    assert r == {"updated": 2, "unknown": 1}
    rows = {x["id"]: x for x in conn.execute("SELECT * FROM price_suggestions")}
    assert rows[a]["overridden"] == 0 and rows[b]["overridden"] == 1 and rows[b]["final_price"] == 34.99


def test_link_sales_one_to_one_and_not_before_suggestion(conn):
    s1 = suggestion(conn, "5Q0959802A", 30, ts="2026-09-01T10:00:00+00:00")
    s2 = suggestion(conn, "5Q0959802A", 30, ts="2026-09-02T10:00:00+00:00")
    sale(conn, "Golf Mk7 WINDOW MOTOR 5Q0 959 802 A", 25, "2026-08-20")  # before: never linked
    sale(conn, "Golf Mk7 WINDOW MOTOR 5Q0 959 802 A", 33, "2026-09-11")
    assert feedback.link_sales(conn) == 1
    r1 = conn.execute("SELECT * FROM price_suggestions WHERE id=?", (s1,)).fetchone()
    assert r1["sold_price"] == 33 and r1["days_to_sell"] == 10
    assert conn.execute("SELECT sold_history_id FROM price_suggestions WHERE id=?", (s2,)).fetchone()[0] is None
    sale(conn, "Golf Mk7 WINDOW MOTOR 5Q0 959 802 A", 31, "2026-09-20")
    assert feedback.link_sales(conn) == 1
    assert conn.execute("SELECT sold_price FROM price_suggestions WHERE id=?", (s2,)).fetchone()[0] == 31


def test_import_endpoint_links_suggestions(client, conn, monkeypatch):
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "t")
    suggestion(conn, "5Q0959802A", 30, ts="2026-09-01T10:00:00+00:00")
    row = {"order_number": "X1", "sales_record_number": "1", "item_number": "9", "custom_label": "",
           "title": "Golf Mk7 WINDOW MOTOR 5Q0 959 802 A", "quantity": "1", "sold_for": "£30.00",
           "postage": "£0.00", "total_price": "£30.00", "sale_date": "05-Sep-26", "dispatched_date": ""}
    r = client.post("/price/sold-history/import", json={"rows": [row]}, headers={"X-Admin-Token": "t"}).json()
    assert r["suggestions_linked"] == 1


def test_report_accuracy_and_overrides(client, conn):
    ids = [suggestion(conn, "5Q0959802A", 30), suggestion(conn, "5Q0959802A", 30),
           suggestion(conn, "1K0959653D", 20, conf="low", part_type="SLIP RING")]
    feedback.record_finals(conn, [{"suggestion_id": ids[0], "final_price": 30},
                                  {"suggestion_id": ids[1], "final_price": 40},
                                  {"suggestion_id": ids[2], "final_price": 25}])
    sale(conn, "WINDOW MOTOR 5Q0 959 802 A", 31, "2026-09-05")   # 3.2% off -> within 15%
    sale(conn, "WINDOW MOTOR 5Q0 959 802 A", 40, "2026-09-06")   # 25% off
    sale(conn, "SLIP RING 1K0 959 653 D", 25, "2026-09-10")      # 20% off
    rep = client.get("/price/report").json()
    high = rep["by_confidence"]["high"]
    assert high["suggestions"] == 2 and high["sold"] == 2
    assert high["override_rate_pct"] == 50.0
    assert high["after_sale"]["engine"]["pct_within_15"] == 50.0
    assert high["after_sale"]["engine"]["median_abs_pct_error"] == round((100 / 31 + 25) / 2, 1)
    assert rep["by_confidence"]["low"]["after_sale"]["engine"]["median_abs_pct_error"] == 20.0
    top = rep["most_overridden_part_types"]
    assert top[0]["overridden"] == 1 and {t["part_type"] for t in top} == {"WINDOW MOTOR FRONT", "SLIP RING"}
    slip = next(t for t in top if t["part_type"] == "SLIP RING")
    assert slip["override_rate_pct"] == 100.0 and slip["median_change_pct"] == 25.0


def test_report_csv_download(client, conn):
    sid = suggestion(conn, "5Q0959802A", 30)
    feedback.record_finals(conn, [{"suggestion_id": sid, "final_price": 35}])
    sale(conn, "WINDOW MOTOR 5Q0 959 802 A", 33, "2026-09-05")
    r = client.get("/price/report", params={"format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["overridden"] == "yes" and rows[0]["sold_price"] == "33.0" and rows[0]["days_to_sell"] == "4"
    assert rows[0]["mode"] == "live" and rows[0]["engine_price"] == "30.0" and rows[0]["final_price"] == "35.0"


def test_report_empty(client):
    rep = client.get("/price/report").json()
    assert rep["total_suggestions"] == 0
    assert rep["by_confidence"]["high"]["after_sale"]["engine"]["n"] == 0
    assert rep["after_sale"]["current"]["n"] == 0 and rep["sold_items"] == []



# ── Shadow mode ────────────────────────────────────────────────────────

def test_suggest_endpoint_reports_and_logs_mode(client, conn, monkeypatch):
    r = client.get("/price/suggest", params={"part_number": "5Q0959802A"}).json()
    assert r["mode"] == "shadow"  # default
    monkeypatch.setenv("PRICING_MODE", "live")
    r2 = client.get("/price/suggest", params={"part_number": "5Q0959802A"}).json()
    assert r2["mode"] == "live"
    monkeypatch.setenv("PRICING_MODE", "nonsense")
    assert client.get("/price/suggest", params={"part_number": "5Q0959802A"}).json()["mode"] == "shadow"
    modes = [row[0] for row in conn.execute("SELECT mode FROM price_suggestions ORDER BY id")]
    assert modes == ["shadow", "live", "shadow"]


def test_health_exposes_pricing_mode(client, monkeypatch):
    assert client.get("/health").json()["pricing_mode"] == "shadow"
    monkeypatch.setenv("PRICING_MODE", "LIVE")
    assert client.get("/health").json()["pricing_mode"] == "live"


def test_shadow_override_measured_against_current_price(client, conn):
    a = suggestion(conn, "5Q0959802A", 29.99, mode="shadow")
    b = suggestion(conn, "5Q0959802A", 29.99, mode="shadow")
    c = suggestion(conn, "5Q0959802A", 29.99, mode="shadow")
    client.post("/price/suggestions/final", json={"items": [
        {"suggestion_id": a, "final_price": 24.99, "current_price": 24.99},   # kept the current price
        {"suggestion_id": b, "final_price": 29.99, "current_price": 24.99},   # changed it (to the engine's)
        {"suggestion_id": c, "final_price": 20.00},                           # current price unknown
    ]})
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM price_suggestions")}
    assert rows[a]["overridden"] == 0 and rows[a]["current_price"] == 24.99
    assert rows[b]["overridden"] == 1
    assert rows[c]["overridden"] is None


def test_live_mode_with_no_engine_price_compares_to_current(conn):
    sid = suggestion(conn, "5G1941999B", None, conf="low", part_type="HEADLIGHT", mode="live")
    feedback.record_finals(conn, [{"suggestion_id": sid, "final_price": 99.0, "current_price": 99.0}])
    assert conn.execute("SELECT overridden FROM price_suggestions WHERE id=?", (sid,)).fetchone()[0] == 0


def test_report_compares_current_engine_and_final(client, conn):
    a = suggestion(conn, "5Q0959802A", 30, mode="shadow")
    b = suggestion(conn, "5Q0959802A", 30, mode="shadow", ts="2026-09-02T10:00:00+00:00")
    feedback.record_finals(conn, [{"suggestion_id": a, "final_price": 20, "current_price": 20},
                                  {"suggestion_id": b, "final_price": 30, "current_price": 20}])
    sale(conn, "WINDOW MOTOR 5Q0 959 802 A", 30, "2026-09-11")   # oldest unused sale for b: 9 days
    sale(conn, "WINDOW MOTOR 5Q0 959 802 A", 30, "2026-09-05")   # a (oldest suggestion) takes this: 4 days
    rep = client.get("/price/report").json()
    assert rep["by_mode"] == {"shadow": 2, "live": 0}
    lst = rep["listing"]
    assert lst["with_final_price"] == 2
    assert lst["final_equals_current_pct"] == 50.0 and lst["final_equals_engine_pct"] == 50.0
    after = rep["after_sale"]
    assert after["engine"]["n"] == 2 and after["engine"]["median_abs_pct_error"] == 0.0
    assert after["current"]["median_abs_pct_error"] == round(100 / 3, 1)
    assert after["current"]["median_bias_pct"] == round(-100 / 3, 1)
    assert after["final"]["n"] == 2
    days = rep["days_to_sell"]
    assert days["all"]["sold"] == 2
    assert days["listed_below_engine"]["sold"] == 1 and days["listed_at_engine"]["sold"] == 1
    assert days["final_equals_current"]["sold"] == 1 and days["final_differs_from_current"]["sold"] == 1
    items = rep["sold_items"]
    assert len(items) == 2
    assert {(i["final_price"], i["days_to_sell"]) for i in items} == {(20, 4), (30, 9)}
    assert {(i["current_price"], i["engine_price"], i["final_price"]) for i in items} == {(20, 30, 20), (20, 30, 30)}


def test_migration_adds_columns_to_existing_table(tmp_path):
    import sqlite3
    from pricing.store import init_pricing_db
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("CREATE TABLE price_suggestions (id INTEGER PRIMARY KEY, timestamp TEXT, part_number_norm TEXT)")
    c.execute("INSERT INTO price_suggestions (timestamp, part_number_norm) VALUES ('t', 'X')")
    init_pricing_db(c)
    init_pricing_db(c)  # idempotent
    cols = {r[1] for r in c.execute("PRAGMA table_info(price_suggestions)")}
    assert {"mode", "current_price"} <= cols
    assert c.execute("SELECT COUNT(*) FROM price_suggestions").fetchone()[0] == 1
    c.close()


# ── Not worth listing ──────────────────────────────────────────────────

def test_engine_flag_logged_with_suggestion(client, conn):
    for p, d in ((5, "2026-09-01"), (6, "2026-09-02"), (7, "2026-09-03")):
        sale(conn, "Golf Mk7 BULB HOLDER 5Q0 945 999", p, d)
    r = client.get("/price/suggest", params={"part_number": "5Q0945999"}).json()
    assert r["not_worth_listing"] is True and r["not_worth_listing_below"] == 8.0
    row = conn.execute("SELECT engine_nwl FROM price_suggestions WHERE id=?", (r["suggestion_id"],)).fetchone()
    assert row[0] == 1


def test_warning_endpoint_records_and_404s(client, conn):
    sid = suggestion(conn, "1K0959653D", 5.99, mode="shadow")
    r = client.post(f"/price/suggestions/{sid}/warning", json={"price_shown": 5.99, "basis": "our last 3 sales £5–£7"})
    assert r.status_code == 200 and r.json() == {"recorded": True}
    row = conn.execute("SELECT nwl_warned, nwl_price_shown, nwl_basis FROM price_suggestions WHERE id=?", (sid,)).fetchone()
    assert tuple(row) == (1, 5.99, "our last 3 sales £5–£7")
    assert client.post("/price/suggestions/99999/warning", json={"price_shown": 1}).status_code == 404


def test_report_not_worth_listing_section(client, conn):
    a = suggestion(conn, "1K0959653D", 5.99, mode="shadow")                                   # warned, listed, sold
    b = suggestion(conn, "6Q0953503E", 4.99, mode="shadow", ts="2026-09-02T10:00:00+00:00")   # warned, not listed
    c = suggestion(conn, "5Q0959802A", 29.99, mode="shadow")                                  # no warning
    for sid, price in ((a, 5.99), (b, 4.99)):
        client.post(f"/price/suggestions/{sid}/warning", json={"price_shown": price, "basis": "x"})
    feedback.record_finals(conn, [{"suggestion_id": a, "final_price": 9.99, "current_price": 5.99},
                                  {"suggestion_id": c, "final_price": 29.99, "current_price": 29.99}])
    sale(conn, "SLIP RING 1K0 959 653 D", 9.5, "2026-09-08")
    nwl = client.get("/price/report").json()["not_worth_listing"]
    assert nwl["threshold"] == 8.0
    assert nwl["warned"] == 2 and nwl["listed_anyway"] == 1 and nwl["not_listed"] == 1
    assert nwl["listed_anyway_pct"] == 50.0
    assert nwl["listed_anyway_sold"] == 1 and nwl["sold_median_price"] == 9.5 and nwl["sold_median_days"] == 7
    assert nwl["sold_at_or_above_threshold"] == 1
    rows = list(csv.DictReader(io.StringIO(client.get("/price/report", params={"format": "csv"}).text)))
    warned = [r for r in rows if r["not_worth_listing_warned"] == "yes"]
    assert len(warned) == 2 and {r["not_worth_listing_price_shown"] for r in warned} == {"5.99", "4.99"}


def test_health_exposes_threshold(client, monkeypatch):
    import pricing_config as pcfg
    assert client.get("/health").json()["not_worth_listing_below"] == 8.0
    monkeypatch.setattr(pcfg, "NOT_WORTH_LISTING_BELOW", 10.0)
    assert client.get("/health").json()["not_worth_listing_below"] == 10.0
