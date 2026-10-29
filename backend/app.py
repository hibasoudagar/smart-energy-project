"""
Smart Energy backend
--------------------
Receives readings from the ESP32 (or simulator.py), stores them in SQLite,
predicts next-hour consumption with the trained model, decides the relay
state, and serves the dashboard.

Run:  python app.py      then open http://localhost:5000
"""
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta

import joblib
import numpy as np
import pandas as pd
import sklearn
from flask import Flask, Response, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "energy.db")
MODEL_PATH = os.path.join(BASE_DIR, "energy_model.pkl")

HISTORY_NEEDED_HOURS = 3        # model needs the last 3 hours
SCALE_WINDOW_HOURS = 24         # "typical power" = average of last 24 h
MIN_READINGS_PER_HOUR = 10      # an hour window needs at least this many readings
PREDICTION_LOG_MINUTES = 10     # store one prediction every 10 min (for evaluation)
DEVICE_OFFLINE_SECONDS = 60     # no reading for 60 s -> device shown offline
DEFAULT_SETTINGS = {"threshold_w": 100.0, "auto_control": 1, "relay_mode": "auto", "tripped": 0}

# ---------------------------------------------------------------- model
bundle = joblib.load(MODEL_PATH)
MODEL = bundle["model"]
FEATURES = bundle["features"]
trained_with = bundle.get("sklearn_version")
if trained_with and trained_with != sklearn.__version__:
    print(f"WARNING: model trained with scikit-learn {trained_with}, you have {sklearn.__version__}.\n"
          f"         If predictions fail, run:  pip install scikit-learn=={trained_with}")
print(f"Loaded model: {bundle.get('model_name', type(MODEL).__name__)}")

app = Flask(__name__)
lock = threading.Lock()


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            power_w REAL NOT NULL,
            voltage REAL, current_a REAL, energy_kwh REAL,
            source TEXT DEFAULT 'esp32');
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            made_at TEXT NOT NULL,
            predicted_w REAL NOT NULL,
            typical_w REAL,
            method TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, str(v)))


def get_settings():
    with db() as conn:
        s = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    return {"threshold_w": float(s["threshold_w"]), "auto_control": s["auto_control"] == "1",
            "relay_mode": s["relay_mode"], "tripped": s["tripped"] == "1"}


def set_setting(key, value):
    with db() as conn:
        conn.execute("REPLACE INTO settings VALUES (?, ?)", (key, str(value)))


def readings_since(start):
    with db() as conn:
        df = pd.read_sql_query("SELECT ts, power_w FROM readings WHERE ts >= ? ORDER BY ts",
                               conn, params=(start.isoformat(),), parse_dates=["ts"])
    return df.set_index("ts")["power_w"]


# ---------------------------------------------------------------- prediction
def make_features(now):
    """Build the same features the notebook used (Step 4). Returns (features, info)."""
    p = readings_since(now - timedelta(hours=SCALE_WINDOW_HOURS))
    if p.empty:
        return None, {"reason": "No readings yet"}

    hours_collected = (now - p.index.min()).total_seconds() / 3600
    if hours_collected < HISTORY_NEEDED_HOURS:
        return None, {"reason": f"Collecting history: {hours_collected:.1f} of {HISTORY_NEEDED_HOURS} hours",
                      "hours_collected": hours_collected}

    # Hour windows ending now: lag 0 = last 60 min, lag 1 = 60-120 min ago, lag 2 = 120-180 min ago
    lags = []
    for k in range(HISTORY_NEEDED_HOURS):
        w = p[(p.index > now - timedelta(hours=k + 1)) & (p.index <= now - timedelta(hours=k))]
        if len(w) < MIN_READINGS_PER_HOUR:
            return None, {"reason": f"Not enough readings {k}-{k + 1} h ago (device was offline)",
                          "hours_collected": hours_collected}
        lags.append(w.mean())

    typical_w = p.mean()
    info = {"hours_collected": hours_collected, "typical_w": typical_w, "last_hour_w": lags[0]}
    if typical_w < 0.05:                      # device essentially off all day: ratios meaningless
        return None, {**info, "reason": "Device off for 24 h", "fallback_w": lags[0]}

    h = now.hour + now.minute / 60            # time of day of the hour being predicted
    feats = pd.DataFrame([{
        "r0": lags[0] / typical_w, "r1": lags[1] / typical_w, "r2": lags[2] / typical_w,
        "hour_sin": math.sin(2 * math.pi * h / 24), "hour_cos": math.cos(2 * math.pi * h / 24),
        "dayofweek": now.weekday(), "is_weekend": int(now.weekday() >= 5),
    }])[FEATURES]
    return feats, info


