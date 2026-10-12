"""Views for Supplier synchronization API."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from supplier.sync_service import SupplierSyncService


class SupplierSyncView(APIView):
    """
    POST /api/sync/supplier/ (no body)
    Runs one full sync from external supplier API and returns summary report.
    Returns:
        200: {"created", "updated", "unchanged", "rejected", "pages_fetched", "requests_made"}
        502: {"error": {"code": "SUPPLIER_UNAVAILABLE", ...}} on network failure or auth error.
    """

    def post(self, request):
        result = SupplierSyncService.run_sync()
        return Response(result, status=status.HTTP_200_OK)
