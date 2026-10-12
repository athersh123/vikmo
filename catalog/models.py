"""Catalog models for VIKMO DealerDesk."""
from decimal import Decimal
from django.db import models
from django.db.models import CheckConstraint, Q


class Product(models.Model):
    """
    Automotive spare part product catalog item.
    Stock lives in Inventory, not on Product, ensuring supplier catalog syncs never overwrite stock.
    """
    sku = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=128, db_index=True)
    brand = models.CharField(max_length=128, db_index=True)
    vehicle_fitment = models.CharField(max_length=255, db_index=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sku"]
        constraints = [
            CheckConstraint(
                check=Q(price__gt=Decimal("0.00")),
                name="check_product_price_gt_zero",
            )
        ]
        indexes = [
            models.Index(fields=["sku"], name="idx_product_sku"),
            models.Index(fields=["name"], name="idx_product_name"),
            models.Index(fields=["brand"], name="idx_product_brand"),
            models.Index(fields=["category"], name="idx_product_category"),
            models.Index(fields=["vehicle_fitment"], name="idx_product_vehicle_fitment"),
        ]

    def __str__(self):
        return f"{self.sku} - {self.name}"

    @property
    def stock(self) -> int:
        if hasattr(self, "inventory"):
            return self.inventory.quantity
        return 0


class Inventory(models.Model):
    """
    Available stock record for a Product.
    Decoupled with OneToOne relation (PROTECT) to ensure supplier sync never affects available stock.
    """
    product = models.OneToOneField(
        Product,
        on_delete=models.PROTECT,
        related_name="inventory",
    )
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(quantity__gte=0),
                name="check_inventory_quantity_gte_zero",
            )
        ]

    def __str__(self):
        return f"{self.product.sku}: {self.quantity}"


class InventoryAdjustment(models.Model):
    """
    Audit log for manual stock adjustments (PUT /api/inventory/{sku}/).
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="adjustments",
    )
    new_quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.product.sku} -> {self.new_quantity} ({self.reason})"


class Dealer(models.Model):
    """
    Authorized dealer account purchasing automotive components.
    """
    dealer_code = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=128, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    gstin = models.CharField(max_length=32, blank=True, default="")
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.dealer_code} - {self.name}"
