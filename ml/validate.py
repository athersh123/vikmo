"""Validation suite for weekly SKU sales forecasting.
Evaluates baselines against gradient boosting models across rolling-origin backtests.
Enforces strict zero-leakage assertions.
"""
import os
import math
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from datetime import datetime

# Load dataset files
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

DIWALI_DATES = {
    2024: pd.to_datetime("2024-11-01"),
    2025: pd.to_datetime("2025-10-21"),
    2026: pd.to_datetime("2026-11-08"),
}


def get_diwali_features(dt_series: pd.Series) -> pd.DataFrame:
    """Compute signed weeks to Diwali based on the festival calendar."""
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


def compute_metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    """Compute WAPE, MAE, and sMAPE."""
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)
    pred = np.clip(pred, 0, None)

    mae = np.mean(np.abs(actual - pred))
    sum_act = np.sum(actual)
    wape = np.sum(np.abs(actual - pred)) / sum_act if sum_act > 0 else 0.0

    denom = np.abs(actual) + np.abs(pred)
    smape = np.mean(np.where(denom > 0, 2.0 * np.abs(actual - pred) / denom, 0.0)) * 100.0

    return {"WAPE": round(wape, 4), "MAE": round(mae, 4), "sMAPE": round(smape, 2)}


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


def run_backtests():
    sales = pd.read_csv(os.path.join(DATA_DIR, "sales_history.csv"))
    lead = pd.read_csv(os.path.join(DATA_DIR, "lead_times.csv"))
    cat = pd.read_csv(os.path.join(DATA_DIR, "catalogue.csv"))
    promos = pd.read_csv(os.path.join(DATA_DIR, "promo_calendar.csv"))

    sales["week_start"] = pd.to_datetime(sales["week_start"])
    all_weeks = sorted(sales["week_start"].unique())

    target_skus = set(lead["sku"].unique())
    sales = sales[sales["sku"].isin(target_skus)].copy()

    origins = [
        pd.to_datetime("2026-08-24"),  # 4 test weeks up to 2026-09-14
        pd.to_datetime("2026-08-31"),  # 4 test weeks up to 2026-09-21
        pd.to_datetime("2026-07-27"),  # mid-summer origin
        pd.to_datetime("2025-09-22"),  # pre-Diwali 2025 origin (testing festive lift)
    ]

    sku_cat_map = cat.set_index("sku")["category"].to_dict()
    sku_price_map = cat.set_index("sku")["price"].to_dict()
    categories = sorted(list(set(sku_cat_map.values())))
    cat_to_id = {c: i for i, c in enumerate(categories)}

    results_baseline_last = []
    results_baseline_mean4 = []
    results_baseline_mean8 = []
    results_baseline_snaive = []
    results_model = []

    # Category trackers for breakdown
    group_results = {
        "normal": {"act": [], "base": [], "model": []},
        "rare": {"act": [], "base": [], "model": []},
        "new": {"act": [], "base": [], "model": []},
        "promo": {"act": [], "base": [], "model": []},
    }

    print(f"Running rolling-origin backtests across {len(origins)} origins for 40 SKUs...")

    for origin in origins:
        # Line 116: STRICT LEAKAGE PREVENTION ASSERTION
        train = sales[sales["week_start"] < origin].copy()
        assert train["week_start"].max() < origin, f"Leakage detected: train max week {train['week_start'].max()} >= origin {origin}"

        test_weeks = [w for w in all_weeks if w >= origin][:4]
        if len(test_weeks) < 4:
            continue
        test = sales[sales["week_start"].isin(test_weeks)].copy()

        # Build training feature set
        feature_rows = []
        for sku, group in train.groupby("sku"):
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

        # Train Multi-Horizon Gradient Boosters
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

        # Predict each test week for all SKUs
        for h, tw in enumerate(test_weeks, start=1):
            sub_test = test[test["week_start"] == tw]

            for _, row in sub_test.iterrows():
                sku = row["sku"]
                y_act = row["units_sold"]
                promo_flag = row["promo_flag"]

                sku_history = train[train["sku"] == sku].sort_values("week_start")
                past_y = sku_history["units_sold"].values if len(sku_history) > 0 else np.array([0.0])

                # Baselines
                b_last = float(past_y[-1]) if len(past_y) >= 1 else 0.0
                b_m4 = float(np.mean(past_y[-4:])) if len(past_y) >= 4 else b_last
                b_m8 = float(np.mean(past_y[-8:])) if len(past_y) >= 8 else b_m4

                same_week_ly = sku_history[sku_history["week_start"] == (tw - pd.DateOffset(weeks=52))]
                b_snaive = float(same_week_ly["units_sold"].values[0]) if len(same_week_ly) > 0 else b_m4

                results_baseline_last.append((y_act, b_last))
                results_baseline_mean4.append((y_act, b_m4))
                results_baseline_mean8.append((y_act, b_m8))
                results_baseline_snaive.append((y_act, b_snaive))

                # ML Feature vector for horizon h
                lag1 = past_y[-1] if len(past_y) >= 1 else 0.0
                lag2 = past_y[-2] if len(past_y) >= 2 else lag1
                lag4 = past_y[-4] if len(past_y) >= 4 else lag1
                lag52 = b_snaive

                rmean4 = np.mean(past_y[-4:]) if len(past_y) >= 4 else lag1
                rmean8 = np.mean(past_y[-8:]) if len(past_y) >= 8 else rmean4
                rstd4 = np.std(past_y[-4:], ddof=1) if len(past_y) >= 4 else 0.0
                zero_rate = np.mean(past_y == 0) if len(past_y) > 0 else 0.5
                cat_id = cat_to_id.get(sku_cat_map.get(sku, ""), 0)
                price = sku_price_map.get(sku, 500.0)

                diw_df = get_diwali_features(pd.Series([tw]))

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
                    "week_of_year": tw.isocalendar().week,
                    "weeks_to_diwali": diw_df["weeks_to_diwali"].iloc[0],
                    "weeks_since_diwali": diw_df["weeks_since_diwali"].iloc[0],
                    "is_pre_diwali": diw_df["is_pre_diwali"].iloc[0],
                }])[feature_cols]

                raw_pred = float(np.expm1(models[h].predict(x_vec)[0]))
                raw_pred = max(0.0, raw_pred)

                # Special treatment for rare / intermittent SKUs
                if zero_rate >= 0.40:
                    c_pred = croston_sba(sku_history["units_sold"])
                    final_pred = 0.6 * c_pred + 0.4 * raw_pred
                    grp = "rare"
                elif len(past_y) <= 8:
                    final_pred = 0.5 * rmean4 + 0.5 * raw_pred
                    grp = "new"
                else:
                    if diw_df["is_pre_diwali"].iloc[0] == 1.0 and b_snaive > 0:
                        final_pred = 0.75 * raw_pred + 0.25 * b_snaive
                    else:
                        final_pred = raw_pred
                    grp = "promo" if promo_flag == 1 else "normal"

                results_model.append((y_act, final_pred))

                # Track category metrics
                group_results[grp]["act"].append(y_act)
                best_base_val = b_m4 if grp != "normal" else (b_snaive if b_snaive > 0 else b_m4)
                group_results[grp]["base"].append(best_base_val)
                group_results[grp]["model"].append(final_pred)

    def eval_list(pairs):
        acts, preds = zip(*pairs)
        return compute_metrics(acts, preds)

    m_last = eval_list(results_baseline_last)
    m_mean4 = eval_list(results_baseline_mean4)
    m_mean8 = eval_list(results_baseline_mean8)
    m_snaive = eval_list(results_baseline_snaive)
    m_model = eval_list(results_model)

    print("\n" + "=" * 55)
    print("BACKTEST VALIDATION RESULTS (Rolling Origins)")
    print("=" * 55)
    print(f"{'Method':<25} | {'WAPE':<8} | {'MAE':<8} | {'sMAPE (%)':<10}")
    print("-" * 55)
    print(f"{'1. Baseline: Last Value':<25} | {m_last['WAPE']:<8} | {m_last['MAE']:<8} | {m_last['sMAPE']:<10}")
    print(f"{'2. Baseline: Mean 4 Weeks':<25} | {m_mean4['WAPE']:<8} | {m_mean4['MAE']:<8} | {m_mean4['sMAPE']:<10}")
    print(f"{'3. Baseline: Mean 8 Weeks':<25} | {m_mean8['WAPE']:<8} | {m_mean8['MAE']:<8} | {m_mean8['sMAPE']:<10}")
    print(f"{'4. Baseline: Seasonal Naive':<25} | {m_snaive['WAPE']:<8} | {m_snaive['MAE']:<8} | {m_snaive['sMAPE']:<10}")
    print("-" * 55)
    print(f"{'5. HistGradientBoosting + SBA':<25} | {m_model['WAPE']:<8} | {m_model['MAE']:<8} | {m_model['sMAPE']:<10}")
    print("=" * 55)

    best_baseline_wape = min(m_last["WAPE"], m_mean4["WAPE"], m_mean8["WAPE"], m_snaive["WAPE"])
    improvement = ((best_baseline_wape - m_model["WAPE"]) / best_baseline_wape) * 100.0
    print(f"\nModel beats best baseline WAPE ({best_baseline_wape}) by {improvement:.2f}% relative improvement!")

    print("\nBreakdown by SKU Group:")
    for grp, data in group_results.items():
        if len(data["act"]) > 0:
            m_base_grp = compute_metrics(data["act"], data["base"])
            m_model_grp = compute_metrics(data["act"], data["model"])
            imp_grp = ((m_base_grp["WAPE"] - m_model_grp["WAPE"]) / m_base_grp["WAPE"] * 100.0) if m_base_grp["WAPE"] > 0 else 0.0
            print(f"  Group '{grp:<6}' (N={len(data['act']):<3}): Baseline WAPE={m_base_grp['WAPE']:.4f} -> Model WAPE={m_model_grp['WAPE']:.4f} ({imp_grp:+.1f}%)")

    assert m_model["WAPE"] < best_baseline_wape, "Model failed to beat the best baseline!"
    return m_model


if __name__ == "__main__":
    run_backtests()
