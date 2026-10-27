"""Orchestrates one request: resolve -> route (cached) -> match -> optimise -> response.

A normal trip makes one routing call. If the price list leaves a stretch of the route longer than a tank, a second
call routes a detour through listed stations; if there's no such detour, the plan says where to buy fuel off the list.
"""

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
from .detour import ROAD_FACTORS, Waypoint, find_waypoints
from .geometry import cumulative_miles, haversine_miles, simplify
from .optimizer import Candidate, InfeasibleRoute, Purchase, naive_plan, plan_fuel_stops
from .resolver import Point, resolve
from .stations import RouteMatches, StationIndex, get_index, match_route

log = logging.getLogger("fuel.planner")
CENT = Decimal("0.01")
GALLON = Decimal("0.001")
ORIGIN_RADII_MILES = (10.0, 25.0)
UNLISTED_PRICE = 1000.0
UNLISTED_SPACING_MILES = 10.0


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


def get_route(start: Point, end: Point, via: tuple = ()) -> tuple[routing.Route, int, bool]:
    """Cached route lookup. A cache outage degrades to an uncached call instead of failing the request."""
    preferred = routing.provider_name()
    key = _route_cache_key(start, end, preferred, via)
    try:
        cached = cache.get(key)
    except Exception:
        log.warning("route cache unavailable; continuing uncached", exc_info=True)
        cached = None
    if cached is not None:
        return cached, 0, True

    route, calls = routing.fetch_route(start.as_tuple(), end.as_tuple(), tuple(w.as_tuple() for w in via))
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


@dataclass
class _Attempt:
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

    t = time.perf_counter()
    route, calls["routing"], cache_hit = get_route(start, finish)
    timings["routing"] = time.perf_counter() - t

    t = time.perf_counter()
    index = get_index()
    origin = _origin_station(index, start, start_fuel)
    # With an empty tank and no listed station near the start, the first fill-up is bought off the list.
    start_unlisted = start_fuel.mode == "empty" and origin is None
    fuel_miles = start_fuel.miles if start_fuel.mode != "empty" else 0.0

    def attempt(r: routing.Route, via: tuple = ()) -> _Attempt:
        a = _Attempt(route=r, via=via, matches=match_route(r, index, max(buffers)))
        unlisted = [0.0] if start_unlisted else []
        for buffer in buffers:
            cands = _candidates(a.matches, index, buffer, origin, unlisted)
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
        start_range = fuel_miles if fuel_miles > 0 else tank
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
        unlisted = np.arange(0.0, route.distance_miles, UNLISTED_SPACING_MILES).tolist()
        chosen.buffer = buffers[0]
        chosen.candidates = _candidates(direct.matches, index, buffers[0], origin, unlisted)
        chosen.purchases = plan_fuel_stops(chosen.candidates, route.distance_miles, tank, fuel_miles, mpg, stop_cost)

    final = chosen.candidates
    uses_unlisted = any(_is_unlisted(c) for c in final)
    cheapest = _bill(plan_fuel_stops(final, chosen.route.distance_miles, tank, fuel_miles, mpg, 0.0), mpg)
    naive = None
    if not uses_unlisted:  # a price-blind plan would buy a different amount off the list: not comparable
        try:
            naive = _bill(naive_plan(final, chosen.route.distance_miles, tank, fuel_miles), mpg)
        except InfeasibleRoute:
            pass
    timings["optimise"] += time.perf_counter() - t

    billed = _bill(chosen.purchases, mpg)
    total_cost = _listed_cost(billed)
    cheapest_cost = min(_listed_cost(cheapest), total_cost)
    unpriced = sum((gal for p, gal, _ in billed if _is_unlisted(p.candidate)), Decimal("0"))
    stops = _stops(billed, index, start, chosen.route, mpg)
    options = _route_options(direct, detour, chosen, tank, include_route)
    warnings.extend(_warnings(direct, chosen, start_unlisted, billed, buffers[0], tank))
    timings["total"] = time.perf_counter() - t0

    query = {"start": start_text, "finish": finish_text}
    if start_fuel.mode != "empty":
        query["start_fuel"] = start_fuel.mode if start_fuel.mode == "full" else f"{start_fuel.miles:g}"
    if stop_cost != cfg["STOP_COST_USD"]:
        query["stop_cost"] = f"{stop_cost:g}"
    response = {
        "start": start.to_dict(start_text),
        "finish": finish.to_dict(finish_text),
        "distance_miles": round(chosen.route.distance_miles, 1),
        "duration_hours": round(chosen.route.duration_hours, 2),
        "fuel_stops": stops,
        "total_fuel_cost": total_cost,
        "total_gallons": float(sum((gal for _, gal, _ in billed), Decimal("0"))),
        "unpriced_gallons": float(unpriced),
        "cost_is_complete": unpriced == 0,
        "cheapest_possible_fuel_cost": cheapest_cost,
        "savings_vs_naive": _money(_listed_cost(naive) - total_cost) if naive is not None else None,
        "route_options": options,
        "map": {"url": f"{map_base_url}?{urlencode(query)}" if map_base_url else None},
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
            "timings_ms": {k: round(v * 1000, 1) for k, v in timings.items()},
        },
    }
    if include_route:
        response["map"]["route"] = _geojson(chosen.route.coords)
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


