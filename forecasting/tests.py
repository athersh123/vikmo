"""Tests for Forecasting and Reorder Suggestions endpoints."""
import math
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Inventory, Product
from forecasting.services import ForecastService


class ForecastingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Seed test product and inventory for FIL-1016
        self.product = Product.objects.create(
            sku="FIL-1016",
            name="Purolator Air Filter",
            category="Filters",
            brand="Purolator",
            vehicle_fitment="Universal",
            price=Decimal("944.99"),
        )
        self.inventory = Inventory.objects.create(product=self.product, quantity=50)

    def test_forecast_sku_valid(self):
        """GET /api/forecast/{sku}/ returns 4 weeks of forecast."""
        res = self.client.get("/api/forecast/FIL-1016/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["sku"], "FIL-1016")
        self.assertEqual(len(data["forecast"]), 4)
        weeks = [x["week_start"] for x in data["forecast"]]
        self.assertEqual(weeks, ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"])
        for item in data["forecast"]:
            self.assertGreaterEqual(item["forecast_units"], 0)

    def test_forecast_sku_not_found(self):
        """GET /api/forecast/{sku}/ returns 404 for unknown or non-forecast SKU."""
        res = self.client.get("/api/forecast/NON-EXISTENT-SKU/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "NOT_FOUND")

    def test_reorder_suggestions_shape_and_count(self):
        """GET /api/reorder-suggestions/ returns 40 items with required fields."""
        res = self.client.get("/api/reorder-suggestions/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("results", data)
        results = data["results"]
        self.assertEqual(len(results), 40)

        required_keys = {
            "sku",
            "current_stock",
            "lead_time_weeks",
            "lead_time_demand",
            "safety_stock",
            "reorder_point",
            "reorder_qty",
        }
        for item in results:
            self.assertTrue(required_keys.issubset(item.keys()))
            self.assertGreaterEqual(item["reorder_qty"], 0)
            self.assertGreaterEqual(item["safety_stock"], 0)

    def test_reorder_math_hand_computed_sku(self):
        """Verify exact calculation for hand-computed SKU FIL-1016."""
        res = self.client.get("/api/reorder-suggestions/")
        self.assertEqual(res.status_code, 200)
        results = {r["sku"]: r for r in res.json()["results"]}
        self.assertIn("FIL-1016", results)
        item = results["FIL-1016"]

        # Lead time for FIL-1016 is 3 weeks
        L = 3
        self.assertEqual(item["lead_time_weeks"], L)
        self.assertEqual(item["current_stock"], 50)

        # Expected values from ForecastService
        fc = ForecastService.get_sku_forecast("FIL-1016")["forecast"]
        expected_ltd = round(sum(x["forecast_units"] for x in fc[:L]), 2)
        self.assertEqual(item["lead_time_demand"], expected_ltd)

        sales_12 = ForecastService.get_sales_history_12w()["FIL-1016"]
        import numpy as np
        sigma = float(np.std(sales_12, ddof=1))
        expected_ss = math.ceil(1.65 * sigma * math.sqrt(L))
        self.assertEqual(item["safety_stock"], expected_ss)

        expected_rop = round(expected_ltd + expected_ss, 2)
        self.assertEqual(item["reorder_point"], expected_rop)

        expected_rq = max(0, math.ceil(expected_rop - 50))
        self.assertEqual(item["reorder_qty"], expected_rq)

    def test_live_inventory_reflection(self):
        """Changing inventory via PUT /api/inventory/{sku}/ updates reorder_qty immediately."""
        # Check initial reorder qty with stock=50
        res1 = self.client.get("/api/reorder-suggestions/")
        item1 = next(r for r in res1.json()["results"] if r["sku"] == "FIL-1016")
        rop = item1["reorder_point"]

        # Update inventory to 500 (well above rop)
        put_res = self.client.put(
            "/api/inventory/FIL-1016/",
            {"quantity": 500, "reason": "Restock shipment received"},
            format="json",
        )
        self.assertEqual(put_res.status_code, 200)

        # Re-fetch reorder suggestions: current_stock should be 500, reorder_qty must be 0
        res2 = self.client.get("/api/reorder-suggestions/")
        item2 = next(r for r in res2.json()["results"] if r["sku"] == "FIL-1016")
        self.assertEqual(item2["current_stock"], 500)
        self.assertEqual(item2["reorder_qty"], 0)

        # Update inventory to 0 (completely depleted)
        put_res2 = self.client.put(
            "/api/inventory/FIL-1016/",
            {"quantity": 0, "reason": "Stock count adjustment"},
            format="json",
        )
        self.assertEqual(put_res2.status_code, 200)

        res3 = self.client.get("/api/reorder-suggestions/")
        item3 = next(r for r in res3.json()["results"] if r["sku"] == "FIL-1016")
        self.assertEqual(item3["current_stock"], 0)
        self.assertEqual(item3["reorder_qty"], math.ceil(rop))
