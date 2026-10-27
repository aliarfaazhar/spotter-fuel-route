import logging

from rest_framework.throttling import AnonRateThrottle

log = logging.getLogger(__name__)


class AnonRateThrottleFailOpen(AnonRateThrottle):
    """Per-IP rate limit kept in the cache; if the cache is down, serve the request rather than fail it."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            log.warning("throttle cache unavailable; allowing request", exc_info=True)
            return True
