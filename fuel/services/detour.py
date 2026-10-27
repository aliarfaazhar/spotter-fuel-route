"""A detour through listed stations, for routes where the price list leaves a stretch longer than a tank.

The price list is thin in places (California has 8 stations, all near the Mexican border), so the shortest route
can be impossible to fuel from it. We then look for waypoints that keep every hop within range:
  - Towns with a listed station are grouped into ~0.5 degree cells, one node per cell.
  - Two nodes are linked when the straight-line distance, scaled up to an estimated road distance, fits in a tank.
  - Dijkstra from start to finish gives the shortest such chain. Nodes already near the direct route are dropped,
    so the router is only pinned where the trip actually has to leave it.
The estimate can be wrong in either direction, so the caller routes through the waypoints (one routing call)
and re-plans on the real road distances before using it.
"""

import threading
from dataclasses import dataclass, field

import numpy as np
from django.db.models import Count
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from fuel.models import Place

from .geometry import chord_to_miles, miles_to_chord, to_xyz
from .resolver import Point
from .routing import Route
from .stations import resample

CELL_DEG = 0.5
# Road miles per straight-line mile. The planner tries the tighter estimate first (shorter detours) and, if the
# real route still has a gap, the cautious one: at most two extra routing calls.
ROAD_FACTORS = (1.2, 1.4)
HOP_SHARE = 0.95  # plan hops at 95% of the range
ON_ROUTE_MILES = 10.0
MAX_WAYPOINTS = 6


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    label: str

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class _Towns:
    lat: np.ndarray
    lon: np.ndarray
    labels: list[str]
    xyz: np.ndarray = field(repr=False)
    tree: cKDTree = field(repr=False)


_towns: _Towns | None = None
_lock = threading.Lock()


def reset() -> None:
    global _towns
    _towns = None


def _get_towns() -> _Towns:
    global _towns
    if _towns is None:
        with _lock:
            if _towns is None:
                _towns = _build_towns()
    return _towns


def _build_towns() -> _Towns:
    """One node per grid cell: the town in it with the most listed stations."""
    rows = (
        Place.objects.annotate(n=Count("stations"))
        .filter(n__gt=0)
        .order_by("-n", "id")
        .values_list("lat", "lon", "name", "state")
    )
    cells: dict[tuple[int, int], tuple[float, float, str]] = {}
    for lat, lon, name, state in rows:
        cells.setdefault((int(lat // CELL_DEG), int(lon // CELL_DEG)), (lat, lon, f"{name}, {state}"))
    picked = list(cells.values())
    lat = np.array([p[0] for p in picked], dtype=float)
    lon = np.array([p[1] for p in picked], dtype=float)
    xyz = to_xyz(lat, lon) if len(picked) else np.empty((0, 3))
    return _Towns(lat, lon, [p[2] for p in picked], xyz, cKDTree(xyz if len(xyz) else np.zeros((1, 3))))


def find_waypoints(
    start: Point, finish: Point, direct: Route, start_range: float, tank: float, road_factor: float
) -> list[Waypoint]:
    """Waypoints for a route whose every hop between listed stations fits in a tank; [] if there is none."""
    towns = _get_towns()
    n = len(towns.labels)
    if n == 0:
        return []
    hop = miles_to_chord(tank * HOP_SHARE / road_factor)
    first_hop = miles_to_chord(start_range * HOP_SHARE / road_factor)

    pairs = towns.tree.sparse_distance_matrix(towns.tree, hop, output_type="ndarray")
    rows, cols, chords = [pairs["i"]], [pairs["j"]], [pairs["v"]]
    s, f = n, n + 1
    for node, point, radius, outgoing in ((s, start, first_hop, True), (f, finish, hop, False)):
        here = to_xyz(point.lat, point.lon)[0]
        near = np.asarray(towns.tree.query_ball_point(here, radius), dtype=int)
        if near.size == 0:
            return []
        dist = np.linalg.norm(towns.xyz[near] - here, axis=1)
        rows.append(np.full(near.size, node) if outgoing else near)
        cols.append(near if outgoing else np.full(near.size, node))
        chords.append(dist)
    weights = chord_to_miles(np.concatenate(chords)) + 1e-3  # +epsilon: a zero weight would mean "no edge"
    graph = coo_matrix((weights, (np.concatenate(rows), np.concatenate(cols))), shape=(n + 2, n + 2)).tocsr()

    dist, pred = dijkstra(graph, directed=True, indices=s, return_predecessors=True)
    if not np.isfinite(dist[f]):
        return []
    path, node = [], pred[f]
    while node != s:
        path.append(int(node))
        node = pred[node]
    path.reverse()

    _, pts = resample(direct, spacing=1.0)
    along = cKDTree(to_xyz(pts[:, 0], pts[:, 1]))
    off, _ = along.query(towns.xyz[path]) if path else (np.empty(0), None)
    keep = [i for i, o in zip(path, np.atleast_1d(chord_to_miles(off)), strict=True) if o > ON_ROUTE_MILES]
    if not keep or len(keep) > MAX_WAYPOINTS:
        return []
    return [Waypoint(float(towns.lat[i]), float(towns.lon[i]), towns.labels[i]) for i in keep]
