from decimal import Decimal

import httpx
import numpy as np
import pytest
import respx

from tests.test_routing import OSRM_URL, osrm_body

API = "/api/route"


@pytest.fixture
def osrm():
    return respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))


def get(client, **params):
    return client.get(API, params)


def test_plan_end_to_end(client, corridor, osrm):
    resp = get(client, start="Chicago, IL", finish="Omaha, NE")
    assert resp.status_code == 200, resp.json()
    body = resp.json()

    assert body["distance_miles"] == pytest.approx(440.0, abs=0.5)
    assert body["meta"]["external_api_calls"] == {"routing": 1, "geocoding": 0}
    stops = body["fuel_stops"]
    assert stops, "an empty tank at the start means at least one purchase"
    assert [s["mile_marker"] for s in stops] == sorted(s["mile_marker"] for s in stops)
    assert all(s["listed"] for s in stops) and body["cost_is_complete"] is True
    assert body["route_options"][0]["selected"] and body["route_options"][0]["status"] == "ok"
    assert Decimal(str(body["total_fuel_cost"])) == sum(Decimal(str(s["cost"])) for s in stops)
    for s in stops:  # each stop's cost is exactly its gallons x the price shown, to the cent
        expected = (Decimal(str(s["gallons"])) * Decimal(str(s["price_per_gallon"]))).quantize(Decimal("0.01"))
        assert Decimal(str(s["cost"])) == expected
    assert body["total_gallons"] == pytest.approx(sum(s["gallons"] for s in stops))
    assert body["total_fuel_cost"] >= body["cheapest_possible_fuel_cost"]
    assert body["map"]["url"].startswith("http://testserver/map?")
    assert body["map"]["route"]["type"] == "LineString"  # the map of the route is in the response
    assert body["trip_fuel_cost"]["usd"] == body["total_fuel_cost"]  # an empty start buys everything it burns
    for s in stops:  # the map pins a stop where the route (the fixture's line along latitude 41) passes it
        if s["mile_marker"] > 0:
            assert s["route_lat"] == pytest.approx(41.0) and -96.0 <= s["route_lon"] <= -87.6
        else:  # the fill-up before departure keeps its town position
            assert (s["route_lat"], s["route_lon"]) == (s["lat"], s["lon"])


def test_second_request_uses_the_route_cache(client, corridor, osrm):
    get(client, start="Chicago, IL", finish="Omaha, NE")
    body = get(client, start="Chicago, IL", finish="Omaha, NE").json()
    assert osrm.call_count == 1
    assert body["meta"]["route_cache_hit"] is True and body["meta"]["external_api_calls"]["routing"] == 0


def test_cache_outage_degrades_to_uncached(client, corridor, osrm, monkeypatch):
    from django.core.cache import cache

    def boom(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get", boom)
    monkeypatch.setattr(cache, "set", boom)
    resp = get(client, start="Chicago, IL", finish="Omaha, NE")
    assert resp.status_code == 200


def test_route_geojson_is_lon_lat_and_can_be_left_out(client, corridor, osrm):
    body = get(client, start="60601", finish="68102").json()
    lon, lat = body["map"]["route"]["coordinates"][0]
    assert lat == pytest.approx(41.0) and lon == pytest.approx(-87.6)
    lean = get(client, start="60601", finish="68102", include_route="false").json()
    assert "route" not in lean["map"]


def test_full_tank_short_trip(client, corridor, osrm):
    body = get(client, start="Chicago, IL", finish="Omaha, NE", start_fuel="full").json()
    assert body["assumptions"]["start_fuel"] == "full"
    assert body["fuel_stops"] == [] and body["total_fuel_cost"] == 0
    trip = body["trip_fuel_cost"]  # nothing bought, but the trip still burns fuel worth something
    assert trip["gallons_used"] == pytest.approx(body["distance_miles"] / 10, abs=0.01)
    assert trip["estimated_gallons"] == trip["gallons_used"] and trip["usd"] > 0
    assert trip["usd"] == pytest.approx(trip["gallons_used"] * trip["estimate_price_per_gallon"], abs=0.01)


def test_post_body_works(client, corridor, osrm):
    resp = client.post(
        API, {"start": "Chicago, IL", "finish": "Omaha, NE", "stop_cost": 0}, content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.json()["total_fuel_cost"] == resp.json()["cheapest_possible_fuel_cost"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"finish": "Omaha, NE"}, "invalid"),
        ({"start": "43.65,-79.38", "finish": "Omaha, NE"}, "outside_usa"),
        ({"start": "Chicago, IL", "finish": "Chicago, IL"}, "same_location"),
        ({"start": "Chicago, IL", "finish": "Omaha, NE", "start_fuel": "half"}, "invalid"),
        ({"start": "Chicago, IL", "finish": "Omaha, NE", "start_fuel": "nan"}, "invalid"),
        ({"start": "Chicago, IL", "finish": "Omaha, NE", "stop_cost": "-1"}, "invalid"),
    ],
)
def test_bad_input_is_a_400_with_one_error_shape(client, corridor, params, code):
    resp = get(client, **params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == code
    assert set(resp.json()["error"]) == {"code", "message", "details"}


def test_gap_without_a_detour_is_planned_with_unlisted_fuel(client, corridor):
    from fuel.models import Station, ZipCode

    Station.objects.filter(opis_id__in=[2, 3, 4]).delete()  # the last station is ~565 mi short of the finish
    ZipCode.objects.create(code="80000", lat=41.0, lon=-106.0)
    long = [[lon, 41.0] for lon in (-87.6, -96.0, -106.0)]
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body(long, meters=1_420_000)))
    resp = get(client, start="Chicago, IL", finish="41.0,-106.0")
    assert resp.status_code == 200, resp.json()
    body = resp.json()

    direct = body["route_options"][0]
    assert direct["selected"] and direct["status"] == "unlisted_fuel_needed" and direct["gap"]["miles"] > 500
    unlisted = [s for s in body["fuel_stops"] if not s["listed"]]
    assert unlisted and all(s["cost"] is None and s["price_per_gallon"] is None for s in unlisted)
    assert body["cost_is_complete"] is False
    assert body["unpriced_gallons"] == pytest.approx(sum(s["gallons"] for s in unlisted))
    assert Decimal(str(body["total_fuel_cost"])) == sum(
        Decimal(str(s["cost"])) for s in body["fuel_stops"] if s["listed"]
    )
    assert body["savings_vs_naive"] is None
    assert any(w["code"] == "unlisted_fuel" for w in body["warnings"])


