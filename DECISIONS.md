# Architectural & Design Decisions — VIKMO DealerDesk

This document records the architectural decisions, database schemas, integrity constraints, and business logic guarantees implemented in VIKMO DealerDesk.

---

## 1. Domain Models & Relational Integrity

### 1.1 Product Model (`catalog/models.py:7-46`)
* **`sku` (`catalog/models.py:12`)**: `CharField(max_length=64, unique=True, db_index=True)`. Acts as the business natural key for catalog identification and search. Made immutable in `PATCH /api/products/{sku}/`.
* **`name`, `category`, `brand`, `vehicle_fitment` (`catalog/models.py:13-16`)**: Indexed attributes matching contract filter requirements (`search`, `category`, `vehicle`).
* **`price` (`catalog/models.py:17`)**: `DecimalField(max_digits=12, decimal_places=2)`. Never float to eliminate IEEE 754 precision issues.
* **Check Constraint `check_product_price_gt_zero` (`catalog/models.py:25-28`)**: Enforces `price > 0.00` directly at the PostgreSQL engine level, preventing corrupt zero or negative price entries.
* **Indexes (`catalog/models.py:30-36`)**: Explicit composite and B-tree indexes on `sku`, `name`, `brand`, `category`, and `vehicle_fitment` for sub-millisecond search lookups.

### 1.2 Inventory Model (`catalog/models.py:48-71`)
* **Decoupling from Product**: Stock is strictly housed in `Inventory` rather than `Product`. This guarantees that supplier catalog synchronizations (which update product metadata) never touch or overwrite warehouse stock.
* **`product` (`catalog/models.py:53-57`)**: `OneToOneField(Product, on_delete=models.PROTECT, related_name="inventory")`.
  * **`on_delete=PROTECT` justification**: A catalog product cannot be removed if inventory records exist. This safeguards physical stock traceability.
* **`quantity` (`catalog/models.py:58`)**: `PositiveIntegerField(default=0)`.
* **Check Constraint `check_inventory_quantity_gte_zero` (`catalog/models.py:63-66`)**: `CHECK quantity >= 0`. Prevents overselling at the database engine level, rejecting invalid deductions even under race conditions.

### 1.3 InventoryAdjustment Model (`catalog/models.py:73-91`)
* **Audit Trail**: Every manual adjustment through `PUT /api/inventory/{sku}/` records the product, the new quantity, the reason string, and an immutable timestamp.
* **`product` (`catalog/models.py:77-81`)**: `ForeignKey(Product, on_delete=models.CASCADE)`. If a product is purged legally in a maintenance window, historical adjustment logs cascade cleanly.

### 1.4 Dealer Model (`catalog/models.py:93-112`)
* **`dealer_code` (`catalog/models.py:97`)**: `CharField(max_length=64, unique=True, db_index=True)`. Enforces business code uniqueness; duplicates return `409 DUPLICATE`.
* **`credit_limit` (`catalog/models.py:103`)**: `DecimalField(max_digits=12, decimal_places=2, default=0.00)`. Formatted as 2-decimal string in API representations.

### 1.5 Order Model (`orders/models.py:16-45`)
* **`order_number` (`orders/models.py:21`)**: `CharField(max_length=64, unique=True, db_index=True)`. Formatted as `ORD-YYYYMMDD-NNNN` in Asia/Kolkata timezone.
* **`dealer` (`orders/models.py:22-26`)**: `ForeignKey(Dealer, on_delete=models.PROTECT)`.
  * **`on_delete=PROTECT` justification**: Prevents accidental deletion of a dealer that has associated historical orders, protecting statutory financial logs and GST audits.
* **`status` (`orders/models.py:27-32`)**: Choices `DRAFT`, `CONFIRMED`, `DISPATCHED`, `DELIVERED`, `CANCELLED`.
* **Financial Totals (`orders/models.py:33-35`)**: `subtotal`, `tax_total`, `grand_total` stored as `DecimalField(max_digits=12, decimal_places=2)`. Exact to the paisa.
* **`stock_deducted` (`orders/models.py:36`)**: Boolean flag tracking whether inventory has been decremented, preventing double deductions or erroneous cancellations.

