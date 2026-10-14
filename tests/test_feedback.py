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


def suggestion(conn, pn, suggested, conf="high", ts="2026-09-01T10:00:00+00:00", part_type="WINDOW MOTOR FRONT"):
    cur = conn.execute(
        "INSERT INTO price_suggestions (timestamp, part_number_norm, part_type, suggested, confidence, source, reason)"
        " VALUES (?, ?, ?, ?, ?, 'own_sold', 'r')", (ts, pn, part_type, suggested, conf))
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
    assert high["suggested_vs_sold"]["pct_within_15"] == 50.0
    assert high["suggested_vs_sold"]["median_abs_pct_error"] == round((100 / 31 + 25) / 2, 1)
    assert rep["by_confidence"]["low"]["suggested_vs_sold"]["median_abs_pct_error"] == 20.0
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


def test_report_empty(client):
    rep = client.get("/price/report").json()
    assert rep["total_suggestions"] == 0
    assert rep["by_confidence"]["high"]["suggested_vs_sold"]["n"] == 0
