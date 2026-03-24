#!/usr/bin/env python3
"""
Import a Garmin activity (JSON) into cardio_sessions + cardio_laps.

Usage:
    python import_garmin.py activity.json
    cat activity.json | python import_garmin.py

Expected JSON structure (Garmin Connect export format):
{
    "activityId": 12345678,
    "activityName": "Morning Run",
    "startTimeLocal": "2026-03-19 07:00:00",
    "activityType": {"typeKey": "running"},
    "duration": 1800.0,
    "distance": 5000.0,
    "calories": 350,
    "averageHR": 155,
    "maxHR": 175,
    "averageSpeed": 2.78,
    "maxSpeed": 3.5,
    "elevationGain": 50.0,
    "elevationLoss": 48.0,
    "averageRunningCadenceInStepsPerMinute": 170,
    "avgPower": 250,
    "normPower": 260,
    "aerobicTrainingEffect": 3.2,
    "anaerobicTrainingEffect": 1.5,
    "trainingEffectLabel": "Tempo",
    "activityTrainingLoad": 120.5,
    "avgStrideLength": 112.0,
    "avgGroundContactTime": 240.0,
    "avgVerticalOscillation": 8.5,
    "laps": [
        {"distance": 1000, "duration": 360, "averageSpeed": 2.78, "averageHR": 150, ...}
    ]
}

Idempotent: skips duplicates by garmin_activity_id or (date, name).
"""

import argparse
import json
import sys
from pathlib import Path

from db import init_db

# Map Garmin activity type keys to our enum
ACTIVITY_TYPE_MAP = {
    "running": "running",
    "trail_running": "running",
    "treadmill_running": "running",
    "cycling": "cycling",
    "road_biking": "cycling",
    "mountain_biking": "cycling",
    "indoor_cycling": "cycling",
    "swimming": "swimming",
    "lap_swimming": "swimming",
    "open_water_swimming": "swimming",
    "rowing": "rowing",
    "indoor_rowing": "rowing",
    "stair_climbing": "stair_climbing",
    "walking": "walking",
    "hiking": "hiking",
    "elliptical": "elliptical",
    "strength_training": "strength",
}


