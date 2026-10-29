"""
Hardware simulator: pretends to be the ESP32 + PZEM-004T + a 60 W bulb.
Use it to test the backend and dashboard before the hardware is wired.

  python simulator.py --backfill 4      # load 4 hours of past readings, then send live readings
  python simulator.py                   # live readings only (every 10 s)
  python simulator.py --backfill 4 --backfill-only
                                        # only load 4 h of history, then exit
                                        # (so a real ESP32 gets predictions straight away)

Stop with Ctrl+C. Delete energy.db afterwards so simulated data doesn't mix with real data.
"""
import argparse
import random
import time
from datetime import datetime, timedelta

import requests

SERVER = "http://localhost:5000"
BULB_W = 60.0


def bulb_should_be_on(t):
    """A simple daily routine: bulb on in the morning and evening, plus random use."""
    h = t.hour + t.minute / 60
    scheduled = 6.5 <= h < 8.5 or 18 <= h < 23
    return scheduled or random.random() < 0.05


def reading(t, relay_on=True):
    on = relay_on and bulb_should_be_on(t)
    return BULB_W * random.uniform(0.97, 1.03) if on else random.uniform(0.0, 0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=float, default=0, help="hours of past data to load first")
    ap.add_argument("--interval", type=float, default=10, help="seconds between live readings")
    ap.add_argument("--backfill-only", action="store_true", help="load history, then exit")
    args = ap.parse_args()

    if args.backfill:
        now = datetime.now()
        start = now - timedelta(hours=args.backfill)
        rows, t = [], start
        while t < now:
            rows.append({"ts": t.isoformat(), "power_w": round(reading(t), 2), "source": "simulator"})
            t += timedelta(seconds=30)
        r = requests.post(f"{SERVER}/api/readings/bulk", json=rows, timeout=30)
        print(f"Backfilled {r.json()['inserted']} readings ({args.backfill} h)")
        if args.backfill_only:
            return

    relay_on = True
    print("Sending live readings. Ctrl+C to stop.")
    while True:
        p = reading(datetime.now(), relay_on)
        try:
            resp = requests.post(f"{SERVER}/api/reading", json={
                "power_w": round(p, 2), "voltage": round(random.uniform(228, 236), 1),
                "current_a": round(p / 230, 3), "source": "simulator"}, timeout=5).json()
            relay_on = resp["relay"] == "on"
            print(f"{datetime.now():%H:%M:%S}  sent {p:6.1f} W  ->  relay {resp['relay'].upper():3}"
                  f"  predicted {resp['predicted_w']} W  alert={resp['alert']}")
        except requests.RequestException as e:
            print("Backend not reachable:", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
