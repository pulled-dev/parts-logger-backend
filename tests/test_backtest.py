import importlib.util
import os
from datetime import date

import pytest
from conftest import fixture_path

import pricing_config as cfg
from pricing import backtest
from pricing.sales_import import import_rows

_n = iter(range(1, 10_000))


def sale(conn, title, price, day, postage=0):
    i = next(_n)
    import_rows(conn, [{"order_number": f"B-{i}", "sales_record_number": str(i), "item_number": str(i),
                        "title": title, "custom_label": "", "quantity": "1", "sold_for": str(price),
                        "postage": str(postage), "total_price": "", "sale_date": day, "dispatched_date": ""}],
                "csv_clean")


# 8K1723523 (accelerator pedal) has an exact entry (9.99) in vag_parts_db.json
# and is NOT variant-sensitive, so the engine always returns a price for it.
PEDAL = "Audi Q5 8R ACCELERATOR PEDAL 8K1 723 523"
# 6C0907379N (ABS pump/modulator/control unit) IS variant-sensitive.
ABS = "Seat Ibiza 6J ABS PUMP 6C0 907 379 N"


def test_metrics():
    assert backtest.metrics([10, 20, 30, None]) == {"n": 3, "mdape": 20.0, "within": 33.3}
    assert backtest.metrics([]) == {"n": 0, "mdape": None, "within": None}
    assert backtest.abs_pct_err(90, 100) == 10
    assert backtest.abs_pct_err(None, 100) is None


def test_old_method_replay():
    assert backtest.old_method_price("6C0907379N") == (24.99, "exact")
    price, kind = backtest.old_method_price("6R0907379X")  # same 6-digit group, no exact entry
    assert kind == "group" and price
    assert backtest.old_method_price("9Z9111111") == (None, "")


def test_sale_never_sees_itself(conn):
    sale(conn, PEDAL, 500, "2026-05-01")  # the only sale of this number
    rows = backtest.run(conn, date(2026, 10, 14))
    r = rows[0]
    assert r["new_source"] != "own_sold"
    # Nothing about the £500 sale leaked into its own suggestion: with no
    # earlier evidence at all the engine gives no price.
    assert r["new_source"] == "no_evidence" and r["new"] is None


def test_only_earlier_sales_used(conn):
    for d in ("2026-01-10", "2026-02-10", "2026-03-10"):
        sale(conn, PEDAL, 30, d)
    sale(conn, PEDAL, 60, "2026-04-10")
    sale(conn, PEDAL, 999, "2026-04-10")  # same day as the sale under test: invisible to it
    rows = {r["actual"]: r for r in backtest.run(conn, date(2026, 10, 14))}
    r = rows[60.0]
    assert r["new_source"] == "own_sold" and r["new"] == 29.99
    assert r["new_err"] == pytest.approx(abs(29.99 - 60) / 60 * 100)
    assert r["old"] == 9.99 and r["old_kind"] == "exact"


def test_window_and_skips(conn):
    sale(conn, PEDAL, 30, "2025-01-01")  # outside a 365-day window ending 2026-10-14
    sale(conn, "Volkswagen Polo 6R WASHER BOTTLE", 15, "2026-06-01")
    rows = backtest.run(conn, date(2026, 10, 14))
    assert len(rows) == 1 and rows[0]["skipped"] == "no part number"
    s = backtest.summarise(rows)
    assert s["no_part_number"] == 1 and s["with_part_number"] == 0


def test_breakerpro_exact_flag_restored(conn):
    sale(conn, PEDAL, 30, "2026-06-01")
    on = backtest.run(conn, date(2026, 10, 14), breakerpro_exact=True)[0]
    assert on["new_source"] == "breakerpro_exact" and on["new"] == 9.99
    assert cfg.USE_BREAKERPRO_EXACT is False
    off = backtest.run(conn, date(2026, 10, 14))[0]
    assert off["new_source"] != "breakerpro_exact"


def test_summary_head_to_head(conn):
    for d in ("2026-01-10", "2026-02-10", "2026-03-10", "2026-04-10"):
        sale(conn, PEDAL, 30, d)
    s = backtest.summarise(backtest.run(conn, date(2026, 10, 14)))
    # The first sale has no earlier evidence, so the engine gives no price for it.
    assert s["old_has_price"] == 4 and s["both_priced"] == 3
    assert s["engine_no_price"]["count"] == 1
    assert s["head_to_head"]["old"]["n"] == 3
    assert s["new_wins"] + s["old_wins"] + s["ties"] == 3
    assert set(s["by_quarter"]) == {"2026 Q1", "2026 Q2"}


def test_cli_writes_report_and_keeps_narrative(tmp_path):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "backtest_pricing.py")
    spec = importlib.util.spec_from_file_location("backtest_pricing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc = tmp_path / "bt.md"
    doc.write_text("# My notes\n\nkeep me\n\n<!-- AUTO:START -->old<!-- AUTO:END -->\n\ntail\n", encoding="utf-8")
    assert mod.main(["--csv", fixture_path("orders_clean_synthetic.csv"), "--doc", str(doc)]) == 0
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("# My notes\n\nkeep me") and text.rstrip().endswith("tail")
    assert "### Headline: head-to-head on the same sales" in text and "old<!--" not in text


def test_engine_no_price_reported_separately(conn):
    # Only sale of a variant-sensitive part: engine gives no price, old method does.
    sale(conn, ABS, 80, "2026-05-01")
    rows = backtest.run(conn, date(2026, 10, 14))
    assert rows[0]["new"] is None and rows[0]["new_source"] == "variant_check"
    assert rows[0]["new_err"] is None
    s = backtest.summarise(rows)
    assert s["engine_no_price"]["count"] == 1 and s["engine_no_price"]["old"]["n"] == 1
    assert s["both_priced"] == 0 and s["head_to_head"]["new"]["n"] == 0
    assert s["new_wins"] + s["old_wins"] + s["ties"] == 0
