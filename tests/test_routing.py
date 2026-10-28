import json

import httpx
import numpy as np
import pytest
import respx

from fuel.services import routing

START, END = (41.0, -87.6), (41.0, -96.0)
ORS_URL = "https://api.heigit.org/openrouteservice/v2/directions/driving-hgv/json"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"


def line(start=START, end=END, n=200):
    lons = np.linspace(start[1], end[1], n)
    return [[float(lon), start[0]] for lon in lons]  # [lon, lat], the order providers use for coordinates


def encode_polyline(lonlat, precision):
    """Google's polyline encoding of [lon, lat] points: the reference algorithm, independent of the decoder."""
    out, prev = [], (0, 0)
    for lon, lat in lonlat:
        point = (round(lat * 10**precision), round(lon * 10**precision))
        for value, last in zip(point, prev, strict=True):
            v = value - last
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = point
    return "".join(out)


def ors_body(coords=None, meters=708_000.0):
    geometry = encode_polyline(coords or line(), 5)
    return {"routes": [{"geometry": geometry, "summary": {"distance": meters, "duration": 25_000.0}}]}


def osrm_body(coords=None, meters=708_000.0):
    geometry = encode_polyline(coords or line(), 6)
    return {"code": "Ok", "routes": [{"geometry": geometry, "distance": meters, "duration": 25_000.0}]}


def test_decode_polyline_matches_googles_example():
    # https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    coords = routing.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", 5)
    assert np.allclose(coords, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])


@pytest.mark.parametrize("precision", [5, 6])
def test_decode_polyline_round_trips(precision):
    rng = np.random.default_rng(precision)
    lats, lons = rng.uniform(25, 49, 300), rng.uniform(-124, -67, 300)
    pts = [[float(lon), float(lat)] for lat, lon in zip(lats, lons, strict=True)]
    coords = routing.decode_polyline(encode_polyline(pts, precision), precision)
    assert np.allclose(coords, [[lat, lon] for lon, lat in pts], atol=10**-precision)


@pytest.mark.parametrize("bad", ["", "_p~iF~ps|U_", "_p~iF", "ab c", "é", None, 12, "~~~~~~~~~~~~~~?"])
def test_decode_polyline_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        routing.decode_polyline(bad, 5)


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


def test_waypoints_are_sent_in_order(ors_key):
    route_mock = respx.post(ORS_URL).mock(return_value=httpx.Response(200, json=ors_body()))
    routing.fetch_route(START, END, via=((43.0, -90.0), (42.0, -93.0)))
    body = json.loads(route_mock.calls.last.request.content)
    assert body["coordinates"] == [[-87.6, 41.0], [-90.0, 43.0], [-93.0, 42.0], [-96.0, 41.0]]
    assert body["radiuses"] == [-1] * 4


def test_osrm_waypoints_in_the_url():
    mock = respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    routing.fetch_route(START, END, via=((43.0, -90.0),))
    assert mock.calls.last.request.url.path.endswith("/-87.6,41.0;-90.0,43.0;-96.0,41.0")
    assert mock.calls.last.request.url.params["geometries"] == "polyline6"  # 1e-6 precision, unlike ORS's 1e-5


def test_no_key_uses_osrm():
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    route, calls = routing.fetch_route(START, END)
    assert calls == 1 and route.provider == "osrm:driving"


@pytest.mark.parametrize("status", [503, 429, 401, 403])  # outage, used-up quota, bad key
def test_ors_failure_falls_back_to_osrm_once(ors_key, status):
    respx.post(ORS_URL).mock(return_value=httpx.Response(status))
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    route, calls = routing.fetch_route(START, END)
    assert calls == 2 and route.provider == "osrm:driving"


@pytest.mark.parametrize("code", [2004, 2009, 2010])
def test_ors_unroutable_points_map_to_no_route(ors_key, code):
    respx.post(ORS_URL).mock(return_value=httpx.Response(404, json={"error": {"code": code, "message": "x"}}))
    with pytest.raises(routing.NoRoute):
        routing.fetch_route(START, END)


def test_rate_limit_on_both_providers(ors_key):
    respx.post(ORS_URL).mock(return_value=httpx.Response(429))
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(429))
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
        httpx.Response(200, json={"code": "Ok", "routes": [{"geometry": "", "distance": 1, "duration": 1}]}),
        httpx.Response(200, json={"code": "Ok", "routes": [{"geometry": "_p~iF~ps|U", "distance": 1, "duration": 1}]}),
        httpx.Response(200, json={"code": "Ok", "routes": [{"geometry": {"coordinates": []}, "distance": 1}]}),
        httpx.Response(200, json={"code": "Ok", "routes": [{}]}),
        httpx.Response(200, text="not json"),
    ],
)
def test_malformed_provider_responses_are_routing_errors(response):
    respx.get(url__startswith=OSRM_URL).mock(return_value=response)
    with pytest.raises(routing.RoutingError):
        routing.fetch_route(START, END)
