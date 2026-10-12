from django.http import JsonResponse


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


def not_found_view(request, *args, **kwargs):
    """Catch-all for unknown routes guaranteeing the contract error shape."""
    return JsonResponse(
        {
            "error": {
                "code": "NOT_FOUND",
                "message": "Resource not found.",
                "details": [],
            }
        },
        status=404,
    )
