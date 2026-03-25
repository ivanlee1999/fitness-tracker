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

    # User summary (body battery at wake, current body battery, active calories, stress)
    try:
        summary = garmin.get_user_summary(date_str)
        if summary:
            bb_wake = summary.get("bodyBatteryAtWakeTime")
            if bb_wake is not None:
                metrics["bodyBatteryWake"] = bb_wake
                has_data = True
            bb_current = summary.get("bodyBatteryMostRecentValue")
            if bb_current is not None:
                metrics["bodyBatterySleep"] = bb_current
            active_cal = summary.get("activeKilocalories")
            if active_cal is not None:
                metrics["activeCalories"] = active_cal
            avg_stress = summary.get("averageStressLevel")
            if avg_stress is not None and avg_stress > 0:
                metrics["stressAvg"] = avg_stress
                has_data = True
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

    # Training readiness (API returns a list; take first item)
    try:
        tr_data = garmin.get_training_readiness(date_str)
        if tr_data:
            tr = tr_data[0] if isinstance(tr_data, list) else tr_data
            if tr.get("score") is not None:
                metrics["trainingReadinessScore"] = tr["score"]
                has_data = True
            if tr.get("level"):
                metrics["trainingReadinessLevel"] = tr["level"]
    except Exception:
        pass

    # Training status (VO2max, training status feedback, training load)
    try:
        ts_data = garmin.get_training_status(date_str)
        if ts_data:
            # VO2 Max — nested under mostRecentVO2Max.generic
            vo2_section = ts_data.get("mostRecentVO2Max", {}).get("generic", {})
            vo2 = vo2_section.get("vo2MaxPreciseValue") or vo2_section.get("vo2MaxValue")
            if vo2 is not None:
                metrics["vo2Max"] = vo2
                has_data = True

            # Training status & load — nested under mostRecentTrainingStatus.latestTrainingStatusData
            ts_status = ts_data.get("mostRecentTrainingStatus", {})
            latest_map = ts_status.get("latestTrainingStatusData", {})
            # Get the primary device entry (first available)
            ts_entry = next(iter(latest_map.values()), None) if latest_map else None
            if ts_entry:
                feedback = ts_entry.get("trainingStatusFeedbackPhrase")
                if feedback:
                    metrics["trainingStatus"] = feedback
                acwr_dto = ts_entry.get("acuteTrainingLoadDTO", {})
                if acwr_dto:
                    acwr_val = acwr_dto.get("dailyAcuteChronicWorkloadRatio")
                    if acwr_val is not None:
                        metrics["acwr"] = acwr_val
                    acute = acwr_dto.get("dailyTrainingLoadAcute")
                    if acute is not None:
                        metrics["acuteLoad"] = acute
                    chronic = acwr_dto.get("dailyTrainingLoadChronic")
                    if chronic is not None:
                        metrics["chronicLoad"] = chronic
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
