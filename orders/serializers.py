"""Serializers for Order and OrderItem API."""
from rest_framework import serializers
from catalog.serializers import format_money
from orders.models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="product.sku", read_only=True)
    name = serializers.CharField(source="product.name", read_only=True)
    unit_price = serializers.SerializerMethodField()
    line_subtotal = serializers.SerializerMethodField()
    line_tax = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "sku",
            "name",
            "quantity",
            "unit_price",
            "line_subtotal",
            "line_tax",
            "line_total",
        ]

    def get_unit_price(self, obj: OrderItem) -> str:
        return format_money(obj.unit_price)

    def get_line_subtotal(self, obj: OrderItem) -> str:
        return format_money(obj.line_subtotal)

    def get_line_tax(self, obj: OrderItem) -> str:
        return format_money(obj.line_tax)

    def get_line_total(self, obj: OrderItem) -> str:
        return format_money(obj.line_total)


class OrderSerializer(serializers.ModelSerializer):
    dealer_id = serializers.IntegerField(source="dealer.id", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)
    subtotal = serializers.SerializerMethodField()
    tax_total = serializers.SerializerMethodField()
    grand_total = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(format="%Y-%m-%dT%H:%M:%S%z")
    updated_at = serializers.DateTimeField(format="%Y-%m-%dT%H:%M:%S%z")

    class Meta:
        model = Order
        fields = [
            "order_number",
            "dealer_id",
            "status",
            "items",
            "subtotal",
            "tax_total",
            "grand_total",
            "created_at",
            "updated_at",
        ]

    def get_subtotal(self, obj: Order) -> str:
        return format_money(obj.subtotal)

    def get_tax_total(self, obj: Order) -> str:
        return format_money(obj.tax_total)

    def get_grand_total(self, obj: Order) -> str:
        return format_money(obj.grand_total)
