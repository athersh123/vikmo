"""Comprehensive tests for Order engine: transitions, stock locks, money precision, and concurrency."""
import concurrent.futures
from decimal import Decimal
import re
from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from catalog.models import Dealer, Inventory, Product
from core.exceptions import (
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidTransitionError,
    OrderLockedError,
)
from orders.models import Order, OrderItem, OrderStatus
from orders.services import (
    calculate_line_item,
    generate_order_number,
    quantize_money,
    OrderService,
)

ORDER_NO_PATTERN = re.compile(r"^ORD-\d{8}-\d{4}$")


class OrderFinancialPrecisionTests(TestCase):
    """Exact GST calculation and half-up boundary test cases."""

    def test_gst_half_up_boundaries(self):
        # 0.05 * 0.18 = 0.009 -> rounds to 0.01
        sub, tax, tot = calculate_line_item(Decimal("0.05"), 1)
        self.assertEqual(sub, Decimal("0.05"))
        self.assertEqual(tax, Decimal("0.01"))
        self.assertEqual(tot, Decimal("0.06"))

        # 0.25 * 0.18 = 0.0450 -> rounds to 0.05 (ROUND_HALF_UP)
        sub, tax, tot = calculate_line_item(Decimal("0.25"), 1)
        self.assertEqual(sub, Decimal("0.25"))
        self.assertEqual(tax, Decimal("0.05"))
        self.assertEqual(tot, Decimal("0.30"))

        # 0.75 * 0.18 = 0.1350 -> rounds to 0.14
        sub, tax, tot = calculate_line_item(Decimal("0.75"), 1)
        self.assertEqual(sub, Decimal("0.75"))
        self.assertEqual(tax, Decimal("0.14"))
        self.assertEqual(tot, Decimal("0.89"))

        # 1459.50 * 3 = 4378.50 -> 4378.50 * 0.18 = 788.13
        sub, tax, tot = calculate_line_item(Decimal("1459.50"), 3)
        self.assertEqual(sub, Decimal("4378.50"))
        self.assertEqual(tax, Decimal("788.13"))
        self.assertEqual(tot, Decimal("5166.63"))

    def test_per_line_rounding_vs_total_rounding(self):
        # 3 distinct items with unit_price 0.25:
        # Per-line tax = 0.045 -> 0.05 each. Sum = 0.15.
        # Order total tax must be sum of line taxes (0.15), NOT (0.75 * 0.18 = 0.14).
        dealer = Dealer.objects.create(dealer_code="DLR-FIN", name="Fin Dealer")
        p1 = Product.objects.create(sku="P1", name="P1", category="C", brand="B", vehicle_fitment="V", price=Decimal("0.25"))
        p2 = Product.objects.create(sku="P2", name="P2", category="C", brand="B", vehicle_fitment="V", price=Decimal("0.25"))
        p3 = Product.objects.create(sku="P3", name="P3", category="C", brand="B", vehicle_fitment="V", price=Decimal("0.25"))
        Inventory.objects.create(product=p1, quantity=10)
        Inventory.objects.create(product=p2, quantity=10)
        Inventory.objects.create(product=p3, quantity=10)

        snapshot, _ = OrderService.create_draft_order(
            dealer_id=dealer.id,
            items_data=[
                {"sku": "P1", "quantity": 1},
                {"sku": "P2", "quantity": 1},
                {"sku": "P3", "quantity": 1},
            ],
        )

        self.assertEqual(snapshot["subtotal"], "0.75")
        self.assertEqual(snapshot["tax_total"], "0.15")  # 0.05 * 3
        self.assertEqual(snapshot["grand_total"], "0.90")