### 1.6 OrderItem Model (`orders/models.py:47-83`)
* **`order` (`orders/models.py:52-56`)**: `ForeignKey(Order, on_delete=models.CASCADE, related_name="items")`. Order line items are part of the order entity lifecycle and cascade when an order draft is deleted.
* **`product` (`orders/models.py:57-61`)**: `ForeignKey(Product, on_delete=models.PROTECT)`.
  * **`on_delete=PROTECT` justification**: A product that has appeared on order line items cannot be deleted from the database.
* **`quantity` (`orders/models.py:62`)**: Enforces `quantity >= 1` with DB Check Constraint `check_order_item_quantity_gte_one` (`orders/models.py:70-73`).
* **Unique Constraint `unique_order_item_per_product` (`orders/models.py:74-77`)**: Guarantees at the database level that no order contains duplicate lines for the same product.

### 1.7 OrderNumberCounter Model (`orders/models.py:85-99`)
* **`date` + `last_seq`**: Tracks daily sequence generation. Atomic PostgreSQL `INSERT ... ON CONFLICT DO UPDATE RETURNING` (`orders/services.py:53-73`) guarantees gap-safe, collision-free numbering across parallel threads.

### 1.8 IdempotencyKey Model (`orders/models.py:101-124`)
* **`key`, `request_hash`, `order`, `response_snapshot`**: Caches the response of `POST /api/orders/`. Replaying the same request returns the original cached response; replaying the same key with a different payload raises `409 IDEMPOTENCY_CONFLICT`.

---

## 2. Order Engine & Business Logic (Phase 2)

### 2.1 State Machine & Central Transition Table (`orders/services.py:27-43, 260-267`)
* Defined in a single declarative transition dictionary (`ORDER_TRANSITIONS`):
  * `confirm`: `DRAFT -> CONFIRMED`
  * `dispatch`: `CONFIRMED -> DISPATCHED`
  * `deliver`: `DISPATCHED -> DELIVERED`
  * `cancel`: `DRAFT -> CANCELLED` and `CONFIRMED -> CANCELLED`
* Validated centrally via `_validate_transition(order, action)` under `select_for_update()` on the order row. Any illegal transition raises `409 INVALID_TRANSITION`.
* `PUT /api/orders/{order_number}/` verifies `order.status == DRAFT`; non-draft orders immediately raise `409 ORDER_LOCKED`.

### 2.2 Concurrency & Deadlock-Free Stock Deduction (`orders/services.py:269-340`)
* **Consistent Row-Lock Ordering**: In `confirm_order()`, inventory rows are locked using `Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("product_id")`. Sorting by product ID eliminates database deadlocks when concurrent multi-line orders touch overlapping sets of SKUs.
* **All-or-Nothing Verification**: If any line item has `quantity > available`, the transaction makes no modifications and raises `409 INSUFFICIENT_STOCK`.
  * `details`: Complete list of every short SKU with `[{"sku": "...", "requested": n, "available": m}]`.
  * `message`: One complete sentence per short item: `"Insufficient stock for <product name>. Available: 5, Requested: 10."`.
* **Atomic Deduction & Price Lock**: On confirmation, stock is decremented and `unit_price` is permanently locked at the product's current catalog price. Future changes to catalog product prices never affect confirmed orders.

### 2.3 Cancel Order & Stock Restoration (`orders/services.py:365-402`)
* When an order is in `CONFIRMED` status with `stock_deducted=True`, calling `cancel_order()` locks the order row, restores inventory in consistent product ID order, and clears `stock_deducted=False` within a single atomic transaction.
* Two concurrent cancel calls on the same order will have the second call see `status == CANCELLED` and immediately reject with `409 INVALID_TRANSITION`, ensuring inventory is restored **exactly once**.
* `DISPATCHED` and `DELIVERED` orders cannot be cancelled (`409 INVALID_TRANSITION`).

