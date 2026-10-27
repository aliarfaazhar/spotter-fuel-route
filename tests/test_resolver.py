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


def test_unknown_text_uses_one_geocoder_call(corridor):
    respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[{"lat": "41.01", "lon": "-87.61", "display_name": "Somewhere, IL"}])
    )
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
        return_value=httpx.Response(200, json=[{"lat": "41.01", "lon": "-87.61", "display_name": "Somewhere, IL"}])
    )
    assert resolve("The Bean, Millennium Park")[1] == 1
    assert resolve("The Bean, Millennium Park")[1] == 0
    assert route.call_count == 1


def test_geocoder_outage_is_a_503_not_unknown_location(corridor):
    respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(429))
    with pytest.raises(ResolveError) as exc:
        resolve("Some Street Somewhere")
    assert (exc.value.code, exc.value.status) == ("geocoder_unavailable", 503)