def predict(now=None):
    now = now or datetime.now()
    feats, info = make_features(now)
    if feats is not None:
        ratio = float(MODEL.predict(feats)[0])
        info = {k: float(v) for k, v in info.items()}
        return {"predicted_w": max(ratio, 0) * info["typical_w"], "method": "model", **info}
    if "fallback_w" in info:
        return {"predicted_w": info["fallback_w"], "method": "fallback (last hour)", **info}
    return {"predicted_w": None, "method": None, **info}


def log_prediction(pred, now):
    if pred["predicted_w"] is None or pred["method"] != "model":
        return
    with db() as conn:
        last = conn.execute("SELECT made_at FROM predictions ORDER BY id DESC LIMIT 1").fetchone()
        if last and now - datetime.fromisoformat(last["made_at"]) < timedelta(minutes=PREDICTION_LOG_MINUTES):
            return
        conn.execute("INSERT INTO predictions (made_at, predicted_w, typical_w, method) VALUES (?,?,?,?)",
                     (now.isoformat(), pred["predicted_w"], pred.get("typical_w"), pred["method"]))


def decide_relay(pred, settings):
    """Safety rule: if the predicted next hour exceeds the threshold, auto-control trips the
    relay OFF and keeps it off until the user presses Reset on the dashboard."""
    alert = bool(pred["predicted_w"] is not None and pred["predicted_w"] > settings["threshold_w"])
    if alert and settings["auto_control"] and settings["relay_mode"] == "auto" and not settings["tripped"]:
        set_setting("tripped", 1)
        settings["tripped"] = True
    if settings["relay_mode"] == "on":
        relay = "on"
    elif settings["relay_mode"] == "off":
        relay = "off"
    else:
        relay = "off" if settings["tripped"] else "on"
    return alert, relay


# ---------------------------------------------------------------- API: device
@app.post("/api/reading")
def post_reading():
    """ESP32 sends: {"power_w": 61.2, "voltage": 231.4, "current_a": 0.27, "energy_kwh": 0.82}"""
    d = request.get_json(force=True, silent=True) or {}
    try:
        power_w = float(d["power_w"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "power_w missing or not a number"}), 400
    if not math.isfinite(power_w) or power_w < 0:
        return jsonify({"error": "invalid power_w"}), 400

    now = datetime.now()
    ts = datetime.fromisoformat(d["ts"]) if d.get("ts") else now     # ts only used by simulator backfill
    with lock:
        with db() as conn:
            conn.execute("INSERT INTO readings (ts, power_w, voltage, current_a, energy_kwh, source) "
                         "VALUES (?,?,?,?,?,?)",
                         (ts.isoformat(), power_w, d.get("voltage"), d.get("current_a"),
                          d.get("energy_kwh"), d.get("source", "esp32")))
        pred = predict(now)
        log_prediction(pred, now)
        alert, relay = decide_relay(pred, get_settings())
    return jsonify({"relay": relay, "alert": alert,
                    "predicted_w": None if pred["predicted_w"] is None else round(pred["predicted_w"], 1)})