### 2.4 Financial Precision & GST Rounding (`orders/services.py:45-56`)
* Implemented exclusively using `decimal.Decimal` and `ROUND_HALF_UP`:
  * `line_subtotal = unit_price * quantity`
  * `line_tax = (line_subtotal * Decimal("0.18")).quantize(Decimal("0.01"), ROUND_HALF_UP)`
  * `line_total = line_subtotal + line_tax`
* Tax is rounded **per line item**, and order totals (`subtotal`, `tax_total`, `grand_total`) are exact sums of the line amounts to the paisa.
* Prevents cumulative rounding drift and passes half-up boundary tests (e.g. `subtotal=0.05 -> tax=0.01`, `0.25 -> tax=0.05`, `0.75 -> tax=0.14`).

### 2.5 Gap-Safe Order Numbers (`orders/services.py:53-73`)
* Format: `ORD-YYYYMMDD-NNNN` where date is evaluated in `Asia/Kolkata` (`zoneinfo.ZoneInfo("Asia/Kolkata")`).
* Uses native PostgreSQL atomic UPSERT:
  ```sql
  INSERT INTO orders_ordernumbercounter ("date", "last_seq")
  VALUES (%s, 1)
  ON CONFLICT ("date")
  DO UPDATE SET "last_seq" = orders_ordernumbercounter."last_seq" + 1
  RETURNING "last_seq";
  ```
* Guaranteed collision-free and gap-safe under 50 simultaneous parallel threads.

### 2.6 Multi-Threaded Idempotency Engine (`orders/services.py:145-215`)
* Computes SHA-256 hash of canonicalized JSON request payload.
* Before checking or creating idempotency records, takes a PostgreSQL transaction-level advisory lock on the key:
  ```sql
  SELECT pg_advisory_xact_lock(hashtext(%s));
  ```
* Serializes concurrent duplicate requests. The first thread creates the order and idempotency record; subsequent concurrent threads cleanly read and return the original snapshot with status `201`.
* Reusing the same `Idempotency-Key` with a different request payload immediately raises `409 IDEMPOTENCY_CONFLICT`.

---

## 3. Supplier Sync Engine (Phase 3)

### 3.1 HTTP Client & Resilient Ingestion (`supplier/client.py:17-115`)
* **All-or-Nothing Fetch**: Ingestion retrieves the entire multi-page feed into memory before initiating any database transactions (`supplier/client.py:31-50`).
* **Retry Strategy**: Each page is attempted up to 5 times:
  * On `429 Too Many Requests`, parses the `Retry-After` header and sleeps the specified seconds before retrying the same cursor (`supplier/client.py:72-84`).
  * On `500`, `502`, `503`, or connection/timeout exceptions, applies short exponential backoff (`supplier/client.py:86-107`).
  * On `401 Unauthorized` or exhaustion of 5 attempts on any page, raises `SupplierUnavailableError` (`supplier/client.py:70-71, 114-115`), which translates via the centralized exception handler to HTTP `502 SUPPLIER_UNAVAILABLE` with zero database writes.

### 3.2 Invalidation & Deduplication Pipeline (`supplier/sync_service.py:76-122`)
* **Sanitization & Rejection**: Items with missing/whitespace SKUs or non-positive/malformed prices (>2 decimals, NaN, Infinity) are stripped into the `rejected` list before deduplication and database application (`supplier/sync_service.py:76-107`).
* **Deterministic Deduplication**: Multiple occurrences of the same SKU across pages are resolved by keeping the version with the latest `updated_at` (parsed from ISO 8601 with UTC `Z`), ensuring identical results regardless of page arrival order (`supplier/sync_service.py:109-122`).

### 3.3 Atomic Application & Stock Invariant (`supplier/sync_service.py:124-180`)
* **Single Atomic Transaction**: Applies all additions and modifications inside `transaction.atomic()` using batch `bulk_create` and `bulk_update`.
* **Stock Invariant**: `supplier_stock` is completely ignored. New products are created with local `Inventory.quantity = 0`; existing product inventory records are untouched (`supplier/sync_service.py:166-173`).
* **Idempotency Guarantee**: A second run against an unchanged feed returns `{"created": 0, "updated": 0, "unchanged": N}` and issues zero database write operations (`supplier/sync_service.py:157-175`).
