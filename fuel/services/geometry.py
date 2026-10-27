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
    """Ramer-Douglas-Peucker on lat/lon (planar approximation is fine for display geometry)."""
    n = len(coords)
    if n < 3:
        return coords
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        a, b = coords[start], coords[end]
        seg = coords[start + 1 : end]
        ab = b - a
        norm = np.hypot(*ab)
        if norm == 0:
            dist = np.hypot(*(seg - a).T)
        else:
            dist = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / norm
        i = int(np.argmax(dist))
        if dist[i] > tolerance_deg:
            mid = start + 1 + i
            keep[mid] = True
            stack.extend([(start, mid), (mid, end)])
    return coords[keep]