def import_activity(conn, activity: dict) -> int | None:
    """Insert a Garmin activity. Returns session_id or None if duplicate."""
    garmin_id = str(activity.get("activityId", "")) or None

    # Check for duplicate by garmin_activity_id
    if garmin_id:
        existing = conn.execute(
            "SELECT id FROM cardio_sessions WHERE garmin_activity_id = ?",
            (garmin_id,),
        ).fetchone()
        if existing:
            return None

    # Parse date/time from startTimeLocal
    start = activity.get("startTimeLocal", "")
    date = start[:10] if len(start) >= 10 else None
    time_ = start[11:16] if len(start) >= 16 else None
    name = activity.get("activityName", "Garmin Activity")

    # Check duplicate by date+name
    if date:
        existing = conn.execute(
            "SELECT id FROM cardio_sessions WHERE date = ? AND name = ?",
            (date, name),
        ).fetchone()
        if existing:
            return None

    # Map activity type
    raw_type = activity.get("activityType", {})
    if isinstance(raw_type, dict):
        raw_type = raw_type.get("typeKey", "other")
    activity_type = ACTIVITY_TYPE_MAP.get(raw_type, "other")

    # Extract HR / power zone times
    def _z(key): return activity.get(key) or 0.0

    cur = conn.execute(
        """INSERT INTO cardio_sessions (
            date, time, name, activity_type,
            duration_seconds, moving_duration_seconds, distance_meters, calories,
            avg_hr_bpm, max_hr_bpm,
            avg_speed_mps, max_speed_mps,
            elevation_gain_m, elevation_loss_m,
            avg_cadence, max_cadence,
            avg_power_watts, normalized_power_watts, total_work_kj,
            training_effect_aerobic, training_effect_anaerobic,
            training_effect_label, training_load,
            avg_stride_length_cm, avg_ground_contact_time_ms,
            avg_vertical_oscillation_cm,
            vo2_max, avg_vertical_ratio, avg_ground_contact_balance,
            avg_respiration_rate, min_respiration_rate, max_respiration_rate,
            avg_grade_adjusted_speed_mps,
            avg_step_speed_loss_pct, avg_step_speed_loss_mps,
            begin_potential_stamina, end_potential_stamina,
            workout_feel, workout_rpe, workout_compliance_score,
            total_steps, avg_temperature_c, impact_load, body_battery_change,
            fastest_split_1k_s, fastest_split_mile_s, fastest_split_5k_s,
            hr_zone1_s, hr_zone2_s, hr_zone3_s, hr_zone4_s, hr_zone5_s,
            power_zone1_s, power_zone2_s, power_zone3_s, power_zone4_s, power_zone5_s,
            garmin_activity_id, source_url, notes
        ) VALUES (
            ?,?,?,?,
            ?,?,?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,?,?,
            ?,?,?,
            ?,
            ?,?,
            ?,?,
            ?,?,?,
            ?,?,?,?,
            ?,?,?,
            ?,?,?,?,?,
            ?,?,?,?,?,
            ?,?,?
        )""",
        (
            date, time_, name, activity_type,
            activity.get("duration"),
            activity.get("movingDuration"),
            activity.get("distance"),
            activity.get("calories"),
            activity.get("averageHR"),
            activity.get("maxHR"),
            activity.get("averageSpeed"),
            activity.get("maxSpeed"),
            activity.get("elevationGain"),
            activity.get("elevationLoss"),
            activity.get("averageRunningCadenceInStepsPerMinute") or activity.get("avgCadence"),
            activity.get("maxRunningCadenceInStepsPerMinute") or activity.get("maxCadence"),
            activity.get("avgPower"),
            activity.get("normPower") or activity.get("normalizedPower"),
            activity.get("totalWork"),
            activity.get("aerobicTrainingEffect"),
            activity.get("anaerobicTrainingEffect"),
            activity.get("trainingEffectLabel"),
            activity.get("activityTrainingLoad") or activity.get("trainingLoad"),
            activity.get("avgStrideLength"),
            activity.get("avgGroundContactTime"),
            activity.get("avgVerticalOscillation"),
            activity.get("vO2MaxValue") or activity.get("vo2Max"),
            activity.get("avgVerticalRatio"),
            activity.get("avgGroundContactBalance"),
            activity.get("avgRespirationRate"),
            activity.get("minRespirationRate"),
            activity.get("maxRespirationRate"),
            activity.get("avgGradeAdjustedSpeed"),
            activity.get("avgStepSpeedLossPercent") or activity.get("avgStepSpeedLossPct"),
            activity.get("avgStepSpeedLoss"),
            activity.get("beginPotentialStamina"),
            activity.get("endPotentialStamina"),
            activity.get("directWorkoutFeel") or activity.get("workoutFeel"),
            activity.get("directWorkoutRpe") or activity.get("workoutRpe"),
            activity.get("directWorkoutComplianceScore") or activity.get("workoutComplianceScore"),
            activity.get("steps"),
            activity.get("avgTemperature"),
            activity.get("impactLoad"),
            activity.get("differenceBodyBattery"),
            activity.get("fastestSplit_1000"),
            activity.get("fastestSplit_1609"),
            activity.get("fastestSplit_5000"),
            _z("hrTimeInZone_1"), _z("hrTimeInZone_2"), _z("hrTimeInZone_3"), _z("hrTimeInZone_4"), _z("hrTimeInZone_5"),
            _z("powerTimeInZone_1"), _z("powerTimeInZone_2"), _z("powerTimeInZone_3"), _z("powerTimeInZone_4"), _z("powerTimeInZone_5"),
            garmin_id,
            activity.get("sourceUrl") or activity.get("source_url"),
            activity.get("notes"),
        ),
    )
    session_id = cur.lastrowid

    # Import laps
    laps = activity.get("laps", [])
    for lap_num, lap in enumerate(laps, 1):
        conn.execute(
            """INSERT INTO cardio_laps (
                session_id, lap_number, distance_m, duration_s,
                avg_speed_mps, avg_hr_bpm, avg_power_watts,
                avg_cadence, elevation_gain_m,
                avg_stride_length_cm, avg_ground_contact_time_ms,
                avg_vertical_oscillation_cm, avg_vertical_ratio,
                normalized_power_watts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                lap_num,
                lap.get("distance"),
                lap.get("duration"),
                lap.get("averageSpeed"),
                lap.get("averageHR"),
                lap.get("avgPower"),
                lap.get("averageCadence") or lap.get("avgCadence"),
                lap.get("elevationGain"),
                lap.get("avgStrideLength") or lap.get("strideLength"),
                lap.get("avgGroundContactTime") or lap.get("groundContactTime"),
                lap.get("avgVerticalOscillation") or lap.get("verticalOscillation"),
                lap.get("avgVerticalRatio") or lap.get("verticalRatio"),
                lap.get("normalizedPower") or lap.get("normPower"),
            ),
        )

    conn.commit()
    return session_id


def main():
    parser = argparse.ArgumentParser(description="Import Garmin activity into fitness DB")
    parser.add_argument("file", nargs="?", help="JSON file with Garmin activity data")
    parser.add_argument("--db", default=None, help="Database path")
    args = parser.parse_args()

    if args.file:
        data = json.loads(Path(args.file).read_text())
    else:
        data = json.load(sys.stdin)

    conn = init_db(args.db)

    # Support single activity or list
    activities = data if isinstance(data, list) else [data]

    inserted = 0
    skipped = 0
    for activity in activities:
        sid = import_activity(conn, activity)
        if sid is not None:
            n_laps = len(activity.get("laps", []))
            print(f"  Inserted '{activity.get('activityName', 'N/A')}' (id={sid}): {n_laps} laps")
            inserted += 1
        else:
            print(f"  Skipped duplicate: '{activity.get('activityName', 'N/A')}'")
            skipped += 1

    print(f"\nDone: {inserted} inserted, {skipped} skipped")
    conn.close()


if __name__ == "__main__":
    main()
