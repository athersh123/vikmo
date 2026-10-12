"""API views for Forecasting and Reorder Suggestions."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from forecasting.services import ForecastService


class SkuForecastView(APIView):
    """
    GET /api/forecast/{sku}/
    Returns 4-week forecast for specified SKU.
    404 NOT_FOUND if SKU not in lead_times.csv or catalogue.
    """

    def get(self, request, sku: str):
        data = ForecastService.get_sku_forecast(sku.strip())
        return Response(data, status=status.HTTP_200_OK)


class ReorderSuggestionsView(APIView):
    """
    GET /api/reorder-suggestions/
    Returns reorder suggestions across all 40 forecast SKUs with live inventory.
    """

    def get(self, request):
        results = ForecastService.get_reorder_suggestions()
        return Response({"results": results}, status=status.HTTP_200_OK)
