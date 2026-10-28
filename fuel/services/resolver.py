"""Turn what a user types into a point in the US, offline whenever possible.

Order: "lat,lon" -> ZIP code (anywhere in the text) -> "City, ST" / "City, State" / common nicknames -> a
single unambiguous city name -> one geocoder call (counted in the response's API-call budget).
"""

import hashlib
import logging
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

from .geometry import chord_to_miles, haversine_miles, to_xyz
from .names import place_key, state_code

log = logging.getLogger(__name__)

NICKNAMES = {
    "nyc": ("New York", "NY"),
    "newyorkcity": ("New York", "NY"),
    "la": ("Los Angeles", "CA"),
    "sf": ("San Francisco", "CA"),
    "dc": ("Washington", "DC"),
    "washingtondc": ("Washington", "DC"),
}
LATLON_RE = re.compile(r"^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$")
ZIP_START_RE = re.compile(r"^(\d{5})(?:-\d{4})?(?=[\s,])")
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
FOREIGN_REGIONS = (
    {"canada", "mexico", "england", "uk", "united kingdom", "scotland", "ireland", "france", "germany", "spain"}
    | {"italy", "japan", "china", "india", "australia", "brazil", "quebec city"}
    | {c.lower() for c in CANADA}
    | set(CANADA.values())
)
# US places with no road link to the mainland: (min_lat, max_lat, min_lon, max_lon)
ISLANDS = {
    "Hawaii": (18.5, 22.5, -161.0, -154.5),
    "Puerto Rico and the US Virgin Islands": (17.5, 18.8, -68.0, -64.4),
    "Guam and the Northern Mariana Islands": (13.0, 21.0, 144.0, 147.0),
    "American Samoa": (-14.6, -11.0, -171.2, -168.1),
    "Southeast Alaska (Juneau, Sitka, Ketchikan)": (54.5, 58.6, -137.5, -130.0),  # Haines/Skagway (59.2+) have roads
    "western Alaska (Kodiak, Nome, Bethel, the Aleutians)": (51.0, 72.0, -180.0, -151.6),  # Homer (-151.55) has a road
    "the Aleutian Islands": (50.0, 56.0, 170.0, 180.0),
    "Nantucket": (41.2, 41.5, -70.35, -69.9),
    "Martha's Vineyard": (41.3, 41.5, -70.85, -70.4),
    "Santa Catalina Island": (33.28, 33.5, -118.62, -118.28),
}
TERRITORIES = {
    "PR": "Puerto Rico",
    "VI": "US Virgin Islands",
    "GU": "Guam",
    "MP": "Northern Mariana Islands",
    "AS": "American Samoa",
}
STATE_ONLY_HINT = "is a whole state or country; give a city, ZIP code, address or 'lat,lon'"
COUNTRY_NAMES = {"usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america"}
BIG_CITY = 100_000  # a state name alone resolves to a city of that name only when it's this big
CONFLICT_MILES = 50.0  # "Chicago, IL 90210": a ZIP this far from the named city is a contradiction
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
        raise ResolveError("outside_usa", f"'{raw}' is not on land in the USA (no US ZIP code area within 30 mi)")
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
    Nominatim always finds *something* ("Tokyo" -> a Tokyo Street in Texas), so a hit is only kept when it
    is a street address the user typed, or a place whose name matches what they typed.
    """
    key = "geocode:v2:" + hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return (Point(*cached) if cached else None), 0

    for _ in range(20):  # wait up to ~2 s for the shared 1 req/s slot
        if _cache_add("nominatim-slot", 1, timeout=1):
            break
        time.sleep(0.1)
    headers = {"User-Agent": "spotter-fuel-route/1.0 (+https://github.com/aliarfaazhar/spotter-fuel-route)"}
    params = {"q": text, "countrycodes": "us", "format": "jsonv2", "addressdetails": 1, "limit": 1}
    try:
        resp = httpx.get(NOMINATIM, params=params, headers=headers, timeout=settings.ROUTING["GEOCODER_TIMEOUT"])
    except httpx.HTTPError as exc:
        raise ResolveError("geocoder_unavailable", "the geocoding service is unreachable", status=503) from exc
    if resp.status_code != 200:
        raise ResolveError("geocoder_unavailable", f"the geocoding service returned {resp.status_code}", status=503)
    try:
        hits = resp.json()
        point = _accept_hit(text, hits[0]) if isinstance(hits, list) and hits else None
    except (ValueError, KeyError, TypeError, IndexError):
        point = None
    _cache_set(key, (point.lat, point.lon, point.label, point.resolved_by) if point else (), 24 * 3600)
    return point, 1


LANDMARK_IMPORTANCE = 0.3  # Nominatim's 0-1 prominence: Millennium Park ~0.5, a corner restaurant ~0.0001
STREET_ADDRESS_RE = re.compile(r"^\s*\d+[a-z]?\s+[a-z]", re.I)


def _accept_hit(text: str, hit: dict) -> Point | None:
    if hit.get("addresstype") in ("country", "state") or hit.get("type") in ("country", "state"):
        return None
    address = hit.get("address") or {}
    state = (address.get("ISO3166-2-lvl4") or "").removeprefix("US-")
    town = next((address[k] for k in ("city", "town", "village", "hamlet", "municipality") if address.get(k)), None)
    parts = [place_key(part) for part in text.split(",")]
    typed = set(parts) - {""}
    is_place = hit.get("category") in ("place", "boundary")
    town_names = {place_key(n) for n in (town, hit.get("name"), address.get("county")) if n}
    areas = {place_key(address[k]) for k in ("city", "town", "village", "hamlet", "suburb", "county") if address.get(k)}
    # A street address only counts in the town that was typed with it ("100 Main St, Boise" must be in Boise).
    street = bool(STREET_ADDRESS_RE.match(text)) and bool(set(parts[1:]) & areas)
    landmark = float(hit.get("importance") or 0) >= LANDMARK_IMPORTANCE and place_key(hit.get("name") or "") in typed
    if not (street or (is_place and typed & town_names) or landmark):
        return None
    if town and state:
        label = f"{town}, {state}"
    elif hit.get("name") and state:
        label = f"{hit['name']}, {state}"
    else:
        label = str(hit.get("display_name", text))
    return Point(float(hit["lat"]), float(hit["lon"]), label, "geocoder")


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:  # a cache outage must not take the API down
        log.warning("geocode cache unavailable", exc_info=True)
        return None


def _cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        log.warning("geocode cache unavailable; not caching", exc_info=True)


def _cache_add(key, value, timeout) -> bool:
    try:
        return cache.add(key, value, timeout)
    except Exception:
        return True


def _split_state(text: str) -> tuple[str, str] | None:
    """'Austin TX', 'New York New York', 'Salt Lake City Utah' -> (city, state code), without a comma."""
    words = text.split()
    for n in (3, 2, 1):
        if len(words) > n and (code := state_code(" ".join(words[-n:]))):
            return " ".join(words[:-n]), code
    return None


def _offline(parts: list[str], raw: str) -> Point | None:
    """City/state lookups in the gazetteer; None when the text isn't a place we know."""
    if len(parts) >= 2:
        state = state_code(parts[-1])
        # "Austin, TX", "Austin, Texas", "350 5th Ave, New York, NY" (city-level precision)
        return _from_place(parts[-2], state, raw) if state else None
    if len(parts) == 1:
        if split := _split_state(parts[0]):
            point = _from_place(*split, raw)
            if point is not None:
                return point
        return _from_place(parts[0], None, raw)
    return None


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

    if raw.lower().strip(" .") in COUNTRY_NAMES:
        raise ResolveError("too_broad", f"'{raw}' {STATE_ONLY_HINT}")
    _check_territory(raw)
    cleaned = COUNTRY_SUFFIX_RE.sub("", raw).strip(" ,")

    # A ZIP at either end ("Chicago, IL 60601", "60601, IL"): it is more precise than any name with it,
    # unless that name is a place more than CONFLICT_MILES away.
    if m := ZIP_RE.search(cleaned) or ZIP_START_RE.match(cleaned):
        code, rest = m.group(1), (cleaned[: m.start()] + " " + cleaned[m.end() :]).strip(" ,")
        z = ZipCode.objects.filter(code=code).first()
        if z is None and not rest:
            raise ResolveError("unknown_zip", f"ZIP code {code} has no Census ZIP area (e.g. a PO box or one building)")
        if z is not None:
            zip_point = Point(z.lat, z.lon, f"ZIP {z.code}", "zip")
            named = None
            if rest and not state_code(rest):
                try:
                    named = _offline([p.strip() for p in rest.split(",") if p.strip()], raw)
                except ResolveError:  # "Springfield 62701": the ZIP settles which Springfield
                    named = None
            if named is not None and haversine_miles(named.lat, named.lon, z.lat, z.lon) > CONFLICT_MILES:
                raise ResolveError("conflicting_location", f"'{raw}': ZIP {code} is not in {named.label}")
            return _check_drivable(zip_point, raw), 0
        cleaned = rest

    if not re.search(r"[A-Za-z]", raw):  # digits and punctuation that aren't a ZIP or valid 'lat,lon'
        if re.search(r"[,.]", raw):
            raise ResolveError("invalid_coordinates", f"'{raw}' is not a valid 'lat,lon' pair")
        raise ResolveError("unknown_location", f"'{raw}' is not a 5-digit ZIP code or a place name")

    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    region = _ascii_lower(parts[-1]).strip(".") if parts else ""
    if len(parts) >= 2 and region in FOREIGN_REGIONS:
        raise ResolveError("outside_usa", f"'{raw}' is not in the USA")
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}", parts[-1]) and not state_code(parts[-1]):
        raise ResolveError("unknown_state", f"'{parts[-1]}' is not a US state code")
    if len(parts) == 1 and state_code(parts[0]):
        return _check_drivable(_state_name_alone(parts[0], raw), raw), 0
    if not parts or not re.search(r"[A-Za-z0-9]", parts[0]):
        raise ResolveError("unknown_location", f"could not find a place name in '{raw}'")

    point, calls = _offline(parts, raw), 0
    if point is None:
        point, calls = _geocode(raw)
    if point is None:
        raise ResolveError(
            "unknown_location", f"could not find '{raw}' in the USA; try 'City, ST', a ZIP code or 'lat,lon'"
        )
    return _check_drivable(point, raw), calls


def _state_name_alone(text: str, raw: str) -> Point:
    """'DC', 'LA', 'New York', 'Washington' mean a city; 'Texas' or 'TX' means nothing we can route to."""
    key = place_key(text)
    if key in NICKNAMES:
        return _from_place(*NICKNAMES[key], raw)
    if state_code(text) == "DC":
        return _from_place("Washington", "DC", raw)
    big = Place.objects.filter(key=key, population__gte=BIG_CITY).order_by("-population").first()
    if big is None:
        raise ResolveError("too_broad", f"'{raw}' {STATE_ONLY_HINT}")
    return Point(big.lat, big.lon, str(big), "place")


def _check_territory(raw: str) -> None:
    """'San Juan, PR', 'Guam', 'Charlotte Amalie, Virgin Islands': no road to the mainland, whatever the town."""
    parts = [_ascii_lower(p).strip(" .") for p in raw.split(",") if p.strip()]
    if not parts:
        return
    last, whole = parts[-1], ", ".join(parts)
    for code, name in TERRITORIES.items():
        if last in (code.lower(), name.lower()) or whole == name.lower():
            raise ResolveError("not_reachable_by_road", f"'{raw}' is in {name}, which has no road to the mainland")


def _ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
