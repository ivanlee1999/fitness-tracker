#!/usr/bin/env python3
"""
Auto-sync Garmin activities and daily metrics into the fitness tracker DB.

Usage (standalone):
    cd /home/ivan/projects/fitness-tracker
    python3 scripts/garmin_sync.py

Can also be imported and called programmatically:
    from garmin_sync import run_sync
    result = run_sync()  # returns {"synced_activities": N, "synced_daily": M, "errors": [...]}

Cron job setup (recommended — every 6 hours):
    Set up an OpenClaw cron job with:
        schedule: everyMs = 21600000  (6 * 60 * 60 * 1000)
        payload: agentTurn with message:
            "Run the Garmin sync for the fitness dashboard by executing:
             cd /home/ivan/projects/fitness-tracker && python3 scripts/garmin_sync.py && echo 'Sync complete'"
        model: minimax-portal/MiniMax-M2.7
        sessionTarget: isolated
        delivery: announce
"""

import os
import sys
from datetime import date, datetime, timedelta

# Allow imports from scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from db import init_db
from import_garmin import import_activity
from import_daily_metrics import upsert_metrics


def _connect_garmin():
    """Connect to Garmin using saved credentials.
    
    Loads OAuth tokens from ~/.garminconnect and populates display_name
    from the garth profile (required for user-scoped API endpoints).
    """
    from garminconnect import Garmin

    g = Garmin()
    g.garth.load(os.path.expanduser("~/.garminconnect"))
    # Populate display_name from cached profile — required for user-scoped
    # endpoints like /wellness/dailyHeartRate/<displayName>.
    # Loading from token cache does NOT set this automatically.
    try:
        profile = g.garth.profile
        if profile and profile.get("displayName"):
            g.display_name = profile["displayName"]
    except Exception:
        pass
    return g


def _get_last_synced_date(conn) -> str | None:
    """Get the date of the most recent activity with a garmin_activity_id."""
    row = conn.execute(
        "SELECT date FROM cardio_sessions WHERE garmin_activity_id IS NOT NULL "
        "ORDER BY date DESC, time DESC LIMIT 1"
    ).fetchone()
    return row["date"] if row else None


def _get_synced_daily_dates(conn, since: str) -> set[str]:
    """Get set of dates that already have daily_metrics entries since a date."""
    rows = conn.execute(
        "SELECT date FROM daily_metrics WHERE date >= ?", (since,)
    ).fetchall()
    return {r["date"] for r in rows}


def sync_activities(conn, garmin) -> tuple[int, list[str]]:
    """Fetch and import new Garmin activities. Returns (count, errors)."""
    errors = []
    synced = 0

    last_date = _get_last_synced_date(conn)

    if last_date:
        # Fetch activities since the last synced date (with some overlap for safety)
        start_date = datetime.strptime(last_date, "%Y-%m-%d").date()
    else:
        # First sync: fetch last 30 days
        start_date = date.today() - timedelta(days=30)

    end_date = date.today()

    try:
        activities = garmin.get_activities_by_date(
            start_date.isoformat(), end_date.isoformat()
        )
    except Exception as e:
        errors.append(f"Failed to fetch activities: {e}")
        return 0, errors

    for activity in activities:
        try:
            sid = import_activity(conn, activity)
            if sid is not None:
                name = activity.get("activityName", "Unknown")
                print(f"  Imported activity: {name} (id={sid})")
                synced += 1
        except Exception as e:
            aid = activity.get("activityId", "?")
            name = activity.get("activityName", "?")
            errors.append(f"Activity {aid} ({name}): {e}")

    return synced, errors


def sync_daily_metrics(conn, garmin) -> tuple[int, list[str]]:
    """Fetch and import daily metrics for recent dates. Returns (count, errors)."""
    errors = []
    synced = 0

    # Sync last 7 days of daily metrics (to catch updates)
    today = date.today()
    start = today - timedelta(days=7)
    existing_dates = _get_synced_daily_dates(conn, start.isoformat())

    for i in range(8):  # 0..7 days ago
        d = (start + timedelta(days=i)).isoformat()

        # Skip dates that already have metrics (unless it's today — always refresh today)
        if d in existing_dates and d != today.isoformat():
            continue

        try:
            metrics = _fetch_daily_metrics_for_date(garmin, d)
            if metrics:
                ok = upsert_metrics(conn, d, metrics)
                if ok:
                    print(f"  Synced daily metrics for {d}")
                    synced += 1
        except Exception as e:
            errors.append(f"Daily metrics {d}: {e}")

    return synced, errors