@app.post("/api/readings/bulk")
def post_bulk():
    """Used by simulator.py --backfill to load past readings quickly."""
    rows = request.get_json(force=True)
    with db() as conn:
        conn.executemany("INSERT INTO readings (ts, power_w, source) VALUES (?,?,?)",
                         [(r["ts"], float(r["power_w"]), r.get("source", "simulator")) for r in rows])
    return jsonify({"inserted": len(rows)})


# ---------------------------------------------------------------- API: dashboard
@app.get("/")
def home():
    """New React front end (static/app.html)."""
    return app.send_static_file("app.html")


@app.get("/classic")
def dashboard():
    """Original simple dashboard."""
    return render_template("dashboard.html")


@app.get("/api/status")
def status():
    now = datetime.now()
    with db() as conn:
        last = conn.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    pred = predict(now)
    settings = get_settings()
    alert, relay = decide_relay(pred, settings)
    online = bool(last) and (now - datetime.fromisoformat(last["ts"])).total_seconds() < DEVICE_OFFLINE_SECONDS
    return jsonify({
        "now": now.isoformat(timespec="seconds"),
        "online": online,
        "last_reading": dict(last) if last else None,
        "prediction": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in pred.items()},
        "settings": settings, "alert": alert, "relay": relay,
    })


@app.get("/api/history")
def history():
    hours = float(request.args.get("hours", 24))
    now = datetime.now()
    p = readings_since(now - timedelta(hours=hours))
    series = p.resample("5min").mean().dropna() if not p.empty else p
    return jsonify({
        "power": [{"t": t.isoformat(timespec="minutes"), "w": round(v, 1)} for t, v in series.items()],
        "evaluation": evaluation(now),
    })


def evaluation(now=None):
    """Compare each logged prediction with the actual average of the hour that followed it."""
    now = now or datetime.now()
    with db() as conn:
        preds = conn.execute("SELECT made_at, predicted_w FROM predictions ORDER BY made_at").fetchall()
    if not preds:
        return {"count": 0, "points": []}
    p = readings_since(datetime.fromisoformat(preds[0]["made_at"]))
    points = []
    for r in preds:
        start = datetime.fromisoformat(r["made_at"])
        end = start + timedelta(hours=1)
        if end > now:
            continue
        w = p[(p.index > start) & (p.index <= end)]
        if len(w) >= MIN_READINGS_PER_HOUR:
            points.append({"t": start.isoformat(timespec="minutes"),
                           "predicted_w": round(r["predicted_w"], 1), "actual_w": round(w.mean(), 1)})
    if not points:
        return {"count": 0, "points": []}
    err = np.array([pt["predicted_w"] - pt["actual_w"] for pt in points])
    return {"count": len(points), "mae_w": round(float(np.abs(err).mean()), 2),
            "rmse_w": round(float(np.sqrt((err ** 2).mean())), 2), "points": points[-144:]}


@app.post("/api/settings")
def update_settings():
    d = request.get_json(force=True)
    if "threshold_w" in d:
        set_setting("threshold_w", max(float(d["threshold_w"]), 0))
    if "auto_control" in d:
        set_setting("auto_control", int(bool(d["auto_control"])))
    if d.get("relay_mode") in ("auto", "on", "off"):
        set_setting("relay_mode", d["relay_mode"])
    if d.get("reset_trip"):
        set_setting("tripped", 0)
    return jsonify(get_settings())


@app.get("/export.csv")
def export_csv():
    with db() as conn:
        readings = pd.read_sql_query("SELECT ts, power_w, voltage, current_a, energy_kwh, source "
                                     "FROM readings ORDER BY ts", conn)
    return Response(readings.to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=energy_readings.csv"})


@app.get("/evaluation.csv")
def evaluation_csv():
    pts = evaluation()["points"]
    return Response(pd.DataFrame(pts).to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=predicted_vs_actual.csv"})


if __name__ == "__main__":
    init_db()
    print("Dashboard: http://localhost:5000   (ESP32 must use this laptop's Wi-Fi IP address)")
    app.run(host="0.0.0.0", port=5000, debug=False)
