import os
import sqlite3

import pytest
from conftest import fixture_path

from pricing.sales_import import (
    CANONICAL_FIELDS, ImportFormatError, build_record, format_summary, import_rows,
    parse_date, parse_money, read_orders_csv,
)

RAW = fixture_path("orders_raw_synthetic.csv")
CLEAN = fixture_path("orders_clean_synthetic.csv")


def _all_text(conn) -> str:
    """Every value in every table, as one string (for PII scanning)."""
    chunks = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            chunks.extend(str(v) for v in row)
    return "\n".join(chunks)


def test_parse_money_and_dates():
    assert parse_money("£14.99") == 14.99
    assert parse_money("£1,204.50") == 1204.50
    assert parse_money("") is None
    assert parse_money("\ufffd6.99") == 6.99  # mis-decoded pound sign
    assert parse_date("14-Oct-26").isoformat() == "2026-10-14"
    assert parse_date("2026-10-01T09:15:00.000Z").isoformat() == "2026-10-01"
    assert parse_date("Oct-14-26").isoformat() == "2026-10-14"
    assert parse_date("not a date") is None


def test_read_clean_format():
    rows, fmt = read_orders_csv(CLEAN)
    assert fmt == "clean"
    assert len(rows) == 6
    assert set(rows[0]) == set(CANONICAL_FIELDS)


def test_read_raw_format_header_on_row_two_and_pii_dropped():
    rows, fmt = read_orders_csv(RAW)
    assert fmt == "raw"
    with open(RAW, encoding="utf-8-sig") as fh:
        fh.readline()
        assert len(fh.readline().split(",")) == 81
    for r in rows:
        assert set(r) == set(CANONICAL_FIELDS)
        assert not any("PII_SENTINEL" in v or "example.invalid" in v for v in r.values())


def test_unrecognised_file_rejected():
    with pytest.raises(ImportFormatError):
        read_orders_csv("a,b,c\n1,2,3\n")


def test_no_buyer_data_reaches_db(conn):
    rows, _ = read_orders_csv(RAW)
    # Even if a caller passes extra keys straight in, they're dropped.
    rows[0]["buyer_email"] = "pii.sentinel@example.invalid"
    rows[0]["Buyer Name"] = "PII_SENTINEL_name"
    summary = import_rows(conn, rows, "csv_raw")
    assert summary["inserted"] == 3
    text = _all_text(conn)
    assert "PII_SENTINEL" not in text
    assert "example.invalid" not in text
    assert "TRACK0001" not in text
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sold_history)")}
    assert not any(c.startswith(("buyer", "post_to")) for c in cols)


def test_raw_skips_order_summary_and_footer_rows(conn):
    rows, _ = read_orders_csv(RAW)
    summary = import_rows(conn, rows, "csv_raw")
    assert summary["inserted"] == 3
    assert summary["skip_reasons"].get("no item", 0) >= 2


def test_price_is_item_plus_postage_mixed_postage(conn):
    rows, _ = read_orders_csv(CLEAN)
    import_rows(conn, rows, "csv_clean")
    got = {r["item_number"]: (r["price"], r["shipping"], r["item_price"])
           for r in conn.execute("SELECT * FROM sold_history")}
    assert got["200000000001"] == (26.98, 6.99, 19.99)    # 2025, £6.99 P&P
    assert got["200000000002"] == (29.99, 0.0, 29.99)     # 2026, free P&P
    assert got["200000000003"] == (51.99, 6.99, 45.0)     # Total price column says £0.00
    assert got["200000000004"] == (15.0, 0.0, 15.0)       # blank P&P
    assert got["200000000005"] == (144.99, 64.99, 80.0)   # courier panel


def test_dedupe_within_file_and_idempotent_rerun(conn):
    rows, _ = read_orders_csv(CLEAN)
    first = import_rows(conn, rows, "csv_clean")
    assert first["inserted"] == 5 and first["duplicates"] == 1
    second = import_rows(conn, rows, "csv_clean")
    assert second["inserted"] == 0 and second["duplicates"] == 6
    assert conn.execute("SELECT COUNT(*) FROM sold_history").fetchone()[0] == 5