class OrderStateMachineTests(TestCase):
    """State transition matrix, price lock, and all-or-nothing stock checks."""

    def setUp(self):
        self.dealer = Dealer.objects.create(dealer_code="DLR-SM", name="State Machine Dealer")
        self.p1 = Product.objects.create(
            sku="SKU-A",
            name="Product A",
            category="Brakes",
            brand="Endurance",
            vehicle_fitment="Fit",
            price=Decimal("100.00"),
        )
        self.p2 = Product.objects.create(
            sku="SKU-B",
            name="Product B",
            category="Brakes",
            brand="Endurance",
            vehicle_fitment="Fit",
            price=Decimal("50.00"),
        )
        self.inv1 = Inventory.objects.create(product=self.p1, quantity=5)
        self.inv2 = Inventory.objects.create(product=self.p2, quantity=2)

    def test_all_or_nothing_multiple_short_lines(self):
        # Request 10 of SKU-A (available 5) and 4 of SKU-B (available 2)
        snapshot, _ = OrderService.create_draft_order(
            dealer_id=self.dealer.id,
            items_data=[
                {"sku": "SKU-A", "quantity": 10},
                {"sku": "SKU-B", "quantity": 4},
            ],
        )
        order_no = snapshot["order_number"]

        with self.assertRaises(InsufficientStockError) as cm:
            OrderService.confirm_order(order_no)

        err = cm.exception
        self.assertEqual(err.code, "INSUFFICIENT_STOCK")
        self.assertEqual(len(err.details), 2)

        skus_in_details = {d["sku"] for d in err.details}
        self.assertEqual(skus_in_details, {"SKU-A", "SKU-B"})
        self.assertIn("Insufficient stock for Product A. Available: 5, Requested: 10", err.message)
        self.assertIn("Insufficient stock for Product B. Available: 2, Requested: 4", err.message)

        # Invariant: Neither product had stock deducted
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 5)
        self.assertEqual(self.inv2.quantity, 2)

    def test_price_lock_after_confirm(self):
        # In DRAFT, price updates on product should reflect dynamically
        snapshot, _ = OrderService.create_draft_order(
            dealer_id=self.dealer.id,
            items_data=[{"sku": "SKU-A", "quantity": 1}],
        )
        order_no = snapshot["order_number"]
        order = Order.objects.get(order_number=order_no)

        # Update product price to 200.00
        self.p1.price = Decimal("200.00")
        self.p1.save()

        # DRAFT snapshot reflects updated price
        draft_snap = OrderService.get_order_snapshot(order)
        self.assertEqual(draft_snap["subtotal"], "200.00")
        self.assertEqual(draft_snap["items"][0]["unit_price"], "200.00")

        # Confirm order locks it at 200.00
        confirmed_snap = OrderService.confirm_order(order_no)
        self.assertEqual(confirmed_snap["subtotal"], "200.00")

        # Later product price changes to 350.00 must NOT affect confirmed order
        self.p1.price = Decimal("350.00")
        self.p1.save()

        order.refresh_from_db()
        after_snap = OrderService.get_order_snapshot(order)
        self.assertEqual(after_snap["subtotal"], "200.00")
        self.assertEqual(after_snap["items"][0]["unit_price"], "200.00")

    def test_invalid_transitions_matrix(self):
        # 1. DRAFT cannot dispatch or deliver
        snap, _ = OrderService.create_draft_order(
            dealer_id=self.dealer.id,
            items_data=[{"sku": "SKU-A", "quantity": 1}],
        )
        order_no = snap["order_number"]

        with self.assertRaises(InvalidTransitionError):
            OrderService.dispatch_order(order_no)

        with self.assertRaises(InvalidTransitionError):
            OrderService.deliver_order(order_no)

        # Confirm order
        OrderService.confirm_order(order_no)

        # 2. CONFIRMED cannot be edited (409 ORDER_LOCKED)
        with self.assertRaises(OrderLockedError):
            OrderService.update_draft_order(order_no, [{"sku": "SKU-A", "quantity": 2}])

        # CONFIRMED cannot be confirmed again or delivered directly
        with self.assertRaises(InvalidTransitionError):
            OrderService.confirm_order(order_no)
        with self.assertRaises(InvalidTransitionError):
            OrderService.deliver_order(order_no)

        # Dispatch order
        OrderService.dispatch_order(order_no)

        # 3. DISPATCHED cannot be cancelled or confirmed
        with self.assertRaises(InvalidTransitionError):
            OrderService.cancel_order(order_no)
        with self.assertRaises(InvalidTransitionError):
            OrderService.confirm_order(order_no)

        # Deliver order
        OrderService.deliver_order(order_no)

        # 4. DELIVERED is final; cannot be cancelled, confirmed, or dispatched
        with self.assertRaises(InvalidTransitionError):
            OrderService.cancel_order(order_no)
        with self.assertRaises(InvalidTransitionError):
            OrderService.dispatch_order(order_no)