def _fetch_daily_metrics_for_date(garmin, date_str: str) -> dict | None:
    """Fetch all daily metric components from Garmin for a single date."""
    metrics = {"date": date_str}
    has_data = False

    # Heart rate / resting HR
    try:
        hr_data = garmin.get_heart_rates(date_str)
        if hr_data:
            rhr = hr_data.get("restingHeartRate")
            if rhr:
                metrics["restingHeartRate"] = rhr
                has_data = True
    except Exception:
        pass

    # HRV
    try:
        hrv_data = garmin.get_hrv_data(date_str)
        if hrv_data:
            summary = hrv_data.get("hrvSummary", {})
            if summary:
                if summary.get("lastNightAvg"):
                    metrics["hrvMs"] = summary["lastNightAvg"]
                    has_data = True
                if summary.get("baseline", {}).get("lowUpper"):
                    metrics["hrvBaselineMs"] = summary["baseline"]["lowUpper"]
                if summary.get("status"):
                    metrics["hrvStatus"] = summary["status"].lower()
    except Exception:
        pass

    # Body battery
    try:
        bb_data = garmin.get_body_battery(date_str)
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            # Body battery is a list of readings; get the first (wake) and last (sleep) values
            charged = [p for p in bb_data if p.get("charged")]
            drained = [p for p in bb_data if p.get("drained")]
            if bb_data:
                # Wake value = max (peak body battery, usually morning)
                values = [p.get("bodyBatteryLevel", 0) for p in bb_data if p.get("bodyBatteryLevel")]
                if values:
                    metrics["bodyBatteryWake"] = max(values)
                    has_data = True
                # Sleep value = min from drained readings (overnight drain low point)
                # Fall back to global min if no explicit drained readings
                if drained:
                    drained_values = [p.get("bodyBatteryLevel", 100) for p in drained if p.get("bodyBatteryLevel") is not None]
                    if drained_values:
                        metrics["bodyBatterySleep"] = min(drained_values)
                elif values:
                    metrics["bodyBatterySleep"] = min(values)
    except Exception:
        pass

    # Sleep
    try:
        sleep_data = garmin.get_sleep_data(date_str)
        if sleep_data:
            daily = sleep_data.get("dailySleepDTO", {})
            if daily:
                # Garmin API returns "sleepTimeSeconds" (not "sleepTimeInSeconds")
                dur_s = daily.get("sleepTimeSeconds") or daily.get("sleepTimeInSeconds")
                if dur_s:
                    metrics["sleepDurationMin"] = round(dur_s / 60, 1)
                    has_data = True
                if daily.get("deepSleepSeconds"):
                    metrics["sleepDeepMin"] = round(daily["deepSleepSeconds"] / 60, 1)
                if daily.get("remSleepSeconds"):
                    metrics["sleepRemMin"] = round(daily["remSleepSeconds"] / 60, 1)
                if daily.get("averageSpO2Value"):
                    metrics["sleepSpo2Avg"] = daily["averageSpO2Value"]
                # Sleep score is nested under sleepScores.overall.value
                sleep_score = (
                    daily.get("sleepScores", {}).get("overall", {}).get("value")
                    or daily.get("overallSleepScore", {}).get("value")
                )
                if sleep_score:
                    metrics["sleepScore"] = sleep_score
    except Exception:
        pass

    # Steps
    try:
        steps_data = garmin.get_steps_data(date_str)
        if steps_data:
            total = sum(s.get("steps", 0) for s in steps_data if isinstance(s, dict))
            if total > 0:
                metrics["steps"] = total
                has_data = True
    except Exception:
        pass

    # Stress
    try:
        stress_data = garmin.get_stress_data(date_str)
        if stress_data:
            avg = stress_data.get("overallStressLevel")
            if avg:
                metrics["stressAvg"] = avg
                has_data = True
    except Exception:
        pass

    # Training readiness
    try:
        tr_data = garmin.get_training_readiness(date_str)
        if tr_data:
            score = tr_data.get("score")
            if score:
                metrics["trainingReadinessScore"] = score
                has_data = True
            level = tr_data.get("level")
            if level:
                metrics["trainingReadinessLevel"] = level.lower()
    except Exception:
        pass

    # Training status
    try:
        ts_data = garmin.get_training_status(date_str)
        if ts_data:
            status = ts_data.get("trainingStatus")
            if status:
                metrics["trainingStatus"] = status.lower()
                has_data = True
            vo2 = ts_data.get("vo2Max")
            if vo2:
                metrics["vo2Max"] = vo2
            # Load data
            acute = ts_data.get("acuteLoad")
            chronic = ts_data.get("chronicLoad")
            if acute and chronic and chronic > 0:
                metrics["acuteLoad"] = acute
                metrics["chronicLoad"] = chronic
                metrics["acwr"] = round(acute / chronic, 2)
    except Exception:
        pass

    return metrics if has_data else None


def run_sync(db_path: str | None = None) -> dict:
    """
    Run the full Garmin sync. Returns a result dict:
        {"synced_activities": N, "synced_daily": M, "errors": [...]}
    """
    conn = init_db(db_path)
    errors = []

    try:
        garmin = _connect_garmin()
    except Exception as e:
        return {"synced_activities": 0, "synced_daily": 0, "errors": [f"Garmin connect failed: {e}"]}

    act_count, act_errors = sync_activities(conn, garmin)
    errors.extend(act_errors)

    daily_count, daily_errors = sync_daily_metrics(conn, garmin)
    errors.extend(daily_errors)

    conn.close()

    return {
        "synced_activities": act_count,
        "synced_daily": daily_count,
        "errors": errors,
    }


def main():
    result = run_sync()

    act = result["synced_activities"]
    daily = result["synced_daily"]
    errors = result["errors"]

    if errors:
        print(f"\nWarnings/Errors ({len(errors)}):")
        for e in errors:
            print(f"  ⚠  {e}")

    if act == 0 and daily == 0 and not errors:
        print("✅ Already up to date")
    else:
        print(f"\nSynced {act} activities, {daily} daily metrics")


if __name__ == "__main__":
    main()
