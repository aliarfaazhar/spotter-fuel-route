"""Django settings for the fuel-route API. Everything environment-specific comes from env vars."""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


DEV_SECRET_KEY = "dev-only-insecure-key-change-me"  # noqa: S105 - local default; wsgi refuses it without DEBUG
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", DEV_SECRET_KEY)  # config/wsgi.py refuses it outside DEBUG
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:  # runserver serves static files itself in DEBUG; whitenoise serves them under gunicorn
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# PostgreSQL via DATABASE_URL (docker-compose sets it); SQLite fallback so the repo runs without Docker.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
    )
}

# Redis when REDIS_URL is set; otherwise a file cache that survives restarts.
if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": os.environ.get("DJANGO_CACHE_DIR", BASE_DIR / ".cache"),
            # The default cap is 300 entries; routes (~30 KB each) shouldn't be culled by small geocoder entries.
            "OPTIONS": {"MAX_ENTRIES": 5000},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Behind a TLS-terminating proxy (Cloud Run, a load balancer): trust X-Forwarded-Proto so map_url is https.
if env_bool("DJANGO_BEHIND_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "3600"))

REST_FRAMEWORK = {
    # The client IP used for rate limiting. Without a proxy, never trust a client-sent X-Forwarded-For.
    "NUM_PROXIES": int(os.environ.get("DJANGO_NUM_PROXIES", "1" if env_bool("DJANGO_BEHIND_PROXY") else "0")),
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    # Every new route can cost calls to free third-party APIs; keep one client from burning through them.
    "DEFAULT_THROTTLE_CLASSES": ["fuel.throttling.AnonRateThrottleFailOpen"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_RATE_LIMIT", "60/min"), "suggest": "600/min"},
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "fuel.errors.exception_handler",
    "COERCE_DECIMAL_TO_STRING": False,
}

# --- Fuel planner -----------------------------------------------------------
FUEL = {
    "VEHICLE_RANGE_MILES": 500.0,
    "MILES_PER_GALLON": 10.0,
    "BUFFER_STEPS_MILES": (10.0, 25.0, 50.0),
    # start_fuel=empty: fill up at the cheapest station this close to the start, trying the nearer radius first
    "ORIGIN_STATION_RADII_MILES": (10.0, 25.0),
    # Each refuelling stop costs driver time; pricing it at $10 removes penny-chasing stops
    # (NYC->LA: 10 stops -> 7 for +0.9% fuel). stop_cost=0 on a request gives the strict cheapest plan.
    "STOP_COST_USD": 10.0,
    "ROUTE_CACHE_TTL_SECONDS": 7 * 24 * 3600,
    "FALLBACK_ROUTE_CACHE_TTL_SECONDS": 600,  # a route from the fallback provider is only kept briefly
    "ROUTE_CACHE_VERSION": 3,
    "GEOCODE_CACHE_TTL_SECONDS": 24 * 3600,
}

ROUTING = {
    "ORS_API_KEY": os.environ.get("ORS_API_KEY", ""),
    "ORS_PROFILE": os.environ.get("ORS_PROFILE", "driving-hgv"),
    # HeiGIT moved ORS off api.openrouteservice.org (reportedly retiring in Nov 2026); keys work on both.
    "ORS_BASE_URL": os.environ.get("ORS_BASE_URL", "https://api.heigit.org/openrouteservice"),
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    # Worst case stays well under gunicorn's 60 s timeout: ORS (5+15) then OSRM fallback (5+20).
    "CONNECT_TIMEOUT": 5.0,
    "ORS_READ_TIMEOUT": 15.0,
    "OSRM_READ_TIMEOUT": 20.0,
    "GEOCODER_TIMEOUT": 5.0,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"fuel": {"handlers": ["console"], "level": os.environ.get("FUEL_LOG_LEVEL", "INFO")}},
}
