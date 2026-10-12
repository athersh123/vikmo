from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


def dashboard_view(request):
    """Serves the interactive VIKMO DealerDesk command portal."""
    return render(request, "index.html")


@csrf_exempt
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
