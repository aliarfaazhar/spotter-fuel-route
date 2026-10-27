"""Turn what a user types into a point in the US, offline whenever possible.

Order: "lat,lon" -> ZIP code (anywhere in the text) -> "City, ST" / "City, State" / common nicknames -> a
single unambiguous city name -> one geocoder call (counted in the response's API-call budget).
"""

import re
from dataclasses import dataclass

import httpx
import numpy as np
from scipy.spatial import cKDTree

from fuel.models import Place, ZipCode

from .geometry import chord_to_miles, to_xyz
from .names import US_STATES, place_key, state_code

NICKNAMES = {
    "nyc": ("New York", "NY"), "newyorkcity": ("New York", "NY"), "manhattan": ("New York", "NY"),
    "la": ("Los Angeles", "CA"), "sf": ("San Francisco", "CA"), "dc": ("Washington", "DC"),
    "washingtondc": ("Washington", "DC"), "vegas": ("Las Vegas", "NV"), "philly": ("Philadelphia", "PA"),
    "nola": ("New Orleans", "LA"), "slc": ("Salt Lake City", "UT"), "okc": ("Oklahoma City", "OK"),
    "kc": ("Kansas City", "MO"), "atl": ("Atlanta", "GA"), "chi": ("Chicago", "IL"),
}  # fmt: skip
LATLON_RE = re.compile(r"^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$")
ZIP_RE = re.compile(r"(?:^|[\s,])(\d{5})(?:-\d{4})?\s*$")
COUNTRY_SUFFIX_RE = re.compile(r"[\s,]*(usa|u\.s\.a\.|us|u\.s\.|united states(?: of america)?)\s*$", re.I)
# Regions that look like "City, Region" but aren't in the US. Checked before geocoding, because a US-only
# geocoder will happily turn "Toronto, Ontario" into "Toronto Paseo, Ontario, California".
FOREIGN_REGIONS = {
    "canada", "mexico", "ontario", "on", "quebec", "qc", "british columbia", "bc", "alberta", "ab", "manitoba",
    "mb", "saskatchewan", "sk", "nova scotia", "ns", "new brunswick", "nb", "newfoundland", "newfoundland and labrador",
    "nl", "prince edward island", "pei", "pe", "yukon", "yt", "northwest territories", "nt", "nunavut", "nu",
    "uk", "united kingdom", "england", "france", "germany", "spain", "italy", "india", "pakistan", "china", "japan",
    "australia", "brazil", "puerto rico",
}  # fmt: skip
MAX_MILES_FROM_US_ZIP = 30.0  # a point farther than this from every US ZIP centroid is not in the US
DOMINANCE_RATIO = 5.0
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
        return {"query": query, "label": self.label, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "resolved_by": self.resolved_by}  # fmt: skip


class ResolveError(ValueError):
    def __init__(self, code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.candidates = code, message, candidates or []


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


def _place_size(p: Place) -> float:
    if p.land_area_sqmi:
        return p.land_area_sqmi * 1000  # Census places: area is a solid proxy for "the city people mean"
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


def _geocode(text: str) -> Point | None:
    """One call to a free geocoder for inputs we can't resolve offline (e.g. a street address with no ZIP)."""
    headers = {"User-Agent": "spotter-fuel-route/1.0 (+https://github.com/aliarfaazhar/spotter-fuel-route)"}
    try:
        resp = httpx.get(NOMINATIM, params={"q": text, "countrycodes": "us", "format": "jsonv2", "limit": 1},
                         headers=headers, timeout=10)  # fmt: skip
        hits = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    if not hits:
        return None
    return Point(float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", text), "geocoder")


def resolve(text: str, allow_geocoder: bool = True) -> tuple[Point, int]:
    """Return the point and the number of external geocoding calls made (0 or 1)."""
    raw = (text or "").strip()
    if not raw:
        raise ResolveError("missing_location", "location is required")

    if m := LATLON_RE.match(raw):
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ResolveError("invalid_coordinates", f"'{raw}' is not a valid 'lat,lon' pair")
        if not is_in_us(lat, lon):
            raise ResolveError("outside_usa", f"{lat},{lon} is not in the USA")
        return Point(lat, lon, f"{lat},{lon}", "coordinates"), 0

    cleaned = COUNTRY_SUFFIX_RE.sub("", raw).strip(" ,")

    if m := ZIP_RE.search(cleaned):
        z = ZipCode.objects.filter(code=m.group(1)).first()
        if z:
            return Point(z.lat, z.lon, f"ZIP {z.code}", "zip"), 0
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

    if point is None and allow_geocoder:
        point = _geocode(raw)
        if point is not None:
            if not is_in_us(point.lat, point.lon):
                raise ResolveError("outside_usa", f"'{raw}' is not in the USA")
            return point, 1
    if point is None:
        raise ResolveError(
            "unknown_location", f"could not find '{raw}' in the USA; try 'City, ST', a ZIP code or 'lat,lon'"
        )
    if point.label.split(", ")[-1] not in US_STATES:
        raise ResolveError("outside_usa", f"'{raw}' is not in the USA")
    return point, 0
