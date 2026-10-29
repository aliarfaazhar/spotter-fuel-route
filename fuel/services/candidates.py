"""The places the optimiser may buy fuel at, and how to tell them apart afterwards.

Three kinds of candidate, told apart by their key:
- a listed station on the route: key is the station's position in the StationIndex (an int);
- the fill-up before departure (start_fuel=empty): ("origin", index position), at mile 0 with no detour;
- fuel bought off the price list where nothing listed is in range: ("unlisted", mile).
"""

import numpy as np
from django.conf import settings

from .geometry import chord_to_miles, to_xyz
from .optimizer import Candidate
from .resolver import Point
from .stations import RouteMatches, StationIndex

# Fuel bought off the list is planned at the typical listed price plus this margin, so a listed station is used
# whenever it saves real money, but the plan never adds a stop just to avoid buying a gallon more off the list.
UNLISTED_PREMIUM_PER_GALLON = 1.0
UNLISTED_FALLBACK_PRICE = 1000.0  # only when the price list has no station at all
UNLISTED_SPACING_MILES = 10.0
# Off-list points every 10 miles share one price, so with stop_cost=0 any split of that fuel costs the same; a
# token cost per stop picks the plan with the fewest stops among them.
UNLISTED_TIE_BREAK_STOP_COST = 0.01
NEAREST_FOR_REFERENCE = 5


def is_unlisted(c: Candidate) -> bool:
    return isinstance(c.key, tuple) and c.key[0] == "unlisted"


def is_origin(c: Candidate) -> bool:
    return isinstance(c.key, tuple) and c.key[0] == "origin"


def is_on_route(c: Candidate) -> bool:
    """A listed station along the route, reached by a detour off it (not the origin, not unlisted fuel)."""
    return not isinstance(c.key, tuple)


def station_idx(c: Candidate) -> int:
    """Position in the StationIndex of a listed candidate (on the route or the origin fill-up)."""
    return c.key[1] if is_origin(c) else c.key


def origin_station(index: StationIndex, start: Point, mode: str) -> tuple[int, float] | None:
    """For start_fuel=empty: the cheapest station close to the start, where the truck fills up before leaving."""
    if mode != "empty":
        return None
    for radius in settings.FUEL["ORIGIN_STATION_RADII_MILES"]:
        near_idx, near_dist = index.within(start.lat, start.lon, radius)
        if near_idx.size:
            j = int(np.argmin(index.price[near_idx]))
            return int(near_idx[j]), float(near_dist[j])
    return None


def reference_price(
    index: StationIndex, start: Point, matches: RouteMatches, buffer: float | None
) -> tuple[float | None, str | None]:
    """A typical listed price for fuel the plan doesn't buy at a listed stop: near the start, else on the route."""
    radius = settings.FUEL["ORIGIN_STATION_RADII_MILES"][-1]
    near_idx, _ = index.within(start.lat, start.lon, radius)
    if near_idx.size:
        basis = f"cheapest listed station within {radius:g} mi of the start"
        return round(float(index.price[near_idx].min()), 3), basis
    on_route = matches.station_idx[matches.off_route_miles <= (buffer or 0)]
    if on_route.size:
        return round(float(np.median(index.price[on_route])), 3), "median listed price along the route"
    if len(index):  # nothing listed nearby at all (most of California): the closest listed stations, wherever
        k = min(NEAREST_FOR_REFERENCE, len(index))
        dist, idx = index.tree.query(to_xyz(start.lat, start.lon)[0], k=k)
        idx, far = np.atleast_1d(idx), float(chord_to_miles(np.atleast_1d(dist).max()))
        basis = f"median of the {k} nearest listed stations ({far:.0f} mi away at most)"
        return round(float(np.median(index.price[idx])), 3), basis
    return None, None


def unlisted_price(index: StationIndex, start: Point, matches: RouteMatches, buffer: float) -> float:
    price, _ = reference_price(index, start, matches, buffer)
    return UNLISTED_FALLBACK_PRICE if price is None else price + UNLISTED_PREMIUM_PER_GALLON


def stop_cost_for(cands: list[Candidate], stop_cost: float) -> float:
    if stop_cost < UNLISTED_TIE_BREAK_STOP_COST and any(is_unlisted(c) for c in cands):
        return UNLISTED_TIE_BREAK_STOP_COST
    return stop_cost


def unlisted_every(distance_miles: float) -> list[float]:
    """Mile markers for off-list fuel along a whole route, when no listed-only plan exists."""
    return np.arange(0.0, distance_miles, UNLISTED_SPACING_MILES).tolist()


def build(
    matches: RouteMatches,
    index: StationIndex,
    buffer: float,
    origin,
    unlisted=(),
    unlisted_at: float = UNLISTED_FALLBACK_PRICE,  # planning price per gallon for off-list fuel (see unlisted_price)
) -> list[Candidate]:
    ok = matches.off_route_miles <= buffer
    out = [
        Candidate(key=int(i), mile=float(m), price=float(index.price[i]), off=float(o))
        for i, m, o in zip(matches.station_idx[ok], matches.mile[ok], matches.off_route_miles[ok], strict=True)
        if origin is None or int(i) != origin[0]
    ]
    if origin is not None:  # filling up before departure: no detour on the route
        out.append(Candidate(key=("origin", origin[0]), mile=0.0, price=float(index.price[origin[0]])))
    out.extend(Candidate(key=("unlisted", float(m)), mile=float(m), price=unlisted_at) for m in unlisted)
    return out
