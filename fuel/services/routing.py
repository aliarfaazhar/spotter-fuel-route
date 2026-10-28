"""The only module that talks to a routing API.

OpenRouteService (truck profile) when ORS_API_KEY is set, otherwise the keyless public OSRM server, so the
project runs with zero signup. If ORS is down or times out we make one fallback call to OSRM.
Both send the route as an encoded polyline (lat, lon), a sixth of the size of GeoJSON.
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


# One client per process, so back-to-back requests reuse the open TLS connection (~0.3 s each).
# retries=1 covers connection failures only (the request never reached the server), never a sent request.
_http = httpx.Client(transport=httpx.HTTPTransport(retries=1), headers={"User-Agent": "spotter-fuel-route/1.0"})


def _timeout(read_timeout: float) -> httpx.Timeout:
    return httpx.Timeout(read_timeout, connect=settings.ROUTING["CONNECT_TIMEOUT"])


def decode_polyline(encoded: str, precision: int) -> np.ndarray:
    """Decode a Google encoded polyline into an (N, 2) array of (lat, lon).

    Providers send the route this way at a sixth of the size of GeoJSON, which roughly halves the download.
    """
    if not isinstance(encoded, str) or not encoded.isascii():
        raise ValueError("polyline must be an ASCII string")
    chunks = np.frombuffer(encoded.encode(), dtype=np.uint8).astype(np.int64) - 63
    if chunks.size == 0 or chunks.min() < 0 or chunks.max() > 63 or chunks[-1] >= 0x20:
        raise ValueError("malformed polyline")
    last = chunks < 0x20  # a chunk without the continuation bit ends a value
    starts = np.flatnonzero(np.r_[True, last[:-1]])
    position = np.arange(chunks.size) - np.repeat(starts, np.diff(np.r_[starts, chunks.size]))
    if position.max() > 11:  # a value longer than any coordinate can be
        raise ValueError("malformed polyline")
    values = np.add.reduceat((chunks & 0x1F) << (5 * position), starts)
    values = np.where(values & 1, ~(values >> 1), values >> 1)  # zigzag: the low bit is the sign
    if values.size % 2:
        raise ValueError("polyline has an odd number of values")
    return np.cumsum(values.reshape(-1, 2), axis=0) / 10.0**precision


def _json(resp: httpx.Response, provider: str) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise RoutingError(f"{provider} returned an unexpected response (HTTP {resp.status_code})")
    return payload


def _route(polyline, precision: int, meters, seconds, provider: str) -> Route:
    """Validate and decode a provider's route."""
    try:
        coords = decode_polyline(polyline, precision)
        distance = float(meters) / METERS_PER_MILE
        duration = float(seconds) / 3600.0
    except (TypeError, ValueError, IndexError) as exc:
        raise RoutingError(f"{provider} returned a malformed route") from exc
    if len(coords) < 2:
        raise RoutingError(f"{provider} returned a route without geometry")
    return Route(coords=coords, distance_miles=distance, duration_hours=duration, provider=provider)


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
        resp = _http.post(
            f"{cfg['ORS_BASE_URL']}/v2/directions/{profile}/json",  # /json: polyline geometry, /geojson: 6x larger
            json=body,
            headers={"Authorization": cfg["ORS_API_KEY"]},
            timeout=_timeout(cfg["ORS_READ_TIMEOUT"]),
        )
    except httpx.HTTPError as exc:
        raise ProviderDown(f"OpenRouteService unreachable: {type(exc).__name__}") from exc

    # A used-up daily quota or a revoked key shouldn't take the API down: OSRM can still route the trip.
    if resp.status_code == 429:
        raise ProviderDown("OpenRouteService rate limit or daily quota reached")
    if resp.status_code >= 500:
        raise ProviderDown(f"OpenRouteService returned {resp.status_code}")
    if resp.status_code in (401, 403):
        raise ProviderDown("OpenRouteService rejected the API key")
    payload = _json(resp, "OpenRouteService")
    if resp.status_code != 200:
        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if err.get("code") in (2004, 2009, 2010) or resp.status_code == 404:
            raise NoRoute(f"no drivable route between these points ({err.get('message', resp.status_code)})")
        raise RoutingError(f"OpenRouteService error {resp.status_code}")

    provider = f"openrouteservice:{profile}"
    try:
        route = payload["routes"][0]
        summary = route["summary"]
        return _route(route["geometry"], 5, summary["distance"], summary["duration"], provider)
    except (KeyError, IndexError, TypeError) as exc:
        raise RoutingError("OpenRouteService returned a malformed route") from exc


def _osrm(points: list[tuple[float, float]]) -> Route:
    base = settings.ROUTING["OSRM_BASE_URL"]
    url = f"{base}/route/v1/driving/" + ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        resp = _http.get(
            url,
            params={"overview": "full", "geometries": "polyline6"},
            timeout=_timeout(settings.ROUTING["OSRM_READ_TIMEOUT"]),
        )
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
        return _route(route["geometry"], 6, route["distance"], route["duration"], "osrm:driving")
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
