import httpx
import pytest
import respx

from fuel.services.resolver import ResolveError, resolve


def test_city_state_variants(corridor):
    for text in ("Chicago, IL", "chicago, illinois", "Chicago IL", "Chicago, IL, USA", "350 N State St, Chicago, IL"):
        point, calls = resolve(text)
        assert (round(point.lat, 1), round(point.lon, 1), calls) == (41.0, -87.6, 0), text


def test_zip_and_address_with_zip(corridor):
    assert resolve("60601")[0].label == "ZIP 60601"
    assert resolve("233 S Wacker Dr, Chicago, IL 60601")[0].resolved_by == "zip"


def test_coordinates(corridor):
    point, _ = resolve("41.0, -87.62")
    assert point.resolved_by == "coordinates"


def test_coordinates_outside_usa_rejected(corridor):
    with pytest.raises(ResolveError) as exc:
        resolve("43.65,-79.38")  # Toronto
    assert exc.value.code == "outside_usa"


def test_dominant_city_wins_without_a_state(corridor):
    point, _ = resolve("Chicago")
    assert point.label == "Chicago, IL"


def test_ambiguous_city_lists_candidates(corridor):
    from tests.conftest import make_place

    make_place("Springfield", "IL", 39.8, -89.6, population=114_000)
    make_place("Springfield", "MO", 37.2, -93.3, population=170_000)
    with pytest.raises(ResolveError) as exc:
        resolve("Springfield")
    assert exc.value.code == "ambiguous_location"
    assert {"Springfield, IL", "Springfield, MO"} <= set(exc.value.candidates)


LANDMARK = {
    "lat": "41.01",  # next to the corridor fixture's ZIPs
    "lon": "-87.61",
    "name": "Millennium Park",
    "category": "leisure",
    "importance": 0.52,
    "display_name": "Millennium Park, Chicago, Illinois, United States",
    "address": {"city": "Chicago", "ISO3166-2-lvl4": "US-IL"},
}


def test_unknown_text_uses_one_geocoder_call(corridor):
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[LANDMARK]))
    point, calls = resolve("The Bean, Millennium Park")
    assert calls == 1 and point.resolved_by == "geocoder"


def test_unknown_text_with_no_geocoder_hit_is_a_400(corridor):
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ResolveError) as exc:
        resolve("Atlantis")
    assert exc.value.code == "unknown_location"


@pytest.mark.parametrize("text", ["Toronto, Ontario", "Toronto, ON", "Vancouver, BC, Canada", "Monterrey, Mexico"])
def test_foreign_regions_are_rejected_before_geocoding(corridor, text):
    # no geocoder route registered: a call would fail the test
    with pytest.raises(ResolveError) as exc:
        resolve(text)
    assert exc.value.code == "outside_usa"


def test_biggest_city_wins_by_population_not_area(corridor):
    from tests.conftest import make_place

    make_place("Atlanta", "GA", 33.76, -84.42, population=510_000)
    big_rural = make_place("Atlanta", "ID", 43.64, -115.36, population=0)
    big_rural.land_area_sqmi = 1303
    big_rural.save()
    assert resolve("Atlanta")[0].label == "Atlanta, GA"


def test_country_suffix_needs_a_separator(corridor):
    from tests.conftest import make_place

    make_place("Columbus", "OH", 39.99, -82.99, population=913_000)
    assert resolve("Columbus")[0].label == "Columbus, OH"  # not "Columb"
    assert resolve("Chicago, IL USA")[0].label == "Chicago, IL"


def test_full_width_unicode_is_normalised(corridor):
    assert resolve("Ｃｈｉｃａｇｏ, ＩＬ")[0].label == "Chicago, IL"


def test_islands_are_rejected_offline(corridor):
    from fuel.models import ZipCode

    ZipCode.objects.create(code="96813", lat=21.31, lon=-157.86)
    with pytest.raises(ResolveError) as exc:
        resolve("96813")
    assert exc.value.code == "not_reachable_by_road"


def test_geocoder_answers_are_cached(corridor):
    route = respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[LANDMARK])
    )
    assert resolve("The Bean, Millennium Park")[1] == 1
    assert resolve("The Bean, Millennium Park")[1] == 0
    assert route.call_count == 1


