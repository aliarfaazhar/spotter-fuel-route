"""The only module that talks to a routing API.

OpenRouteService (truck profile) when ORS_API_KEY is set, otherwise the keyless public OSRM server, so the
project runs with zero signup. If ORS is down or times out we make one fallback call to OSRM.
Providers speak GeoJSON [lon, lat]; everything leaving this module is (lat, lon).
"""

import logging
from dataclasses import dataclass, field

import httpx
import numpy as np
from django.conf import settings

log = logging.getLogger(__name__)
METERS_PER_MILE = 1609.344


@dataclass(frozen=True)
class Route:
    coords: np.ndarray = field(repr=False)  # (N, 2) lat, lon
    distance_miles: float
    duration_hours: float
    provider: str


class RoutingError(Exception):
    status = 502
    code = "routing_unavailable"


class NoRoute(RoutingError):
    status = 422
    code = "no_route"


class RateLimited(RoutingError):
    status = 503
    code = "routing_rate_limited"


class ProviderDown(RoutingError):
    """Network error, timeout or 5xx: worth one fallback attempt."""


def _client(read_timeout: float) -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=settings.ROUTING["CONNECT_TIMEOUT"])
    return httpx.Client(timeout=timeout, headers={"User-Agent": "spotter-fuel-route/1.0"})


def _json(resp: httpx.Response, provider: str) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise RoutingError(f"{provider} returned an unexpected response (HTTP {resp.status_code})")
    return payload


def _route(coordinates, meters, seconds, provider: str) -> Route:
    """Validate a provider's geometry ([lon, lat] pairs) and convert it to (lat, lon)."""
    try:
        arr = np.asarray(coordinates, dtype=float)[:, :2]
        distance = float(meters) / METERS_PER_MILE
        duration = float(seconds) / 3600.0
    except (TypeError, ValueError, IndexError) as exc:
        raise RoutingError(f"{provider} returned a malformed route") from exc
    if len(arr) < 2 or not np.isfinite(arr).all():
        raise RoutingError(f"{provider} returned a route without geometry")
    return Route(coords=arr[:, ::-1].copy(), distance_miles=distance, duration_hours=duration, provider=provider)


def _ors(points: list[tuple[float, float]]) -> Route:
    cfg = settings.ROUTING
    profile = cfg["ORS_PROFILE"]
    body = {
        "coordinates": [[lon, lat] for lat, lon in points],
        "radiuses": [-1] * len(points),  # snap to the nearest road however far: default 350 m rejects some city points
        "instructions": False,
        "elevation": False,
        # never send "options" with driving-hgv: it switches ORS to a 100 km dynamic-weights limit
    }
    try:
        with _client(cfg["ORS_READ_TIMEOUT"]) as client:
            resp = client.post(
                f"{cfg['ORS_BASE_URL']}/v2/directions/{profile}/geojson",
                json=body,
                headers={"Authorization": cfg["ORS_API_KEY"]},
            )
    except httpx.HTTPError as exc:
        raise ProviderDown(f"OpenRouteService unreachable: {type(exc).__name__}") from exc

    if resp.status_code == 429:
        raise RateLimited("OpenRouteService rate limit reached, retry shortly")
    if resp.status_code >= 500:
        raise ProviderDown(f"OpenRouteService returned {resp.status_code}")
    if resp.status_code in (401, 403):
        raise RoutingError("OpenRouteService rejected the API key")
    payload = _json(resp, "OpenRouteService")
    if resp.status_code != 200:
        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if err.get("code") in (2004, 2009, 2010) or resp.status_code == 404:
            raise NoRoute(f"no drivable route between these points ({err.get('message', resp.status_code)})")
        raise RoutingError(f"OpenRouteService error {resp.status_code}")

    provider = f"openrouteservice:{profile}"
    try:
        feature = payload["features"][0]
        summary = feature["properties"]["summary"]
        return _route(feature["geometry"]["coordinates"], summary["distance"], summary["duration"], provider)
    except (KeyError, IndexError, TypeError) as exc:
        raise RoutingError("OpenRouteService returned a malformed route") from exc


def _osrm(points: list[tuple[float, float]]) -> Route:
    base = settings.ROUTING["OSRM_BASE_URL"]
    url = f"{base}/route/v1/driving/" + ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        with _client(settings.ROUTING["OSRM_READ_TIMEOUT"]) as client:
            resp = client.get(url, params={"overview": "full", "geometries": "geojson"})
    except httpx.HTTPError as exc:
        raise ProviderDown(f"OSRM unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 429:
        raise RateLimited("OSRM rate limit reached, retry shortly")
    if resp.status_code >= 500:
        raise ProviderDown(f"OSRM returned {resp.status_code}")
    payload = _json(resp, "OSRM")
    if payload.get("code") != "Ok" or not payload.get("routes"):
        if payload.get("code") in ("NoRoute", "NoSegment", "InvalidValue"):
            raise NoRoute(f"no drivable route between these points ({payload.get('code')})")
        raise RoutingError(f"OSRM error: {payload.get('code', resp.status_code)}")
    try:
        route = payload["routes"][0]
        return _route(route["geometry"]["coordinates"], route["distance"], route["duration"], "osrm:driving")
    except (KeyError, IndexError, TypeError) as exc:
        raise RoutingError("OSRM returned a malformed route") from exc


def fetch_route(
    start: tuple[float, float], end: tuple[float, float], via: tuple[tuple[float, float], ...] = ()
) -> tuple[Route, int]:
    """Return the route (through `via` in order) and how many routing-API calls it took (1, or 2 when falling back)."""
    points = [start, *via, end]
    if not settings.ROUTING["ORS_API_KEY"]:
        return _osrm(points), 1
    try:
        return _ors(points), 1
    except ProviderDown as exc:
        log.warning("ORS failed (%s); falling back to OSRM", exc)
        return _osrm(points), 2


def provider_name() -> str:
    return f"openrouteservice:{settings.ROUTING['ORS_PROFILE']}" if settings.ROUTING["ORS_API_KEY"] else "osrm:driving"
