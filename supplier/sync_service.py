"""Supplier synchronization service implementing contract §8 rules."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from django.db import transaction

from catalog.models import Inventory, Product
from core.exceptions import SupplierUnavailableError
from supplier.client import SupplierClient

logger = logging.getLogger(__name__)


def parse_iso8601(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp with Z into UTC datetime."""
    if not ts_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        clean = str(ts_str).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def validate_price(raw_price) -> tuple[Decimal | None, str | None]:
    """
    Validate supplier price.
    Must be a positive decimal with at most 2 decimal places.
    Rejects negatives, zero, floats with >2 decimal places, non-numeric, NaN, Infinity.
    """
    if raw_price is None:
        return None, "missing price"

    # Reject float types if raw_price has more than 2 decimals or float noise
    price_str = str(raw_price).strip()
    if not price_str:
        return None, "empty price"

    try:
        dec = Decimal(price_str)
    except (InvalidOperation, ValueError, TypeError):
        return None, "invalid price format"

    if dec.is_nan() or dec.is_infinite():
        return None, "price cannot be NaN or Infinite"

    if dec <= Decimal("0.00"):
        return None, "price must be positive"

    tup = dec.as_tuple()
    if tup.exponent < -2:
        return None, "price has more than 2 decimal places"

    return dec.quantize(Decimal("0.01")), None


class SupplierSyncService:
    """
    Orchestrates end-to-end synchronization with external supplier API.
    Guarantees:
    - All-or-nothing database writes.
    - Invalid items rejected and excluded before dedup.
    - Latest updated_at wins on duplicate SKUs.
    - Stock is never altered (supplier_stock ignored).
    - Idempotent re-runs perform zero database updates on unchanged data.
    """

    @classmethod
    def run_sync(cls, client: SupplierClient = None) -> dict:
        client = client or SupplierClient()

        # Step 0: Fetch full feed into memory (raises SupplierUnavailableError on failure)
        raw_items, pages_fetched, requests_made = client.fetch_all_products()

        rejected = []
        valid_items = []

        # Step 1: Reject invalid items
        for it in raw_items:
            supplier_sku = str(it.get("supplier_sku", "")).strip()

            sku = it.get("sku")
            if not sku or not isinstance(sku, str) or not sku.strip():
                rejected.append({"supplier_sku": supplier_sku, "reason": "missing or empty sku"})
                continue
            sku = sku.strip()

            price_dec, err_reason = validate_price(it.get("price"))
            if err_reason:
                rejected.append({"supplier_sku": supplier_sku, "reason": err_reason})
                continue

            valid_items.append(
                {
                    "sku": sku,
                    "supplier_sku": supplier_sku,
                    "name": str(it.get("name", "")).strip(),
                    "category": str(it.get("category", "")).strip(),
                    "brand": str(it.get("brand", "")).strip(),
                    "vehicle_fitment": str(it.get("vehicle_fitment", "")).strip(),
                    "description": str(it.get("description", "")).strip(),
                    "price": price_dec,
                    "updated_at": parse_iso8601(it.get("updated_at")),
                }
            )

        # Step 2: Deduplicate SKUs keeping the latest updated_at
        deduped = {}
        for it in valid_items:
            sku = it["sku"]
            if sku not in deduped:
                deduped[sku] = it
            else:
                existing = deduped[sku]
                if it["updated_at"] > existing["updated_at"]:
                    deduped[sku] = it

        unique_valid_items = list(deduped.values())

        # Step 3 & 4: Apply to database in ONE atomic transaction
        created_count = 0
        updated_count = 0
        unchanged_count = 0

        with transaction.atomic():
            valid_skus = [it["sku"] for it in unique_valid_items]
            existing_products = {p.sku: p for p in Product.objects.filter(sku__in=valid_skus)}

            to_create_products = []
            to_update_products = []

            for it in unique_valid_items:
                sku = it["sku"]
                p = existing_products.get(sku)

                if p is None:
                    # New product -> create with stock 0
                    new_p = Product(
                        sku=sku,
                        name=it["name"],
                        category=it["category"],
                        brand=it["brand"],
                        vehicle_fitment=it["vehicle_fitment"],
                        description=it["description"],
                        price=it["price"],
                    )
                    to_create_products.append(new_p)
                    created_count += 1
                else:
                    # Check for diffs
                    has_diff = (
                        p.name != it["name"]
                        or p.category != it["category"]
                        or p.brand != it["brand"]
                        or p.vehicle_fitment != it["vehicle_fitment"]
                        or p.description != it["description"]
                        or p.price != it["price"]
                    )
                    if has_diff:
                        p.name = it["name"]
                        p.category = it["category"]
                        p.brand = it["brand"]
                        p.vehicle_fitment = it["vehicle_fitment"]
                        p.description = it["description"]
                        p.price = it["price"]
                        to_update_products.append(p)
                        updated_count += 1
                    else:
                        unchanged_count += 1

            if to_create_products:
                Product.objects.bulk_create(to_create_products)
                # Create corresponding inventory records with quantity 0
                created_p_map = {p.sku: p for p in Product.objects.filter(sku__in=[p.sku for p in to_create_products])}
                new_inventories = [Inventory(product=created_p_map[p.sku], quantity=0) for p in to_create_products]
                Inventory.objects.bulk_create(new_inventories)

            if to_update_products:
                Product.objects.bulk_update(
                    to_update_products,
                    fields=["name", "category", "brand", "vehicle_fitment", "description", "price", "updated_at"],
                )

        assert created_count + updated_count + unchanged_count == len(unique_valid_items)

        return {
            "created": created_count,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "rejected": rejected,
            "pages_fetched": pages_fetched,
            "requests_made": requests_made,
        }
