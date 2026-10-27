"""Turn what a user types into a point in the US, offline whenever possible.

Order: "lat,lon" -> ZIP code (anywhere in the text) -> "City, ST" / "City, State" / common nicknames -> a
single unambiguous city name -> one geocoder call (counted in the response's API-call budget).
"""

import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx
import numpy as np
from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

from fuel.models import Place, ZipCode

from .geometry import chord_to_miles, to_xyz
from .names import place_key, state_code

NICKNAMES = {
    "nyc": ("New York", "NY"),
    "newyorkcity": ("New York", "NY"),
    "la": ("Los Angeles", "CA"),
    "sf": ("San Francisco", "CA"),
    "dc": ("Washington", "DC"),
    "washingtondc": ("Washington", "DC"),
}
LATLON_RE = re.compile(r"^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$")
ZIP_RE = re.compile(r"(?:^|[\s,])(\d{5})(?:-\d{4})?\s*$")
# needs a separator before it, so "Columbus" keeps its "us"
COUNTRY_SUFFIX_RE = re.compile(r"[\s,]+(usa|u\.s\.a\.?|us|u\.s\.?|united states(?: of america)?)\s*$", re.I)
# Regions that look like "City, Region" but aren't in the US. Checked before geocoding, because a US-only
# geocoder will happily turn "Toronto, Ontario" into "Toronto Paseo, Ontario, California".
CANADA = {
    "AB": "alberta",
    "BC": "british columbia",
    "MB": "manitoba",
    "NB": "new brunswick",
    "NL": "newfoundland and labrador",
    "NS": "nova scotia",
    "NT": "northwest territories",
    "NU": "nunavut",
    "ON": "ontario",
    "PE": "prince edward island",
    "QC": "quebec",
    "SK": "saskatchewan",
    "YT": "yukon",
}
FOREIGN_REGIONS = {"canada", "mexico"} | {c.lower() for c in CANADA} | set(CANADA.values())
# US places with no road link to the mainland: (min_lat, max_lat, min_lon, max_lon)
ISLANDS = {
    "Hawaii": (18.5, 22.5, -161.0, -154.5),
    "Puerto Rico and the US Virgin Islands": (17.5, 18.8, -68.0, -64.4),
    "Guam and the Northern Mariana Islands": (13.0, 21.0, 144.0, 147.0),
}
MAX_MILES_FROM_US_ZIP = 30.0  # a point farther than this from every US ZIP centroid is not in the US
DOMINANCE_RATIO = 3.0  # a bare city name picks the biggest match if it's 3x the population of the next
NOMINATIM = "https://nominatim.openstreetmap.org/search"


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float
    label: str
    resolved_by: str  # coordinates | zip | place | geocoder

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def to_dict(self, query: str) -> dict:
        return {
            "query": query,
            "label": self.label,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "resolved_by": self.resolved_by,
        }


