"""Read the committed data files and load them into the database (used by the data migration and commands)."""

import csv
import gzip
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
PLACES = DATA_DIR / "places.csv.gz"
PLACES_EXTRA = DATA_DIR / "places_extra.csv"  # Nominatim results for station towns the gazetteer lacks
PLACES_MANUAL = DATA_DIR / "places_manual.csv"  # the last few towns, located by hand with the source noted
ZIPS = DATA_DIR / "zips.csv.gz"
STATIONS = DATA_DIR / "stations.csv.gz"  # cleaned feed: US only, trimmed, one row per (station, rack, price)


def _rows(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def place_rows() -> Iterator[dict]:
    yield from _rows(PLACES)
    yield from _rows(PLACES_EXTRA)
    yield from _rows(PLACES_MANUAL)


def zip_rows() -> Iterator[dict]:
    yield from _rows(ZIPS)


def station_rows() -> Iterator[dict]:
    yield from _rows(STATIONS)


def _num(value: str, cast):
    return cast(value) if value not in ("", None) else None


def load_reference_data(place_model, zip_model, station_model, price_model, batch_size: int = 5000) -> dict:
    """(Re)load places, ZIPs, stations and prices. Takes model classes so it works from migrations."""
    price_model.objects.all().delete()
    station_model.objects.all().delete()
    place_model.objects.all().delete()
    zip_model.objects.all().delete()

    seen: set[tuple[str, str]] = set()
    places = []
    for r in place_rows():
        ident = (r["key"], r["state"])
        if ident in seen:  # places_extra never overrides the gazetteer
            continue
        seen.add(ident)
        places.append(place_model(
            key=r["key"], name=r["name"], state=r["state"], lat=float(r["lat"]), lon=float(r["lon"]),
            land_area_sqmi=_num(r["land_area_sqmi"], float), population=_num(r["population"], int) or None,
            source=r["source"],
        ))  # fmt: skip
    place_model.objects.bulk_create(places, batch_size=batch_size)
    zip_model.objects.bulk_create(
        (zip_model(code=r["code"], lat=float(r["lat"]), lon=float(r["lon"])) for r in zip_rows()),
        batch_size=batch_size,
    )

    place_ids = {(k, s): pk for pk, k, s in place_model.objects.values_list("pk", "key", "state")}
    stations, prices = {}, []
    for r in station_rows():
        sid = int(r["opis_id"])
        if sid not in stations:
            stations[sid] = station_model(
                opis_id=sid, name=r["name"], address=r["address"], city=r["city"], state=r["state"],
                highway=r["highway"], exit_number=r["exit_number"],
                place_id=place_ids[(r["place_key"], r["state"])],
            )  # fmt: skip
        prices.append(price_model(station_id=sid, rack_id=int(r["rack_id"]), price=Decimal(r["price"])))
    station_model.objects.bulk_create(stations.values(), batch_size=batch_size)
    price_model.objects.bulk_create(prices, batch_size=batch_size, ignore_conflicts=True)
    return {"places": len(places), "stations": len(stations), "prices": len(prices)}
