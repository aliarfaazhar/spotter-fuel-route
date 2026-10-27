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
from .optimizer import (
    Candidate,
    InfeasibleRoute,
    cost_of,
    naive_plan,
    plan_fuel_stops,
    plan_fuel_stops_with_stop_cost,
)
from .resolver import Point, resolve
from .stations import RouteMatches, get_index, match_route

log = logging.getLogger("fuel.planner")
CENT = Decimal("0.01")


class PlanError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass(frozen=True)
class StartFuel:
    mode: str  # "empty" | "full" | "miles"
    miles: float

    @classmethod
    def parse(cls, value, tank: float) -> "StartFuel":
        text = str(value if value is not None else "empty").strip().lower()
        if text in ("", "empty"):
            return cls("empty", 0.0)
        if text == "full":
            return cls("full", tank)
        try:
            miles = float(text)
        except ValueError:
            raise PlanError(400, "invalid_start_fuel", "start_fuel must be 'empty', 'full' or miles of range") from None
        if not 0 <= miles <= tank:
            raise PlanError(400, "invalid_start_fuel", f"start_fuel must be between 0 and {tank:g} miles")
        return cls("miles", miles)


def _route_cache_key(start: Point, end: Point) -> str:
    cfg = settings.FUEL
    coords = ":".join(f"{v:.5f}" for v in (*start.as_tuple(), *end.as_tuple()))
    return f"route:v{cfg['ROUTE_CACHE_VERSION']}:{routing.provider_name()}:{coords}"


def get_route(start: Point, end: Point) -> tuple[routing.Route, int, bool]:
    key = _route_cache_key(start, end)
    cached = cache.get(key)
    if cached is not None:
        return cached, 0, True
    route, calls = routing.fetch_route(start.as_tuple(), end.as_tuple())
    cache.set(key, route, settings.FUEL["ROUTE_CACHE_TTL_SECONDS"])
    return route, calls, False


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def plan_trip(start_text: str, finish_text: str, start_fuel=None, stop_cost=None, include_route: bool = False,
              map_base_url: str = "") -> dict:  # fmt: skip
    cfg = settings.FUEL
    tank, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["MILES_PER_GALLON"]
    stop_cost = cfg["STOP_COST_USD"] if stop_cost is None else float(stop_cost)
    if not 0 <= stop_cost <= 1000:
        raise PlanError(400, "invalid_stop_cost", "stop_cost must be between 0 and 1000 USD")
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    fuel = StartFuel.parse(start_fuel, tank)
    start, start_calls = resolve(start_text)
    finish, finish_calls = resolve(finish_text)
    timings["resolve"] = time.perf_counter() - t0
    calls = {"routing": 0, "geocoding": start_calls + finish_calls}
    warnings: list[dict] = []

    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < 0.1:
        raise PlanError(400, "same_location", "start and finish are the same place")

    t = time.perf_counter()
    route, routing_calls, cache_hit = get_route(start, finish)
    calls["routing"] = routing_calls
    timings["routing"] = time.perf_counter() - t

    t = time.perf_counter()
    index = get_index()
    buffers = cfg["BUFFER_STEPS_MILES"]
    matches = match_route(route, index, max(buffers))

    origin = None
    if fuel.mode == "empty":
        near_idx, near_dist = index.within(start.lat, start.lon, cfg["ORIGIN_STATION_RADIUS_MILES"])
        if near_idx.size:
            best = int(near_idx[np.argmin(index.price[near_idx])])
            origin = (best, float(near_dist[near_idx == best][0]))
        else:
            fuel = StartFuel("full", tank)
            warnings.append({
                "code": "no_station_near_start",
                "message": f"no station within {cfg['ORIGIN_STATION_RADIUS_MILES']:g} mi of the start; "
                           "assumed the vehicle leaves with a full tank",
            })  # fmt: skip
    timings["match"] = time.perf_counter() - t

    t = time.perf_counter()
    purchases, buffer_used, infeasible = None, None, None
    for buffer in buffers:
        candidates = _candidates(matches, index, buffer, origin)
        try:
            purchases = plan_fuel_stops_with_stop_cost(
                candidates, route.distance_miles, tank, fuel.miles, mpg, stop_cost
            )
            buffer_used = buffer
            break
        except InfeasibleRoute as exc:
            infeasible = exc
    if purchases is None:
        raise PlanError(
            422,
            "no_fuel_coverage",
            f"the fuel price list has no station to cover part of this route: {infeasible}",
            {"stranded_at_mile": round(infeasible.stranded_at_mile, 1),
             "next_station_mile": round(infeasible.next_point_mile, 1),
             "gap_miles": round(infeasible.gap_miles, 1),
             "route_miles": round(route.distance_miles, 1), "search_buffer_miles": max(buffers)},
        )  # fmt: skip
    if buffer_used != buffers[0]:
        warnings.append({
            "code": "wide_station_search",
            "message": f"no station within {buffers[0]:g} mi of the route covered a stretch; "
                       f"searched up to {buffer_used:g} mi off-route",
        })  # fmt: skip
    final = _candidates(matches, index, buffer_used, origin)
    cheapest_cost = cost_of(plan_fuel_stops(final, route.distance_miles, tank, fuel.miles), mpg)
    try:
        naive_cost = cost_of(naive_plan(final, route.distance_miles, tank, fuel.miles), mpg)
    except InfeasibleRoute:
        naive_cost = None
    timings["optimise"] = time.perf_counter() - t

    stops = _stops(purchases, index, matches, origin, mpg)
    total_cost = sum((s["cost"] for s in stops), Decimal("0"))
    purchased_gallons = sum((Decimal(str(s["gallons"])) for s in stops), Decimal("0"))
    timings["total"] = time.perf_counter() - t0

    query = {"start": start_text, "finish": finish_text}
    if start_fuel is not None:
        query["start_fuel"] = str(start_fuel)
    if stop_cost != cfg["STOP_COST_USD"]:
        query["stop_cost"] = f"{stop_cost:g}"
    response = {
        "start": start.to_dict(start_text),
        "finish": finish.to_dict(finish_text),
        "distance_miles": round(route.distance_miles, 1),
        "duration_hours": round(route.duration_hours, 2),
        "fuel_stops": stops,
        "total_fuel_cost": _money(total_cost),
        "fuel_purchased_gallons": float(purchased_gallons),
        "trip_fuel_gallons": round(route.distance_miles / mpg, 3),
        "savings_vs_naive": _money(Decimal(str(naive_cost)) - total_cost) if naive_cost is not None else None,
        "cheapest_possible_fuel_cost": _money(Decimal(str(cheapest_cost))),
        "assumptions": {
            "vehicle_range_miles": tank,
            "miles_per_gallon": mpg,
            "start_fuel": fuel.mode if fuel.mode != "miles" else f"{fuel.miles:g} miles",
            "stop_cost_usd": stop_cost,
            "station_search_buffer_miles": buffer_used,
            "station_price": "lowest quote per station in the feed",
            "station_location": "centre of the station's town (the feed has no coordinates)",
        },
        "warnings": warnings,
        "map_url": f"{map_base_url}?{urlencode(query)}" if map_base_url else None,
        "google_maps_url": _google_maps_url(start, finish, stops),
        "routing_provider": route.provider,
        "external_api_calls": calls,
        "route_cache_hit": cache_hit,
        "timings_ms": {k: round(v * 1000, 1) for k, v in timings.items()},
    }
    if include_route:
        line = simplify(route.coords)
        response["route"] = {"type": "LineString", "coordinates": [[round(lo, 5), round(la, 5)] for la, lo in line]}
    log.info(
        "plan start=%r finish=%r miles=%.0f stops=%d cost=%s cache_hit=%s calls=%s ms=%s",
        start_text, finish_text, route.distance_miles, len(stops), response["total_fuel_cost"], cache_hit, calls,
        response["timings_ms"],
    )  # fmt: skip
    return response


