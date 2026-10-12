"""Serializers for Catalog API."""
from decimal import Decimal
import re
from rest_framework import serializers
from catalog.models import Dealer, Inventory, InventoryAdjustment, Product
from core.exceptions import DuplicateError, ValidationError

MONEY_REGEX = re.compile(r"^\d+(\.\d{1,2})?$")


def format_money(val: Decimal) -> str:
    """Format decimal amount as a 2-decimal string: '1459.50'."""
    if val is None:
        return "0.00"
    return f"{Decimal(val):.2f}"


def validate_money_amount(value) -> Decimal:
    """Ensure value is a positive decimal with at most 2 decimal places."""
    try:
        dec = Decimal(str(value))
    except Exception:
        raise ValidationError("Invalid price format.")

    if dec <= Decimal("0.00"):
        raise ValidationError("Price must be greater than 0.")

    # Check decimal places
    tup = dec.as_tuple()
    if tup.exponent < -2:
        raise ValidationError("Price cannot have more than 2 decimal places.")

    return dec


class ProductSerializer(serializers.ModelSerializer):
    stock = serializers.SerializerMethodField()
    price = serializers.CharField()

    class Meta:
        model = Product
        fields = [
            "sku",
            "name",
            "category",
            "brand",
            "vehicle_fitment",
            "price",
            "stock",
            "description",
        ]
        extra_kwargs = {
            "sku": {"validators": []},
        }

    def get_stock(self, obj: Product) -> int:
        if hasattr(obj, "inventory"):
            return obj.inventory.quantity
        return 0

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["price"] = format_money(instance.price)
        return ret

    def validate_price(self, value):
        return validate_money_amount(value)

    def validate_sku(self, value):
        sku = str(value).strip()
        if not sku:
            raise ValidationError("SKU cannot be empty.")
        return sku

    def create(self, validated_data):
        product = Product.objects.create(**validated_data)
        # By contract, POST /api/products/ creates product with stock 0
        Inventory.objects.get_or_create(product=product, defaults={"quantity": 0})
        return product

    def update(self, instance, validated_data):
        # SKU is immutable
        validated_data.pop("sku", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()
        return instance


class DealerSerializer(serializers.ModelSerializer):
    credit_limit = serializers.CharField(required=False, default="0.00")

    class Meta:
        model = Dealer
        fields = [
            "id",
            "dealer_code",
            "name",
            "city",
            "phone",
            "email",
            "gstin",
            "credit_limit",
        ]
        extra_kwargs = {
            "dealer_code": {"validators": []},
        }

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["credit_limit"] = format_money(instance.credit_limit)
        return ret

    def validate_dealer_code(self, value):
        code = str(value).strip()
        if not code:
            raise ValidationError("dealer_code cannot be empty.")
        return code

    def validate_credit_limit(self, value):
        try:
            dec = Decimal(str(value))
        except Exception:
            raise ValidationError("Invalid credit_limit format.")
        if dec < Decimal("0.00"):
            raise ValidationError("credit_limit cannot be negative.")
        return dec
