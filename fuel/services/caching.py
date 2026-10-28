"""Cache access that never fails a request: a Redis or cache outage degrades to uncached work."""

import logging

from django.core.cache import cache

log = logging.getLogger(__name__)


def safe_get(key: str):
    try:
        return cache.get(key)
    except Exception:
        log.warning("cache unavailable; continuing uncached (%s)", key.split(":", 1)[0], exc_info=True)
        return None


def safe_set(key: str, value, timeout: float) -> None:
    try:
        cache.set(key, value, timeout)
    except Exception:
        log.warning("cache unavailable; not caching (%s)", key.split(":", 1)[0], exc_info=True)


def safe_add(key: str, value, timeout: float) -> bool:
    """cache.add; True when the cache is down, so a lock or rate slot held there never blocks a request."""
    try:
        return cache.add(key, value, timeout)
    except Exception:
        return True


def safe_delete(key: str) -> None:
    try:
        cache.delete(key)
    except Exception:
        log.warning("cache unavailable; not deleting (%s)", key.split(":", 1)[0], exc_info=True)
