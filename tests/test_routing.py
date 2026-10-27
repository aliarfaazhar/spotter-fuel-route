import json

import httpx
import numpy as np
import pytest
import respx

from fuel.services import routing

START, END = (41.0, -87.6), (41.0, -96.0)
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"


def line(start=START, end=END, n=200):
    lons = np.linspace(start[1], end[1], n)
    return [[float(lon), start[0]] for lon in lons]  # GeoJSON order: [lon, lat]


def ors_body(coords=None, meters=708_000.0):
    return {
        "features": [
            {
                "geometry": {"coordinates": coords or line()},
                "properties": {"summary": {"distance": meters, "duration": 25_000.0}},
            }
        ]
    }


def osrm_body(coords=None, meters=708_000.0):
    return {
        "code": "Ok",
        "routes": [{"geometry": {"coordinates": coords or line()}, "distance": meters, "duration": 25_000.0}],
    }


@pytest.fixture
def ors_key(settings):
    settings.ROUTING = {**settings.ROUTING, "ORS_API_KEY": "test-key"}


def test_ors_request_and_lonlat_swap(ors_key):
    route_mock = respx.post(ORS_URL).mock(return_value=httpx.Response(200, json=ors_body()))
    route, calls = routing.fetch_route(START, END)

    body = json.loads(route_mock.calls.last.request.content)
    assert body["coordinates"] == [[-87.6, 41.0], [-96.0, 41.0]]
    assert body["radiuses"] == [-1, -1] and body["instructions"] is False
    assert "options" not in body  # options + driving-hgv => ORS's 100 km dynamic-weights limit
    assert calls == 1 and route.provider == "openrouteservice:driving-hgv"
    assert route.distance_miles == pytest.approx(708_000 / 1609.344)
    # (lat, lon) out: the line stays on latitude 41, i.e. inside the US
    assert np.allclose(route.coords[:, 0], 41.0) and route.coords[:, 1].min() == pytest.approx(-96.0)


def test_no_key_uses_osrm():
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    route, calls = routing.fetch_route(START, END)
    assert calls == 1 and route.provider == "osrm:driving"


def test_ors_outage_falls_back_to_osrm_once(ors_key):
    respx.post(ORS_URL).mock(return_value=httpx.Response(503))
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    route, calls = routing.fetch_route(START, END)
    assert calls == 2 and route.provider == "osrm:driving"


@pytest.mark.parametrize("code", [2004, 2009, 2010])
def test_ors_unroutable_points_map_to_no_route(ors_key, code):
    respx.post(ORS_URL).mock(return_value=httpx.Response(404, json={"error": {"code": code, "message": "x"}}))
    with pytest.raises(routing.NoRoute):
        routing.fetch_route(START, END)


def test_rate_limit(ors_key):
    respx.post(ORS_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(routing.RateLimited):
        routing.fetch_route(START, END)


def test_timeout_without_fallback_is_unavailable():
    respx.get(url__startswith=OSRM_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(routing.RoutingError) as exc:
        routing.fetch_route(START, END)
    assert exc.value.status == 502


@pytest.mark.live
def test_live_osrm():
    route, _ = routing.fetch_route((41.8781, -87.6298), (41.2565, -95.9345))
    assert 450 < route.distance_miles < 500


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="<html>blocked</html>"),
        httpx.Response(
            200, json={"code": "Ok", "routes": [{"geometry": {"coordinates": []}, "distance": 1, "duration": 1}]}
        ),
        httpx.Response(200, json={"code": "Ok", "routes": [{}]}),
        httpx.Response(200, text="not json"),
    ],
)
def test_malformed_provider_responses_are_routing_errors(response):
    respx.get(url__startswith=OSRM_URL).mock(return_value=response)
    with pytest.raises(routing.RoutingError):
        routing.fetch_route(START, END)
