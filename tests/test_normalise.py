import pytest

from pricing.normalise import base_number, extract_part_numbers, middle_group, normalise, spaced


@pytest.mark.parametrize("raw,expected", [
    ("6R2 880 201 B", "6R2880201B"),
    ("6R2880201B", "6R2880201B"),
    ("6r2 880 201 b", "6R2880201B"),
    ("6R2.880.201.B", "6R2880201B"),
    ("6R2-880-201-B", "6R2880201B"),
    ("  1k0 907 379 ar  ", "1K0907379AR"),
    ("8R0 845 300", "8R0845300"),
    ("575 419 091 J", "575419091J"),      # all-digit SEAT prefix
    ("04L 907 309 R", "04L907309R"),
    ("5KO 959 702 B", "5K0959702B"),      # letter O typed for zero
])
def test_normalise_formats(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("Part Number is 6R2 880 201 B", "6R2880201B"),
    ("Part number is 03C 906 024 CN fully tested", "03C906024CN"),
    ("2002-2007 Volkswagen Polo 9N3 LOWER GRILLE (DRIVER SIDE) 6Q0 853 666 H", "6Q0853666H"),
    ("Golf Mk7 Inner tail light bulb holder (Driver)5G0 945 260 A", "5G0945260A"),
    ("VW Golf 6R2 880 201 Golf Passat", "6R2880201"),  # trailing word isn't an index
    ("Audi Q3 Rear seatbelt buckle 4G0 857 739 A9B 9", "4G0857739"),
])
def test_normalise_embedded_in_text(text, expected):
    assert normalise(text) == expected


@pytest.mark.parametrize("junk", [None, "", "   ", "hello", "N/A", "2008-2012", "1.6 TDI CAY",
                                  "Volkswagen Up Move 2011-2020 GEARBOX NZC", "!!!"])
def test_normalise_junk(junk):
    assert normalise(junk) == ""


def test_normalise_non_vag_code_kept():
    assert normalise("0 281 002 315") == "0281002315"


def test_years_are_not_part_numbers():
    assert extract_part_numbers("2009-2014 VOLKSWAGEN POLO 6R 2009 2014") == []


@pytest.mark.parametrize("raw,expected", [
    ("6R2 880 201 B", "6R2880201"),
    ("6R2 880 201", "6R2880201"),
    ("1K0907379AR", "1K0907379"),
    ("1K0 907 379 AS", "1K0907379"),
    ("0 281 002 315", "0281002315"),
    ("junk!", ""),
])
def test_base_number(raw, expected):
    assert base_number(raw) == expected


def test_index_letter_variants_share_base():
    variants = ["5Q0 959 655 M", "5Q0959655N", "5q0-959-655-AB", "5Q0 959 655"]
    assert {base_number(v) for v in variants} == {"5Q0959655"}
    assert len({normalise(v) for v in variants}) == 4


def test_extract_multiple_and_dedupe():
    t = "6R2 880 201 B or 6R2880201B also 1K0 959 653 D"
    assert extract_part_numbers(t) == ["6R2880201B", "1K0959653D"]


def test_middle_group_and_spaced():
    assert middle_group("6J3 837 401 AJ") == "837401"
    assert middle_group("hello") == ""
    assert spaced("6R2880201B") == "6R2 880 201 B"
    assert spaced("8R0845300") == "8R0 845 300"
