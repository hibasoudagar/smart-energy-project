"""
Train the energy prediction model on your own laptop (no Colab needed).
Does exactly the same steps as ml/Smart_Energy_ML_Model.ipynb and saves
energy_model.pkl in this folder, using your laptop's scikit-learn version.

Usage (in the backend folder):
    python train_model.py "C:\\path\\to\\household_power_consumption.txt"
If no path is given, it looks for the file in this folder and in ../ml.
Takes about 2-4 minutes.
"""
import os
import sys
import time

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, precision_score, r2_score, recall_score

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURES = ["r0", "r1", "r2", "hour_sin", "hour_cos", "dayofweek", "is_weekend"]


def find_dataset():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for p in [os.path.join(HERE, "household_power_consumption.txt"),
              os.path.join(HERE, "..", "ml", "household_power_consumption.txt")]:
        if os.path.exists(p):
            return p
    sys.exit("Dataset not found. Run:  python train_model.py \"C:\\path\\to\\household_power_consumption.txt\"")


def main():
    t0 = time.time()
    path = find_dataset()

    # Step 1 - Load
    print("Step 1: Loading", path)
    df = pd.read_csv(path, sep=";", na_values=["?"], low_memory=False)
    print("  Raw shape:", df.shape)

    # Step 2 - Clean
    print("Step 2: Cleaning")
    df["datetime"] = pd.to_datetime(df["Date"] + " " + df["Time"], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    cols = ["Global_active_power", "Global_reactive_power", "Voltage", "Global_intensity",
            "Sub_metering_1", "Sub_metering_2", "Sub_metering_3"]
    df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")
    df = (df.dropna(subset=["datetime"]).sort_values("datetime")
            .drop_duplicates(subset=["datetime"]).set_index("datetime").drop(columns=["Date", "Time"]))
    before = df["Global_active_power"].isna().sum()
    df[cols] = df[cols].interpolate(method="time", limit=5, limit_area="inside")
    after = df["Global_active_power"].isna().sum()
    print(f"  Missing before: {before:,} ({before / len(df):.2%}) | filled: {before - after:,} | still missing: {after:,}")

    # Step 3 - Hourly
    print("Step 3: Hourly averages")
    power = df["Global_active_power"]
    hourly = power.resample("h").mean()
    hourly[power.resample("h").count() < 45] = np.nan
    data = hourly.to_frame("power_kw")
    print(f"  Hourly rows: {len(data):,} | hours dropped: {data['power_kw'].isna().sum()}")

    # Step 4 - Features
    print("Step 4: Features")
    p = data["power_kw"]
    data["scale"] = p.rolling(24, min_periods=24).mean()
    for k in [0, 1, 2]:
        data[f"r{k}"] = p.shift(k) / data["scale"]
    data["target_kw"] = p.shift(-1)
    data["target"] = data["target_kw"] / data["scale"]
    nt = data.index + pd.Timedelta(hours=1)
    data["hour_sin"] = np.sin(2 * np.pi * nt.hour / 24)
    data["hour_cos"] = np.cos(2 * np.pi * nt.hour / 24)
    data["dayofweek"] = nt.dayofweek
    data["is_weekend"] = (nt.dayofweek >= 5).astype(int)
    data = data.dropna(subset=FEATURES + ["target", "scale"])
    print(f"  Model-ready rows: {len(data):,}")

    # Step 5 - Split
    split = int(len(data) * 0.8)
    train, test = data.iloc[:split], data.iloc[split:]
    print(f"Step 5: Train {train.index.min():%Y-%m-%d} -> {train.index.max():%Y-%m-%d} ({len(train):,} h) | "
          f"Test {test.index.min():%Y-%m-%d} -> {test.index.max():%Y-%m-%d} ({len(test):,} h)")

    # Step 6 - Train
    print("Step 6: Training Linear Regression and Random Forest (1-2 min)...")
    lr = LinearRegression().fit(train[FEATURES], train["target"])
    rf = RandomForestRegressor(n_estimators=150, max_depth=12, min_samples_leaf=10,
                               n_jobs=-1, random_state=42).fit(train[FEATURES], train["target"])
    scale = test["scale"].values
    y = test["target_kw"]
    preds = {"Naive (persistence)": test["power_kw"].values,
             "Linear Regression": lr.predict(test[FEATURES]) * scale,
             "Random Forest": rf.predict(test[FEATURES]) * scale}

    # Step 7 - Compare
    print("\nStep 7: Results on test data")
    rows = [{"Model": n, "MAE (kW)": mean_absolute_error(y, yp),
             "RMSE (kW)": np.sqrt(mean_squared_error(y, yp)), "R2": r2_score(y, yp)} for n, yp in preds.items()]
    results = pd.DataFrame(rows).set_index("Model").round(4)
    print(results.to_string())
    best = results.drop(index="Naive (persistence)")["RMSE (kW)"].idxmin()
    print("  Best ML model by RMSE:", best)

    # Step 8 - Alerts
    print("\nStep 8: Alert check (threshold 2.0 kW)")
    actual = (y > 2.0).astype(int)
    for n, yp in preds.items():
        ph = (yp > 2.0).astype(int)
        print(f"  {n:22s} precision {precision_score(actual, ph, zero_division=0):.3f} | "
              f"recall {recall_score(actual, ph, zero_division=0):.3f} | alerts {ph.sum()}")

    # Step 9 - Save
    model = {"Linear Regression": lr, "Random Forest": rf}[best]
    out = os.path.join(HERE, "energy_model.pkl")
    joblib.dump({"model": model, "model_name": best, "features": FEATURES,
                 "scale_window_hours": 24, "sklearn_version": sklearn.__version__}, out, compress=3)
    print(f"\nStep 9: Saved {out} ({best}, scikit-learn {sklearn.__version__}, "
          f"{os.path.getsize(out) / 1e6:.1f} MB) in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
