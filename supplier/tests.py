"""Comprehensive tests for Supplier sync: retries, backoff, deduplication, and DB invariants."""
from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.test import TestCase
import requests

from catalog.models import Inventory, Product
from core.exceptions import SupplierUnavailableError
from supplier.client import SupplierClient
from supplier.sync_service import SupplierSyncService


class SupplierSyncTests(TestCase):
    def setUp(self):
        # Create an existing product with stock
        self.p_existing = Product.objects.create(
            sku="BRK-1001",
            name="Existing Brake Pad",
            category="Brakes",
            brand="OEM",
            vehicle_fitment="Pulsar",
            price=Decimal("500.00"),
            description="Existing description",
        )
        self.inv_existing = Inventory.objects.create(
            product=self.p_existing,
            quantity=88,
        )

    def test_client_retry_429_with_retry_after(self):
        """Client sleeps for Retry-After and retries the same page."""
        session = MagicMock()

        # Mock 429 on first attempt, 200 on second attempt
        resp_429 = MagicMock(status_code=429, headers={"Retry-After": "0.01"})
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "items": [{"sku": "NEW-01", "price": "100.00"}],
            "next_cursor": None,
        }

        session.get.side_effect = [resp_429, resp_200]

        client = SupplierClient(session=session)
        items, pages, requests_count = client.fetch_all_products()

        self.assertEqual(len(items), 1)
        self.assertEqual(pages, 1)
        self.assertEqual(requests_count, 2)
        self.assertEqual(session.get.call_count, 2)

    def test_client_5xx_recovery(self):
        """Client retries on 500 error with backoff and succeeds on subsequent attempt."""
        session = MagicMock()

        resp_500 = MagicMock(status_code=500)
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "items": [{"sku": "NEW-02", "price": "250.00"}],
            "next_cursor": None,
        }

        session.get.side_effect = [resp_500, resp_200]

        client = SupplierClient(session=session)
        with patch("time.sleep"):  # Speed up tests
            items, pages, reqs = client.fetch_all_products()

        self.assertEqual(len(items), 1)
        self.assertEqual(pages, 1)
        self.assertEqual(reqs, 2)

    def test_client_page_failing_5_times_raises_and_writes_nothing(self):
        """A page failing 5 times raises SupplierUnavailableError and writes nothing."""
        session = MagicMock()
        resp_503 = MagicMock(status_code=503)
        session.get.return_value = resp_503

        client = SupplierClient(session=session)

        with patch("time.sleep"):
            with self.assertRaises(SupplierUnavailableError):
                SupplierSyncService.run_sync(client=client)

        # Nothing written to database
        self.assertEqual(Product.objects.count(), 1)
        self.inv_existing.refresh_from_db()
        self.assertEqual(self.inv_existing.quantity, 88)

    def test_client_401_fast_fail(self):
        """401 raises SupplierUnavailableError immediately without retrying 5 times."""
        session = MagicMock()
        resp_401 = MagicMock(status_code=401)
        session.get.return_value = resp_401

        client = SupplierClient(session=session)

        with self.assertRaises(SupplierUnavailableError):
            client.fetch_all_products()

        self.assertEqual(session.get.call_count, 1)

    def test_invalid_price_and_sku_variants_rejected(self):
        """Invalid items are rejected and never fail the sync."""
        feed = [
            # Valid item
            {"sku": "VAL-01", "name": "Valid Item", "category": "C", "brand": "B", "vehicle_fitment": "V", "price": "100.00"},
            # Missing sku
            {"supplier_sku": "SUP-BAD1", "sku": "", "price": "100.00"},
            {"supplier_sku": "SUP-BAD2", "sku": "   ", "price": "100.00"},
            # Non-numeric price
            {"supplier_sku": "SUP-BAD3", "sku": "INV-01", "price": "N/A"},
            # Float with >2 decimals
            {"supplier_sku": "SUP-BAD4", "sku": "INV-02", "price": "12.345"},
            # Zero and negative price
            {"supplier_sku": "SUP-BAD5", "sku": "INV-03", "price": "0.00"},
            {"supplier_sku": "SUP-BAD6", "sku": "INV-04", "price": "-50.00"},
        ]

        client = MagicMock()
        client.fetch_all_products.return_value = (feed, 1, 1)

        result = SupplierSyncService.run_sync(client=client)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unchanged"], 0)
        self.assertEqual(len(result["rejected"]), 6)

        # Verify new item has stock 0
        new_prod = Product.objects.get(sku="VAL-01")
        self.assertEqual(new_prod.stock, 0)

    def test_duplicate_skus_latest_updated_at_wins_both_orders(self):
        """Duplicate SKUs: latest updated_at wins whether newer arrives first or last."""
        feed = [
            # Case 1: Newer arrives FIRST
            {
                "sku": "DUP-A",
                "name": "Newer A",
                "price": "120.00",
                "updated_at": "2026-10-10T12:00:00Z",
            },
            {
                "sku": "DUP-A",
                "name": "Older A",
                "price": "100.00",
                "updated_at": "2026-10-01T10:00:00Z",
            },
            # Case 2: Newer arrives LAST
            {
                "sku": "DUP-B",
                "name": "Older B",
                "price": "200.00",
                "updated_at": "2026-10-01T10:00:00Z",
            },
            {
                "sku": "DUP-B",
                "name": "Newer B",
                "price": "250.00",
                "updated_at": "2026-10-10T12:00:00Z",
            },
        ]

        client = MagicMock()
        client.fetch_all_products.return_value = (feed, 1, 1)

        result = SupplierSyncService.run_sync(client=client)

        self.assertEqual(result["created"], 2)

        prod_a = Product.objects.get(sku="DUP-A")
        self.assertEqual(prod_a.name, "Newer A")
        self.assertEqual(prod_a.price, Decimal("120.00"))

        prod_b = Product.objects.get(sku="DUP-B")
        self.assertEqual(prod_b.name, "Newer B")
        self.assertEqual(prod_b.price, Decimal("250.00"))

    def test_stock_unchanged_and_idempotent_rerun(self):
        """Supplier sync NEVER changes warehouse stock; re-run on unchanged feed updates 0 rows."""
        feed = [
            {
                "sku": "BRK-1001",  # Existing product
                "name": "Existing Brake Pad Updated",
                "category": "Brakes",
                "brand": "OEM",
                "vehicle_fitment": "Pulsar",
                "description": "Existing description",
                "price": "550.00",
                "supplier_stock": 9999,  # Must be ignored!
            }
        ]

        client = MagicMock()
        client.fetch_all_products.return_value = (feed, 1, 1)

        # First run -> updates price & name
        r1 = SupplierSyncService.run_sync(client=client)
        self.assertEqual(r1["created"], 0)
        self.assertEqual(r1["updated"], 1)
        self.assertEqual(r1["unchanged"], 0)

        # Invariant: warehouse stock remains 88!
        self.inv_existing.refresh_from_db()
        self.assertEqual(self.inv_existing.quantity, 88)

        # Second run with exact same feed -> idempotent: updated=0, unchanged=1
        r2 = SupplierSyncService.run_sync(client=client)
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["updated"], 0)
        self.assertEqual(r2["unchanged"], 1)

        # Stock still untouched
        self.inv_existing.refresh_from_db()
        self.assertEqual(self.inv_existing.quantity, 88)
