"""Orchestrates one request: resolve -> route (cached) -> match -> optimise -> response.

A normal trip makes one routing call. If the price list leaves a stretch of the route longer than a tank, a second
call routes a detour through listed stations; if there's no such detour, the plan says where to buy fuel off the list.
"""

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np
from django.conf import settings

from . import candidates, report, routing
from .caching import safe_add, safe_delete, safe_get, safe_set
from .detour import ROAD_FACTORS, Waypoint, find_waypoints
from .geometry import haversine_miles, simplify
from .optimizer import Candidate, InfeasibleRoute, Purchase, naive_plan, plan_fuel_stops
from .resolver import Point, resolve
from .stations import RouteMatches, get_index, match_route

log = logging.getLogger("fuel.planner")
OFF_ROAD_WARN_MILES, OFF_ROAD_REJECT_MILES = 2.0, 10.0
# How long a request waits for another one already fetching the same route, before fetching it itself.
# Above the worst case for one fetch (ORS, then the OSRM fallback): see ROUTING timeouts.
ROUTE_LOCK_SECONDS = 50.0
ROUTE_LOCK_POLL_SECONDS = 0.05


class PlanError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass(frozen=True)
class StartFuel:
    """`empty`: the truck fills up at a station near the start before leaving. Otherwise miles already in the tank."""

    mode: str  # "empty" | "full" | "miles"
    miles: float


def _route_cache_key(start: Point, end: Point, provider: str, via: tuple = ()) -> str:
    points = (start.as_tuple(), *(w.as_tuple() for w in via), end.as_tuple())
    coords = ":".join(f"{v:.5f}" for p in points for v in p)
    return f"route:v{settings.FUEL['ROUTE_CACHE_VERSION']}:{provider}:{coords}"


def _ttl(provider: str) -> int:
    """A route (or plan) from the fallback provider mustn't stand in for the preferred one for long."""
    if provider != routing.provider_name():
        return settings.FUEL["FALLBACK_ROUTE_CACHE_TTL_SECONDS"]
    return settings.FUEL["ROUTE_CACHE_TTL_SECONDS"]


def get_route(start: Point, end: Point, via: tuple = ()) -> tuple[routing.Route, int, bool]:
    """Cached route lookup. A cache outage degrades to an uncached call instead of failing the request."""
    key = _route_cache_key(start, end, routing.provider_name(), via)
    cached = safe_get(key)
    if cached is not None:
        return cached, 0, True

    # Many requests for the same new route at once (a popular lane, a load test) make one routing call:
    # the first takes the lock and fetches, the rest wait for its result instead of calling the provider too.
    lock = key + ":lock"
    deadline = time.monotonic() + ROUTE_LOCK_SECONDS
    while not safe_add(lock, 1, ROUTE_LOCK_SECONDS):
        if time.monotonic() > deadline:
            break  # the holder is stuck: fetch anyway rather than fail
        time.sleep(ROUTE_LOCK_POLL_SECONDS)
        cached = safe_get(key)
        if cached is not None:
            return cached, 0, True
    try:
        return _fetch_and_cache(key, start, end, via)
    finally:
        safe_delete(lock)  # on failure too, so a waiting request takes over at once


def _fetch_and_cache(key: str, start: Point, end: Point, via: tuple) -> tuple[routing.Route, int, bool]:
    route, calls = routing.fetch_route(start.as_tuple(), end.as_tuple(), tuple(w.as_tuple() for w in via))
    # Cache a simplified line (~20 m tolerance): matching resamples every half mile, so nothing is lost.
    stored = routing.Route(
        coords=simplify(route.coords, tolerance_deg=0.0002),
        distance_miles=route.distance_miles,
        duration_hours=route.duration_hours,
        provider=route.provider,
    )
    safe_set(key, stored, _ttl(route.provider))
    return stored, calls, False


def _plan_cache_key(start: Point, finish: Point, start_fuel: StartFuel, stop_cost: float, include_route: bool):
    ends = ":".join(f"{v:.5f}" for v in (*start.as_tuple(), *finish.as_tuple()))
    params = f"{start_fuel.mode}:{start_fuel.miles:g}:{stop_cost:g}:{int(include_route)}"
    return f"plan:v{settings.FUEL['ROUTE_CACHE_VERSION']}:{routing.provider_name()}:{ends}:{params}"


