"""Views for Order API endpoints."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from core.exceptions import ResourceNotFoundError, ValidationError
from core.pagination import StandardResultsSetPagination
from orders.models import Order
from orders.serializers import OrderSerializer
from orders.services import OrderService


class OrderListCreateView(APIView):
    """
    POST /api/orders/ — Creates DRAFT order with validation & optional idempotency.
    GET /api/orders/ — Paginated list with dealer_id and status filters.
    """

    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")

        dealer_id = data.get("dealer_id")
        if dealer_id is None:
            raise ValidationError("Field 'dealer_id' is required.")

        items = data.get("items")
        if items is None:
            raise ValidationError("Field 'items' is required.")

        idempotency_key = request.headers.get("Idempotency-Key") or request.META.get("HTTP_IDEMPOTENCY_KEY")
        raw_body = request.body

        snapshot, status_code = OrderService.create_draft_order(
            dealer_id=dealer_id,
            items_data=items,
            idempotency_key_str=idempotency_key,
            request_raw_body=raw_body,
        )
        return Response(snapshot, status=status_code)

    def get(self, request):
        qs = Order.objects.prefetch_related("items__product").select_related("dealer").all()

        dealer_id = request.query_params.get("dealer_id")
        if dealer_id:
            try:
                qs = qs.filter(dealer_id=int(dealer_id))
            except ValueError:
                pass

        order_status = request.query_params.get("status")
        if order_status:
            qs = qs.filter(status=order_status.strip())

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        if page is not None:
            serializer = OrderSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = OrderSerializer(qs, many=True)
        return Response(serializer.data)


class OrderDetailUpdateView(APIView):
    """
    GET /api/orders/{order_number}/ — Retrieve order.
    PUT /api/orders/{order_number}/ — Replace all items in DRAFT order.
    """

    def get(self, request, order_number):
        try:
            order = Order.objects.prefetch_related("items__product").select_related("dealer").get(
                order_number=order_number
            )
        except Order.DoesNotExist:
            raise ResourceNotFoundError(f"Order '{order_number}' not found.")
        return Response(OrderSerializer(order).data)

    def put(self, request, order_number):
        data = request.data
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")

        items = data.get("items")
        if items is None:
            raise ValidationError("Field 'items' is required.")

        updated_snapshot = OrderService.update_draft_order(order_number, items)
        return Response(updated_snapshot, status=status.HTTP_200_OK)


class OrderConfirmView(APIView):
    """POST /api/orders/{order_number}/confirm/"""

    def post(self, request, order_number):
        order_data = OrderService.confirm_order(order_number)
        return Response(order_data, status=status.HTTP_200_OK)


class OrderDispatchView(APIView):
    """POST /api/orders/{order_number}/dispatch/"""

    def post(self, request, order_number):
        order_data = OrderService.dispatch_order(order_number)
        return Response(order_data, status=status.HTTP_200_OK)


class OrderDeliverView(APIView):
    """POST /api/orders/{order_number}/deliver/"""

    def post(self, request, order_number):
        order_data = OrderService.deliver_order(order_number)
        return Response(order_data, status=status.HTTP_200_OK)


class OrderCancelView(APIView):
    """POST /api/orders/{order_number}/cancel/"""

    def post(self, request, order_number):
        order_data = OrderService.cancel_order(order_number)
        return Response(order_data, status=status.HTTP_200_OK)
