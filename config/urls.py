from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from fuel.views import PlaceSuggestView, RoutePlanView, route_map

urlpatterns = [
    path("", RedirectView.as_view(url="/map?start=New+York,+NY&finish=Los+Angeles,+CA")),
    path("api/route", RoutePlanView.as_view(), name="route-plan"),
    path("api/places", PlaceSuggestView.as_view(), name="place-suggest"),
    path("map", route_map, name="route-map"),
    path("admin/", admin.site.urls),
]
