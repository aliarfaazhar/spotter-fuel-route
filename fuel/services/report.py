"""Turns a finished fuel plan into the pieces of the API response: billed stops, trip cost, route options, warnings.

Money is Decimal from here on: each stop's gallons are rounded to 3 dp and its cost to the cent, and every total
is the sum of the rounded stop figures, so the response always adds up.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import numpy as np
from django.conf import settings
from django.db.models import Min

from fuel.models import Station

from .candidates import is_on_route, is_origin, is_unlisted, station_idx
from .geometry import chord_to_miles, cumulative_miles, haversine_miles, simplify, to_xyz
from .optimizer import Purchase
from .resolver import Point
from .routing import Route
from .stations import StationIndex

if TYPE_CHECKING:
    from .planner import Attempt

CENT = Decimal("0.01")
GALLON = Decimal("0.001")
ROUNDING_SLACK_GALLONS = Decimal("0.01")
NEAREST_FOR_REFERENCE = 5

Bill = list[tuple[Purchase, Decimal, Decimal]]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP) + 0  # "+ 0" turns -0.00 into 0.00


def bill(purchases: list[Purchase], mpg: float) -> Bill:
    """(purchase, gallons, cost) with gallons rounded to 3 dp and each stop's cost rounded to the cent."""
    out = []
    for p in purchases:
        gallons = (Decimal(repr(p.miles)) / Decimal(repr(mpg))).quantize(GALLON, rounding=ROUND_HALF_UP)
        out.append((p, gallons, money(gallons * Decimal(repr(p.candidate.price)))))
    return out


def listed_cost(billed: Bill) -> Decimal:
    return sum((cost for p, _, cost in billed if not is_unlisted(p.candidate)), Decimal("0"))


def unpriced_gallons(billed: Bill) -> Decimal:
    return sum((gal for p, gal, _ in billed if is_unlisted(p.candidate)), Decimal("0"))


def total_gallons(billed: Bill) -> Decimal:
    return sum((gal for _, gal, _ in billed), Decimal("0"))


