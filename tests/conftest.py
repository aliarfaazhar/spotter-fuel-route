import os

import pytest
import respx
from django.core.cache import cache

from fuel.models import Place, Station, StationPrice, ZipCode
from fuel.services import detour, resolver, stations


@pytest.fixture(autouse=True)
def _isolated(settings, request):
    """Fresh caches per test, and no real HTTP: respx rejects any request a test didn't mock."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    settings.ROUTING = {**settings.ROUTING, "ORS_API_KEY": ""}
    stations.reset_index()
    detour.reset()
    resolver._zip_tree = None
    if request.node.get_closest_marker("live"):
        if not os.environ.get("RUN_LIVE"):
            pytest.skip("live test: set RUN_LIVE=1")
        yield
        return
    with respx.mock:  # the global router, so tests can register routes with respx.get(...)
        yield
    stations.reset_index()
    detour.reset()
    resolver._zip_tree = None


def make_place(name, state, lat, lon, population=1000, key=None):
    from fuel.services.names import place_key

    return Place.objects.create(
        name=name,
        key=key or place_key(name),
        state=state,
        lat=lat,
        lon=lon,
        population=population,
        source="census_place",
    )


def make_station(opis_id, place, *prices, highway="I-80", exit_number="1"):
    s = Station.objects.create(
        opis_id=opis_id,
        name=f"STOP {opis_id}",
        address=f"{highway}, EXIT {exit_number}",
        city=place.name,
        state=place.state,
        highway=highway,
        exit_number=exit_number,
        place=place,
    )
    for rack, price in enumerate(prices, 1):
        StationPrice.objects.create(station=s, rack_id=rack, price=price)
    return s


@pytest.fixture
def corridor(db):
    """A straight east-west corridor along latitude 41 with stations every ~100 miles."""
    chicago = make_place("Chicago", "IL", 41.0, -87.6, population=2_700_000)
    make_place("Chicago", "MN", 45.0, -93.0, population=40)
    omaha = make_place("Omaha", "NE", 41.0, -96.0, population=480_000)
    ZipCode.objects.create(code="60601", lat=41.0, lon=-87.62)
    ZipCode.objects.create(code="68102", lat=41.0, lon=-95.98)
    towns = []
    for i, lon in enumerate([-87.62, -89.5, -91.4, -93.3, -95.2]):
        towns.append(make_place(f"Town{i}", "IA" if i else "IL", 41.02, lon))
    make_station(1, towns[0], "3.50")
    make_station(2, towns[1], "3.10", "3.20")
    make_station(3, towns[2], "3.40")
    make_station(4, towns[3], "2.90")
    make_station(5, towns[4], "3.60")
    return {"chicago": chicago, "omaha": omaha}
