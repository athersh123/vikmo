"""Comprehensive tests for Orders lifecycle, pricing, stock rules, and idempotency."""
from decimal import Decimal
import re
from django.test import TestCase
from rest_framework.test import APIClient
from catalog.models import Dealer, Inventory, Product
from orders.models import Order, OrderStatus

ORDER_NO_PATTERN = re.compile(r"^ORD-\d{8}-\d{4}$")
MONEY_PATTERN = re.compile(r"^\d+\.\d{2}$")


class OrderLifecycleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dealer = Dealer.objects.create(
            dealer_code="DLR-TEST-1",
            name="Test Moto Dealer",
            credit_limit=Decimal("100000.00"),
        )
        self.p1 = Product.objects.create(
            sku="BRK-1044",
            name="Brake Pad Set - Bajaj Pulsar 150",
            category="Brakes",
            brand="Endurance",
            vehicle_fitment="Bajaj Pulsar 150",
            price=Decimal("1459.50"),
        )
        self.inv1 = Inventory.objects.create(product=self.p1, quantity=10)

        self.p2 = Product.objects.create(
            sku="OIL-1001",
            name="Coolant 1L",
            category="Engine & Oils",
            brand="Motul",
            vehicle_fitment="Universal",
            price=Decimal("604.25"),
        )
        self.inv2 = Inventory.objects.create(product=self.p2, quantity=5)

    def test_create_draft_order_validations(self):
        # Empty items -> 400 VALIDATION_ERROR
        res = self.client.post("/api/orders/", {"dealer_id": self.dealer.id, "items": []}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

        # Non-existent dealer -> 400 VALIDATION_ERROR
        res = self.client.post(
            "/api/orders/",
            {"dealer_id": 99999, "items": [{"sku": "BRK-1044", "quantity": 1}]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

        # Non-existent SKU -> 400 VALIDATION_ERROR
        res = self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": "NONEXISTENT", "quantity": 1}]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

        # Duplicate SKU in single order -> 400 VALIDATION_ERROR
        res = self.client.post(
            "/api/orders/",
            {
                "dealer_id": self.dealer.id,
                "items": [
                    {"sku": "BRK-1044", "quantity": 1},
                    {"sku": "BRK-1044", "quantity": 2},
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

        # Invalid quantity (0, bool, float, negative)
        for invalid_qty in [0, -1, True, False, 2.5]:
            res = self.client.post(
                "/api/orders/",
                {"dealer_id": self.dealer.id, "items": [{"sku": "BRK-1044", "quantity": invalid_qty}]},
                format="json",
            )
            self.assertEqual(res.status_code, 400)

    def test_create_order_pricing_and_money_format(self):
        # 3 units of BRK-1044 @ 1459.50
        # line_subtotal = 3 * 1459.50 = 4378.50
        # line_tax = 4378.50 * 0.18 = 788.13
        # line_total = 5166.63
        payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "BRK-1044", "quantity": 3}],
        }
        res = self.client.post("/api/orders/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()

        self.assertTrue(ORDER_NO_PATTERN.match(data["order_number"]))
        self.assertEqual(data["status"], "DRAFT")
        self.assertEqual(data["subtotal"], "4378.50")
        self.assertEqual(data["tax_total"], "788.13")
        self.assertEqual(data["grand_total"], "5166.63")

        item = data["items"][0]
        self.assertEqual(item["sku"], "BRK-1044")
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["unit_price"], "1459.50")
        self.assertEqual(item["line_subtotal"], "4378.50")
        self.assertEqual(item["line_tax"], "788.13")
        self.assertEqual(item["line_total"], "5166.63")

        # Draft order must NOT change stock
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 10)

    def test_idempotency_key(self):
        payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "BRK-1044", "quantity": 1}],
        }
        headers = {"HTTP_IDEMPOTENCY_KEY": "test-key-12345"}

        # First attempt -> 201
        res1 = self.client.post("/api/orders/", payload, format="json", **headers)
        self.assertEqual(res1.status_code, 201)
        order_num = res1.json()["order_number"]

        # Duplicate request with same key & body -> returns original snapshot
        res2 = self.client.post("/api/orders/", payload, format="json", **headers)
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(res2.json()["order_number"], order_num)

        # Same key with different body -> 409 IDEMPOTENCY_CONFLICT
        different_payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "OIL-1001", "quantity": 2}],
        }
        res3 = self.client.post("/api/orders/", different_payload, format="json", **headers)
        self.assertEqual(res3.status_code, 409)
        self.assertEqual(res3.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_order_edit_and_lock(self):
        # Create draft
        res = self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": "BRK-1044", "quantity": 1}]},
            format="json",
        )
        order_number = res.json()["order_number"]

        # PUT replaces items
        res_put = self.client.put(
            f"/api/orders/{order_number}/",
            {"items": [{"sku": "OIL-1001", "quantity": 2}]},
            format="json",
        )
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(len(res_put.json()["items"]), 1)
        self.assertEqual(res_put.json()["items"][0]["sku"], "OIL-1001")

        # Confirm order
        self.client.post(f"/api/orders/{order_number}/confirm/")

        # Attempt to edit confirmed order -> 409 ORDER_LOCKED
        res_locked = self.client.put(
            f"/api/orders/{order_number}/",
            {"items": [{"sku": "BRK-1044", "quantity": 1}]},
            format="json",
        )
        self.assertEqual(res_locked.status_code, 409)
        self.assertEqual(res_locked.json()["error"]["code"], "ORDER_LOCKED")

    def test_confirm_stock_deduction_and_insufficient_stock(self):
        # Create order requesting 12 units when only 10 exist
        res = self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": "BRK-1044", "quantity": 12}]},
            format="json",
        )
        order_number = res.json()["order_number"]

        # Confirm -> 409 INSUFFICIENT_STOCK
        conf_res = self.client.post(f"/api/orders/{order_number}/confirm/")
        self.assertEqual(conf_res.status_code, 409)
        err = conf_res.json()["error"]
        self.assertEqual(err["code"], "INSUFFICIENT_STOCK")
        self.assertEqual(len(err["details"]), 1)
        self.assertEqual(err["details"][0]["sku"], "BRK-1044")
        self.assertEqual(err["details"][0]["requested"], 12)
        self.assertEqual(err["details"][0]["available"], 10)

        # Stock unchanged
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 10)

        # Update order to valid quantity (3)
        self.client.put(f"/api/orders/{order_number}/", {"items": [{"sku": "BRK-1044", "quantity": 3}]}, format="json")

        # Confirm succeeds
        ok_conf = self.client.post(f"/api/orders/{order_number}/confirm/")
        self.assertEqual(ok_conf.status_code, 200)
        self.assertEqual(ok_conf.json()["status"], "CONFIRMED")

        # Stock deducted to 7
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 7)

    def test_full_lifecycle_and_cancellation_restores_stock(self):
        res = self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": "OIL-1001", "quantity": 2}]},
            format="json",
        )
        order_number = res.json()["order_number"]

        # Confirm
        self.client.post(f"/api/orders/{order_number}/confirm/")
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv2.quantity, 3)

        # Cancel confirmed order -> restores stock to 5
        res_cancel = self.client.post(f"/api/orders/{order_number}/cancel/")
        self.assertEqual(res_cancel.status_code, 200)
        self.assertEqual(res_cancel.json()["status"], "CANCELLED")

        self.inv2.refresh_from_db()
        self.assertEqual(self.inv2.quantity, 5)

        # Re-cancelling or acting on cancelled order -> 409 INVALID_TRANSITION
        bad_act = self.client.post(f"/api/orders/{order_number}/confirm/")
        self.assertEqual(bad_act.status_code, 409)
        self.assertEqual(bad_act.json()["error"]["code"], "INVALID_TRANSITION")
