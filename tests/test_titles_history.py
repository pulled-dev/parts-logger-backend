import pytest

from pricing.history import own_sales, part_type_index, part_type_sales
from pricing.sales_import import import_rows
from pricing.titles import canonical_part_type, engine_group, find_part_type, parse_title


@pytest.mark.parametrize("title,ptype,model,gen,eng", [
    ("2012-2017 Volkswagen Golf Mk7 1.6 TDI CLHA Brake Master Cylinder 5Q2 611 301",
     "BRAKE MASTER CYLINDER", "GOLF", "MK7", "CLH"),
    ("Volkswagen Polo 6R 2009-2014 WINDOW MOTOR (FRONT DRIVER SIDE) 6R0 959 802 AN",
     "WINDOW MOTOR FRONT", "POLO", "6R", ""),
    ("SEAT Alhambra 7N 2015-2022 GATEWAY CONTROL MODULE 7N0 907 530 BJ",
     "GATEWAY CONTROL MODULE", "ALHAMBRA", "7N", ""),
    ("Seat Ibiza 6J 2010 Drivers Seat", "SEAT", "IBIZA", "6J", ""),
    ("Audi A3 8V 2015 2.0 TDI CRB ECU (ENGINE) 04L 907 309 D", "ENGINE ECU", "A3", "8V", "CRB"),
    ("Volkswagen Golf Plus 2007-2013 1.4 TSI CAX AIR CON COMPRESSOR 1K0 820 859 T",
     "AIR CON COMPRESSOR", "GOLF PLUS", "", "CAX"),
    ("Volkswagen Golf Mk7 Tsi 2012-2017 A BAG MODULE 5Q0 959 655 C", "AIRBAG MODULE", "GOLF", "MK7", ""),
    ("Skoda Fabia 5J 2010-2014 HEADLIGHT/HEADLAMP (PASSENGER SIDE) 5J1 941 015",
     "HEADLIGHT", "FABIA", "5J", ""),
])
def test_parse_title(title, ptype, model, gen, eng):
    p = parse_title(title)
    assert (p["part_type"], p["model"], p["generation"], p["engine_group"]) == (ptype, model, gen, eng)


def test_engine_group_first_three_letters():
    assert engine_group("DSGB") == "DSG"   # real engine code, not the gearbox
    assert engine_group("clha") == "CLH"
    assert engine_group("") == ""
    assert engine_group("AB") == ""


def test_engine_code_needs_displacement_context():
    assert parse_title("Seat Alhambra 7N 2.0 TDI SUSPENSION LEG HUB 7N0 413 031 J")["engine_group"] == ""
    assert parse_title("Volkswagen Polo 1.2 Petrol CGP Engine Wiring Loom")["engine_group"] == "CGP"


def test_canonical_part_type_and_unknown():
    assert canonical_part_type("AIR BAG (DRIVER SIDE)") == "AIRBAG"
    assert canonical_part_type("REAR/TAIL LIGHT (PASSENGER SIDE)") == "REAR LIGHT"
    assert find_part_type("completely unrelated words") == ""


def _row(i, title, price, date):
    return {"order_number": f"T-{i}", "sales_record_number": str(i), "item_number": str(1000 + i),
            "title": title, "custom_label": "", "quantity": "1", "sold_for": str(price),
            "postage": "0", "total_price": "", "sale_date": date, "dispatched_date": ""}


@pytest.fixture
def seeded(conn):
    rows = [
        _row(1, "Volkswagen Golf Mk7 2014 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 A", 30, "2026-01-10"),
        _row(2, "Volkswagen Golf Mk7 2014 WINDOW MOTOR (FRONT PASSENGER) 5Q0 959 801 A", 34, "2026-03-10"),
        _row(3, "Volkswagen Golf Mk6 2010 WINDOW MOTOR (FRONT DRIVER) 5K0 959 802 B", 20, "2026-02-10"),
        _row(4, "Volkswagen Golf Mk7 2014 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 B", 32, "2026-06-10"),
        _row(5, "Volkswagen Golf Mk7 1.6 TDI CLHA ENGINE ECU 04L 907 309 R", 90, "2026-04-01"),
        _row(6, "Volkswagen Golf Mk7 2.0 TDI CRBC ENGINE ECU 04L 907 309 D", 120, "2026-04-02"),
    ]
    import_rows(conn, rows, "csv_clean")
    return conn


def test_own_sales_exact_base_and_before(seeded):
    assert [p for p, _ in own_sales(seeded, part_number="5Q0959802A")] == [30]
    assert sorted(p for p, _ in own_sales(seeded, base="5Q0959802")) == [30, 32]
    # Nothing on/after the cut-off date is visible.
    assert [p for p, _ in own_sales(seeded, base="5Q0959802", before="2026-06-10")] == [30]
    assert own_sales(seeded, base="5Q0959802", before="2026-06-11", lookback_days=100) == [(32, "2026-06-10")]


def test_part_type_levels_most_specific_first(seeded):
    level, rows = part_type_sales(seeded, "WINDOW MOTOR FRONT", "GOLF", "MK7", min_sales=2)
    assert level == "model+gen" and sorted(p for p, _ in rows) == [30, 32, 34]
    level, rows = part_type_sales(seeded, "WINDOW MOTOR FRONT", "GOLF", "MK5", min_sales=1)
    assert level == "model" and len(rows) == 4
    # Engine group only used for engine-relevant part types.
    level, rows = part_type_sales(seeded, "ENGINE ECU", "GOLF", "MK7", "CLH", min_sales=1)
    assert level == "model+gen+engine" and [p for p, _ in rows] == [90]


def test_part_type_index(seeded):
    idx = part_type_index(seeded, min_sales=2)
    top = idx[0]
    assert (top["part_type"], top["model"], top["generation"], top["count"]) == ("WINDOW MOTOR FRONT", "GOLF", "MK7", 3)
    assert top["median"] == 32
