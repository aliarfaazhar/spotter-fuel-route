"""In-memory station index and route matching.

All ~6.6k stations are loaded once per process with a single query (cheapest quote per station) into numpy
arrays. Matching builds a KD-tree over the *route* (a few thousand points, ~1 ms) and asks, in one vectorised
call, for every nearby station's closest route point: that gives its mile marker and off-route distance.
"""

import threading
from dataclasses import dataclass, field

import numpy as np
from django.db.models import Min
from scipy.spatial import cKDTree

from fuel.models import Station

from .geometry import EARTH_RADIUS_MILES, chord_to_miles, cumulative_miles, miles_to_chord, to_xyz
from .routing import Route

ROUTE_SAMPLE_SPACING_MILES = 0.5


@dataclass(frozen=True)
class StationIndex:
    ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray  # USD/gal, cheapest quote
    xyz: np.ndarray = field(repr=False)
    tree: cKDTree = field(repr=False)

    def __len__(self) -> int:
        return len(self.ids)

    def within(self, lat: float, lon: float, radius_miles: float) -> tuple[np.ndarray, np.ndarray]:
        """Indices of stations within `radius_miles` of a point, and their distances."""
        idx = np.asarray(self.tree.query_ball_point(to_xyz(lat, lon)[0], miles_to_chord(radius_miles)), dtype=int)
        if idx.size == 0:
            return idx, np.empty(0)
        dist = chord_to_miles(np.linalg.norm(self.xyz[idx] - to_xyz(lat, lon)[0], axis=1))
        return idx, dist


_index: StationIndex | None = None
_lock = threading.Lock()


def get_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = _build_index()
    return _index


def reset_index() -> None:
    global _index
    _index = None


def _build_index() -> StationIndex:
    rows = list(
        Station.objects.annotate(price=Min("prices__price"))
        .order_by("opis_id")
        .values_list("opis_id", "place__lat", "place__lon", "price")
    )
    arr = np.array([(r[0], r[1], r[2], float(r[3])) for r in rows], dtype=float).reshape(-1, 4)
    xyz = to_xyz(arr[:, 1], arr[:, 2]) if len(arr) else np.empty((0, 3))
    return StationIndex(
        ids=arr[:, 0].astype(int), lat=arr[:, 1], lon=arr[:, 2], price=arr[:, 3], xyz=xyz,
        tree=cKDTree(xyz if len(xyz) else np.zeros((1, 3))),
    )  # fmt: skip


@dataclass(frozen=True)
class RouteMatches:
    station_idx: np.ndarray  # positions in the StationIndex
    mile: np.ndarray  # mile marker along the route
    off_route_miles: np.ndarray


def match_route(route: Route, index: StationIndex, max_buffer_miles: float) -> RouteMatches:
    empty = RouteMatches(np.empty(0, int), np.empty(0), np.empty(0))
    if len(route.coords) < 2 or len(index) == 0:
        return empty

    # Resample at a fixed spacing by interpolating along the full-resolution line, scaled to the router's own
    # distance. Interpolating (not picking vertices) matters: straight highway stretches can have vertices
    # miles apart, which would otherwise shift a station's mile marker by that much.
    cum = cumulative_miles(route.coords, route.distance_miles)
    pts_mile = np.append(np.arange(0.0, cum[-1], ROUTE_SAMPLE_SPACING_MILES), cum[-1])
    pts = np.column_stack([np.interp(pts_mile, cum, route.coords[:, 0]), np.interp(pts_mile, cum, route.coords[:, 1])])

    # Cheap bounding-box prefilter before the tree query.
    pad_lat = max_buffer_miles / 69.0
    pad_lon = max_buffer_miles / (69.0 * max(np.cos(np.radians(np.abs(pts[:, 0]).max())), 0.05))
    box = (
        (index.lat >= pts[:, 0].min() - pad_lat) & (index.lat <= pts[:, 0].max() + pad_lat)
        & (index.lon >= pts[:, 1].min() - pad_lon) & (index.lon <= pts[:, 1].max() + pad_lon)
    )  # fmt: skip
    candidates = np.flatnonzero(box)
    if candidates.size == 0:
        return empty

    tree = cKDTree(to_xyz(pts[:, 0], pts[:, 1]))
    dist, nearest = tree.query(index.xyz[candidates], k=1, distance_upper_bound=miles_to_chord(max_buffer_miles))
    hit = np.isfinite(dist)
    return RouteMatches(
        station_idx=candidates[hit],
        mile=pts_mile[nearest[hit]],
        off_route_miles=chord_to_miles(dist[hit]),
    )


__all__ = ["EARTH_RADIUS_MILES", "RouteMatches", "StationIndex", "get_index", "match_route", "reset_index"]
