"""Business logic layer for Order lifecycle, state transitions, and financial calculations."""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from zoneinfo import ZoneInfo

from django.db import connection, transaction
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

# Central transition matrix mapping action -> {from_status: to_status}
ORDER_TRANSITIONS = {
    "confirm": {
        OrderStatus.DRAFT: OrderStatus.CONFIRMED,
    },
    "dispatch": {
        OrderStatus.CONFIRMED: OrderStatus.DISPATCHED,
    },
    "deliver": {
        OrderStatus.DISPATCHED: OrderStatus.DELIVERED,
    },
    "cancel": {
        OrderStatus.DRAFT: OrderStatus.CANCELLED,
        OrderStatus.CONFIRMED: OrderStatus.CANCELLED,
    },
}


def quantize_money(amount: Decimal) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)


def calculate_line_item(unit_price: Decimal, quantity: int):
    """
    Calculate financial line item values per contract §7.4:
    line_subtotal = unit_price * quantity
    line_tax = round_half_up(line_subtotal * 0.18)
    line_total = line_subtotal + line_tax
    """
    subtotal = quantize_money(Decimal(unit_price) * quantity)
    tax = quantize_money(subtotal * GST_RATE)
    total = quantize_money(subtotal + tax)
    return subtotal, tax, total


def generate_order_number() -> str:
    """
    Generate sequential gap-safe order number ORD-YYYYMMDD-NNNN.
    Date is based on Asia/Kolkata timezone.
    Uses PostgreSQL atomic INSERT ... ON CONFLICT DO UPDATE RETURNING.
    """
    now = timezone.now().astimezone(KOLKATA_TZ)
    today = now.date()
    date_str = today.strftime("%Y%m%d")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders_ordernumbercounter ("date", "last_seq")
            VALUES (%s, 1)
            ON CONFLICT ("date")
            DO UPDATE SET "last_seq" = orders_ordernumbercounter."last_seq" + 1
            RETURNING "last_seq";
            """,
            [today],
        )
        seq = cursor.fetchone()[0]

    return f"ORD-{date_str}-{seq:04d}"


def canonicalize_payload(payload) -> str:
    """Generate SHA-256 hash of canonicalized JSON request payload."""
    if isinstance(payload, bytes):
        try:
            payload = json.loads(payload.decode("utf-8"))
        except Exception:
            return hashlib.sha256(payload).hexdigest()
    if isinstance(payload, dict):
        canonical_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    return hashlib.sha256(str(payload).encode("utf-8")).hexdigest()


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
    """Centralized service for Order lifecycles, state machine, and pricing."""

    @staticmethod
    def get_order_snapshot(order: Order, refresh_draft_prices: bool = True):
        """
        Produce serialized order representation.
        While in DRAFT, recomputes with current catalog prices.
        Once CONFIRMED, uses locked unit prices.
        """
        if order.status == OrderStatus.DRAFT and refresh_draft_prices:
            # Recompute draft totals against current product prices
            subtotal_sum = Decimal("0.00")
            tax_sum = Decimal("0.00")
            order_items = list(order.items.select_related("product").all())
            items_to_update = []

            for item in order_items:
                current_price = item.product.price
                line_sub, line_tx, line_tot = calculate_line_item(current_price, item.quantity)
                item.unit_price = current_price
                item.line_subtotal = line_sub
                item.line_tax = line_tx
                item.line_total = line_tot
                items_to_update.append(item)
                subtotal_sum += line_sub
                tax_sum += line_tx

            OrderItem.objects.bulk_update(
                items_to_update,
                fields=["unit_price", "line_subtotal", "line_tax", "line_total"],
            )
            order.subtotal = quantize_money(subtotal_sum)
            order.tax_total = quantize_money(tax_sum)
            order.grand_total = quantize_money(subtotal_sum + tax_sum)
            order.save(update_fields=["subtotal", "tax_total", "grand_total", "updated_at"])

        from orders.serializers import OrderSerializer
        return OrderSerializer(order).data

    @staticmethod
    def create_draft_order(dealer_id: int, items_data: list, idempotency_key_str: str = None, request_raw_body=b""):
        """
        Create a new DRAFT order with transactional safety and idempotency locking.
        """
        request_hash = canonicalize_payload(request_raw_body) if idempotency_key_str else ""

        with transaction.atomic():
            if idempotency_key_str:
                # Advisory lock serializes parallel requests with identical idempotency key
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [idempotency_key_str])

                existing_record = IdempotencyKey.objects.filter(key=idempotency_key_str).first()
                if existing_record:
                    if existing_record.request_hash != request_hash:
                        raise IdempotencyConflictError("Idempotency key reused with different request payload.")
                    return existing_record.response_snapshot, existing_record.status_code

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
    def _validate_transition(order: Order, action: str) -> str:
        """Validate and return target status using central transition table."""
        action_map = ORDER_TRANSITIONS.get(action, {})
        target_status = action_map.get(order.status)
        if not target_status:
            raise InvalidTransitionError(f"Action '{action}' is not allowed from status '{order.status}'.")
        return target_status

    @staticmethod
    def confirm_order(order_number: str):
        """
        DRAFT -> CONFIRMED.
        1. Row lock order row; re-check status == DRAFT.
        2. Lock inventory rows in consistent order (order_by product_id) to eliminate deadlocks.
        3. Check every line. If ANY is short: return 409 INSUFFICIENT_STOCK with complete details.
        4. Otherwise deduct stock (quantity = quantity - n), lock unit prices, recompute totals, set CONFIRMED.
        """
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            target_status = OrderService._validate_transition(order, "confirm")

            order_items = list(order.items.select_related("product").all())
            product_ids = sorted([it.product_id for it in order_items])

            # Lock inventory rows in consistent ascending product_id order to avoid deadlocks
            inventories = {
                inv.product_id: inv
                for inv in Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("product_id")
            }

            insufficient_details = []
            insufficient_sentences = []

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
                    insufficient_sentences.append(
                        f"Insufficient stock for {item.product.name}. Available: {available}, Requested: {item.quantity}"
                    )

            if insufficient_details:
                message = ". ".join(insufficient_sentences)
                raise InsufficientStockError(
                    message=message,
                    details=insufficient_details,
                )

            # Deduct stock and lock prices at current catalog price
            subtotal_sum = Decimal("0.00")
            tax_sum = Decimal("0.00")

            for item in order_items:
                inv = inventories[item.product_id]
                # Belt-and-braces stock check and decrement
                if inv.quantity < item.quantity:
                    raise InsufficientStockError("Insufficient stock available.")
                inv.quantity -= item.quantity
                inv.save(update_fields=["quantity", "updated_at"])

                # Lock unit price forever at confirm
                current_price = item.product.price
                line_sub, line_tx, line_tot = calculate_line_item(current_price, item.quantity)
                item.unit_price = current_price
                item.line_subtotal = line_sub
                item.line_tax = line_tx
                item.line_total = line_tot
                item.save(update_fields=["unit_price", "line_subtotal", "line_tax", "line_total"])

                subtotal_sum += line_sub
                tax_sum += line_tx

            order.subtotal = quantize_money(subtotal_sum)
            order.tax_total = quantize_money(tax_sum)
            order.grand_total = quantize_money(subtotal_sum + tax_sum)
            order.status = target_status
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

            target_status = OrderService._validate_transition(order, "dispatch")
            order.status = target_status
            order.save(update_fields=["status", "updated_at"])

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

            target_status = OrderService._validate_transition(order, "deliver")
            order.status = target_status
            order.save(update_fields=["status", "updated_at"])

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data

    @staticmethod
    def cancel_order(order_number: str):
        """
        DRAFT -> CANCELLED or CONFIRMED -> CANCELLED.
        Restores stock exactly once for CONFIRMED orders.
        DISPATCHED or DELIVERED cannot be cancelled.
        """
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(order_number=order_number)
            except Order.DoesNotExist:
                raise ResourceNotFoundError(f"Order '{order_number}' not found.")

            target_status = OrderService._validate_transition(order, "cancel")

            if order.status == OrderStatus.CONFIRMED and order.stock_deducted:
                order_items = list(order.items.select_related("product").all())
                product_ids = sorted([it.product_id for it in order_items])

                # Consistent order locking
                inventories = {
                    inv.product_id: inv
                    for inv in Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("product_id")
                }

                for item in order_items:
                    inv = inventories.get(item.product_id)
                    if inv:
                        inv.quantity += item.quantity
                        inv.save(update_fields=["quantity", "updated_at"])

                order.stock_deducted = False

            order.status = target_status
            order.save(update_fields=["status", "stock_deducted", "updated_at"])

            from orders.serializers import OrderSerializer
            return OrderSerializer(order).data
