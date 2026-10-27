"""Load the committed data files (gazetteer, ZIPs, cleaned fuel feed) so `migrate` is the only setup step."""

from django.db import migrations

from fuel.services.datafiles import load_reference_data


def load(apps, schema_editor):
    load_reference_data(
        apps.get_model("fuel", "Place"),
        apps.get_model("fuel", "ZipCode"),
        apps.get_model("fuel", "Station"),
        apps.get_model("fuel", "StationPrice"),
    )


def unload(apps, schema_editor):
    for name in ("StationPrice", "Station", "Place", "ZipCode"):
        apps.get_model("fuel", name).objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("fuel", "0001_initial")]
    operations = [migrations.RunPython(load, unload)]
