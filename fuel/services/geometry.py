"""Spherical geometry helpers on numpy arrays. Coordinates are always (lat, lon) in degrees here."""

import numpy as np

EARTH_RADIUS_MILES = 3958.8


def to_xyz(lat, lon) -> np.ndarray:
    """Unit vectors on the sphere; Euclidean (chord) distance between them is monotonic in great-circle distance."""
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat_r)
    return np.column_stack([cos_lat * np.cos(lon_r), cos_lat * np.sin(lon_r), np.sin(lat_r)])


def miles_to_chord(miles: float) -> float:
    return 2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_miles(coords: np.ndarray, total_miles: float | None = None) -> np.ndarray:
    """Distance along a (lat, lon) polyline at every vertex, optionally scaled to the router's own total."""
    if len(coords) < 2:
        return np.zeros(len(coords))
    legs = haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    cum = np.concatenate([[0.0], np.cumsum(legs)])
    if total_miles is not None and cum[-1] > 0:
        cum *= total_miles / cum[-1]
    return cum


def simplify(coords: np.ndarray, tolerance_deg: float = 0.002) -> np.ndarray:
    """Ramer-Douglas-Peucker on lat/lon (planar approximation is fine for display geometry).

    Splits every open segment of one level in a single vectorised pass instead of one segment at a time: the
    same points are kept, without thousands of small numpy calls on a long route.
    """
    n = len(coords)
    if n < 3:
        return coords
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    starts, ends = np.array([0]), np.array([n - 1])
    while starts.size:
        inner = ends - starts - 1  # points strictly between each segment's ends
        open_ = inner > 0
        starts, ends, inner = starts[open_], ends[open_], inner[open_]
        if not starts.size:
            break
        first = np.cumsum(inner) - inner  # where each segment's points begin in the flat arrays below
        seg = np.repeat(np.arange(starts.size), inner)
        idx = np.repeat(starts + 1, inner) + np.arange(inner.sum()) - np.repeat(first, inner)
        a = coords[starts][seg]
        ab = (coords[ends] - coords[starts])[seg]
        norm = np.hypot(ab[:, 0], ab[:, 1])
        p = coords[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            dist = np.abs(ab[:, 0] * (p[:, 1] - a[:, 1]) - ab[:, 1] * (p[:, 0] - a[:, 0])) / norm
        same_ends = norm == 0
        if same_ends.any():  # a loop back to its start: distance from that point instead
            dist[same_ends] = np.hypot(*(p[same_ends] - a[same_ends]).T)
        # The first farthest point of each segment (argmax semantics), split there if it's beyond tolerance.
        is_max = dist == np.maximum.reduceat(dist, first)[seg]
        far = np.minimum.reduceat(np.where(is_max, np.arange(dist.size), dist.size), first)
        split = dist[far] > tolerance_deg
        mids = idx[far[split]]
        keep[mids] = True
        starts, ends = np.concatenate([starts[split], mids]), np.concatenate([mids, ends[split]])
    return coords[keep]
