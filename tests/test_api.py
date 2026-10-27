from decimal import Decimal

import httpx
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
    assert Decimal(str(body["total_fuel_cost"])) == sum(Decimal(str(s["cost"])) for s in stops)
    for s in stops:  # each stop's cost is exactly its gallons x the price shown, to the cent
        expected = (Decimal(str(s["gallons"])) * Decimal(str(s["price_per_gallon"]))).quantize(Decimal("0.01"))
        assert Decimal(str(s["cost"])) == expected
    assert body["total_gallons"] == pytest.approx(sum(s["gallons"] for s in stops))
    assert body["total_fuel_cost"] >= body["cheapest_possible_fuel_cost"]
    assert body["map"]["url"].startswith("http://testserver/map?")
    assert body["map"]["route"]["type"] == "LineString"  # the map of the route is in the response


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


def test_coverage_gap_is_a_422_with_details(client, corridor):
    from fuel.models import Station

    Station.objects.filter(opis_id__in=[2, 3, 4]).delete()  # the last station is ~565 mi short of the finish
    long = [[lon, 41.0] for lon in (-87.6, -96.0, -106.0)]
    from fuel.models import ZipCode

    ZipCode.objects.create(code="80000", lat=41.0, lon=-106.0)
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body(long, meters=1_420_000)))
    resp = get(client, start="Chicago, IL", finish="41.0,-106.0", start_fuel="0")
    assert resp.status_code == 422, resp.json()
    err = resp.json()["error"]
    assert err["code"] == "no_fuel_coverage" and err["details"]["gap_miles"] > 0


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