def geojson(coords: np.ndarray) -> dict:
    line = simplify(coords, tolerance_deg=0.0003)  # ~30 m: follows the road at street zoom (NYC->LA ~75 KB)
    return {"type": "LineString", "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in line]}


def trip_fuel_cost(billed: Bill, bought_usd: Decimal, chosen: Attempt, index: StationIndex, start: Point) -> dict:
    """What all the fuel the trip burns is worth, not just what's bought on the way.

    total_fuel_cost is the money spent at listed stops. A trip that starts with fuel in the tank (or buys some
    off the list) burns more than that; this values those gallons at a reference price so the trip has a cost.
    """
    mpg = settings.FUEL["MILES_PER_GALLON"]
    detours = sum(2 * p.candidate.off for p, _, _ in billed if is_on_route(p.candidate))
    used = (Decimal(repr((chosen.route.distance_miles + detours) / mpg))).quantize(GALLON, rounding=ROUND_HALF_UP)
    from_tank = used - total_gallons(billed)
    if from_tank < ROUNDING_SLACK_GALLONS:  # per-stop rounding can leave a stray 0.001 gal
        from_tank = Decimal("0")
    estimated = from_tank + unpriced_gallons(billed)
    out = {
        "usd": bought_usd,
        "gallons_used": float(used),
        "bought_at_stops_usd": bought_usd,
        "estimated_gallons": float(estimated),
        "estimate_price_per_gallon": None,
        "estimate_basis": None,
    }
    if estimated == 0:
        return out
    price, basis = _reference_price(index, start, chosen)
    if price is None:
        out["usd"] = None
        return out
    out |= {
        "usd": money(bought_usd + estimated * Decimal(repr(price))),
        "estimate_price_per_gallon": price,
        "estimate_basis": basis,
    }
    return out


def _reference_price(index: StationIndex, start: Point, chosen: Attempt) -> tuple[float | None, str | None]:
    """The price used for fuel the plan doesn't buy at a listed stop: near the start, else typical on the route."""
    radius = settings.FUEL["ORIGIN_STATION_RADII_MILES"][-1]
    near_idx, _ = index.within(start.lat, start.lon, radius)
    if near_idx.size:
        basis = f"cheapest listed station within {radius:g} mi of the start"
        return round(float(index.price[near_idx].min()), 3), basis
    on_route = chosen.matches.station_idx[chosen.matches.off_route_miles <= (chosen.buffer or 0)]
    if on_route.size:
        return round(float(np.median(index.price[on_route])), 3), "median listed price along the route"
    if len(index):  # nothing listed nearby at all (most of California): the closest listed stations, wherever
        k = min(NEAREST_FOR_REFERENCE, len(index))
        dist, idx = index.tree.query(to_xyz(start.lat, start.lon)[0], k=k)
        idx, far = np.atleast_1d(idx), float(chord_to_miles(np.atleast_1d(dist).max()))
        basis = f"median of the {k} nearest listed stations ({far:.0f} mi away at most)"
        return round(float(np.median(index.price[idx])), 3), basis
    return None, None


def _gap_dict(a: Attempt, include_route: bool) -> dict:
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
        out["route"] = geojson(coords)
    return out


def route_options(direct: Attempt, detour: Attempt | None, chosen: Attempt, include: bool) -> list[dict]:
    """The direct route first, then any detour; exactly one is `selected` (the one the plan above uses)."""
    tank = settings.FUEL["VEHICLE_RANGE_MILES"]

    def base(a: Attempt, name: str) -> dict:
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
            d["route"] = geojson(a.route.coords)
        return d

    first = base(direct, "Direct route")
    if direct.gap is None:
        first |= {"status": "ok", "note": "Shortest route"}
    else:
        g = direct.gap
        first |= {
            "status": "out_of_range" if chosen is not direct else "unlisted_fuel_needed",
            "note": f"Shortest route, but out of range on listed stations: between mile {g.stranded_at_mile:.0f} "
            f"and mile {g.next_point_mile:.0f} no listed station is in range ({g.gap_miles:.0f} mi of road, plus the "
            f"drive off it to the stations; a tank covers {tank:g} mi)",
            "gap": _gap_dict(direct, include),
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
                "gap": _gap_dict(detour, include),
            }
        options.append(d)
    return options


def warnings(direct: Attempt, chosen: Attempt, start_unlisted: bool, billed: Bill) -> list[dict]:
    tank = settings.FUEL["VEHICLE_RANGE_MILES"]
    first_buffer = settings.FUEL["BUFFER_STEPS_MILES"][0]
    out = []
    if chosen is not direct:
        g = direct.gap
        out.append(
            {
                "code": "detour_for_fuel_coverage",
                "message": f"on the direct route, no listed station is in range between mile "
                f"{g.stranded_at_mile:.0f} and mile {g.next_point_mile:.0f} (a tank covers {tank:g} mi); "
                f"this plan goes via {' and '.join(w.label for w in chosen.via)} "
                f"(+{chosen.route.distance_miles - direct.route.distance_miles:.0f} mi)",
            }
        )
    for p, gallons, _ in billed:
        if not is_unlisted(p.candidate):
            continue
        if p.candidate.mile == 0 and start_unlisted:
            radius = settings.FUEL["ORIGIN_STATION_RADII_MILES"][-1]
            message = (
                f"no station in the price list within {radius:g} mi of the start: buy {gallons} gal "
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


def stops(billed: Bill, index: StationIndex, start: Point, route: Route) -> list[dict]:
    """One entry per purchase, in order, with the station's details (one query for all of them)."""
    mpg = settings.FUEL["MILES_PER_GALLON"]
    ids = [int(index.ids[station_idx(p.candidate)]) for p, _, _ in billed if not is_unlisted(p.candidate)]
    stations = {
        s.opis_id: s
        for s in Station.objects.filter(opis_id__in=ids).select_related("place").annotate(price=Min("prices__price"))
    }
    cum = cumulative_miles(route.coords, route.distance_miles)

    def on_route(mile: float) -> tuple[float, float]:
        return float(np.interp(mile, cum, route.coords[:, 0])), float(np.interp(mile, cum, route.coords[:, 1]))

    out = []
    for n, (p, gallons, cost) in enumerate(billed, 1):
        stop = {
            "stop": n,
            "listed": not is_unlisted(p.candidate),
            "mile_marker": round(p.candidate.mile, 1),
            "fuel_on_arrival_gallons": round(p.fuel_on_arrival_miles / mpg, 2) + 0.0,
            "gallons": float(gallons),
        }
        if not stop["listed"]:  # any station near this point of the route; nothing in the price list reaches it
            lat, lon = on_route(p.candidate.mile)
            stop |= {
                "route_lat": round(lat, 6),
                "route_lon": round(lon, 6),
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
            s = stations[int(index.ids[station_idx(p.candidate)])]
            origin = is_origin(p.candidate)
            off = haversine_miles(start.lat, start.lon, s.place.lat, s.place.lon) if origin else p.candidate.off
            # lat/lon is the station's town; route_lat/lon is where the route passes it (the exit to take).
            # The fill-up before departure is near the start, not on the route, so it keeps the town.
            route_lat, route_lon = (s.place.lat, s.place.lon) if origin else on_route(p.candidate.mile)
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
                "route_lat": round(route_lat, 6),
                "route_lon": round(route_lon, 6),
                "off_route_miles": round(float(off), 1) + 0.0,
                "price_per_gallon": float(s.price.normalize()),
                "cost": cost,
            }
        out.append(stop)
    return out
