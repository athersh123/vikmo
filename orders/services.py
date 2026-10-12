"""Business logic layer for Order lifecycle and financial calculations."""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from catalog.models import Dealer, Inventory, Product
from core.exceptions import (
    DuplicateError,
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidTransitionError,
    OrderLockedError,
    ResourceNotFoundError,
    ValidationError,
)
from orders.models import IdempotencyKey, Order, OrderItem, OrderNumberCounter, OrderStatus

GST_RATE = Decimal("0.18")
CENTS = Decimal("0.01")
KOLKATA_TZ = ZoneInfo("Asia/Kolkata")


def quantize_money(amount: Decimal) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)


def generate_order_number() -> str:
    """
    Generate sequential gap-safe order number ORD-YYYYMMDD-NNNN.
    Date is based on Asia/Kolkata timezone.
    Uses select_for_update on OrderNumberCounter for concurrency safety.
    """
    now = timezone.now().astimezone(KOLKATA_TZ)
    today = now.date()
    date_str = today.strftime("%Y%m%d")

    counter, _ = OrderNumberCounter.objects.select_for_update().get_or_create(
        date=today,
        defaults={"last_seq": 0},
    )
    counter.last_seq += 1
    counter.save()

    return f"ORD-{date_str}-{counter.last_seq:04d}"


def calculate_line_item(unit_price: Decimal, quantity: int):
    """
    Calculate financial line item values per contract §7.4:
    line_subtotal = unit_price * quantity
    line_tax = round_half_up(line_subtotal * 0.18)
    line_total = line_subtotal + line_tax
    """
    subtotal = quantize_money(unit_price * quantity)
    tax = quantize_money(subtotal * GST_RATE)
    total = quantize_money(subtotal + tax)
    return subtotal, tax, total


def validate_and_prepare_items(items_data):
    """
    Validates items payload:
    - At least 1 item
    - quantity integer >= 1 (no bool, float, str, <= 0)
    - SKU exists
    - No duplicate SKU in one order
    """
    if not isinstance(items_data, list) or len(items_data) == 0:
        raise ValidationError("Order must contain at least one item.")

    seen_skus = set()
    prepared = []

    for item in items_data:
        if not isinstance(item, dict):
            raise ValidationError("Each item must be an object.")

        sku = item.get("sku")
        if not sku or not isinstance(sku, str):
            raise ValidationError("Item 'sku' is required.")
        sku = sku.strip()

        if sku in seen_skus:
            raise ValidationError(f"Duplicate SKU '{sku}' in order.")
        seen_skus.add(sku)

        quantity = item.get("quantity")
        if type(quantity) is not int or isinstance(quantity, bool) or quantity < 1:
            raise ValidationError(f"Quantity for SKU '{sku}' must be an integer >= 1.")

        try:
            product = Product.objects.get(sku=sku)
        except Product.DoesNotExist:
            raise ValidationError(f"Product with SKU '{sku}' does not exist.")

        prepared.append({"product": product, "quantity": quantity})

    return prepared


