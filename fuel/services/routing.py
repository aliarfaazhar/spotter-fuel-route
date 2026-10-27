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


def _client() -> httpx.Client:
    cfg = settings.ROUTING
    timeout = httpx.Timeout(cfg["READ_TIMEOUT"], connect=cfg["CONNECT_TIMEOUT"])
    return httpx.Client(timeout=timeout, headers={"User-Agent": "spotter-fuel-route/1.0"})


def _lonlat_to_latlon(coordinates) -> np.ndarray:
    arr = np.asarray(coordinates, dtype=float)[:, :2]
    return arr[:, ::-1].copy()


def _ors(start: tuple[float, float], end: tuple[float, float]) -> Route:
    cfg = settings.ROUTING
    profile = cfg["ORS_PROFILE"]
    body = {
        "coordinates": [[start[1], start[0]], [end[1], end[0]]],
        "radiuses": [-1, -1],  # snap to the nearest road however far: default 350 m rejects some city points
        "instructions": False,
        "elevation": False,
        # never send "options" with driving-hgv: it switches ORS to a 100 km dynamic-weights limit
    }
    try:
        with _client() as client:
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
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application") else {}
    if resp.status_code != 200:
        err = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
        if err.get("code") in (2004, 2009, 2010) or resp.status_code == 404:
            raise NoRoute(f"no drivable route between these points ({err.get('message', resp.status_code)})")
        if resp.status_code in (401, 403):
            raise RoutingError("OpenRouteService rejected the API key")
        raise RoutingError(f"OpenRouteService error {resp.status_code}")

    feature = payload["features"][0]
    summary = feature["properties"].get("summary", {})
    return Route(
        coords=_lonlat_to_latlon(feature["geometry"]["coordinates"]),
        distance_miles=summary.get("distance", 0.0) / METERS_PER_MILE,
        duration_hours=summary.get("duration", 0.0) / 3600.0,
        provider=f"openrouteservice:{profile}",
    )


def _osrm(start: tuple[float, float], end: tuple[float, float]) -> Route:
    base = settings.ROUTING["OSRM_BASE_URL"]
    url = f"{base}/route/v1/driving/{start[1]},{start[0]};{end[1]},{end[0]}"
    try:
        with _client() as client:
            resp = client.get(url, params={"overview": "full", "geometries": "geojson"})
    except httpx.HTTPError as exc:
        raise ProviderDown(f"OSRM unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 429:
        raise RateLimited("OSRM rate limit reached, retry shortly")
    if resp.status_code >= 500:
        raise ProviderDown(f"OSRM returned {resp.status_code}")
    payload = resp.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        if payload.get("code") in ("NoRoute", "NoSegment", "InvalidValue"):
            raise NoRoute(f"no drivable route between these points ({payload.get('code')})")
        raise RoutingError(f"OSRM error: {payload.get('code', resp.status_code)}")
    route = payload["routes"][0]
    return Route(
        coords=_lonlat_to_latlon(route["geometry"]["coordinates"]),
        distance_miles=route["distance"] / METERS_PER_MILE,
        duration_hours=route["duration"] / 3600.0,
        provider="osrm:driving",
    )


def fetch_route(start: tuple[float, float], end: tuple[float, float]) -> tuple[Route, int]:
    """Return the route and how many routing-API calls it took (1, or 2 when falling back)."""
    if not settings.ROUTING["ORS_API_KEY"]:
        return _osrm(start, end), 1
    try:
        return _ors(start, end), 1
    except ProviderDown as exc:
        log.warning("ORS failed (%s); falling back to OSRM", exc)
        return _osrm(start, end), 2


def provider_name() -> str:
    return f"openrouteservice:{settings.ROUTING['ORS_PROFILE']}" if settings.ROUTING["ORS_API_KEY"] else "osrm:driving"
