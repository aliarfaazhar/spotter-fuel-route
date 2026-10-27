import pytest

from fuel.management.commands.import_fuel_prices import parse_highway
from fuel.services.names import census_aliases, place_key, state_code, strip_census_suffix


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("St. Louis", "Saint Louis"),
        ("La Grange", "LaGrange"),
        ("O'Neill", "ONeill"),
        ("Mc Donald", "McDonald"),
        ("S Coffeyville", "South Coffeyville"),
        ("Ft. Worth", "Fort Worth"),
        ("Mt Vernon", "Mount Vernon"),
        ("New Castle      ", "New Castle"),
    ],
)
def test_place_key_treats_spellings_as_equal(a, b):
    assert place_key(a) == place_key(b)


def test_census_suffixes_are_stripped():
    assert strip_census_suffix("Abbeville city") == "Abbeville"
    assert strip_census_suffix("Edison township") == "Edison"
    assert strip_census_suffix("Indianapolis city (balance)") == "Indianapolis"


def test_consolidated_governments_are_findable_by_their_short_name():
    names = census_aliases("Nashville-Davidson metropolitan government (balance)")
    assert "Nashville" in names and "Nashville-Davidson" in names


def test_state_code_accepts_codes_and_names():
    assert state_code("tx") == "TX"
    assert state_code("Texas") == "TX"
    assert state_code("Ontario") is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("I-44, EXIT 283 & US-69", ("I-44", "283")),
        ("I-80 Exit 223", ("I-80", "223")),
        ("I-80, EXIT 4C/US-46", ("I-80", "4C")),
        ("US-13 & US-40", ("US-13", "")),
        ("SR-375", ("SR-375", "")),
    ],
)
def test_parse_highway(address, expected):
    assert parse_highway(address) == expected