class ResolveError(ValueError):
    def __init__(self, code: str, message: str, candidates: list[str] | None = None, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.candidates, self.status = code, message, candidates or [], status


_zip_tree: tuple[cKDTree, np.ndarray] | None = None


def _zips() -> tuple[cKDTree, np.ndarray]:
    global _zip_tree
    if _zip_tree is None:
        arr = np.array(list(ZipCode.objects.values_list("lat", "lon")), dtype=float).reshape(-1, 2)
        _zip_tree = (cKDTree(to_xyz(arr[:, 0], arr[:, 1]) if len(arr) else np.zeros((1, 3))), arr)
    return _zip_tree


def is_in_us(lat: float, lon: float) -> bool:
    tree, arr = _zips()
    if not len(arr):
        return True
    dist, _ = tree.query(to_xyz(lat, lon)[0])
    return float(chord_to_miles(dist)) <= MAX_MILES_FROM_US_ZIP


def _check_drivable(point: "Point", raw: str) -> "Point":
    # Gazetteer places and ZIPs are US by construction; coordinates and geocoder answers need the border check.
    if point.resolved_by in ("coordinates", "geocoder") and not is_in_us(point.lat, point.lon):
        raise ResolveError("outside_usa", f"'{raw}' is not in the USA")
    for name, (lat0, lat1, lon0, lon1) in ISLANDS.items():
        if lat0 <= point.lat <= lat1 and lon0 <= point.lon <= lon1:
            raise ResolveError("not_reachable_by_road", f"'{raw}' is in {name}, which has no road to the mainland")
    return point


def _place_size(p: Place) -> float:
    """Population decides "the Atlanta people mean" (land area doesn't: rural townships are huge)."""
    return float(p.population or 0)


def _from_place(name: str, state: str | None, raw: str) -> Point | None:
    key = place_key(name)
    if not key:
        return None
    if key in NICKNAMES and (state is None or NICKNAMES[key][1] == state):
        name, state = NICKNAMES[key]
        key = place_key(name)
    qs = Place.objects.filter(key=key)
    if state:
        place = qs.filter(state=state).first()
        return Point(place.lat, place.lon, str(place), "place") if place else None

    matches = sorted(qs, key=_place_size, reverse=True)
    if not matches:
        return None
    if len(matches) == 1 or _place_size(matches[0]) >= DOMINANCE_RATIO * max(_place_size(matches[1]), 1):
        return Point(matches[0].lat, matches[0].lon, str(matches[0]), "place")
    raise ResolveError(
        "ambiguous_location",
        f"'{raw}' matches places in several states; add the state, e.g. '{matches[0]}'",
        [str(p) for p in matches[:8]],
    )


def _geocode(text: str) -> tuple[Point | None, int]:
    """Free-text lookup on Nominatim for inputs we can't resolve offline. Returns (point, calls made).

    Nominatim's usage policy: at most 1 request/second and cache the results, so answers (including "not
    found") are cached for a day and requests are spaced at least a second apart across all workers.
    """
    key = "geocode:v1:" + hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return (Point(*cached) if cached else None), 0

    for _ in range(20):  # wait up to ~2 s for the shared 1 req/s slot
        if _cache_add("nominatim-slot", 1, timeout=1):
            break
        time.sleep(0.1)
    headers = {"User-Agent": "spotter-fuel-route/1.0 (+https://github.com/aliarfaazhar/spotter-fuel-route)"}
    params = {"q": text, "countrycodes": "us", "format": "jsonv2", "limit": 1}
    try:
        resp = httpx.get(NOMINATIM, params=params, headers=headers, timeout=settings.ROUTING["GEOCODER_TIMEOUT"])
    except httpx.HTTPError as exc:
        raise ResolveError("geocoder_unavailable", "the geocoding service is unreachable", status=503) from exc
    if resp.status_code != 200:
        raise ResolveError("geocoder_unavailable", f"the geocoding service returned {resp.status_code}", status=503)
    try:
        hits = resp.json()
        hit = hits[0] if isinstance(hits, list) and hits else None
        if hit:
            point = Point(float(hit["lat"]), float(hit["lon"]), str(hit.get("display_name", text)), "geocoder")
        else:
            point = None
    except (ValueError, KeyError, TypeError):
        point = None
    _cache_set(key, (point.lat, point.lon, point.label, point.resolved_by) if point else (), 24 * 3600)
    return point, 1


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:  # a cache outage must not take the API down
        return None


def _cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        pass


def _cache_add(key, value, timeout) -> bool:
    try:
        return cache.add(key, value, timeout)
    except Exception:
        return True


def resolve(text: str) -> tuple[Point, int]:
    """Return the point and the number of external geocoding calls made (0 or 1)."""
    raw = unicodedata.normalize("NFKC", text or "").strip()  # full-width "Ｃｈｉｃａｇｏ" -> "Chicago"
    if not raw:
        raise ResolveError("missing_location", "location is required")

    if m := LATLON_RE.match(raw):
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ResolveError("invalid_coordinates", f"'{raw}' is not a valid 'lat,lon' pair")
        return _check_drivable(Point(lat, lon, f"{lat},{lon}", "coordinates"), raw), 0

    cleaned = COUNTRY_SUFFIX_RE.sub("", raw).strip(" ,")

    if m := ZIP_RE.search(cleaned):
        z = ZipCode.objects.filter(code=m.group(1)).first()
        if z:
            return _check_drivable(Point(z.lat, z.lon, f"ZIP {z.code}", "zip"), raw), 0
        if cleaned == m.group(1):
            raise ResolveError("unknown_zip", f"ZIP code {m.group(1)} was not found")
        cleaned = cleaned[: m.start()].strip(" ,")

    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) >= 2 and parts[-1].lower().strip(".") in FOREIGN_REGIONS:
        raise ResolveError("outside_usa", f"'{raw}' is not in the USA")
    point = None
    if len(parts) >= 2:
        state = state_code(parts[-1])
        if state:  # "Austin, TX", "Austin, Texas", "350 5th Ave, New York, NY" (city-level precision)
            point = _from_place(parts[-2], state, raw)
    elif len(parts) == 1:
        words = parts[0].rsplit(" ", 1)
        if len(words) == 2 and len(words[1]) == 2 and state_code(words[1]):  # "Austin TX"
            point = _from_place(words[0], state_code(words[1]), raw)
        if point is None:
            point = _from_place(parts[0], None, raw)

    calls = 0
    if point is None:
        point, calls = _geocode(raw)
    if point is None:
        raise ResolveError(
            "unknown_location", f"could not find '{raw}' in the USA; try 'City, ST', a ZIP code or 'lat,lon'"
        )
    return _check_drivable(point, raw), calls