class OrderConcurrencyTests(TransactionTestCase):
    """Real multi-threaded concurrency tests against PostgreSQL."""

    def setUp(self):
        self.dealer = Dealer.objects.create(dealer_code="DLR-CONC", name="Concurrency Dealer")
        self.p = Product.objects.create(
            sku="CONC-SKU",
            name="Limited Stock Part",
            category="Engine",
            brand="OEM",
            vehicle_fitment="Universal",
            price=Decimal("100.00"),
        )
        self.inv = Inventory.objects.create(product=self.p, quantity=5)

    def test_20_threads_confirm_last_5_units(self):
        """20 threads attempt to confirm orders for the last 5 units. Exactly 5 succeed, stock reaches 0."""
        order_numbers = []
        for _ in range(20):
            snap, _ = OrderService.create_draft_order(
                dealer_id=self.dealer.id,
                items_data=[{"sku": "CONC-SKU", "quantity": 1}],
            )
            order_numbers.append(snap["order_number"])

        successes = []
        failures = []

        def worker(order_no):
            # Close existing connection in thread so a clean connection is opened
            connection.close()
            try:
                res = OrderService.confirm_order(order_no)
                return True, res
            except InsufficientStockError as e:
                return False, e
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(worker, order_numbers))

        for ok, val in results:
            if ok:
                successes.append(val)
            else:
                failures.append(val)

        self.assertEqual(len(successes), 5)
        self.assertEqual(len(failures), 15)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity, 0)

    def test_cancel_twice_concurrently_restores_once(self):
        """Two concurrent cancels on a confirmed order restore stock exactly once."""
        snap, _ = OrderService.create_draft_order(
            dealer_id=self.dealer.id,
            items_data=[{"sku": "CONC-SKU", "quantity": 2}],
        )
        order_no = snap["order_number"]
        OrderService.confirm_order(order_no)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity, 3)

        results = []

        def cancel_worker():
            connection.close()
            try:
                OrderService.cancel_order(order_no)
                return True
            except InvalidTransitionError:
                return False
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel_worker), executor.submit(cancel_worker)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

        # Stock restored from 3 back to 5 exactly once
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.quantity, 5)

    def test_order_number_uniqueness_under_50_threads(self):
        """50 concurrent threads generate order numbers without any collisions."""
        order_numbers = []

        def worker(_):
            connection.close()
            try:
                return generate_order_number()
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            order_numbers = list(executor.map(worker, range(50)))

        self.assertEqual(len(order_numbers), 50)
        self.assertEqual(len(set(order_numbers)), 50)
        for num in order_numbers:
            self.assertTrue(ORDER_NO_PATTERN.match(num))

    def test_idempotency_under_20_parallel_requests(self):
        """20 parallel requests with same Idempotency-Key return the identical order snapshot."""
        idempotency_key = "parallel-key-xyz-789"
        raw_body = b'{"dealer_id": 1, "items": [{"sku": "CONC-SKU", "quantity": 1}]}'

        order_snapshots = []

        def worker(_):
            connection.close()
            try:
                snap, code = OrderService.create_draft_order(
                    dealer_id=self.dealer.id,
                    items_data=[{"sku": "CONC-SKU", "quantity": 1}],
                    idempotency_key_str=idempotency_key,
                    request_raw_body=raw_body,
                )
                return snap["order_number"]
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            order_snapshots = list(executor.map(worker, range(20)))

        self.assertEqual(len(order_snapshots), 20)
        # All 20 threads returned the exact same order number
        self.assertEqual(len(set(order_snapshots)), 1)

        # Exactly 1 Order record exists in DB for this idempotency key
        self.assertEqual(Order.objects.filter(idempotency_records__key=idempotency_key).count(), 1)

        # Conflicting request payload with same key returns 409 IDEMPOTENCY_CONFLICT
        conflicting_body = b'{"dealer_id": 1, "items": [{"sku": "CONC-SKU", "quantity": 2}]}'
        with self.assertRaises(IdempotencyConflictError):
            OrderService.create_draft_order(
                dealer_id=self.dealer.id,
                items_data=[{"sku": "CONC-SKU", "quantity": 2}],
                idempotency_key_str=idempotency_key,
                request_raw_body=conflicting_body,
            )
