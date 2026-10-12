"""Views for Catalog and Inventory APIs."""
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from catalog.models import Dealer, Inventory, InventoryAdjustment, Product
from catalog.serializers import DealerSerializer, ProductSerializer
from core.exceptions import ResourceNotFoundError, ValidationError


class ProductListCreateView(APIView):
    """
    GET /api/products/ — Paginated list with search, category, vehicle filters.
    POST /api/products/ — Create new product with stock 0.
    """

    def get(self, request):
        qs = Product.objects.select_related("inventory").all()

        search = request.query_params.get("search")
        if search:
            search = search.strip()
            qs = qs.filter(
                Q(sku__icontains=search)
                | Q(name__icontains=search)
                | Q(brand__icontains=search)
            )

        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category=category.strip())

        vehicle = request.query_params.get("vehicle")
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle.strip())

        paginator = self.request.versioning_scheme  # not used
        # Use standard pagination from settings
        from core.pagination import StandardResultsSetPagination

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        if page is not None:
            serializer = ProductSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = ProductSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        sku = request.data.get("sku") if isinstance(request.data, dict) else None
        if sku and Product.objects.filter(sku=str(sku).strip()).exists():
            from core.exceptions import DuplicateError
            raise DuplicateError(f"Product with SKU '{sku}' already exists.")

        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)


class ProductDetailView(APIView):
    """
    GET /api/products/{sku}/ — Retrieve product by SKU.
    PATCH /api/products/{sku}/ — Partial update product by SKU (SKU is immutable).
    """

    def get_object(self, sku):
        try:
            return Product.objects.select_related("inventory").get(sku=sku)
        except Product.DoesNotExist:
            raise ResourceNotFoundError(f"Product with SKU '{sku}' not found.")

    def get(self, request, sku):
        product = self.get_object(sku)
        return Response(ProductSerializer(product).data)

    def patch(self, request, sku):
        product = self.get_object(sku)
        serializer = ProductSerializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ProductSerializer(product).data)


class InventoryUpdateView(APIView):
    """
    PUT /api/inventory/{sku}/
    Body: {"quantity": 25, "reason": "stock count correction"}
    Sets available stock. Rejects booleans, floats, strings, negative quantities.
    Does not touch existing orders.
    """

    def put(self, request, sku):
        try:
            product = Product.objects.get(sku=sku)
        except Product.DoesNotExist:
            raise ResourceNotFoundError(f"Product with SKU '{sku}' not found.")

        data = request.data
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")

        if "quantity" not in data:
            raise ValidationError("Field 'quantity' is required.")

        quantity = data.get("quantity")
        # Strict validation: reject bools, floats, strings, negatives
        if type(quantity) is not int or isinstance(quantity, bool) or quantity < 0:
            raise ValidationError("Quantity must be a non-negative integer.")

        reason = str(data.get("reason", "manual update")).strip()

        with transaction.atomic():
            inv, _ = Inventory.objects.select_for_update().get_or_create(
                product=product,
                defaults={"quantity": quantity},
            )
            inv.quantity = quantity
            inv.save()

            InventoryAdjustment.objects.create(
                product=product,
                new_quantity=quantity,
                reason=reason,
            )

        return Response(
            {
                "sku": product.sku,
                "quantity": quantity,
            },
            status=status.HTTP_200_OK,
        )


class DealerListCreateView(APIView):
    """
    GET /api/dealers/ — Paginated list of dealers.
    POST /api/dealers/ — Create dealer (dealer_code and name required).
    """

    def get(self, request):
        qs = Dealer.objects.all()
        from core.pagination import StandardResultsSetPagination

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        if page is not None:
            serializer = DealerSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = DealerSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        code = request.data.get("dealer_code") if isinstance(request.data, dict) else None
        if code and Dealer.objects.filter(dealer_code=str(code).strip()).exists():
            from core.exceptions import DuplicateError
            raise DuplicateError(f"Dealer with code '{code}' already exists.")

        serializer = DealerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dealer = serializer.save()
        return Response(DealerSerializer(dealer).data, status=status.HTTP_201_CREATED)


class DealerDetailView(APIView):
    """
    GET /api/dealers/{id}/ — Retrieve dealer by numeric id.
    """

    def get(self, request, pk):
        try:
            dealer = Dealer.objects.get(pk=pk)
        except Dealer.DoesNotExist:
            raise ResourceNotFoundError(f"Dealer with id '{pk}' not found.")
        return Response(DealerSerializer(dealer).data)