class OrderService:
    """Service handling all order state machine transitions and calculations."""

    @staticmethod
    def create_draft_order(dealer_id: int, items_data: list, idempotency_key_str: str = None, request_raw_body: bytes = b""):
        """
        Create a new DRAFT order with transactional safety and optional idempotency.
        """
        request_hash = hashlib.sha256(request_raw_body).hexdigest() if idempotency_key_str else ""

        with transaction.atomic():
            if idempotency_key_str:
                existing_key = IdempotencyKey.objects.select_for_update().filter(key=idempotency_key_str).first()
                if existing_key:
                    if existing_key.request_hash != request_hash:
                        raise IdempotencyConflictError("Idempotency key reused with different request payload.")
                    return existing_key.response_snapshot, existing_key.status_code

            # Validate dealer
            try:
                dealer = Dealer.objects.get(pk=dealer_id)
            except (Dealer.DoesNotExist, ValueError, TypeError):
                raise ValidationError(f"Dealer with id '{dealer_id}' does not exist.")

            prepared_items = validate_and_prepare_items(items_data)

            order_number = generate_order_number()

            subtotal_sum = Decimal("0.00")
            tax_sum = Decimal("0.00")

            order = Order.objects.create(
                order_number=order_number,
                dealer=dealer,
                status=OrderStatus.DRAFT,
                subtotal=Decimal("0.00"),
                tax_total=Decimal("0.00"),
                grand_total=Decimal("0.00"),
            )

            for prep in prepared_items:
                product = prep["product"]
                qty = prep["quantity"]
                unit_price = product.price

                line_subtotal, line_tax, line_total = calculate_line_item(unit_price, qty)
                subtotal_sum += line_subtotal
                tax_sum += line_tax

                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=qty,
                    unit_price=unit_price,
                    line_subtotal=line_subtotal,
                    line_tax=line_tax,
                    line_total=line_total,
                )

            order.subtotal = quantize_money(subtotal_sum)
            order.tax_total = quantize_money(tax_sum)
            order.grand_total = quantize_money(subtotal_sum + tax_sum)
            order.save()

            from orders.serializers import OrderSerializer
            snapshot = OrderSerializer(order).data

            if idempotency_key_str:
                IdempotencyKey.objects.create(
                    key=idempotency_key_str,
                    request_hash=request_hash,
                    order=order,
                    response_snapshot=snapshot,
                    status_code=201,
                )

            return snapshot, 201

    @staticmethod
    def update_draft_order(order_number: str, items_data: list):
        """
        PUT /api/orders/{order_number}/
        Replaces all items. Allowed only while DRAFT, else 409 ORDER_LOCKED.
        """
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            if order.status != OrderStatus.DRAFT:
                raise OrderLockedError("Only DRAFT orders can be modified.")

            prepared_items = validate_and_prepare_items(items_data)

            order.items.all().delete()

            subtotal_sum = Decimal("0.00")
            tax_sum = Decimal("0.00")

            for prep in prepared_items:
                product = prep["product"]
                qty = prep["quantity"]
                unit_price = product.price

                line_subtotal, line_tax, line_total = calculate_line_item(unit_price, qty)
                subtotal_sum += line_subtotal
                tax_sum += line_tax

                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=qty,
                    unit_price=unit_price,
                    line_subtotal=line_subtotal,
                    line_tax=line_tax,
                    line_total=line_total,
                )

            order.subtotal = quantize_money(subtotal_sum)
            order.tax_total = quantize_money(tax_sum)
            order.grand_total = quantize_money(subtotal_sum + tax_sum)
            order.save()

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data

    @staticmethod
    def confirm_order(order_number: str):
        """
        DRAFT -> CONFIRMED.
        Validates stock under select_for_update locks. All-or-nothing.
        Deducts stock on success.
        """
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            if order.status != OrderStatus.DRAFT:
                raise InvalidTransitionError(f"Cannot confirm order in '{order.status}' status.")

            order_items = list(order.items.select_related("product").all())
            product_ids = [it.product_id for it in order_items]

            # Lock inventory rows ordered by id to prevent deadlocks
            inventories = {
                inv.product_id: inv
                for inv in Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("id")
            }

            insufficient_details = []
            insufficient_messages = []

            for item in order_items:
                inv = inventories.get(item.product_id)
                available = inv.quantity if inv else 0
                if item.quantity > available:
                    insufficient_details.append(
                        {
                            "sku": item.product.sku,
                            "requested": item.quantity,
                            "available": available,
                        }
                    )
                    insufficient_messages.append(
                        f"Insufficient stock for {item.product.name}. Available: {available}, Requested: {item.quantity}."
                    )

            if insufficient_details:
                message = " ".join(insufficient_messages)
                raise InsufficientStockError(
                    message=message,
                    details=insufficient_details,
                )

            # Deduct stock
            for item in order_items:
                inv = inventories[item.product_id]
                inv.quantity -= item.quantity
                inv.save()

            order.status = OrderStatus.CONFIRMED
            order.stock_deducted = True
            order.save()

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data

    @staticmethod
    def dispatch_order(order_number: str):
        """CONFIRMED -> DISPATCHED."""
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            if order.status != OrderStatus.CONFIRMED:
                raise InvalidTransitionError(f"Cannot dispatch order in '{order.status}' status.")

            order.status = OrderStatus.DISPATCHED
            order.save()

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data

    @staticmethod
    def deliver_order(order_number: str):
        """DISPATCHED -> DELIVERED."""
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            if order.status != OrderStatus.DISPATCHED:
                raise InvalidTransitionError(f"Cannot deliver order in '{order.status}' status.")

            order.status = OrderStatus.DELIVERED
            order.save()

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data

    @staticmethod
    def cancel_order(order_number: str):
        """
        DRAFT -> CANCELLED or CONFIRMED -> CANCELLED (restores stock).
        """
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            if order.status not in (OrderStatus.DRAFT, OrderStatus.CONFIRMED):
                raise InvalidTransitionError(f"Cannot cancel order in '{order.status}' status.")

            if order.status == OrderStatus.CONFIRMED and order.stock_deducted:
                order_items = list(order.items.select_related("product").all())
                product_ids = [it.product_id for it in order_items]

                inventories = {
                    inv.product_id: inv
                    for inv in Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("id")
                }

                for item in order_items:
                    inv = inventories.get(item.product_id)
                    if inv:
                        inv.quantity += item.quantity
                        inv.save()

                order.stock_deducted = False

            order.status = OrderStatus.CANCELLED
            order.save()

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data
