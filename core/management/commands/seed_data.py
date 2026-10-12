"""Management command to seed catalogue and dealer data idempotently."""
import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Dealer, Inventory, Product


class Command(BaseCommand):
    help = "Load catalogue.csv and dealers.csv into PostgreSQL. Idempotent."

    def handle(self, *args, **options):
        data_dir = Path(settings.DATA_DIR)
        catalogue_path = data_dir / "catalogue.csv"
        dealers_path = data_dir / "dealers.csv"

        if not catalogue_path.exists():
            self.stderr.write(self.style.ERROR(f"Catalogue file not found at {catalogue_path}"))
            return

        if not dealers_path.exists():
            self.stderr.write(self.style.ERROR(f"Dealers file not found at {dealers_path}"))
            return

        self.stdout.write("Seeding products and initial inventory...")
        products_seeded = 0
        stock_created = 0

        with catalogue_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            with transaction.atomic():
                for row in reader:
                    sku = row["sku"].strip()
                    name = row["name"].strip()
                    category = row["category"].strip()
                    brand = row["brand"].strip()
                    vehicle_fitment = row["vehicle_fitment"].strip()
                    price = Decimal(row["price"].strip())
                    description = row.get("description", "").strip()
                    stock_qty = int(row["stock"].strip())

                    product, p_created = Product.objects.update_or_create(
                        sku=sku,
                        defaults={
                            "name": name,
                            "category": category,
                            "brand": brand,
                            "vehicle_fitment": vehicle_fitment,
                            "price": price,
                            "description": description,
                        },
                    )
                    if p_created:
                        products_seeded += 1

                    # Important: get_or_create ensures re-running does NOT reset stock of already seeded products
                    inv, inv_created = Inventory.objects.get_or_create(
                        product=product,
                        defaults={"quantity": stock_qty},
                    )
                    if inv_created:
                        stock_created += 1

        self.stdout.write(f"Catalogue done: {products_seeded} new products, {stock_created} new inventory records.")

        self.stdout.write("Seeding dealers...")
        dealers_seeded = 0
        with dealers_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            with transaction.atomic():
                for row in reader:
                    code = row["dealer_code"].strip()
                    name = row["name"].strip()
                    city = row.get("city", "").strip()
                    phone = row.get("phone", "").strip()
                    email = row.get("email", "").strip()
                    gstin = row.get("gstin", "").strip()
                    credit_limit_str = row.get("credit_limit", "0.00").strip()
                    credit_limit = Decimal(credit_limit_str) if credit_limit_str else Decimal("0.00")

                    _, d_created = Dealer.objects.update_or_create(
                        dealer_code=code,
                        defaults={
                            "name": name,
                            "city": city,
                            "phone": phone,
                            "email": email,
                            "gstin": gstin,
                            "credit_limit": credit_limit,
                        },
                    )
                    if d_created:
                        dealers_seeded += 1

        self.stdout.write(self.style.SUCCESS(f"Dealers done: {dealers_seeded} new dealers seeded."))
