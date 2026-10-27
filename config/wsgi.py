"""WSGI entry point (gunicorn). Refuses to serve with the published development secret key."""

import os

from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

from django.conf import settings  # noqa: E402

if not settings.DEBUG and settings.SECRET_KEY == settings.DEV_SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY: the development default must not be used with DEBUG off.")
