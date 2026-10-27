from django.contrib import admin

from fuel.models import Place, Station, StationPrice, ZipCode


class StationPriceInline(admin.TabularInline):
    model = StationPrice
    extra = 0


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "highway", "exit_number")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
    raw_id_fields = ("place",)
    inlines = [StationPriceInline]


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "lat", "lon", "source")
    list_filter = ("source", "state")
    search_fields = ("name", "key")


admin.site.register(ZipCode)
