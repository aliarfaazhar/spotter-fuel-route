"""Orchestrates one request: resolve -> route (cached; the only routing call) -> match -> optimise -> response."""

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.db.models import Min

from fuel.models import Station

from . import routing
from .geometry import haversine_miles, simplify
from .optimizer import Candidate, InfeasibleRoute, Purchase, naive_plan, plan_fuel_stops
from .resolver import Point, resolve
from .stations import RouteMatches, StationIndex, get_index, match_route

log = logging.getLogger("fuel.planner")
CENT = Decimal("0.01")
GALLON = Decimal("0.001")
ORIGIN_RADII_MILES = (10.0, 25.0)


class PlanError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass(frozen=True)
class StartFuel:
    """`empty`: the truck fills up at a station near the start before leaving. Otherwise miles already in the tank."""

    mode: str  # "empty" | "full" | "miles"
    miles: float


def _route_cache_key(start: Point, end: Point, provider: str) -> str:
    coords = ":".join(f"{v:.5f}" for v in (*start.as_tuple(), *end.as_tuple()))
    return f"route:v{settings.FUEL['ROUTE_CACHE_VERSION']}:{provider}:{coords}"


def get_route(start: Point, end: Point) -> tuple[routing.Route, int, bool]:
    """Cached route lookup. A cache outage degrades to an uncached call instead of failing the request."""
    preferred = routing.provider_name()
    key = _route_cache_key(start, end, preferred)
    try:
        cached = cache.get(key)
    except Exception:
        log.warning("route cache unavailable; continuing uncached", exc_info=True)
        cached = None
    if cached is not None:
        return cached, 0, True

    route, calls = routing.fetch_route(start.as_tuple(), end.as_tuple())
    # Cache a simplified line (~20 m tolerance): matching resamples every half mile, so nothing is lost.
    stored = routing.Route(
        coords=simplify(route.coords, tolerance_deg=0.0002),
        distance_miles=route.distance_miles,
        duration_hours=route.duration_hours,
        provider=route.provider,
    )
    ttl = settings.FUEL["ROUTE_CACHE_TTL_SECONDS"]
    if route.provider != preferred:  # a fallback route: don't let it stand in for the preferred one for long
        ttl = settings.FUEL["FALLBACK_ROUTE_CACHE_TTL_SECONDS"]
    try:
        cache.set(key, stored, ttl)
    except Exception:
        log.warning("route cache unavailable; not caching", exc_info=True)
    return stored, calls, False


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP) + 0  # "+ 0" turns -0.00 into 0.00


def _bill(purchases: list[Purchase], mpg: float) -> list[tuple[Purchase, Decimal, Decimal]]:
    """(purchase, gallons, cost) with gallons rounded to 3 dp and each stop's cost rounded to the cent."""
    out = []
    for p in purchases:
        gallons = (Decimal(repr(p.miles)) / Decimal(repr(mpg))).quantize(GALLON, rounding=ROUND_HALF_UP)
        out.append((p, gallons, _money(gallons * Decimal(repr(p.candidate.price)))))
    return out


