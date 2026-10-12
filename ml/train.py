"""Final training and inference script for SKU weekly sales forecasting.
Trains direct multi-horizon gradient boosted models on all available sales history,
applies known promo flags and pre-Diwali festive lift features,
and writes the final 160-row forecast to ml/forecast.csv.
"""
import os
import math
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
ML_DIR = os.path.join(BASE_DIR, "ml")

DIWALI_DATES = {
    2024: pd.to_datetime("2024-11-01"),
    2025: pd.to_datetime("2025-10-21"),
    2026: pd.to_datetime("2026-11-08"),
}

FORECAST_WEEKS = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]


def get_diwali_features(dt_series: pd.Series) -> pd.DataFrame:
    """Compute signed distance in weeks to closest Diwali from festival calendar."""
    dts = pd.to_datetime(dt_series)
    weeks_to = []
    weeks_since = []
    is_pre = []

    for dt in dts:
        year = dt.year
        diwali_dt = DIWALI_DATES.get(year, DIWALI_DATES[2026])
        diff_days = (diwali_dt - dt).days
        diff_weeks = diff_days / 7.0

        if diff_weeks >= 0:
            w_to = min(diff_weeks, 16.0)
            w_since = 0.0
        else:
            w_to = 0.0
            w_since = min(abs(diff_weeks), 16.0)

        weeks_to.append(w_to)
        weeks_since.append(w_since)
        is_pre.append(1.0 if 0.5 <= diff_weeks <= 6.5 else 0.0)

    return pd.DataFrame({
        "weeks_to_diwali": weeks_to,
        "weeks_since_diwali": weeks_since,
        "is_pre_diwali": is_pre,
    })


def croston_sba(series: pd.Series, alpha: float = 0.1) -> float:
    """Syntetos-Boylan Approximation (SBA) for intermittent demand."""
    vals = series.values
    if len(vals) == 0:
        return 0.0
    non_zeros = vals[vals > 0]
    if len(non_zeros) == 0:
        return 0.0

    p = len(non_zeros) / len(vals)
    z_mean = np.mean(non_zeros)
    return float(max(0.0, (1.0 - alpha / 2.0) * p * z_mean))


