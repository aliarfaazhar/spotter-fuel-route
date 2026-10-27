import math

from django.conf import settings
from rest_framework import serializers

from fuel.services.planner import StartFuel


class RouteQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text="'City, ST', a ZIP code, an address or 'lat,lon'.")
    finish = serializers.CharField(max_length=200)
    start_fuel = serializers.CharField(
        max_length=10,
        required=False,
        default="empty",
        help_text="'empty' (default: fill up near the start), 'full', or miles of range already in the tank.",
    )
    stop_cost = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text="USD charged per refuelling stop (driver time). Default 10; 0 gives the strict cheapest plan.",
    )
    include_route = serializers.BooleanField(
        required=False, default=True, help_text="Include the route as a GeoJSON LineString under map.route."
    )

    def validate_start_fuel(self, value: str) -> StartFuel:
        tank = settings.FUEL["VEHICLE_RANGE_MILES"]
        text = value.strip().lower()
        if text in ("", "empty"):
            return StartFuel("empty", 0.0)
        if text == "full":
            return StartFuel("full", tank)
        try:
            miles = float(text)
        except ValueError:
            raise serializers.ValidationError("must be 'empty', 'full' or miles of range in the tank") from None
        if not math.isfinite(miles) or not 0 <= miles <= tank:
            raise serializers.ValidationError(f"must be between 0 and {tank:g} miles")
        if miles == 0:
            return StartFuel("empty", 0.0)  # an empty tank means filling up near the start, same as "empty"
        return StartFuel("miles", miles)

    def validate_stop_cost(self, value: float) -> float:
        if not math.isfinite(value):
            raise serializers.ValidationError("must be a number")
        return value