@dataclass
class Attempt:
    """One route and the fuel plan on it (or the gap that made planning impossible)."""

    route: routing.Route
    via: tuple[Waypoint, ...]
    matches: RouteMatches
    candidates: list[Candidate] | None = None
    purchases: list[Purchase] | None = None
    buffer: float | None = None
    gap: InfeasibleRoute | None = None  # set when no listed-only plan exists on this route


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
    buffers = cfg["BUFFER_STEPS_MILES"]
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    start, start_calls = resolve(start_text)
    finish, finish_calls = resolve(finish_text)
    timings["resolve"] = time.perf_counter() - t0
    calls = {"routing": 0, "geocoding": start_calls + finish_calls}
    warnings: list[dict] = []

    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise PlanError(400, "same_location", "start and finish are the same place")

    query = {"start": start_text, "finish": finish_text}
    if start_fuel.mode != "empty":
        query["start_fuel"] = start_fuel.mode if start_fuel.mode == "full" else f"{start_fuel.miles:g}"
    if stop_cost != cfg["STOP_COST_USD"]:
        query["stop_cost"] = f"{stop_cost:g}"
    map_url = f"{map_base_url}?{urlencode(query)}" if map_base_url else None

    # A repeat of the same trip is answered from the finished plan: no routing, matching or optimising.
    plan_key = _plan_cache_key(start, finish, start_fuel, stop_cost, include_route)
    cached = safe_get(plan_key)
    if cached is not None:
        timings["total"] = time.perf_counter() - t0
        return cached | {
            "start": start.to_dict(start_text),
            "finish": finish.to_dict(finish_text),
            "map": {**cached["map"], "url": map_url},
            "meta": {
                "routing_provider": cached["meta"]["routing_provider"],
                "external_api_calls": {"routing": 0, "geocoding": calls["geocoding"]},
                "route_cache_hit": True,
                "plan_cache_hit": True,
                "timings_ms": {k: round(v * 1000, 1) for k, v in timings.items()},
            },
        }

    t = time.perf_counter()
    route, calls["routing"], cache_hit = get_route(start, finish)
    timings["routing"] = time.perf_counter() - t
    _check_snapping(start, route.coords[0], "start", warnings)
    _check_snapping(finish, route.coords[-1], "finish", warnings)

    t = time.perf_counter()
    index = get_index()
    origin = candidates.origin_station(index, start, start_fuel.mode)
    # No listed station near the start: the first fill-up (empty tank) or top-up (part-full tank) is bought off
    # the list, only as much as it takes to reach a listed station.
    near_start, _ = index.within(start.lat, start.lon, cfg["ORIGIN_STATION_RADII_MILES"][-1])
    start_unlisted = start_fuel.mode != "full" and origin is None and near_start.size == 0
    fuel_miles = start_fuel.miles if start_fuel.mode != "empty" else 0.0

    def attempt(r: routing.Route, via: tuple = ()) -> Attempt:
        a = Attempt(route=r, via=via, matches=match_route(r, index, max(buffers)))
        unlisted = [0.0] if start_unlisted else []
        for buffer in buffers:
            cands = candidates.build(a.matches, index, buffer, origin, unlisted)
            try:
                a.purchases = plan_fuel_stops(cands, r.distance_miles, tank, fuel_miles, mpg, stop_cost)
                a.candidates, a.buffer, a.gap = cands, buffer, None
                return a
            except InfeasibleRoute as exc:
                a.gap = exc
        return a

    direct = attempt(route)
    chosen = direct
    timings["optimise"] = time.perf_counter() - t

    detour = None
    if direct.purchases is None:
        # The shortest route has a stretch no tank covers on listed stations: look for a detour that doesn't.
        t = time.perf_counter()
        start_range = tank if start_unlisted or fuel_miles <= 0 else fuel_miles
        tried: set[tuple] = set()
        for factor in ROAD_FACTORS:
            via = tuple(find_waypoints(start, finish, route, start_range, tank, factor))
            if not via or via in tried:
                continue
            tried.add(via)
            detour_route, detour_calls, detour_hit = get_route(start, finish, via)
            calls["routing"] += detour_calls
            cache_hit = cache_hit and detour_hit
            detour = attempt(detour_route, via)
            if detour.purchases is not None:
                chosen = detour
                break
        timings["detour"] = time.perf_counter() - t

    t = time.perf_counter()
    if chosen.purchases is None:
        # No detour either: plan the direct route, buying off the list only where nothing listed is in range.
        unlisted = candidates.unlisted_every(route.distance_miles)
        chosen.buffer = buffers[0]
        chosen.candidates = candidates.build(direct.matches, index, buffers[0], origin, unlisted)
        chosen.purchases = plan_fuel_stops(chosen.candidates, route.distance_miles, tank, fuel_miles, mpg, stop_cost)

    final = chosen.candidates
    uses_unlisted = any(candidates.is_unlisted(c) for c in final)
    cheapest = report.bill(plan_fuel_stops(final, chosen.route.distance_miles, tank, fuel_miles, mpg, 0.0), mpg)
    naive = None
    if not uses_unlisted:  # a price-blind plan would buy a different amount off the list: not comparable
        try:
            naive = report.bill(naive_plan(final, chosen.route.distance_miles, tank, fuel_miles), mpg)
        except InfeasibleRoute:
            pass
    timings["optimise"] += time.perf_counter() - t

    billed = report.bill(chosen.purchases, mpg)
    total_cost = report.listed_cost(billed)
    cheapest_cost = min(report.listed_cost(cheapest), total_cost)
    unpriced = report.unpriced_gallons(billed)
    stops = report.stops(billed, index, start, chosen.route)
    trip_cost = report.trip_fuel_cost(billed, total_cost, chosen, index, start)
    options = report.route_options(direct, detour, chosen, include_route)
    warnings.extend(report.warnings(direct, chosen, start_unlisted, billed))
    timings["total"] = time.perf_counter() - t0

    response = {
        "start": start.to_dict(start_text),
        "finish": finish.to_dict(finish_text),
        "distance_miles": round(chosen.route.distance_miles, 1),
        "duration_hours": round(chosen.route.duration_hours, 2),
        "fuel_stops": stops,
        "total_fuel_cost": total_cost,
        "total_gallons": float(report.total_gallons(billed)),
        "unpriced_gallons": float(unpriced),
        "trip_fuel_cost": trip_cost,
        "cost_is_complete": unpriced == 0,
        "cheapest_possible_fuel_cost": cheapest_cost,
        "savings_vs_naive": report.money(report.listed_cost(naive) - total_cost) if naive is not None else None,
        "route_options": options,
        "map": {"url": map_url},
        "assumptions": {
            "vehicle_range_miles": tank,
            "miles_per_gallon": mpg,
            "start_fuel": start_fuel.mode if start_fuel.mode != "miles" else f"{start_fuel.miles:g} miles",
            "stop_cost_usd": stop_cost,
            "station_search_miles_off_route": chosen.buffer,
            "station_price": "lowest quote per station in the price list",
            "station_location": "centre of the station's town (the price list has no coordinates)",
        },
        "warnings": warnings,
        "meta": {
            "routing_provider": chosen.route.provider,
            "external_api_calls": calls,
            "route_cache_hit": cache_hit,
            "plan_cache_hit": False,
            "timings_ms": {k: round(v * 1000, 1) for k, v in timings.items()},
        },
    }
    if include_route:
        response["map"]["route"] = report.geojson(chosen.route.coords)
    safe_set(plan_key, response, _ttl(chosen.route.provider))
    log.info(
        "plan miles=%.0f stops=%d cost=%s unpriced_gal=%s detour=%s cache_hit=%s calls=%s ms=%s",
        chosen.route.distance_miles,
        len(stops),
        total_cost,
        unpriced,
        bool(chosen.via),
        cache_hit,
        calls,
        response["meta"]["timings_ms"],
    )
    return response


def _check_snapping(point: Point, on_road: np.ndarray, which: str, warnings: list) -> None:
    """A typed lat,lon in a lake gets snapped to the nearest road by the router; say so, or refuse if it's far."""
    if point.resolved_by not in ("coordinates", "geocoder"):
        return
    moved = float(haversine_miles(point.lat, point.lon, on_road[0], on_road[1]))
    if moved > OFF_ROAD_REJECT_MILES:
        raise PlanError(
            400,
            "not_near_a_road",
            f"the {which} is {moved:.0f} mi from the nearest road; is it on land?",
            {which: moved},
        )
    if moved > OFF_ROAD_WARN_MILES:
        warnings.append(
            {"code": "moved_to_road", "message": f"the {which} was moved {moved:.1f} mi to the nearest road"}
        )