def test_records_have_part_number_and_part_type(conn):
    rows, _ = read_orders_csv(CLEAN)
    import_rows(conn, rows, "csv_clean")
    r = conn.execute("SELECT * FROM sold_history WHERE item_number='200000000001'").fetchone()
    assert r["part_number_norm"] == "6R0959802AN"
    assert r["base_number"] == "6R0959802"
    assert r["part_type"] == "WINDOW MOTOR FRONT"
    assert (r["model"], r["generation"]) == ("POLO", "6R")
    assert r["sold_date"] == "2025-01-10"
    ecu = conn.execute("SELECT * FROM sold_history WHERE item_number='200000000003'").fetchone()
    assert ecu["engine_group"] == "CGP"
    nopn = conn.execute("SELECT * FROM sold_history WHERE item_number='200000000004'").fetchone()
    assert nopn["part_number_norm"] == "" and nopn["part_type"] == "WASHER BOTTLE"


def test_dry_run_writes_nothing(conn):
    rows, _ = read_orders_csv(CLEAN)
    summary = import_rows(conn, rows, "csv_clean", dry_run=True)
    assert summary["inserted"] == 5 and summary["duplicates"] == 1
    assert conn.execute("SELECT COUNT(*) FROM sold_history").fetchone()[0] == 0
    text = format_summary(summary, "clean")
    assert "DRY RUN" in text and "Would insert:" in text and "5" in text


def test_build_record_rejects_bad_rows():
    base = {f: "" for f in CANONICAL_FIELDS}
    assert build_record(base, "csv_clean")[1] == "no item"
    r = dict(base, item_number="1", title="X", sold_for="", sale_date="01-Jan-25")
    assert build_record(r, "csv_clean")[1] == "no price"
    r = dict(base, item_number="1", title="X", sold_for="£5", sale_date="garbage")
    assert build_record(r, "csv_clean")[1] == "bad date"


# ── CLI ────────────────────────────────────────────────────────────────

def _cli():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "import_ebay_sales.py")
    spec = importlib.util.spec_from_file_location("import_ebay_sales", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_dry_run_creates_no_db(tmp_path, capsys):
    db_path = tmp_path / "pricing.db"
    assert _cli().main([CLEAN, "--dry-run", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN - nothing written" in out
    assert "Would insert:        5" in out
    assert not db_path.exists()


def test_cli_dry_run_leaves_existing_db_untouched(tmp_path, capsys):
    db_path = tmp_path / "pricing.db"
    cli = _cli()
    assert cli.main([CLEAN, "--db", str(db_path)]) == 0
    before = db_path.read_bytes()
    assert cli.main([RAW, "--dry-run", "--db", str(db_path)]) == 0
    assert db_path.read_bytes() == before
    assert "Would insert:        3" in capsys.readouterr().out


def test_cli_import_then_rerun_idempotent(tmp_path, capsys):
    db_path = tmp_path / "pricing.db"
    cli = _cli()
    cli.main([CLEAN, "--db", str(db_path), "--show-index", "5"])
    cli.main([CLEAN, "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "Inserted:            5" in out and "Inserted:            0" in out
    assert "part-type groups" in out
    n = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sold_history").fetchone()[0]
    assert n == 5


def test_cli_refuses_automanagement(capsys):
    assert _cli().main(["C:\\AutoManagement\\sales.csv", "--dry-run"]) == 1
    assert "AutoManagement" in capsys.readouterr().out


# ── API import endpoint ───────────────────────────────────────────────

def test_import_endpoint_disabled_without_admin_token(client):
    r = client.post("/price/sold-history/import", json={"rows": []})
    assert r.status_code == 503


def test_import_endpoint_rejects_bad_token(client, monkeypatch):
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "s3cret")
    r = client.post("/price/sold-history/import", json={"rows": []}, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401


def test_import_endpoint_inserts_and_strips_extra_keys(client, monkeypatch, tmp_db):
    monkeypatch.setenv("PRICING_ADMIN_TOKEN", "s3cret")
    rows, _ = read_orders_csv(CLEAN)
    rows[0]["buyer_email"] = "pii.sentinel@example.invalid"
    hdr = {"X-Admin-Token": "s3cret"}
    r = client.post("/price/sold-history/import", json={"rows": rows, "dry_run": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["inserted"] == 5
    r = client.post("/price/sold-history/import", json={"rows": rows}, headers=hdr)
    assert r.json()["inserted"] == 5
    r = client.post("/price/sold-history/import", json={"rows": rows}, headers=hdr)
    assert r.json()["inserted"] == 0
    c = sqlite3.connect(tmp_db)
    assert "example.invalid" not in _all_text(c)
    c.close()
