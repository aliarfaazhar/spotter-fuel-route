"""Place-name autocomplete for the map page, offline.

Places with a known population (~30k cities and towns) are held in memory sorted by their normalised key, so a
prefix is a binary search; the matches are ranked by population. Every suggestion is a "City, ST" label the
resolver accepts as-is.
"""

import bisect
import heapq
import threading

from fuel.models import Place

from .names import place_key, state_code
from .resolver import ISLANDS, TERRITORIES

NO_ROAD_TO_MAINLAND = ("HI", *TERRITORIES)  # the planner rejects these; don't suggest them

_entries: tuple[list[str], list[tuple[int, str]]] | None = None
_lock = threading.Lock()


def reset() -> None:
    global _entries
    _entries = None


def _load() -> tuple[list[str], list[tuple[int, str]]]:
    global _entries
    if _entries is None:
        with _lock:
            if _entries is None:
                rows = sorted(
                    Place.objects.filter(population__gt=0)
                    .exclude(state__in=NO_ROAD_TO_MAINLAND)
                    .values_list("key", "population", "name", "state", "lat", "lon"),
                    key=lambda r: r[0],
                )
                rows = [r for r in rows if not _on_an_island(r[4], r[5])]
                _entries = ([r[0] for r in rows], [(r[1], f"{r[2]}, {r[3]}") for r in rows])
    return _entries


def _on_an_island(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in ISLANDS.values())


def suggest(text: str, limit: int = 8) -> list[str]:
    """Up to `limit` "City, ST" labels whose name starts with the text, biggest first. "Spring, T" narrows by state."""
    name, _, state_text = (text or "").partition(",")
    prefix = place_key(name)
    if len(prefix) < 2:
        return []
    state = state_text.strip().upper()
    keys, values = _load()
    lo = bisect.bisect_left(keys, prefix)
    hi = bisect.bisect_left(keys, prefix + "￿")
    matches = values[lo:hi]
    if state:
        code = state_code(state)
        matches = [m for m in matches if (m[1].endswith(", " + code) if code else m[1][-2:].startswith(state))]
    seen, out = set(), []
    for _, label in heapq.nlargest(limit * 2, matches):
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out[:limit]
