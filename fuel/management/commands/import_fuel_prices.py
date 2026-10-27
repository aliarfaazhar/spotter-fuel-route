"""Clean the OPIS fuel-price CSV, locate every station, and write data/stations.csv.gz (then load the DB).

    python manage.py import_fuel_prices data/fuel-prices.csv [--geocode-missing]

What the raw feed needs (all found by profiling it):
  * no coordinates at all -> station = its town's point from the offline gazetteer
  * ~620 Canadian rows (ON, AB, BC, ...) in a "USA only" task -> dropped
  * city names padded with trailing spaces -> trimmed
  * the same OPIS ID quoted at several prices (different rack IDs / names) -> every quote kept as a
    StationPrice row; the planner uses the lowest
  * towns the gazetteer lacks -> optional one-off Nominatim lookup (1 req/s, cached in places_extra.csv)
"""

import csv
import gzip
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel.models import Place, Station, StationPrice, ZipCode
from fuel.services import datafiles
from fuel.services.names import US_STATES, place_key

HIGHWAY_RE = re.compile(r"^\s*((?:I|US|SR|HWY|STATE ROUTE|ROUTE)[\s-]*\d+[A-Z]?)", re.I)
EXIT_RE = re.compile(r"\bEXIT\s*#?\s*(\d+[A-Z]?)", re.I)
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "spotter-fuel-route/1.0 (+https://github.com/aliarfaazhar/spotter-fuel-route)"
STATION_FIELDS = ["opis_id", "name", "address", "city", "state", "rack_id", "price", "highway", "exit_number",
                  "place_key"]  # fmt: skip


def parse_highway(address: str) -> tuple[str, str]:
    hw = HIGHWAY_RE.match(address)
    ex = EXIT_RE.search(address)
    highway = re.sub(r"[\s-]+", "-", hw.group(1).upper()) if hw else ""
    return highway, (ex.group(1).upper() if ex else "")


def state_bounds(places: dict) -> dict[str, tuple[float, float, float, float]]:
    """Per-state lat/lon box from the gazetteer itself, padded; used to sanity-check geocoder answers."""
    box: dict[str, list[float]] = {}
    for (_, state), (lat, lon) in places.items():
        b = box.setdefault(state, [lat, lat, lon, lon])
        b[0], b[1], b[2], b[3] = min(b[0], lat), max(b[1], lat), min(b[2], lon), max(b[3], lon)
    return {s: (b[0] - 0.2, b[1] + 0.2, b[2] - 0.2, b[3] + 0.2) for s, b in box.items()}


class Command(BaseCommand):
    help = "Clean + geocode the OPIS fuel price CSV into data/stations.csv.gz and load it."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path)
        parser.add_argument("--geocode-missing", action="store_true",
                            help="Locate towns missing from the gazetteer via Nominatim (1 req/s).")  # fmt: skip
        parser.add_argument("--no-load", action="store_true", help="Only write the data files.")

    def handle(self, *args, csv_path: Path, geocode_missing: bool, no_load: bool, **opts):
        if not csv_path.exists():
            raise CommandError(f"{csv_path} not found")

        known = {(r["key"], r["state"]): (float(r["lat"]), float(r["lon"])) for r in datafiles.place_rows()}
        if not known:
            raise CommandError("gazetteer is empty: run `manage.py build_gazetteer --download` first")

        stats = defaultdict(int)
        rows, seen = [], set()
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for raw in csv.DictReader(fh):
                r = {k.strip(): (v or "").strip() for k, v in raw.items()}
                stats["rows"] += 1
                if r["State"] not in US_STATES:
                    stats["dropped_non_us"] += 1
                    continue
                ident = (r["OPIS Truckstop ID"], r["Rack ID"], r["Retail Price"])
                if ident in seen:
                    stats["dropped_exact_duplicates"] += 1
                    continue
                seen.add(ident)
                highway, exit_number = parse_highway(r["Address"])
                rows.append({
                    "opis_id": int(r["OPIS Truckstop ID"]), "name": r["Truckstop Name"], "address": r["Address"],
                    "city": r["City"], "state": r["State"], "rack_id": int(r["Rack ID"]), "price": r["Retail Price"],
                    "highway": highway, "exit_number": exit_number, "place_key": place_key(r["City"]),
                })  # fmt: skip

        towns = {(r["place_key"], r["state"]): r["city"] for r in rows}
        missing = sorted((k, s, city) for (k, s), city in towns.items() if (k, s) not in known)
        if missing and geocode_missing:
            self._geocode(missing, known)
            missing = [(k, s, city) for k, s, city in missing if (k, s) not in known]

        unresolved = {(k, s) for k, s, _ in missing}
        if unresolved:
            stats["dropped_unlocated_towns"] = len(unresolved)
            for _key, s, city in missing:
                self.stderr.write(f"  unlocated: {city}, {s}")
        kept = [r for r in rows if (r["place_key"], r["state"]) not in unresolved]
        stats["dropped_unlocated_rows"] = len(rows) - len(kept)

        with gzip.open(datafiles.STATIONS, "wt", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=STATION_FIELDS)
            writer.writeheader()
            writer.writerows(sorted(kept, key=lambda r: (r["opis_id"], r["rack_id"], r["price"])))

        stats["price_quotes"] = len(kept)
        stats["stations"] = len({r["opis_id"] for r in kept})
        by_id = defaultdict(set)
        for r in kept:
            by_id[r["opis_id"]].add(r["price"])
        stats["stations_with_conflicting_prices"] = sum(1 for v in by_id.values() if len(v) > 1)
        self.stdout.write(", ".join(f"{k}={v}" for k, v in stats.items()))

        if not no_load:
            with transaction.atomic():
                counts = datafiles.load_reference_data(Place, ZipCode, Station, StationPrice)
            self.stdout.write(self.style.SUCCESS(f"loaded {counts}"))

    def _geocode(self, missing, known) -> None:
        bounds = state_bounds(known)
        extra_exists = datafiles.PLACES_EXTRA.exists()
        with (
            datafiles.PLACES_EXTRA.open("a", newline="", encoding="utf-8") as fh,
            httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20) as client,
        ):
            writer = csv.DictWriter(fh, fieldnames=["key", "name", "state", "lat", "lon", "land_area_sqmi",
                                                   "population", "source"])  # fmt: skip
            if not extra_exists:
                writer.writeheader()
            for i, (key, state, city) in enumerate(missing, 1):
                params = {"city": city, "state": US_STATES[state], "countrycodes": "us", "format": "jsonv2",
                          "limit": 1}  # fmt: skip
                resp = client.get(NOMINATIM, params=params)
                time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
                hits = resp.json() if resp.status_code == 200 else []
                if not hits:
                    self.stdout.write(f"  [{i}/{len(missing)}] no match: {city}, {state}")
                    continue
                lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
                lo_lat, hi_lat, lo_lon, hi_lon = bounds[state]
                if not (lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon):
                    self.stdout.write(f"  [{i}/{len(missing)}] outside {state}, ignored: {city}")
                    continue
                writer.writerow({"key": key, "name": city, "state": state, "lat": lat, "lon": lon,
                                 "land_area_sqmi": "", "population": "", "source": "nominatim"})  # fmt: skip
                fh.flush()
                known[(key, state)] = (lat, lon)
                self.stdout.write(f"  [{i}/{len(missing)}] {city}, {state} -> {lat:.4f},{lon:.4f}")
