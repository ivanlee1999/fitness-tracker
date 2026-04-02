#!/usr/bin/env python3
"""
Fitness Tracker MCP Server

Tools available to the AI assistant:
  - import_garmin_cardio   — fetch a run/ride/swim from Garmin and store it
  - import_garmin_gym      — fetch a strength session from Garmin and store it
  - import_garmin_daily    — fetch daily recovery metrics (HRV, sleep, etc.)
  - list_recent_activities — list recent Garmin activities (preview before import)
  - get_db_summary         — quick stats on what's already in the DB

All tools are idempotent: duplicate imports are silently skipped.

Usage (stdio, via mcporter):
    python3 mcp_server.py

Environment:
    FITNESS_DB_PATH   — path to fitness.db  (default: ./fitness.db)
    GARMINTOKENS      — path to garmin token dir (default: ~/.garminconnect)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Garmin + DB imports ──────────────────────────────────────────
try:
    from garminconnect import Garmin
except ImportError:
    print("ERROR: garminconnect not installed. Run: pip install garminconnect", file=sys.stderr)
    sys.exit(1)

_BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FITNESS_DB_PATH", _BASE / "fitness.db"))
GARMIN_TOKENS = Path(os.environ.get("GARMINTOKENS", Path.home() / ".garminconnect"))

sys.path.insert(0, str(_BASE / "scripts"))
from db import init_db
from import_garmin import import_activity, ACTIVITY_TYPE_MAP
from import_gym import insert_session as insert_gym_session
from import_daily_metrics import upsert_metrics

from mcp.server.fastmcp import FastMCP

VERSION = "1.0.0"

mcp = FastMCP("fitness-tracker")

# ── Garmin client (lazy, cached) ──────────────────────────────────
_garmin = None


def get_garmin():
    global _garmin
    if _garmin is None:
        g = Garmin()
        g.garth.load(str(GARMIN_TOKENS))
        _garmin = g
    return _garmin


def get_conn():
    return init_db(DB_PATH)


# ════════════════════════════════════════════════════════════════
# Tool: list_recent_activities
# ════════════════════════════════════════════════════════════════
@mcp.tool()
def list_recent_activities(limit: int = 10) -> str:
    """
    List recent Garmin activities so you can pick which one(s) to import.

    Args:
        limit: How many activities to return (default 10, max 50).

    Returns JSON list with: activityId, name, type, date, distance_km, duration_min.
    """
    limit = min(int(limit), 50)
    g = get_garmin()
    raw = g.get_activities(0, limit)

    results = []
    for a in raw:
        dist = a.get("distance", 0) or 0
        dur = a.get("duration", 0) or 0
        results.append({
            "activityId": a.get("activityId"),
            "name": a.get("activityName"),
            "type": a.get("activityType", {}).get("typeKey"),
            "date": a.get("startTimeLocal", "")[:10],
            "start_time": a.get("startTimeLocal"),
            "distance_km": round(dist / 1000, 2),
            "duration_min": round(dur / 60, 1),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
        })
    return json.dumps(results, indent=2)


# ════════════════════════════════════════════════════════════════
# Tool: import_garmin_cardio
# ════════════════════════════════════════════════════════════════
@mcp.tool()
def import_garmin_cardio(activity_id: int = 0, date_str: str = "") -> str:
    """
    Import a cardio session (run, ride, swim, etc.) from Garmin into the fitness DB.

    Args:
        activity_id: Specific Garmin activity ID (preferred). Use list_recent_activities to find it.
                     Pass 0 to use date_str or import most recent.
        date_str:    YYYY-MM-DD — imports all cardio activities for that day. Leave empty to use activity_id.

    Returns a summary of what was inserted or skipped.
    """
    g = get_garmin()
    conn = get_conn()

    activities = []

    if activity_id and int(activity_id) > 0:
        raw_list = g.get_activities(0, 50)
        matched = [a for a in raw_list if str(a.get("activityId")) == str(activity_id)]
        if not matched:
            return f"Activity {activity_id} not found in your recent 50 activities."
        activities = matched
    elif date_str:
        data = g.get_activities_fordate(date_str)
        payload = data.get("ActivitiesForDay", {}).get("payload", [])
        if not payload:
            return f"No activities found for {date_str} on Garmin."
        activities = payload
    else:
        raw_list = g.get_activities(0, 1)
        if not raw_list:
            return "No activities found on Garmin."
        activities = raw_list

    inserted = []
    skipped = []

    for a in activities:
        act_id = a.get("activityId")
        raw_type = a.get("activityType", {})
        if isinstance(raw_type, dict):
            act_type_key = raw_type.get("typeKey", "other")
        else:
            act_type_key = str(raw_type)
        mapped_type = ACTIVITY_TYPE_MAP.get(act_type_key, "other")

        if mapped_type == "strength":
            skipped.append(f"{a.get('activityName')} (strength — use import_garmin_gym)")
            continue

        # Fetch laps
        laps = []
        try:
            splits = g.get_activity_splits(act_id)
            for lap in splits.get("lapDTOs", []):
                laps.append({
                    "distance": lap.get("distance"),
                    "duration": lap.get("duration"),
                    "averageSpeed": lap.get("averageSpeed"),
                    "averageHR": lap.get("averageHR"),
                    "avgPower": lap.get("averagePower"),
                    "averageCadence": lap.get("averageRunCadence") or lap.get("averageCadence"),
                    "elevationGain": lap.get("elevationGain"),
                    # Running economy fields per lap
                    "strideLength": lap.get("strideLength"),
                    "groundContactTime": lap.get("groundContactTime"),
                    "verticalOscillation": lap.get("verticalOscillation"),
                    "verticalRatio": lap.get("verticalRatio"),
                    "normalizedPower": lap.get("normalizedPower"),
                })
        except Exception:
            pass

        activity_data = {
            "activityId": act_id,
            "activityName": a.get("activityName"),
            "startTimeLocal": a.get("startTimeLocal"),
            "activityType": a.get("activityType"),
            "duration": a.get("duration"),
            "movingDuration": a.get("movingDuration"),
            "distance": a.get("distance"),
            "calories": a.get("calories"),
            "averageHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "averageSpeed": a.get("averageSpeed"),
            "maxSpeed": a.get("maxSpeed"),
            "elevationGain": a.get("elevationGain"),
            "elevationLoss": a.get("elevationLoss"),
            "averageRunningCadenceInStepsPerMinute": a.get("averageRunningCadenceInStepsPerMinute"),
            "maxRunningCadenceInStepsPerMinute": a.get("maxRunningCadenceInStepsPerMinute"),
            "avgCadence": a.get("averageBikingCadenceInRevPerMinute"),
            "avgPower": a.get("avgPower"),
            "normPower": a.get("normPower"),
            "totalWork": a.get("totalWork"),
            "aerobicTrainingEffect": a.get("aerobicTrainingEffect"),
            "anaerobicTrainingEffect": a.get("anaerobicTrainingEffect"),
            "trainingEffectLabel": a.get("trainingEffectLabel"),
            "activityTrainingLoad": a.get("activityTrainingLoad"),
            "avgStrideLength": a.get("avgStrideLength"),
            "avgGroundContactTime": a.get("avgGroundContactTime"),
            "avgVerticalOscillation": a.get("avgVerticalOscillation"),
            "vO2MaxValue": a.get("vO2MaxValue"),
            "avgVerticalRatio": a.get("avgVerticalRatio"),
            "avgGroundContactBalance": a.get("avgGroundContactBalance"),
            "avgRespirationRate": a.get("avgRespirationRate"),
            "minRespirationRate": a.get("minRespirationRate"),
            "maxRespirationRate": a.get("maxRespirationRate"),
            "avgGradeAdjustedSpeed": a.get("avgGradeAdjustedSpeed"),
            "avgStepSpeedLossPercent": a.get("avgStepSpeedLossPercent"),
            "avgStepSpeedLoss": a.get("avgStepSpeedLoss"),
            "beginPotentialStamina": a.get("beginPotentialStamina"),
            "endPotentialStamina": a.get("endPotentialStamina"),
            "directWorkoutFeel": a.get("directWorkoutFeel"),
            "directWorkoutRpe": a.get("directWorkoutRpe"),
            "directWorkoutComplianceScore": a.get("directWorkoutComplianceScore"),
            "steps": a.get("steps"),
            "avgTemperature": a.get("avgTemperature"),
            "impactLoad": a.get("impactLoad"),
            "differenceBodyBattery": a.get("differenceBodyBattery"),
            "fastestSplit_1000": a.get("fastestSplit_1000"),
            "fastestSplit_1609": a.get("fastestSplit_1609"),
            "fastestSplit_5000": a.get("fastestSplit_5000"),
            "hrTimeInZone_1": a.get("hrTimeInZone_1"),
            "hrTimeInZone_2": a.get("hrTimeInZone_2"),
            "hrTimeInZone_3": a.get("hrTimeInZone_3"),
            "hrTimeInZone_4": a.get("hrTimeInZone_4"),
            "hrTimeInZone_5": a.get("hrTimeInZone_5"),
            "powerTimeInZone_1": a.get("powerTimeInZone_1"),
            "powerTimeInZone_2": a.get("powerTimeInZone_2"),
            "powerTimeInZone_3": a.get("powerTimeInZone_3"),
            "powerTimeInZone_4": a.get("powerTimeInZone_4"),
            "powerTimeInZone_5": a.get("powerTimeInZone_5"),
            "laps": laps,
        }

        sid = import_activity(conn, activity_data)
        if sid is not None:
            dist_km = round((a.get("distance") or 0) / 1000, 2)
            dur_min = round((a.get("duration") or 0) / 60, 1)
            inserted.append(
                f"Inserted '{a.get('activityName')}' ({mapped_type}) on {a.get('startTimeLocal', '')[:10]} "
                f"— {dist_km} km, {dur_min} min, {len(laps)} laps (id={sid})"
            )
        else:
            skipped.append(f"Skipped '{a.get('activityName')}' on {a.get('startTimeLocal', '')[:10]} — already in DB")

    conn.close()

    lines = []
    if inserted:
        lines.append(f"Inserted {len(inserted)}:")
        lines.extend(inserted)
    if skipped:
        lines.append(f"Skipped {len(skipped)}:")
        lines.extend(skipped)
    if not lines:
        lines.append("Nothing to import.")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Tool: import_garmin_gym
# ════════════════════════════════════════════════════════════════
@mcp.tool()
def import_garmin_gym(activity_id: int = 0, date_str: str = "") -> str:
    """
    Import a strength training session from Garmin into the fitness DB,
    including individual exercise sets with reps and weights.

    Args:
        activity_id: Specific Garmin activity ID. Use list_recent_activities to find it.
                     Pass 0 to auto-find by date_str or most recent.
        date_str:    YYYY-MM-DD — finds the most recent strength session on that day.

    Returns a summary of what was inserted or skipped.
    """
    g = get_garmin()
    conn = get_conn()

    target_id = None
    if activity_id and int(activity_id) > 0:
        target_id = int(activity_id)
    elif date_str:
        raw = g.get_activities_by_date(date_str, date_str, "strength_training")
        if not raw:
            return f"No strength_training activity found for {date_str} on Garmin."
        target_id = raw[0].get("activityId")
    else:
        raw_list = g.get_activities(0, 20)
        for a in raw_list:
            t = a.get("activityType", {})
            if isinstance(t, dict) and t.get("typeKey") == "strength_training":
                target_id = a.get("activityId")
                break
        if not target_id:
            return "No recent strength_training activity found on Garmin."

    # Get summary
    raw_list = g.get_activities(0, 50)
    summary = next((a for a in raw_list if str(a.get("activityId")) == str(target_id)), None)
    if not summary:
        return f"Activity {target_id} not found in your recent 50 activities."

    name = summary.get("activityName", "Strength Training")
    start = summary.get("startTimeLocal", "")
    date_val = start[:10] if len(start) >= 10 else date.today().isoformat()
    time_val = start[11:16] if len(start) >= 16 else None
    duration_s = summary.get("duration") or 0
    calories = summary.get("calories")

    # Check duplicate before fetching exercise detail
    existing = conn.execute(
        "SELECT id FROM gym_sessions WHERE date = ? AND name = ?", (date_val, name)
    ).fetchone()
    if existing:
        conn.close()
        return f"Skipped '{name}' on {date_val} — already in DB."

    # Fetch exercise sets from Garmin
    exercises = []
    try:
        sets_data = g.get_activity_exercise_sets(target_id)
        exercise_sets = sets_data.get("exerciseSets", []) or []

        ex_map = defaultdict(list)
        ex_order = {}
        order_counter = [0]

        for item in exercise_sets:
            category = item.get("category", "UNKNOWN")
            ex_name_raw = item.get("exerciseName") or category
            ex_name = ex_name_raw.replace("_", " ").title() if ex_name_raw else category.replace("_", " ").title()

            if ex_name not in ex_order:
                ex_order[ex_name] = order_counter[0]
                order_counter[0] += 1

            set_type_raw = item.get("setType", "ACTIVE").upper()
            set_type = {
                "ACTIVE": "working",
                "WARMUP": "warmup",
                "DROP_SET": "dropset",
                "FAILURE": "failure",
            }.get(set_type_raw, "working")

            reps = item.get("repetitions") or item.get("reps")
            weight_g = item.get("weight")
            weight_lb = round(weight_g * 0.00220462, 1) if weight_g else None

            ex_map[ex_name].append({
                "set_type": set_type,
                "reps": reps,
                "weight_lb": weight_lb,
            })

        for ex_name, sets in sorted(ex_map.items(), key=lambda kv: ex_order[kv[0]]):
            exercises.append({
                "name": ex_name,
                "muscle_groups": "",
                "equipment": "",
                "sets": sets,
            })
    except Exception as e:
        exercises = []

    session = {
        "date": date_val,
        "time": time_val,
        "name": name,
        "duration_min": round(duration_s / 60) if duration_s else None,
        "calories": calories,
        "source_url": f"https://connect.garmin.com/modern/activity/{target_id}",
        "exercises": exercises,
    }

    sid = insert_gym_session(conn, session)
    conn.close()

    if sid is None:
        return f"Skipped '{name}' on {date_val} — already in DB."

    n_ex = len(exercises)
    n_sets = sum(len(e["sets"]) for e in exercises)
    dur_min = round(duration_s / 60, 1) if duration_s else "?"
    return (
        f"Inserted '{name}' on {date_val} (id={sid})\n"
        f"  Duration: {dur_min} min | Calories: {calories}\n"
        f"  Exercises: {n_ex} | Sets: {n_sets}\n"
        f"  Garmin: https://connect.garmin.com/modern/activity/{target_id}"
    )


# ════════════════════════════════════════════════════════════════
# Tool: import_garmin_daily
# ════════════════════════════════════════════════════════════════
@mcp.tool()
def import_garmin_daily(date_str: str = "", days: int = 1) -> str:
    """
    Import daily recovery metrics from Garmin: HRV, resting HR, sleep, body battery, readiness.

    Args:
        date_str: YYYY-MM-DD start date (default: yesterday, since today may be incomplete).
        days:     How many consecutive days to import going backwards (default 1).

    Returns a summary of upserted days.
    """
    g = get_garmin()
    conn = get_conn()

    if not date_str:
        start = date.today() - timedelta(days=1)
    else:
        start = date.fromisoformat(date_str)

    upserted = []
    errors = []

    for i in range(int(days)):
        d = (start - timedelta(days=i)).isoformat()
        metrics = {}

        try:
            summary = g.get_user_summary(d)
            if summary:
                metrics["resting_hr"] = summary.get("restingHeartRate")
                metrics["steps"] = summary.get("totalSteps")
                metrics["active_calories"] = summary.get("activeKilocalories")
                metrics["stress_avg"] = summary.get("averageStressLevel")
        except Exception as e:
            errors.append(f"{d}: summary — {e}")

        try:
            sleep = g.get_sleep_data(d)
            sd = (sleep.get("dailySleepDTO") or {})
            sleep_s = sd.get("sleepTimeSeconds") or 0
            metrics["sleep_duration_min"] = round(sleep_s / 60, 1) if sleep_s else None
            scores = sd.get("sleepScores") or {}
            if isinstance(scores, dict):
                overall = scores.get("overall") or {}
                metrics["sleep_score"] = overall.get("value") if isinstance(overall, dict) else None
            metrics["sleep_deep_min"] = round((sd.get("deepSleepSeconds") or 0) / 60, 1) or None
            metrics["sleep_rem_min"] = round((sd.get("remSleepSeconds") or 0) / 60, 1) or None
            metrics["sleep_spo2_avg"] = sd.get("averageSpO2Value") or sd.get("avgSpo2")
        except Exception as e:
            errors.append(f"{d}: sleep — {e}")

        try:
            hrv = g.get_hrv_data(d)
            hrv_sum = hrv.get("hrvSummary") or {}
            metrics["hrv_ms"] = hrv_sum.get("weeklyAvg") or hrv_sum.get("lastNight")
            baseline = hrv_sum.get("baseline") or {}
            metrics["hrv_baseline_ms"] = baseline.get("lowUpper") if isinstance(baseline, dict) else None
            status = hrv_sum.get("status") or ""
            metrics["hrv_status"] = status.lower() or None
        except Exception as e:
            errors.append(f"{d}: HRV — {e}")

        try:
            rr = g.get_training_readiness(d)
            rr_list = rr if isinstance(rr, list) else [rr]
            for item in rr_list:
                if isinstance(item, dict) and item.get("calendarDate") == d:
                    metrics["training_readiness_score"] = item.get("score")
                    level = item.get("level") or ""
                    metrics["training_readiness_level"] = level.lower() or None
                    break
        except Exception:
            pass

        try:
            bb = g.get_body_battery(d, d)
            if isinstance(bb, list) and bb:
                first = bb[0]
                if isinstance(first, dict):
                    metrics["body_battery_wake"] = first.get("charged")
        except Exception:
            pass

        # Remove None values
        metrics = {k: v for k, v in metrics.items() if v is not None}

        if metrics:
            ok = upsert_metrics(conn, d, metrics)
            if ok:
                upserted.append(d)

    conn.close()

    lines = []
    if upserted:
        lines.append(f"Upserted {len(upserted)} day(s): {', '.join(upserted)}")
    if errors:
        lines.append(f"Partial failures: {'; '.join(errors[:3])}")
    if not lines:
        lines.append("Nothing was upserted.")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Tool: get_db_summary
# ════════════════════════════════════════════════════════════════
@mcp.tool()
def get_db_summary() -> str:
    """
    Return a quick summary of what's already stored in the fitness DB:
    counts, date ranges, and the 5 most recent entries of each type.
    """
    conn = get_conn()

    def q1(sql):
        r = conn.execute(sql).fetchone()
        return dict(r) if r else {}

    def ql(sql):
        return [dict(r) for r in conn.execute(sql).fetchall()]

    summary = {
        "cardio": {
            **q1("SELECT COUNT(*) as n, MIN(date) as first, MAX(date) as last FROM cardio_sessions"),
            "recent": ql(
                "SELECT date, name, activity_type, ROUND(distance_meters/1000.0,2) as dist_km,"
                " ROUND(duration_seconds/60.0,1) as dur_min FROM cardio_sessions ORDER BY date DESC LIMIT 5"
            ),
        },
        "gym": {
            **q1("SELECT COUNT(*) as n, MIN(date) as first, MAX(date) as last FROM gym_sessions"),
            "recent": ql(
                "SELECT date, name, duration_min FROM gym_sessions ORDER BY date DESC LIMIT 5"
            ),
        },
        "daily_metrics": {
            **q1("SELECT COUNT(*) as n, MIN(date) as first, MAX(date) as last FROM daily_metrics"),
        },
    }

    conn.close()
    return json.dumps(summary, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
