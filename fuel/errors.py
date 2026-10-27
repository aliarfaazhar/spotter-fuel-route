"""One error shape for every failure: {"error": {"code", "message", "details"}}."""

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from fuel.services.planner import PlanError
from fuel.services.resolver import ResolveError
from fuel.services.routing import RoutingError


def error_response(status: int, code: str, message: str, details: dict | None = None, headers=None) -> Response:
    return Response({"error": {"code": code, "message": message, "details": details or {}}}, status=status,
                    headers=headers)  # fmt: skip


def exception_handler(exc, context):
    if isinstance(exc, ResolveError):
        details = {"candidates": exc.candidates} if exc.candidates else {}
        return error_response(400, exc.code, exc.message, details)
    if isinstance(exc, PlanError):
        return error_response(exc.status, exc.code, exc.message, exc.details)
    if isinstance(exc, RoutingError):
        headers = {"Retry-After": "60"} if exc.status == 503 else None
        return error_response(exc.status, exc.code, str(exc), headers=headers)

    response = drf_exception_handler(exc, context)
    if response is not None:
        code = getattr(exc, "default_code", "error")
        detail = response.data
        message = detail.get("detail", "invalid request") if isinstance(detail, dict) else str(detail)
        details = detail if isinstance(detail, dict) and "detail" not in detail else {}
        response.data = {"error": {"code": code, "message": str(message), "details": details}}
    return response