def _candidates(matches: RouteMatches, index, buffer: float, origin) -> list[Candidate]:
    ok = matches.off_route_miles <= buffer
    out = [
        Candidate(key=int(i), mile=float(m), price=float(index.price[i]))
        for i, m in zip(matches.station_idx[ok], matches.mile[ok], strict=True)
        if origin is None or int(i) != origin[0]
    ]
    if origin is not None:
        out.append(Candidate(key=origin[0], mile=0.0, price=float(index.price[origin[0]])))
    return out


def _stops(purchases, index, matches: RouteMatches, origin, mpg: float) -> list[dict]:
    ids = [int(index.ids[p.candidate.key]) for p in purchases]
    stations = {
        s.opis_id: s
        for s in Station.objects.filter(opis_id__in=ids).select_related("place").annotate(price=Min("prices__price"))
    }
    off_route = dict(zip(matches.station_idx.tolist(), matches.off_route_miles.tolist(), strict=True))
    if origin is not None:
        off_route[origin[0]] = origin[1]
    stops = []
    for n, (p, opis_id) in enumerate(zip(purchases, ids, strict=True), 1):
        s = stations[opis_id]
        gallons = (Decimal(str(p.miles)) / Decimal(str(mpg))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        price = s.price.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        stops.append({
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
            "off_route_miles": round(off_route.get(p.candidate.key, 0.0), 1),
            "price_per_gallon": float(price),
            "fuel_on_arrival_gallons": round(p.fuel_on_arrival_miles / mpg, 2),
            "gallons": float(gallons),
            "cost": _money(gallons * s.price),
        })  # fmt: skip
    return stops


def _google_maps_url(start: Point, finish: Point, stops: list[dict]) -> str:
    params = {"api": "1", "origin": f"{start.lat:.5f},{start.lon:.5f}",
              "destination": f"{finish.lat:.5f},{finish.lon:.5f}", "travelmode": "driving"}  # fmt: skip
    waypoints = "|".join(f"{s['lat']},{s['lon']}" for s in stops[:9])  # Google allows up to 9 waypoints
    if waypoints:
        params["waypoints"] = waypoints
    return "https://www.google.com/maps/dir/?" + urlencode(params)