@pytest.fixture
def detour_world(db):
    """Nothing listed along the straight line from Chicago to (41, -100); one listed town well north of it."""
    from fuel.models import ZipCode
    from tests.conftest import make_place, make_station

    make_place("Chicago", "IL", 41.0, -87.6, population=2_700_000)
    ZipCode.objects.create(code="60601", lat=41.0, lon=-87.62)
    ZipCode.objects.create(code="69000", lat=41.0, lon=-100.0)
    make_station(1, make_place("Startville", "IL", 41.02, -87.62), "3.50")
    make_station(2, make_place("Northtown", "MN", 43.5, -94.0), "3.00")


def detour_osrm(request):
    """The direct route is a straight line; a request with a waypoint gets a line through it."""
    pairs = request.url.path.rsplit("/", 1)[-1].split(";")
    pts = [[float(v) for v in p.split(",")] for p in pairs]  # [lon, lat]
    coords, miles = [], 0.0
    from fuel.services.geometry import haversine_miles

    for (lon0, lat0), (lon1, lat1) in zip(pts, pts[1:], strict=False):
        coords += [
            [float(lon), float(lat)]
            for lon, lat in zip(np.linspace(lon0, lon1, 100), np.linspace(lat0, lat1, 100), strict=True)
        ]
        miles += float(haversine_miles(lat0, lon0, lat1, lon1))
    return httpx.Response(200, json=osrm_body(coords, meters=miles * 1609.344))


def test_gap_is_bridged_by_a_detour_through_listed_stations(client, detour_world):
    mock = respx.get(url__startswith=OSRM_URL).mock(side_effect=detour_osrm)
    resp = get(client, start="Chicago, IL", finish="41.0,-100.0")
    assert resp.status_code == 200, resp.json()
    body = resp.json()

    direct, detour = body["route_options"]
    assert direct["status"] == "out_of_range" and not direct["selected"]
    assert direct["gap"]["miles"] > 500 and direct["gap"]["route"]["type"] == "LineString"
    assert direct["route"]["type"] == "LineString"  # drawn on the map next to the chosen route
    assert detour["selected"] and detour["status"] == "ok" and detour["via"] == ["Northtown, MN"]
    assert detour["extra_miles"] > 0 and body["distance_miles"] == detour["distance_miles"]
    assert [s["city"] for s in body["fuel_stops"]] == ["Startville", "Northtown"]
    assert body["cost_is_complete"] is True
    assert any(w["code"] == "detour_for_fuel_coverage" for w in body["warnings"])
    assert body["meta"]["external_api_calls"]["routing"] == 2

    again = get(client, start="Chicago, IL", finish="41.0,-100.0").json()
    assert again["meta"]["external_api_calls"]["routing"] == 0 and mock.call_count == 2


def test_no_station_near_the_start_buys_only_what_it_must_off_the_list(client, corridor, osrm):
    from fuel.models import Station

    Station.objects.filter(opis_id=1).delete()  # the next listed station is ~98 mi down the road
    body = get(client, start="Chicago, IL", finish="Omaha, NE").json()
    first = body["fuel_stops"][0]
    assert not first["listed"] and first["mile_marker"] == 0
    assert 9 < first["gallons"] < 11  # just enough to reach the first listed station
    assert body["cost_is_complete"] is False and body["unpriced_gallons"] == first["gallons"]
    assert body["trip_fuel_cost"]["usd"] > body["total_fuel_cost"]  # the unlisted gallons are valued too