def plan_trip(
    start_text: str,
    finish_text: str,
    start_fuel: StartFuel,
    stop_cost: float,
    include_route: bool = True,
    map_base_url: str = "",
) -> dict:
    cfg = settings.FUEL
    tank, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["MILES_PER_GALLON"]
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    start, start_calls = resolve(start_text)
    finish, finish_calls = resolve(finish_text)
    timings["resolve"] = time.perf_counter() - t0
    calls = {"routing": 0, "geocoding": start_calls + finish_calls}
    warnings: list[dict] = []

    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise PlanError(400, "same_location", "start and finish are the same place")

    t = time.perf_counter()
    route, calls["routing"], cache_hit = get_route(start, finish)
    timings["routing"] = time.perf_counter() - t

    t = time.perf_counter()
    index = get_index()
    buffers = cfg["BUFFER_STEPS_MILES"]
    matches = match_route(route, index, max(buffers))
    origin = _origin_station(index, start, start_fuel, warnings, tank)
    fuel_miles = 0.0 if origin is not None else (tank if start_fuel.mode == "empty" else start_fuel.miles)
    timings["match"] = time.perf_counter() - t

    t = time.perf_counter()
    purchases, buffer_used, infeasible = None, None, None
    for buffer in buffers:
        candidates = _candidates(matches, index, buffer, origin)
        try:
            purchases = plan_fuel_stops(candidates, route.distance_miles, tank, fuel_miles, mpg, stop_cost)
            buffer_used = buffer
            break
        except InfeasibleRoute as exc:
            infeasible = exc
    if purchases is None:
        raise PlanError(
            422,
            "no_fuel_coverage",
            f"the fuel price list has no station to cover part of this route: {infeasible}",
            {
                "last_reachable_station_mile": round(infeasible.stranded_at_mile, 1),
                "next_station_mile": round(infeasible.next_point_mile, 1),
                "gap_miles": round(infeasible.gap_miles, 1),
                "route_miles": round(route.distance_miles, 1),
                "searched_up_to_miles_off_route": max(buffers),
            },
        )
    if buffer_used != buffers[0]:
        warnings.append(
            {
                "code": "wide_station_search",
                "message": f"part of the route has no station within {buffers[0]:g} mi; stations up to "
                f"{buffer_used:g} mi off-route were considered (detours are included in fuel and cost)",
            }
        )
    final = _candidates(matches, index, buffer_used, origin)
    cheapest = _bill(plan_fuel_stops(final, route.distance_miles, tank, fuel_miles, mpg, 0.0), mpg)
    try:
        naive = _bill(naive_plan(final, route.distance_miles, tank, fuel_miles), mpg)
    except InfeasibleRoute:
        naive = None
    timings["optimise"] = time.perf_counter() - t

    billed = _bill(purchases, mpg)
    total_cost = sum((cost for _, _, cost in billed), Decimal("0"))
    cheapest_cost = min(sum((cost for _, _, cost in cheapest), Decimal("0")), total_cost)
    stops = _stops(billed, index, start, mpg)
    timings["total"] = time.perf_counter() - t0

    query = {"start": start_text, "finish": finish_text}
    if start_fuel.mode != "empty":
        query["start_fuel"] = start_fuel.mode if start_fuel.mode == "full" else f"{start_fuel.miles:g}"
    if stop_cost != cfg["STOP_COST_USD"]:
        query["stop_cost"] = f"{stop_cost:g}"
    response = {
        "start": start.to_dict(start_text),
        "finish": finish.to_dict(finish_text),
        "distance_miles": round(route.distance_miles, 1),
        "duration_hours": round(route.duration_hours, 2),
        "fuel_stops": stops,
        "total_fuel_cost": total_cost,
        "total_gallons": float(sum((gal for _, gal, _ in billed), Decimal("0"))),
        "cheapest_possible_fuel_cost": cheapest_cost,
        "savings_vs_naive": (
            _money(sum((cost for _, _, cost in naive), Decimal("0")) - total_cost) if naive is not None else None
        ),
        "map": {"url": f"{map_base_url}?{urlencode(query)}" if map_base_url else None},
        "assumptions": {
            "vehicle_range_miles": tank,
            "miles_per_gallon": mpg,
            "start_fuel": start_fuel.mode if start_fuel.mode != "miles" else f"{start_fuel.miles:g} miles",
            "stop_cost_usd": stop_cost,
            "station_search_miles_off_route": buffer_used,
            "station_price": "lowest quote per station in the price list",
            "station_location": "centre of the station's town (the price list has no coordinates)",
        },
        "warnings": warnings,
        "meta": {
            "routing_provider": route.provider,
            "external_api_calls": calls,
            "route_cache_hit": cache_hit,
            "timings_ms": {k: round(v * 1000, 1) for k, v in timings.items()},
        },
    }
    if include_route:
        line = simplify(route.coords)
        response["map"]["route"] = {
            "type": "LineString",
            "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in line],
        }
    log.info(
        "plan miles=%.0f stops=%d cost=%s cache_hit=%s calls=%s ms=%s",
        route.distance_miles,
        len(stops),
        total_cost,
        cache_hit,
        calls,
        response["meta"]["timings_ms"],
    )
    return response


def _origin_station(index: StationIndex, start: Point, fuel: StartFuel, warnings: list, tank: float):
    """For start_fuel=empty: the cheapest station close to the start, where the truck fills up before leaving."""
    if fuel.mode != "empty":
        return None
    for radius in ORIGIN_RADII_MILES:
        near_idx, near_dist = index.within(start.lat, start.lon, radius)
        if near_idx.size:
            j = int(np.argmin(index.price[near_idx]))
            return int(near_idx[j]), float(near_dist[j])
    warnings.append(
        {
            "code": "no_station_near_start",
            "message": f"no station within {ORIGIN_RADII_MILES[-1]:g} mi of the start; "
            f"assumed the vehicle leaves with a full tank ({tank:g} mi)",
        }
    )
    return None


def _candidates(matches: RouteMatches, index: StationIndex, buffer: float, origin) -> list[Candidate]:
    ok = matches.off_route_miles <= buffer
    out = [
        Candidate(key=int(i), mile=float(m), price=float(index.price[i]), off=float(o))
        for i, m, o in zip(matches.station_idx[ok], matches.mile[ok], matches.off_route_miles[ok], strict=True)
        if origin is None or int(i) != origin[0]
    ]
    if origin is not None:  # filling up before departure: no detour on the route
        out.append(Candidate(key=("origin", origin[0]), mile=0.0, price=float(index.price[origin[0]])))
    return out


def _stops(billed, index: StationIndex, start: Point, mpg: float) -> list[dict]:
    def station_idx(key) -> int:
        return key[1] if isinstance(key, tuple) else key

    ids = [int(index.ids[station_idx(p.candidate.key)]) for p, _, _ in billed]
    stations = {
        s.opis_id: s
        for s in Station.objects.filter(opis_id__in=ids).select_related("place").annotate(price=Min("prices__price"))
    }
    stops = []
    for n, ((p, gallons, cost), opis_id) in enumerate(zip(billed, ids, strict=True), 1):
        s = stations[opis_id]
        is_origin = isinstance(p.candidate.key, tuple)
        off_route = haversine_miles(start.lat, start.lon, s.place.lat, s.place.lon) if is_origin else p.candidate.off
        stops.append(
            {
                "stop": n,
                "opis_id": s.opis_id,
                "name": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "highway": s.highway or None,
                "exit": s.exit_number or None,
                "lat": round(s.place.lat, 6),
                "lon": round(s.place.lon, 6),
                "mile_marker": round(p.candidate.mile, 1),
                "off_route_miles": round(float(off_route), 1) + 0.0,
                "price_per_gallon": float(s.price.normalize()),
                "fuel_on_arrival_gallons": round(p.fuel_on_arrival_miles / mpg, 2) + 0.0,
                "gallons": float(gallons),
                "cost": cost,
            }
        )
    return stops
