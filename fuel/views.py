from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.serializers import RouteQuerySerializer
from fuel.services.planner import plan_trip
from fuel.services.suggest import suggest
from fuel.throttling import SuggestThrottle


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops between two US locations.

    GET  /api/route?start=Chicago, IL&finish=Houston, TX[&start_fuel=empty|full|<miles>][&stop_cost=<usd>]
                   [&include_route=false]
    POST /api/route  with the same fields as JSON
    """

    def get(self, request: Request) -> Response:
        return self._plan(request, request.query_params)

    def post(self, request: Request) -> Response:
        return self._plan(request, request.data)

    def _plan(self, request: Request, data) -> Response:
        query = RouteQuerySerializer(data=data)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        stop_cost = params.get("stop_cost")
        plan = plan_trip(
            params["start"],
            params["finish"],
            start_fuel=params["start_fuel"],
            stop_cost=settings.FUEL["STOP_COST_USD"] if stop_cost is None else stop_cost,
            include_route=params["include_route"],
            map_base_url=request.build_absolute_uri(reverse("route-map")),
        )
        return Response(plan)


class PlaceSuggestView(APIView):
    """Autocomplete: GET /api/places?q=chic -> {"results": ["Chicago, IL", "Chicopee, MA", ...]}. Offline."""

    throttle_classes = [SuggestThrottle]

    def get(self, request: Request) -> Response:
        return Response({"results": suggest(request.query_params.get("q", "")[:100])})


def route_map(request):
    """Leaflet page; it calls /api/route itself, so it works on a cold cache and costs no extra routing call."""
    return render(request, "fuel/map.html")
