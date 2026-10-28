"""One error shape for every failure: {"error": {"code", "message", "details"}}."""

import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from fuel.services.planner import PlanError
from fuel.services.resolver import ResolveError
from fuel.services.routing import RoutingError

log = logging.getLogger("fuel.errors")


def error_response(status: int, code: str, message: str, details: dict | None = None, headers=None) -> Response:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return Response(body, status=status, headers=headers)


def exception_handler(exc, context):
    if isinstance(exc, ResolveError):
        details = {"candidates": exc.candidates} if exc.candidates else {}
        return error_response(exc.status, exc.code, exc.message, details)
    if isinstance(exc, PlanError):
        return error_response(exc.status, exc.code, exc.message, exc.details)
    if isinstance(exc, RoutingError):
        headers = {"Retry-After": "60"} if exc.status == 503 else None
        return error_response(exc.status, exc.code, str(exc), headers=headers)

    response = drf_exception_handler(exc, context)
    if response is None:  # anything unexpected: log it, answer in the same JSON shape, leak nothing
        log.exception("unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "something went wrong; please try again")
    if isinstance(exc, Http404):
        code = "not_found"
    elif isinstance(exc, PermissionDenied):
        code = "permission_denied"
    else:
        code = getattr(exc, "default_code", "error")
    detail = response.data
    message = detail.get("detail", "invalid request") if isinstance(detail, dict) else str(detail)
    details = detail if isinstance(detail, dict) and "detail" not in detail else {}
    response.data = {"error": {"code": code, "message": str(message), "details": details}}
    return response


def json_404(request, exception=None, **kwargs):
    """Unknown URLs answer in the same error shape as the API, never with an HTML page."""
    body = {"error": {"code": "not_found", "message": f"no such endpoint: {request.path}", "details": {}}}
    return JsonResponse(body, status=404)


def json_500(request):
    body = {"error": {"code": "internal_error", "message": "something went wrong; please try again", "details": {}}}
    return JsonResponse(body, status=500)
