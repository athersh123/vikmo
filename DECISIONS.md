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
* **`date` + `last_seq`**: Tracks daily sequence generation. Locked inside an atomic transaction using `select_for_update()` (`orders/services.py:29-43`) to guarantee gap-safe, collision-free numbering across parallel threads.

### 1.8 IdempotencyKey Model (`orders/models.py:101-124`)
* **`key`, `request_hash`, `order`, `response_snapshot`**: Caches the response of `POST /api/orders/`. Replaying the same request returns the original cached response; replaying the same key with a different payload raises `409 IDEMPOTENCY_CONFLICT`.

---

## 2. API Design & Business Rules

### 2.1 Centralized Exception Architecture (`core/exceptions.py:12-140`)
* Standard error envelope enforced by one central DRF handler:
  ```json
  {"error": {"code": "...", "message": "...", "details": []}}
  ```
* Dedicated domain exceptions (`DuplicateError`, `InsufficientStockError`, `InvalidTransitionError`, `OrderLockedError`, `IdempotencyConflictError`, `ResourceNotFoundError`, `ValidationError`).
* Fallback catch-all route (`dealerdesk/urls.py:10`, `core/views.py:9-21`) guarantees unknown routes and malformed JSON payloads conform to the exact JSON error contract even during framework-level errors.

### 2.2 Financial Calculation & Rounding (`orders/services.py:45-56`)
* Tax computed strictly per line item: `line_tax = (line_subtotal * Decimal("0.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.
* Order total is the exact sum of line totals, preventing rounding drift.

### 2.3 Concurrency & Stock Locks (`orders/services.py:180-230`)
* Stock deduction during order confirmation uses `Inventory.objects.select_for_update().filter(...).order_by("id")`.
* Sorting by product id eliminates database deadlocks when concurrent transactions lock overlapping items.
* All-or-nothing confirmation: If any line item is short, transaction rolls back and returns `409 INSUFFICIENT_STOCK` detailing all short lines.

### 2.4 Idempotent Data Seeding (`core/management/commands/seed_data.py:12-85`)
* Uses `update_or_create` for products and dealers, and `get_or_create` for inventory records.
* Guarantees running `python manage.py seed_data` multiple times never duplicates records and never resets stock quantities modified by operations.
