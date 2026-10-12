"""Orders models for VIKMO DealerDesk."""
from decimal import Decimal
from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint
from catalog.models import Dealer, Product


class OrderStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    CONFIRMED = "CONFIRMED", "Confirmed"
    DISPATCHED = "DISPATCHED", "Dispatched"
    DELIVERED = "DELIVERED", "Delivered"
    CANCELLED = "CANCELLED", "Cancelled"


class Order(models.Model):
    """
    Sales order placed by a dealer.
    Tracks state machine lifecycle and financial totals (exact to 2 decimal places).
    """
    order_number = models.CharField(max_length=64, unique=True, db_index=True)
    dealer = models.ForeignKey(
        Dealer,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    status = models.CharField(
        max_length=32,
        choices=OrderStatus.choices,
        default=OrderStatus.DRAFT,
        db_index=True,
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    stock_deducted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_number} ({self.status})"


class OrderItem(models.Model):
    """
    Individual line item in an Order.
    Financial amounts are calculated and locked per line item using ROUND_HALF_UP at 18% GST.
    """
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    line_tax = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(quantity__gte=1),
                name="check_order_item_quantity_gte_one",
            ),
            UniqueConstraint(
                fields=["order", "product"],
                name="unique_order_item_per_product",
            ),
        ]
        ordering = ["id"]

    def __str__(self):
        return f"{self.order.order_number}: {self.product.sku} x {self.quantity}"


class OrderNumberCounter(models.Model):
    """
    Daily sequence generator for gap-safe ORD-YYYYMMDD-NNNN order numbering.
    Uses select_for_update() row locking to avoid collisions under parallel threads.
    """
    date = models.DateField(unique=True)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Order Number Counter"
        verbose_name_plural = "Order Number Counters"

    def __str__(self):
        return f"{self.date}: {self.last_seq}"


class IdempotencyKey(models.Model):
    """
    Persisted idempotency token cache for POST /api/orders/.
    Prevents duplicate orders on client network retries.
    """
    key = models.CharField(max_length=255, unique=True, db_index=True)
    request_hash = models.CharField(max_length=64)
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idempotency_records",
    )
    response_snapshot = models.JSONField(default=dict)
    status_code = models.PositiveIntegerField(default=201)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.key