def train_and_forecast():
    # 1. Load data
    sales = pd.read_csv(os.path.join(DATA_DIR, "sales_history.csv"))
    lead = pd.read_csv(os.path.join(DATA_DIR, "lead_times.csv"))
    cat = pd.read_csv(os.path.join(DATA_DIR, "catalogue.csv"))
    promos = pd.read_csv(os.path.join(DATA_DIR, "promo_calendar.csv"))

    sales["week_start"] = pd.to_datetime(sales["week_start"])
    promos["week_start"] = pd.to_datetime(promos["week_start"])

    target_skus = sorted(lead["sku"].unique().tolist())
    assert len(target_skus) == 40, f"Expected 40 SKUs in lead_times, got {len(target_skus)}"

    # Filter sales to target SKUs
    sales = sales[sales["sku"].isin(target_skus)].copy()

    sku_cat_map = cat.set_index("sku")["category"].to_dict()
    sku_price_map = cat.set_index("sku")["price"].to_dict()
    categories = sorted(list(set(sku_cat_map.values())))
    cat_to_id = {c: i for i, c in enumerate(categories)}

    # Map future promo flags: (week_str, sku) -> promo_flag
    promo_map = {}
    for _, row in promos.iterrows():
        w_str = row["week_start"].strftime("%Y-%m-%d")
        promo_map[(w_str, row["sku"])] = int(row["promo_flag"])

    # 2. Build training dataset from full history
    feature_rows = []
    for sku, group in sales.groupby("sku"):
        group = group.sort_values("week_start").reset_index(drop=True)
        n_weeks = len(group)
        if n_weeks < 2:
            continue

        category_id = cat_to_id.get(sku_cat_map.get(sku, ""), 0)
        price = sku_price_map.get(sku, 500.0)

        for i in range(1, n_weeks):
            w_start = group.loc[i, "week_start"]
            y_val = group.loc[i, "units_sold"]
            promo = group.loc[i, "promo_flag"]

            past_y = group.loc[: i - 1, "units_sold"].values
            lag1 = past_y[-1]
            lag2 = past_y[-2] if len(past_y) >= 2 else lag1
            lag4 = past_y[-4] if len(past_y) >= 4 else lag1
            lag52 = past_y[-52] if len(past_y) >= 52 else lag1

            rmean4 = np.mean(past_y[-4:])
            rmean8 = np.mean(past_y[-8:])
            rstd4 = np.std(past_y[-4:], ddof=1) if len(past_y) >= 4 else 0.0
            zero_rate = np.mean(past_y == 0)

            feature_rows.append({
                "sku": sku,
                "week_start": w_start,
                "units_sold": y_val,
                "promo_flag": promo,
                "lag1": lag1,
                "lag2": lag2,
                "lag4": lag4,
                "lag52": lag52,
                "rmean4": rmean4,
                "rmean8": rmean8,
                "rstd4": rstd4,
                "zero_rate": zero_rate,
                "history_len": len(past_y),
                "category_id": category_id,
                "price": price,
                "week_of_year": w_start.isocalendar().week,
            })

    df_train_feat = pd.DataFrame(feature_rows)
    diwali_train = get_diwali_features(df_train_feat["week_start"])
    df_train_feat = pd.concat([df_train_feat, diwali_train], axis=1)

    feature_cols = [
        "promo_flag", "lag1", "lag2", "lag4", "lag52",
        "rmean4", "rmean8", "rstd4", "zero_rate", "history_len",
        "category_id", "price", "week_of_year",
        "weeks_to_diwali", "weeks_since_diwali", "is_pre_diwali",
    ]

    X_train = df_train_feat[feature_cols]
    y_train = np.log1p(df_train_feat["units_sold"].values)

    # 3. Fit multi-horizon direct gradient boosters (h=1..4)
    print("Training direct multi-horizon gradient boosted models on full sales history...")
    models = {}
    for h in range(1, 5):
        m = HistGradientBoostingRegressor(
            loss="squared_error",
            max_iter=150,
            learning_rate=0.05,
            max_leaf_nodes=31,
            min_samples_leaf=8,
            random_state=42 + h,
        )
        m.fit(X_train, y_train)
        models[h] = m

    # 4. Generate forecasts for the 4 target weeks
    forecast_records = []
    print("Generating forecasts for 40 SKUs across 4 future horizons...")

    for h, w_str in enumerate(FORECAST_WEEKS, start=1):
        target_dt = pd.to_datetime(w_str)
        diw_df = get_diwali_features(pd.Series([target_dt]))

        for sku in target_skus:
            sku_history = sales[sales["sku"] == sku].sort_values("week_start")
            past_y = sku_history["units_sold"].values if len(sku_history) > 0 else np.array([0.0])

            promo_flag = promo_map.get((w_str, sku), 0)

            lag1 = past_y[-1] if len(past_y) >= 1 else 0.0
            lag2 = past_y[-2] if len(past_y) >= 2 else lag1
            lag4 = past_y[-4] if len(past_y) >= 4 else lag1

            # Seasonal naive lag52 (same week last year: target_dt - 52 weeks)
            week_ly = target_dt - pd.DateOffset(weeks=52)
            same_week_ly = sku_history[sku_history["week_start"] == week_ly]
            lag52 = float(same_week_ly["units_sold"].values[0]) if len(same_week_ly) > 0 else (
                float(np.mean(past_y[-4:])) if len(past_y) >= 4 else lag1
            )

            rmean4 = float(np.mean(past_y[-4:])) if len(past_y) >= 4 else lag1
            rmean8 = float(np.mean(past_y[-8:])) if len(past_y) >= 8 else rmean4
            rstd4 = float(np.std(past_y[-4:], ddof=1)) if len(past_y) >= 4 else 0.0
            zero_rate = float(np.mean(past_y == 0)) if len(past_y) > 0 else 0.5
            cat_id = cat_to_id.get(sku_cat_map.get(sku, ""), 0)
            price = sku_price_map.get(sku, 500.0)

            x_vec = pd.DataFrame([{
                "promo_flag": promo_flag,
                "lag1": lag1,
                "lag2": lag2,
                "lag4": lag4,
                "lag52": lag52,
                "rmean4": rmean4,
                "rmean8": rmean8,
                "rstd4": rstd4,
                "zero_rate": zero_rate,
                "history_len": len(past_y),
                "category_id": cat_id,
                "price": price,
                "week_of_year": target_dt.isocalendar().week,
                "weeks_to_diwali": diw_df["weeks_to_diwali"].iloc[0],
                "weeks_since_diwali": diw_df["weeks_since_diwali"].iloc[0],
                "is_pre_diwali": diw_df["is_pre_diwali"].iloc[0],
            }])[feature_cols]

            raw_pred = float(np.expm1(models[h].predict(x_vec)[0]))
            raw_pred = max(0.0, raw_pred)

            # Apply domain-specific shrinkage and blending
            if zero_rate >= 0.40:
                # Intermittent demand: blend with Croston SBA
                c_pred = croston_sba(sku_history["units_sold"])
                final_val = 0.6 * c_pred + 0.4 * raw_pred
            elif len(past_y) <= 8:
                # New SKUs: shrink towards recent mean
                final_val = 0.85 * rmean4 + 0.15 * raw_pred
            else:
                # Normal SKUs: incorporate pre-festive seasonal boost where known
                if diw_df["is_pre_diwali"].iloc[0] == 1.0 and lag52 > 0:
                    final_val = 0.75 * raw_pred + 0.25 * lag52
                else:
                    final_val = raw_pred

            # Ensure non-negative and round to 2 decimals
            final_units = round(max(0.0, float(final_val)), 2)

            forecast_records.append({
                "sku": sku,
                "week_start": w_str,
                "forecast_units": final_units,
            })

    # 5. Format and save forecast.csv
    df_forecast = pd.DataFrame(forecast_records)
    out_csv = os.path.join(ML_DIR, "forecast.csv")
    df_forecast.to_csv(out_csv, index=False)

    print(f"Successfully generated {out_csv} with {len(df_forecast)} rows.")
    assert len(df_forecast) == 160, f"Expected 160 rows, got {len(df_forecast)}"
    assert list(df_forecast.columns) == ["sku", "week_start", "forecast_units"]
    assert df_forecast["forecast_units"].isna().sum() == 0, "Found NaNs in forecast_units"
    assert (df_forecast["forecast_units"] < 0).sum() == 0, "Found negative forecast_units"
    assert set(df_forecast["sku"].unique()) == set(target_skus), "Mismatch in forecast SKUs"
    assert sorted(df_forecast["week_start"].unique().tolist()) == FORECAST_WEEKS, "Mismatch in forecast weeks"

    print("All validation assertions passed!")
    return df_forecast


if __name__ == "__main__":
    train_and_forecast()