def test_geocoder_outage_is_a_503_not_unknown_location(corridor):
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(429))
    with pytest.raises(ResolveError) as exc:
        resolve("Some Street Somewhere")
    assert (exc.value.code, exc.value.status) == ("geocoder_unavailable", 503)


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Tokyo", {"name": "Tokyo", "category": "amenity", "importance": 0.0001, "address": {"city": "San Antonio"}}),
        (
            "Washington, WA",
            {"name": "Washougal", "category": "place", "importance": 0.4, "address": {"town": "Washougal"}},
        ),
        ("Kansas", {"name": "Kansas", "addresstype": "state", "category": "boundary", "importance": 0.8}),
    ],
)
def test_geocoder_guesses_that_dont_match_the_input_are_rejected(corridor, text, hit):
    hit = {"lat": "40.0", "lon": "-95.0", "display_name": "x", **hit}
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[hit]))
    with pytest.raises(ResolveError) as exc:
        resolve(text)
    assert exc.value.code in ("unknown_location", "too_broad")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("USA", "too_broad"),
        ("Texas", "too_broad"),
        ("TX", "too_broad"),
        (", IL", "too_broad"),
        ("41.88,-87.63,", "invalid_coordinates"),
        ("1000,1", "invalid_coordinates"),
        ("0", "unknown_location"),
        ("Montréal, Québec", "outside_usa"),
        ("London, England", "outside_usa"),
    ],
)
def test_inputs_rejected_offline(corridor, text, code):
    with pytest.raises(ResolveError) as exc:
        resolve(text)
    assert exc.value.code == code


def test_zip_that_contradicts_the_city_is_rejected(corridor):
    from fuel.models import ZipCode

    ZipCode.objects.create(code="90210", lat=34.1, lon=-118.4)
    with pytest.raises(ResolveError) as exc:
        resolve("Chicago, IL 90210")
    assert exc.value.code == "conflicting_location"
    assert resolve("Chicago, IL 60601")[0].resolved_by == "zip"


def test_city_and_full_state_name_without_a_comma(corridor):
    assert resolve("Omaha Nebraska")[0].label == "Omaha, NE"


@pytest.mark.parametrize(("lat", "lon"), [(58.30, -134.42), (57.79, -152.41), (64.50, -165.41), (41.28, -70.10)])
def test_places_with_no_road_to_the_mainland(corridor, lat, lon):
    from fuel.models import ZipCode

    ZipCode.objects.create(code="99999", lat=lat, lon=lon)
    with pytest.raises(ResolveError) as exc:
        resolve(f"{lat},{lon}")
    assert exc.value.code == "not_reachable_by_road"


def test_state_name_that_is_also_a_big_city(corridor):
    from tests.conftest import make_place

    make_place("New York", "NY", 40.66, -73.94, population=8_800_000)
    make_place("Texas", "WI", 44.9, -89.6, population=1_000)
    assert resolve("New York")[0].label == "New York, NY"
    with pytest.raises(ResolveError) as exc:
        resolve("Texas")
    assert exc.value.code == "too_broad"


@pytest.mark.parametrize("text", ["60601 1234", "60601, IL", "60601 IL", "IL 60601", "Chicago 60601"])
def test_zip_anywhere_with_a_state_or_city(corridor, text):
    point, calls = resolve(text)
    assert point.resolved_by == "zip" and calls == 0


def test_nicknames_and_dc_alone(corridor):
    from tests.conftest import make_place

    make_place("Washington", "DC", 38.9, -77.03, population=700_000)
    for text in ("DC", "dc", "District of Columbia", "DC 20001"):
        if text == "DC 20001":
            from fuel.models import ZipCode

            ZipCode.objects.create(code="20001", lat=38.91, lon=-77.02)
        assert resolve(text)[0].lat == pytest.approx(38.9, abs=0.1), text


@pytest.mark.parametrize("text", ["San Juan, PR", "Guam", "Charlotte Amalie, VI", "San Juan, Puerto Rico"])
def test_territories_are_not_reachable_by_road(corridor, text):
    with pytest.raises(ResolveError) as exc:
        resolve(text)
    assert exc.value.code == "not_reachable_by_road"


def test_street_address_must_be_in_the_town_typed(corridor):
    wrong_town = {
        "lat": "41.01",
        "lon": "-87.61",
        "category": "highway",
        "address": {"town": "Idaho City", "ISO3166-2-lvl4": "US-ID"},
    }
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[wrong_town]))
    with pytest.raises(ResolveError):
        resolve("100 Main St, Boise")
