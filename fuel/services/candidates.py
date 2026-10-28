"""The places the optimiser may buy fuel at, and how to tell them apart afterwards.

Three kinds of candidate, told apart by their key:
- a listed station on the route: key is the station's position in the StationIndex (an int);
- the fill-up before departure (start_fuel=empty): ("origin", index position), at mile 0 with no detour;
- fuel bought off the price list where nothing listed is in range: ("unlisted", mile).
"""

import numpy as np
from django.conf import settings

from .optimizer import Candidate
from .resolver import Point
from .stations import RouteMatches, StationIndex

# Steers the optimiser to buy as little unlisted fuel as possible; never shown or billed.
UNLISTED_PRICE = 1000.0
UNLISTED_SPACING_MILES = 10.0


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


def unlisted_every(distance_miles: float) -> list[float]:
    """Mile markers for off-list fuel along a whole route, when no listed-only plan exists."""
    return np.arange(0.0, distance_miles, UNLISTED_SPACING_MILES).tolist()


def build(matches: RouteMatches, index: StationIndex, buffer: float, origin, unlisted=()) -> list[Candidate]:
    ok = matches.off_route_miles <= buffer
    out = [
        Candidate(key=int(i), mile=float(m), price=float(index.price[i]), off=float(o))
        for i, m, o in zip(matches.station_idx[ok], matches.mile[ok], matches.off_route_miles[ok], strict=True)
        if origin is None or int(i) != origin[0]
    ]
    if origin is not None:  # filling up before departure: no detour on the route
        out.append(Candidate(key=("origin", origin[0]), mile=0.0, price=float(index.price[origin[0]])))
    out.extend(Candidate(key=("unlisted", float(m)), mile=float(m), price=UNLISTED_PRICE) for m in unlisted)
    return out
