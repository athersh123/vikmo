"""Services for loading forecast data and computing reorder suggestions."""
import csv
import math
import os
from typing import Dict, List, Optional
import numpy as np

from django.conf import settings
from catalog.models import Inventory, Product
from core.exceptions import ResourceNotFoundError

BASE_DIR = getattr(settings, "BASE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
ML_DIR = os.path.join(BASE_DIR, "ml")


class ForecastService:
    """Manages forecast CSV caching, validation, and reorder math."""

    _lead_times_cache: Optional[Dict[str, int]] = None
    _forecast_cache: Optional[Dict[str, List[dict]]] = None
    _sales_history_cache: Optional[Dict[str, List[float]]] = None

    @classmethod
    def get_lead_times(cls) -> Dict[str, int]:
        """Load data/lead_times.csv."""
        if cls._lead_times_cache is not None:
            return cls._lead_times_cache

        lead_path = os.path.join(DATA_DIR, "lead_times.csv")
        lead_dict = {}
        if os.path.exists(lead_path):
            with open(lead_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = row.get("sku", "").strip()
                    lt = int(row.get("lead_time_weeks", 1))
                    if sku:
                        lead_dict[sku] = lt

        cls._lead_times_cache = lead_dict
        return cls._lead_times_cache

    @classmethod
    def get_forecast_data(cls) -> Dict[str, List[dict]]:
        """Load ml/forecast.csv."""
        if cls._forecast_cache is not None:
            return cls._forecast_cache

        fc_path = os.path.join(ML_DIR, "forecast.csv")
        fc_dict = {}
        if os.path.exists(fc_path):
            with open(fc_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = row.get("sku", "").strip()
                    w_start = row.get("week_start", "").strip()
                    units = float(row.get("forecast_units", 0.0))
                    if sku not in fc_dict:
                        fc_dict[sku] = []
                    fc_dict[sku].append({
                        "week_start": w_start,
                        "forecast_units": units,
                    })

        cls._forecast_cache = fc_dict
        return cls._forecast_cache

    @classmethod
    def get_sales_history_12w(cls) -> Dict[str, List[float]]:
        """Load most recent 12 weeks of units_sold per SKU from data/sales_history.csv."""
        if cls._sales_history_cache is not None:
            return cls._sales_history_cache

        sales_path = os.path.join(DATA_DIR, "sales_history.csv")
        sku_weeks = {}
        if os.path.exists(sales_path):
            with open(sales_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = row.get("sku", "").strip()
                    w_start = row.get("week_start", "").strip()
                    units = float(row.get("units_sold", 0.0))
                    if sku not in sku_weeks:
                        sku_weeks[sku] = []
                    sku_weeks[sku].append((w_start, units))

        # Sort chronologically and take last 12 weeks
        sales_12w = {}
        for sku, entries in sku_weeks.items():
            entries.sort(key=lambda x: x[0])
            last_12 = [units for _, units in entries[-12:]]
            sales_12w[sku] = last_12

        cls._sales_history_cache = sales_12w
        return cls._sales_history_cache

    @classmethod
    def get_sku_forecast(cls, sku: str) -> dict:
        """
        Produce 4-week forecast for a given SKU.
        Raises ResourceNotFoundError if SKU not in lead_times.csv or catalogue.
        """
        lead_times = cls.get_lead_times()
        if sku not in lead_times:
            raise ResourceNotFoundError(f"SKU '{sku}' is not one of the 40 forecast items.")

        forecast_map = cls.get_forecast_data()
        sku_fc = forecast_map.get(sku, [])
        if not sku_fc:
            raise ResourceNotFoundError(f"Forecast for SKU '{sku}' not found.")

        # Sort forecast by week_start ascending
        sorted_fc = sorted(sku_fc, key=lambda x: x["week_start"])
        return {
            "sku": sku,
            "forecast": sorted_fc,
        }

    @classmethod
    def get_reorder_suggestions(cls) -> List[dict]:
        """
        Compute reorder suggestions for all 40 forecast SKUs using live inventory.
        Exact formula:
        L = lead_time_weeks
        lead_time_demand = sum of forecast_units of first L forecast weeks
        sigma = sample standard deviation (ddof=1) of units_sold over most recent 12 weeks
                (all weeks if fewer than 12; if only 1 week, sigma = 0)
        safety_stock = ceil(1.65 * sigma * sqrt(L))
        reorder_point = lead_time_demand + safety_stock
        reorder_qty = max(0, ceil(reorder_point - current_stock))
        """
        lead_times = cls.get_lead_times()
        forecast_data = cls.get_forecast_data()
        sales_history = cls.get_sales_history_12w()

        # Query live inventory from DB for all 40 SKUs
        inventory_rows = {
            inv.product.sku: inv.quantity
            for inv in Inventory.objects.filter(product__sku__in=list(lead_times.keys())).select_related("product")
        }

        results = []
        for sku in lead_times.keys():
            L = lead_times[sku]
            current_stock = inventory_rows.get(sku, 0)

            # 1. Lead time demand
            sku_fc = sorted(forecast_data.get(sku, []), key=lambda x: x["week_start"])
            first_L_weeks = sku_fc[:L]
            ltd = sum(item["forecast_units"] for item in first_L_weeks)
            lead_time_demand = round(float(ltd), 2)

            # 2. Sigma (sample std over recent <=12 weeks)
            recent_sales = sales_history.get(sku, [])
            if len(recent_sales) <= 1:
                sigma = 0.0
            else:
                sigma = float(np.std(recent_sales, ddof=1))

            # 3. Safety stock
            safety_stock = math.ceil(1.65 * sigma * math.sqrt(L))

            # 4. Reorder point
            reorder_point = round(lead_time_demand + safety_stock, 2)

            # 5. Reorder quantity
            reorder_qty = max(0, math.ceil(reorder_point - current_stock))

            results.append({
                "sku": sku,
                "current_stock": current_stock,
                "lead_time_weeks": L,
                "lead_time_demand": lead_time_demand,
                "safety_stock": safety_stock,
                "reorder_point": reorder_point,
                "reorder_qty": reorder_qty,
            })

        return results