def _geojson(coords: np.ndarray) -> dict:
    line = simplify(coords)
    return {"type": "LineString", "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in line]}


def _is_unlisted(c: Candidate) -> bool:
    return isinstance(c.key, tuple) and c.key[0] == "unlisted"


def _listed_cost(billed) -> Decimal:
    return sum((cost for p, _, cost in billed if not _is_unlisted(p.candidate)), Decimal("0"))


def _gap_dict(a: _Attempt, tank: float, include_route: bool) -> dict:
    gap = a.gap
    out = {
        "from_mile": round(gap.stranded_at_mile, 1),
        "to_mile": round(gap.next_point_mile, 1),
        "miles": round(gap.gap_miles, 1),
    }
    if include_route:
        cum = cumulative_miles(a.route.coords, a.route.distance_miles)
        inside = (cum >= gap.stranded_at_mile) & (cum <= gap.next_point_mile)
        ends = [gap.stranded_at_mile, gap.next_point_mile]
        lat = np.interp(ends, cum, a.route.coords[:, 0])
        lon = np.interp(ends, cum, a.route.coords[:, 1])
        coords = np.vstack([[lat[0], lon[0]], a.route.coords[inside], [lat[1], lon[1]]])
        out["route"] = _geojson(coords)
    return out


def _route_options(direct: _Attempt, detour: _Attempt | None, chosen: _Attempt, tank: float, include: bool):
    """The direct route first, then any detour; exactly one is `selected` (the one the plan above uses)."""

    def base(a: _Attempt, name: str) -> dict:
        d = {
            "name": name,
            "via": [w.label for w in a.via],
            "distance_miles": round(a.route.distance_miles, 1),
            "duration_hours": round(a.route.duration_hours, 2),
            "selected": a is chosen,
        }
        if a is not direct:
            d["extra_miles"] = round(a.route.distance_miles - direct.route.distance_miles, 1)
        if include and a is not chosen:  # the chosen route's line is already map.route
            d["route"] = _geojson(a.route.coords)
        return d

    first = base(direct, "Direct route")
    if direct.gap is None:
        first |= {"status": "ok", "note": "Shortest route"}
    else:
        g = direct.gap
        first |= {
            "status": "out_of_range" if chosen is not direct else "unlisted_fuel_needed",
            "note": f"Shortest route, but out of range on listed stations: none between mile "
            f"{g.stranded_at_mile:.0f} and mile {g.next_point_mile:.0f} ({g.gap_miles:.0f} mi; a tank covers "
            f"{tank:g} mi)",
            "gap": _gap_dict(direct, tank, include),
        }
    options = [first]
    if detour is not None:
        d = base(detour, "Via " + " and ".join(w.label for w in detour.via))
        if detour.purchases is not None:
            d |= {"status": "ok", "note": "Every leg is within range of a listed station"}
        else:
            d |= {
                "status": "out_of_range",
                "note": "Tried as a detour, but it still has a gap",
                "gap": _gap_dict(detour, tank, include),
            }
        options.append(d)
    return options


def _warnings(direct: _Attempt, chosen: _Attempt, start_unlisted: bool, billed, first_buffer: float, tank: float):
    out = []
    if chosen is not direct:
        g = direct.gap
        out.append(
            {
                "code": "detour_for_fuel_coverage",
                "message": f"the direct route has no listed station between mile {g.stranded_at_mile:.0f} and "
                f"mile {g.next_point_mile:.0f}, more than a tank ({tank:g} mi); this plan goes via "
                f"{' and '.join(w.label for w in chosen.via)} "
                f"(+{chosen.route.distance_miles - direct.route.distance_miles:.0f} mi)",
            }
        )
    for p, gallons, _ in billed:
        if not _is_unlisted(p.candidate):
            continue
        if p.candidate.mile == 0 and start_unlisted:
            message = (
                f"no station in the price list within {ORIGIN_RADII_MILES[-1]:g} mi of the start: buy {gallons} gal "
                "at any station before leaving (not priced, not in total_fuel_cost)"
            )
        else:
            message = (
                f"no listed station is in range around mile {p.candidate.mile:.0f}: buy {gallons} gal at any "
                "station there (not priced, not in total_fuel_cost)"
            )
        out.append({"code": "unlisted_fuel", "message": message})
    if chosen.buffer is not None and chosen.buffer != first_buffer:
        out.append(
            {
                "code": "wide_station_search",
                "message": f"part of the route has no station within {first_buffer:g} mi; stations up to "
                f"{chosen.buffer:g} mi off-route were considered (detours are included in fuel and cost)",
            }
        )
    return out


def _origin_station(index: StationIndex, start: Point, fuel: StartFuel):
    """For start_fuel=empty: the cheapest station close to the start, where the truck fills up before leaving."""
    if fuel.mode != "empty":
        return None
    for radius in ORIGIN_RADII_MILES:
        near_idx, near_dist = index.within(start.lat, start.lon, radius)
        if near_idx.size:
            j = int(np.argmin(index.price[near_idx]))
            return int(near_idx[j]), float(near_dist[j])
    return None


def _candidates(matches: RouteMatches, index: StationIndex, buffer: float, origin, unlisted=()) -> list[Candidate]:
    ok = matches.off_route_miles <= buffer
    out = [
        Candidate(key=int(i), mile=float(m), price=float(index.price[i]), off=float(o))
        for i, m, o in zip(matches.station_idx[ok], matches.mile[ok], matches.off_route_miles[ok], strict=True)
        if origin is None or int(i) != origin[0]
    ]
    if origin is not None:  # filling up before departure: no detour on the route
        out.append(Candidate(key=("origin", origin[0]), mile=0.0, price=float(index.price[origin[0]])))
    # Fuel bought off the list where nothing listed is in range. The price only steers the optimiser (buy as
    # little of it as possible); it is never shown or billed.
    out.extend(Candidate(key=("unlisted", float(m)), mile=float(m), price=UNLISTED_PRICE) for m in unlisted)
    return out


def _stops(billed, index: StationIndex, start: Point, route: routing.Route, mpg: float) -> list[dict]:
    listed = [p for p, _, _ in billed if not _is_unlisted(p.candidate)]
    ids = [int(index.ids[_station_idx(p.candidate.key)]) for p in listed]
    stations = {
        s.opis_id: s
        for s in Station.objects.filter(opis_id__in=ids).select_related("place").annotate(price=Min("prices__price"))
    }
    cum = cumulative_miles(route.coords, route.distance_miles)
    stops = []
    for n, (p, gallons, cost) in enumerate(billed, 1):
        stop = {
            "stop": n,
            "listed": not _is_unlisted(p.candidate),
            "mile_marker": round(p.candidate.mile, 1),
            "fuel_on_arrival_gallons": round(p.fuel_on_arrival_miles / mpg, 2) + 0.0,
            "gallons": float(gallons),
        }
        if not stop["listed"]:  # any station near this point of the route; nothing in the price list reaches it
            lat = float(np.interp(p.candidate.mile, cum, route.coords[:, 0]))
            lon = float(np.interp(p.candidate.mile, cum, route.coords[:, 1]))
            stop |= {
                "name": None,
                "address": None,
                "city": None,
                "state": None,
                "highway": None,
                "exit": None,
                "opis_id": None,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "off_route_miles": 0.0,
                "price_per_gallon": None,
                "cost": None,
            }
        else:
            s = stations[int(index.ids[_station_idx(p.candidate.key)])]
            is_origin = isinstance(p.candidate.key, tuple)
            off = haversine_miles(start.lat, start.lon, s.place.lat, s.place.lon) if is_origin else p.candidate.off
            stop |= {
                "opis_id": s.opis_id,
                "name": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "highway": s.highway or None,
                "exit": s.exit_number or None,
                "lat": round(s.place.lat, 6),
                "lon": round(s.place.lon, 6),
                "off_route_miles": round(float(off), 1) + 0.0,
                "price_per_gallon": float(s.price.normalize()),
                "cost": cost,
            }
        stops.append(stop)
    return stops


def _station_idx(key) -> int:
    return key[1] if isinstance(key, tuple) else key
