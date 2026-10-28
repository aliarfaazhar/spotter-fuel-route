from django.contrib import admin
from django.urls import path, re_path
from django.views.generic import RedirectView

from fuel.errors import json_404
from fuel.views import PlaceSuggestView, RoutePlanView, route_map

urlpatterns = [
    path("", RedirectView.as_view(url="/map")),  # the empty map: opening the site never spends a routing call
    path("api/route", RoutePlanView.as_view(), name="route-plan"),
    path("api/places", PlaceSuggestView.as_view(), name="place-suggest"),
    path("map", route_map, name="route-map"),
    path("admin/", admin.site.urls),
    re_path(r"^api/", json_404),  # unknown API paths get a JSON 404 even with DEBUG on
]

handler404 = "fuel.errors.json_404"
handler500 = "fuel.errors.json_500"
