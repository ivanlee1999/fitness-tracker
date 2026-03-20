#!/usr/bin/env python3
"""
Import daily metrics (from Garmin daily summary) into the fitness database.

Usage:
    python import_daily_metrics.py metrics.json
    cat metrics.json | python import_daily_metrics.py

Expected JSON:
{
    "date": "2026-03-19",
    "restingHeartRate": 52,
    "hrvMs": 45.0,
    "hrvBaselineMs": 42.0,
    "hrvStatus": "balanced",
    "bodyBatteryWake": 75,
    "bodyBatterySleep": 95,
    "trainingReadinessScore": 68,
    "trainingReadinessLevel": "moderate",
    "vo2Max": 48.5,
    "sleepDurationMin": 420,
    "sleepScore": 82,
    "sleepDeepMin": 90,
    "sleepRemMin": 110,
    "sleepSpo2Avg": 96.5,
    "steps": 8500,
    "activeCalories": 450,
    "stressAvg": 32,
    "trainingStatus": "productive",
    "acwr": 1.1,
    "acuteLoad": 350,
    "chronicLoad": 320
}

Also supports snake_case keys. Idempotent by date (upsert).
"""

import argparse
import json
import sys
from pathlib import Path

from db import init_db


def to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    import re
    s1 = re.sub(r"([A-Z])", r"_\1", name)
    return s1.lower().lstrip("_")


# Map from possible JSON keys (camelCase) to DB columns
FIELD_MAP = {
    "resting_hr": "resting_hr",
    "resting_heart_rate": "resting_hr",
    "hrv_ms": "hrv_ms",
    "hrv_baseline_ms": "hrv_baseline_ms",
    "hrv_status": "hrv_status",
    "body_battery_wake": "body_battery_wake",
    "body_battery_sleep": "body_battery_sleep",
    "training_readiness_score": "training_readiness_score",
    "training_readiness_level": "training_readiness_level",
    "vo2_max": "vo2_max",
    "sleep_duration_min": "sleep_duration_min",
    "sleep_score": "sleep_score",
    "sleep_deep_min": "sleep_deep_min",
    "sleep_rem_min": "sleep_rem_min",
    "sleep_spo2_avg": "sleep_spo2_avg",
    "steps": "steps",
    "active_calories": "active_calories",
    "stress_avg": "stress_avg",
    "training_status": "training_status",
    "acwr": "acwr",
    "acute_load": "acute_load",
    "chronic_load": "chronic_load",
}

DB_COLUMNS = list(set(FIELD_MAP.values()))


def normalize_metrics(raw: dict) -> dict:
    """Normalize key names to DB column names."""
    result = {}
    for key, val in raw.items():
        snake = to_snake(key)
        col = FIELD_MAP.get(snake)
        if col:
            result[col] = val
    # Also try direct match
    for key, val in raw.items():
        if key in FIELD_MAP:
            result[FIELD_MAP[key]] = val
        elif key in DB_COLUMNS:
            result[key] = val
    return result


def upsert_metrics(conn, date: str, metrics: dict) -> bool:
    """Upsert daily metrics for a date. Returns True if inserted/updated."""
    normalized = normalize_metrics(metrics)

    existing = conn.execute(
        "SELECT id FROM daily_metrics WHERE date = ?", (date,)
    ).fetchone()

    if existing:
        # Update
        sets = ", ".join(f"{col} = ?" for col in normalized.keys())
        if not sets:
            return False
        vals = list(normalized.values()) + [date]
        conn.execute(f"UPDATE daily_metrics SET {sets} WHERE date = ?", vals)
    else:
        # Insert
        normalized["date"] = date
        cols = ", ".join(normalized.keys())
        placeholders = ", ".join("?" for _ in normalized)
        conn.execute(
            f"INSERT INTO daily_metrics ({cols}) VALUES ({placeholders})",
            list(normalized.values()),
        )

    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(description="Import daily metrics into fitness DB")
    parser.add_argument("file", nargs="?", help="JSON file with daily metrics")
    parser.add_argument("--db", default=None, help="Database path")
    args = parser.parse_args()

    if args.file:
        data = json.loads(Path(args.file).read_text())
    else:
        data = json.load(sys.stdin)

    conn = init_db(args.db)

    # Support single day or list
    entries = data if isinstance(data, list) else [data]

    count = 0
    for entry in entries:
        date = entry.get("date")
        if not date:
            print(f"  Skipping entry without date: {entry}")
            continue
        ok = upsert_metrics(conn, date, entry)
        if ok:
            print(f"  Upserted metrics for {date}")
            count += 1

    print(f"\nDone: {count} days upserted")
    conn.close()


if __name__ == "__main__":
    main()
