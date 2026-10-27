from django.db import models
from django.db.models import Q


class Place(models.Model):
    """A named US place (city, town, township, CDP, hamlet) with a representative point."""

    class Source(models.TextChoices):
        CENSUS_PLACE = "census_place", "Census place"
        CENSUS_COUSUB = "census_cousub", "Census county subdivision"
        GEONAMES = "geonames", "GeoNames"
        NOMINATIM = "nominatim", "Nominatim (OSM)"
        MANUAL = "manual", "Located by hand (see data/places_manual.csv)"

    name = models.CharField(max_length=120)
    key = models.CharField(max_length=120, help_text="Normalised name used for lookups (see services.names).")
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lon = models.FloatField()
    land_area_sqmi = models.FloatField(null=True, blank=True)
    population = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "state"], name="uniq_place_key_state"),
            models.CheckConstraint(
                condition=Q(lat__gte=-90, lat__lte=90, lon__gte=-180, lon__lte=180), name="place_valid_coords"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.state}"


class ZipCode(models.Model):
    """US ZIP Code Tabulation Area centroid, for ZIP inputs."""

    code = models.CharField(max_length=5, primary_key=True)
    lat = models.FloatField()
    lon = models.FloatField()

    def __str__(self) -> str:
        return self.code


class Station(models.Model):
    """A truck stop from the OPIS feed. Located at its town's representative point."""

    opis_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2)
    highway = models.CharField(max_length=20, blank=True, help_text="First road in the address, e.g. I-44.")
    exit_number = models.CharField(max_length=10, blank=True)
    place = models.ForeignKey(Place, on_delete=models.PROTECT, related_name="stations")

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"


class StationPrice(models.Model):
    """One price quote from the feed. A station can be quoted on several rack IDs at different prices."""

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name="prices")
    rack_id = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=8)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["station", "rack_id", "price"], name="uniq_station_rack_price"),
            models.CheckConstraint(condition=Q(price__gt=0), name="price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.station_id} rack {self.rack_id}: {self.price}"