def test_routing_outage_is_a_502(client, corridor):
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(500))
    resp = get(client, start="Chicago, IL", finish="Omaha, NE")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "routing_unavailable"


def test_map_page_renders_without_routing(client, db):
    resp = client.get("/map?start=Chicago,+IL&finish=Omaha,+NE")
    assert resp.status_code == 200 and b"/api/route" in resp.content


def test_start_fuel_zero_means_empty(client, corridor, osrm):
    a = get(client, start="Chicago, IL", finish="Omaha, NE", start_fuel="0").json()
    b = get(client, start="Chicago, IL", finish="Omaha, NE").json()
    assert a["fuel_stops"] == b["fuel_stops"] and a["assumptions"]["start_fuel"] == "empty"


def test_unexpected_errors_are_json_500(client, corridor, monkeypatch):
    from fuel import views

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(views, "plan_trip", boom)
    resp = get(client, start="Chicago, IL", finish="Omaha, NE")
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "internal_error"


@pytest.mark.parametrize("path", ["/api/", "/api/route/", "/api/routes", "/api/places/x"])
def test_unknown_api_paths_are_json_404(client, db, path):
    resp = client.get(path)
    assert resp.status_code == 404 and resp["Content-Type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "not_found"


def test_forwarded_for_is_not_trusted_without_a_proxy(settings):
    from rest_framework.test import APIRequestFactory

    from fuel.throttling import AnonRateThrottleFailOpen

    request = APIRequestFactory().get("/api/route", HTTP_X_FORWARDED_FOR="1.2.3.4", REMOTE_ADDR="9.9.9.9")
    assert AnonRateThrottleFailOpen().get_ident(request) == "9.9.9.9"


def test_blank_and_boolean_params(client, corridor, osrm):
    ok = client.post(
        API, {"start": "Chicago, IL", "finish": "Omaha, NE", "start_fuel": ""}, content_type="application/json"
    )
    assert ok.status_code == 200 and ok.json()["assumptions"]["start_fuel"] == "empty"
    default = get(client, start="Chicago, IL", finish="Omaha, NE", stop_cost="").json()
    assert default["assumptions"]["stop_cost_usd"] == 10
    bad = client.post(
        API, {"start": "Chicago, IL", "finish": "Omaha, NE", "stop_cost": True}, content_type="application/json"
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid"


def test_bare_number_is_not_sent_to_the_geocoder(client, corridor):
    resp = get(client, start="123", finish="Omaha, NE")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unknown_location"


def test_unknown_state_code_is_rejected_offline(client, corridor):
    resp = get(client, start="Atlantis, ZZ", finish="Omaha, NE")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unknown_state"


def test_san_francisco_is_on_land(db):
    from fuel.services.datafiles import COORDINATE_FIXES, place_rows

    fixed = {
        (r["key"], r["state"]): (float(r["lat"]), float(r["lon"])) for r in place_rows() if r["key"] == "sanfrancisco"
    }
    assert fixed[("sanfrancisco", "CA")] == COORDINATE_FIXES[("sanfrancisco", "CA")]


def test_part_full_tank_with_no_station_near_the_start_tops_up_off_the_list(client, corridor, osrm):
    from fuel.models import Station

    Station.objects.filter(opis_id=1).delete()  # the first listed station is ~98 mi in
    body = get(client, start="Chicago, IL", finish="Omaha, NE", start_fuel="50").json()
    first = body["fuel_stops"][0]
    assert not first["listed"] and first["mile_marker"] == 0
    assert 4 < first["gallons"] < 6  # 50 mi in the tank, ~98 mi to the first listed station


def test_coordinates_far_from_any_road_are_rejected(client, corridor):
    from fuel.models import ZipCode

    ZipCode.objects.create(code="60000", lat=41.3, lon=-87.6)
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    resp = get(client, start="41.3,-87.6", finish="Omaha, NE")  # the mocked route starts ~21 mi south
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "not_near_a_road"


def test_repeat_trip_is_served_from_the_plan_cache(client, corridor, osrm):
    first = get(client, start="Chicago, IL", finish="Omaha, NE").json()
    again = get(client, start="chicago", finish="Omaha, NE").json()  # same point, typed differently
    assert first["meta"]["plan_cache_hit"] is False
    assert again["meta"]["plan_cache_hit"] is True and again["meta"]["external_api_calls"]["routing"] == 0
    assert again["fuel_stops"] == first["fuel_stops"] and again["total_fuel_cost"] == first["total_fuel_cost"]
    assert again["start"]["query"] == "chicago" and "start=chicago" in again["map"]["url"]
    other = get(client, start="Chicago, IL", finish="Omaha, NE", stop_cost="0").json()
    assert other["meta"]["plan_cache_hit"] is False  # different parameters, different plan


def test_home_opens_the_empty_map(client):
    # Not a pre-filled trip: just opening the site must never spend a routing call.
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"] == "/map"
