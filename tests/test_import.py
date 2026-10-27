import csv
import gzip

import pytest
from django.core.management import call_command
from django.db.models import Min

from fuel.models import Place, Station, StationPrice
from fuel.services import datafiles

FEED = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend          ,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,931,3.799
30,ESSO,"HWY-401",Toronto,ON,1,1.50
40,SHEETZ,"I-81, EXIT 5",St. Louis,MO,1,3.10
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    places = tmp_path / "places.csv.gz"
    with gzip.open(places, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "name", "state", "lat", "lon", "land_area_sqmi", "population", "source"])
        w.writerow(["bigcabin", "Big Cabin", "OK", 36.54, -95.22, 1.2, "", "census_place"])
        w.writerow(["gilabend", "Gila Bend", "AZ", 32.95, -112.72, 3.0, "", "census_place"])
        w.writerow(["saintlouis", "St. Louis", "MO", 38.63, -90.24, 61.7, "", "census_place"])
    for name in ("PLACES", "PLACES_EXTRA", "PLACES_MANUAL", "ZIPS", "STATIONS"):
        monkeypatch.setattr(datafiles, name, tmp_path / getattr(datafiles, name).name)
    monkeypatch.setattr(datafiles, "PLACES", places)
    feed = tmp_path / "feed.csv"
    feed.write_text(FEED)
    return feed


@pytest.mark.django_db
def test_import_cleans_the_feed(data_dir):
    call_command("import_fuel_prices", str(data_dir))

    assert set(Station.objects.values_list("opis_id", flat=True)) == {7, 20, 40}  # Toronto dropped
    gila = Station.objects.get(opis_id=20)
    assert gila.city == "Gila Bend"  # trailing spaces trimmed
    assert gila.highway == "I-8" and gila.exit_number == "119"
    # exact duplicate quote collapsed, the second rack's different price kept
    prices = sorted(float(p) for p in StationPrice.objects.filter(station=gila).values_list("price", flat=True))
    assert prices == pytest.approx([3.799, 3.899])
    assert float(Station.objects.filter(opis_id=20).annotate(p=Min("prices__price")).get().p) == pytest.approx(3.799)
    # "St. Louis" in the feed joins the gazetteer's "St. Louis" through the normalised key
    assert Station.objects.get(opis_id=40).place == Place.objects.get(key="saintlouis", state="MO")


@pytest.mark.django_db
def test_station_index_loads_in_one_query(data_dir, django_assert_num_queries):
    from fuel.services.stations import _build_index

    call_command("import_fuel_prices", str(data_dir))
    with django_assert_num_queries(1):
        index = _build_index()
    assert len(index) == 3
    assert index.price[list(index.ids).index(20)] == pytest.approx(3.799)
