"""Comprehensive tests for Catalog, Inventory, and Dealer APIs."""
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient
from catalog.models import Dealer, Inventory, InventoryAdjustment, Product


class CatalogAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.product1 = Product.objects.create(
            sku="BRK-1001",
            name="Front Brake Pads",
            category="Brakes",
            brand="Endurance",
            vehicle_fitment="Bajaj Pulsar 150",
            price=Decimal("450.50"),
            description="High quality brake pads",
        )
        self.inv1 = Inventory.objects.create(product=self.product1, quantity=100)

        self.product2 = Product.objects.create(
            sku="OIL-2001",
            name="Engine Oil 1L",
            category="Engine & Oils",
            brand="Motul",
            vehicle_fitment="Universal",
            price=Decimal("600.00"),
            description="Synthetic oil",
        )
        self.inv2 = Inventory.objects.create(product=self.product2, quantity=50)

        self.dealer = Dealer.objects.create(
            dealer_code="DLR-001",
            name="Speedway Motors",
            city="Chennai",
            phone="9876543210",
            email="speedway@example.com",
            credit_limit=Decimal("50000.00"),
        )

    def test_products_list_pagination_and_fields(self):
        res = self.client.get("/api/products/?page_size=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("count", data)
        self.assertIn("results", data)
        self.assertEqual(data["count"], 2)
        item = data["results"][0]
        self.assertEqual(item["sku"], "BRK-1001")
        self.assertEqual(item["price"], "450.50")
        self.assertEqual(item["stock"], 100)

    def test_products_filter_search(self):
        # Case-insensitive brand match
        res = self.client.get("/api/products/?search=motul")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["count"], 1)
        self.assertEqual(res.json()["results"][0]["sku"], "OIL-2001")

        # Category exact match
        res = self.client.get("/api/products/?category=Brakes")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["count"], 1)

        # Vehicle substring match
        res = self.client.get("/api/products/?vehicle=Pulsar")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["count"], 1)

    def test_product_detail_and_404(self):
        res = self.client.get("/api/products/BRK-1001/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Front Brake Pads")

        # Unknown SKU -> 404 with error shape
        res404 = self.client.get("/api/products/NONEXISTENT/")
        self.assertEqual(res404.status_code, 404)
        err = res404.json()["error"]
        self.assertEqual(err["code"], "NOT_FOUND")
        self.assertIsInstance(err["details"], list)

    def test_product_create_and_validation(self):
        # Successful create starts with stock 0
        payload = {
            "sku": "FLT-3001",
            "name": "Air Filter",
            "category": "Filters",
            "brand": "Bosch",
            "vehicle_fitment": "KTM Duke 200",
            "price": "299.99",
            "description": "OEM replacement filter",
        }
        res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["sku"], "FLT-3001")
        self.assertEqual(data["stock"], 0)

        # Duplicate SKU -> 409 DUPLICATE
        dup_res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(dup_res.status_code, 409)
        self.assertEqual(dup_res.json()["error"]["code"], "DUPLICATE")

        # Invalid price (<= 0)
        payload["sku"] = "FLT-3002"
        payload["price"] = "0.00"
        res_bad_price = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res_bad_price.status_code, 400)
        self.assertEqual(res_bad_price.json()["error"]["code"], "VALIDATION_ERROR")

        # Invalid price (> 2 decimals)
        payload["price"] = "19.999"
        res_decimals = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res_decimals.status_code, 400)

    def test_product_patch_sku_immutable(self):
        res = self.client.patch(
            "/api/products/BRK-1001/",
            {"price": "499.00", "sku": "NEW-SKU"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["sku"], "BRK-1001")  # Unchanged
        self.assertEqual(data["price"], "499.00")

    def test_inventory_update_strict_validation(self):
        # Valid update
        res = self.client.put(
            "/api/inventory/BRK-1001/",
            {"quantity": 75, "reason": "Cycle count audit"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"sku": "BRK-1001", "quantity": 75})

        # Verify audit log
        adj = InventoryAdjustment.objects.filter(product=self.product1).first()
        self.assertIsNotNone(adj)
        self.assertEqual(adj.new_quantity, 75)

        # Rejects boolean True
        res_bool = self.client.put("/api/inventory/BRK-1001/", {"quantity": True}, format="json")
        self.assertEqual(res_bool.status_code, 400)

        # Rejects string "50"
        res_str = self.client.put("/api/inventory/BRK-1001/", {"quantity": "50"}, format="json")
        self.assertEqual(res_str.status_code, 400)

        # Rejects float 25.5
        res_float = self.client.put("/api/inventory/BRK-1001/", {"quantity": 25.5}, format="json")
        self.assertEqual(res_float.status_code, 400)

        # Rejects negative integer -5
        res_neg = self.client.put("/api/inventory/BRK-1001/", {"quantity": -5}, format="json")
        self.assertEqual(res_neg.status_code, 400)

        # Unknown SKU -> 404 NOT_FOUND
        res_unknown = self.client.put("/api/inventory/UNKNOWN-001/", {"quantity": 10}, format="json")
        self.assertEqual(res_unknown.status_code, 404)
        self.assertEqual(res_unknown.json()["error"]["code"], "NOT_FOUND")

    def test_dealer_crud_and_duplicate(self):
        # Create dealer
        payload = {"dealer_code": "DLR-002", "name": "Apex Spares", "credit_limit": "25000.00"}
        res = self.client.post("/api/dealers/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["credit_limit"], "25000.00")

        # Duplicate dealer code -> 409 DUPLICATE
        dup = self.client.post("/api/dealers/", payload, format="json")
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(dup.json()["error"]["code"], "DUPLICATE")

        # Detail and 404
        d_id = res.json()["id"]
        res_get = self.client.get(f"/api/dealers/{d_id}/")
        self.assertEqual(res_get.status_code, 200)

        res_404 = self.client.get("/api/dealers/99999/")
        self.assertEqual(res_404.status_code, 404)
