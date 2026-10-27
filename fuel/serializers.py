from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text="'City, ST', a ZIP code, an address or 'lat,lon'.")
    finish = serializers.CharField(max_length=200)
    start_fuel = serializers.CharField(
        max_length=10, required=False, help_text="'empty' (default), 'full', or miles of range in the tank."
    )
    stop_cost = serializers.FloatField(
        required=False, min_value=0, max_value=1000, help_text="USD per refuelling stop (default 10; 0 = cheapest)."
    )
    include = serializers.CharField(max_length=50, required=False, help_text="'route' adds the GeoJSON line.")
