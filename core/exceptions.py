"""Centralized exception handling for VIKMO DealerDesk."""
import logging
from django.http import Http404, JsonResponse
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class BaseAppException(APIException):
    """Base exception adhering to DealerDesk error response shape."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_message = "A validation error occurred."

    def __init__(self, message=None, code=None, details=None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        self.details = details if details is not None else []
        super().__init__(self.message)


class ValidationError(BaseAppException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_message = "Invalid input."


class ResourceNotFoundError(BaseAppException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "NOT_FOUND"
    default_message = "Resource not found."


class DuplicateError(BaseAppException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "DUPLICATE"
    default_message = "Resource already exists."


class InsufficientStockError(BaseAppException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "INSUFFICIENT_STOCK"
    default_message = "Insufficient stock available."


class InvalidTransitionError(BaseAppException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "INVALID_TRANSITION"
    default_message = "Status change not allowed from the current status."


class OrderLockedError(BaseAppException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "ORDER_LOCKED"
    default_message = "Order is locked and cannot be edited."


class IdempotencyConflictError(BaseAppException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "IDEMPOTENCY_CONFLICT"
    default_message = "Idempotency key reused with different request parameters."


class SupplierUnavailableError(BaseAppException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "SUPPLIER_UNAVAILABLE"
    default_message = "Supplier sync could not fetch the full feed."


def _flatten_drf_errors(detail):
    """Flatten DRF error detail structures into a clean list of details."""
    messages = []
    if isinstance(detail, dict):
        for k, v in detail.items():
            sub = _flatten_drf_errors(v)
            for m in sub:
                messages.append(f"{k}: {m}" if k != "non_field_errors" else m)
    elif isinstance(detail, list):
        for item in detail:
            messages.extend(_flatten_drf_errors(item))
    else:
        messages.append(str(detail))
    return messages


def custom_exception_handler(exc, context):
    """
    Central DRF exception handler.
    Always returns:
    {"error": {"code": "...", "message": "...", "details": [...]}}
    """
    if isinstance(exc, BaseAppException):
        return Response(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
            status=exc.status_code,
        )

    # Let DRF handle standard exceptions (404, 400 validation, parse errors, etc.)
    response = exception_handler(exc, context)

    if response is not None:
        status_code = response.status_code
        error_code = "VALIDATION_ERROR"

        if status_code == 404:
            error_code = "NOT_FOUND"
            message = "Resource not found."
            details = []
        elif status_code == 400:
            error_code = "VALIDATION_ERROR"
            details = _flatten_drf_errors(response.data)
            message = details[0] if details else "Invalid input data."
        elif status_code == 409:
            error_code = "DUPLICATE"
            details = _flatten_drf_errors(response.data)
            message = details[0] if details else "Conflict error."
        else:
            details = _flatten_drf_errors(response.data)
            message = details[0] if details else "An error occurred."

        return Response(
            {
                "error": {
                    "code": error_code,
                    "message": message,
                    "details": details,
                }
            },
            status=status_code,
        )

    # Unhandled server errors (500)
    logger.exception("Unhandled server exception: %s", exc)
    return Response(
        {
            "error": {
                "code": "SERVER_ERROR",
                "message": "Internal server error.",
                "details": [],
            }
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def django_404_handler(request, exception=None):
    """Handler for 404 errors occurring outside DRF view dispatch."""
    return JsonResponse(
        {
            "error": {
                "code": "NOT_FOUND",
                "message": "The requested URL was not found.",
                "details": [],
            }
        },
        status=404,
    )


def django_500_handler(request):
    """Handler for 500 errors occurring outside DRF view dispatch."""
    return JsonResponse(
        {
            "error": {
                "code": "SERVER_ERROR",
                "message": "Internal server error.",
                "details": [],
            }
        },
        status=500,
    )
