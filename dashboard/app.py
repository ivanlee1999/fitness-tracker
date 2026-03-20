"""
Fitness Tracker Dashboard — FastAPI + Jinja2 + Chart.js

Routes:
    /                       Today's summary
    /gym                    Gym history
    /gym/exercise/{name}    Exercise progression chart
    /cardio                 Cardio history
    /cardio/{type}          Cardio trends for a specific activity type
    /recovery               HRV, HR, body battery, sleep charts
    /advice                 Rule-based suggestions
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "fitness.db"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Ivan's Fitness Tracker")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Helpers ──────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def format_pace(speed_mps: float | None) -> str:
    """Convert m/s to min:sec/km pace string."""
    if not speed_mps or speed_mps <= 0:
        return "N/A"
    pace_s_per_km = 1000.0 / speed_mps
    mins = int(pace_s_per_km // 60)
    secs = int(pace_s_per_km % 60)
    return f"{mins}:{secs:02d}/km"


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "N/A"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# Make helpers available in templates
@app.middleware("http")
async def add_template_globals(request: Request, call_next):
    response = await call_next(request)
    return response


# ── Routes ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Today's summary page."""
    db = get_db()
    today = date.today().isoformat()

    # Last gym workout
    last_gym = db.execute(
        "SELECT * FROM gym_sessions ORDER BY date DESC, time DESC LIMIT 1"
    ).fetchone()

    last_gym_exercises = []
    if last_gym:
        last_gym_exercises = rows_to_dicts(db.execute(
            """SELECT ge.*, GROUP_CONCAT(
                    gs.set_type || ': ' || gs.reps || 'x' || COALESCE(gs.weight_lb, 0) || 'lb',
                    ' | '
               ) as sets_summary
               FROM gym_exercises ge
               LEFT JOIN gym_sets gs ON gs.exercise_id = ge.id
               WHERE ge.session_id = ?
               GROUP BY ge.id
               ORDER BY ge.exercise_order""",
            (last_gym["id"],),
        ).fetchall())

    # Last cardio
    last_cardio = db.execute(
        "SELECT * FROM cardio_sessions ORDER BY date DESC, time DESC LIMIT 1"
    ).fetchone()

    # Today's metrics
    todays_metrics = db.execute(
        "SELECT * FROM daily_metrics WHERE date = ?", (today,)
    ).fetchone()

    # Recent metrics for context
    recent_metrics = db.execute(
        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT 1"
    ).fetchone()

    # Workout frequency (last 7 days)
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    gym_count_week = db.execute(
        "SELECT COUNT(*) as cnt FROM gym_sessions WHERE date >= ?", (week_ago,)
    ).fetchone()["cnt"]
    cardio_count_week = db.execute(
        "SELECT COUNT(*) as cnt FROM cardio_sessions WHERE date >= ?", (week_ago,)
    ).fetchone()["cnt"]

    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "today": today,
        "last_gym": dict(last_gym) if last_gym else None,
        "last_gym_exercises": last_gym_exercises,
        "last_cardio": dict(last_cardio) if last_cardio else None,
        "todays_metrics": dict(todays_metrics) if todays_metrics else None,
        "recent_metrics": dict(recent_metrics) if recent_metrics else None,
        "gym_count_week": gym_count_week,
        "cardio_count_week": cardio_count_week,
        "format_pace": format_pace,
        "format_duration": format_duration,
    })


@app.get("/gym", response_class=HTMLResponse)
async def gym_history(request: Request, muscle_group: str = ""):
    """List gym sessions, filterable by muscle group."""
    db = get_db()

    if muscle_group:
        sessions = rows_to_dicts(db.execute(
            """SELECT DISTINCT gs.*
               FROM gym_sessions gs
               JOIN gym_exercises ge ON ge.session_id = gs.id
               WHERE ge.muscle_groups LIKE ?
               ORDER BY gs.date DESC, gs.time DESC""",
            (f"%{muscle_group}%",),
        ).fetchall())
    else:
        sessions = rows_to_dicts(db.execute(
            "SELECT * FROM gym_sessions ORDER BY date DESC, time DESC"
        ).fetchall())

    # Get exercises for each session
    for s in sessions:
        s["exercises"] = rows_to_dicts(db.execute(
            """SELECT ge.*, GROUP_CONCAT(
                    gs.set_type || ': ' || gs.reps || 'x' || COALESCE(gs.weight_lb, 0) || 'lb',
                    ' | '
               ) as sets_summary
               FROM gym_exercises ge
               LEFT JOIN gym_sets gs ON gs.exercise_id = ge.id
               WHERE ge.session_id = ?
               GROUP BY ge.id
               ORDER BY ge.exercise_order""",
            (s["id"],),
        ).fetchall())

    # Get all unique muscle groups for filter
    all_muscles_raw = db.execute(
        "SELECT DISTINCT muscle_groups FROM gym_exercises WHERE muscle_groups != ''"
    ).fetchall()
    all_muscles = set()
    for row in all_muscles_raw:
        for mg in row["muscle_groups"].split(","):
            mg = mg.strip()
            if mg:
                all_muscles.add(mg)
    all_muscles = sorted(all_muscles)

    # Get all exercise names for linking
    exercise_names = [r["name"] for r in db.execute(
        "SELECT DISTINCT name FROM gym_exercises ORDER BY name"
    ).fetchall()]

    db.close()

    return templates.TemplateResponse("gym.html", {
        "request": request,
        "sessions": sessions,
        "muscle_group": muscle_group,
        "all_muscles": all_muscles,
        "exercise_names": exercise_names,
    })


@app.get("/gym/exercise/{name}", response_class=HTMLResponse)
async def exercise_progression(request: Request, name: str):
    """Exercise progression chart."""
    db = get_db()
    decoded_name = unquote(name)

    progression = rows_to_dicts(db.execute(
        """SELECT date, exercise_name, top_set_weight_lb, top_set_reps, min_assist_lb
           FROM exercise_progression
           WHERE exercise_name = ?
           ORDER BY date""",
        (decoded_name,),
    ).fetchall())

    # Get detailed sets for the exercise
    detailed = rows_to_dicts(db.execute(
        """SELECT gs.date, ge.name, gset.set_number, gset.set_type,
                  gset.reps, gset.weight_lb, gset.assist_lb
           FROM gym_sessions gs
           JOIN gym_exercises ge ON ge.session_id = gs.id
           JOIN gym_sets gset ON gset.exercise_id = ge.id
           WHERE ge.name = ?
           ORDER BY gs.date, gset.set_number""",
        (decoded_name,),
    ).fetchall())

    # Check if this is an assisted exercise
    is_assisted = any(r.get("min_assist_lb") is not None and r["min_assist_lb"] > 0 for r in progression)

    db.close()

    return templates.TemplateResponse("exercise.html", {
        "request": request,
        "name": decoded_name,
        "progression": progression,
        "detailed": detailed,
        "is_assisted": is_assisted,
    })


@app.get("/cardio", response_class=HTMLResponse)
async def cardio_history(request: Request, activity_type: str = ""):
    """List cardio sessions."""
    db = get_db()

    if activity_type:
        sessions = rows_to_dicts(db.execute(
            "SELECT * FROM cardio_sessions WHERE activity_type = ? ORDER BY date DESC",
            (activity_type,),
        ).fetchall())
    else:
        sessions = rows_to_dicts(db.execute(
            "SELECT * FROM cardio_sessions ORDER BY date DESC"
        ).fetchall())

    # Get activity types for filter
    activity_types = [r["activity_type"] for r in db.execute(
        "SELECT DISTINCT activity_type FROM cardio_sessions ORDER BY activity_type"
    ).fetchall()]

    db.close()

    return templates.TemplateResponse("cardio.html", {
        "request": request,
        "sessions": sessions,
        "activity_type": activity_type,
        "activity_types": activity_types,
        "format_pace": format_pace,
        "format_duration": format_duration,
    })


@app.get("/cardio/{activity_type}", response_class=HTMLResponse)
async def cardio_trends_page(request: Request, activity_type: str):
    """Cardio trends charts for a specific activity type."""
    db = get_db()

    trends = rows_to_dicts(db.execute(
        """SELECT * FROM cardio_trends
           WHERE activity_type = ?
           ORDER BY year_week""",
        (activity_type,),
    ).fetchall())

    sessions = rows_to_dicts(db.execute(
        """SELECT date, name, duration_seconds, distance_meters,
                  avg_speed_mps, avg_hr_bpm, avg_power_watts, calories
           FROM cardio_sessions
           WHERE activity_type = ?
           ORDER BY date""",
        (activity_type,),
    ).fetchall())

    db.close()

    return templates.TemplateResponse("cardio_trends.html", {
        "request": request,
        "activity_type": activity_type,
        "trends": trends,
        "sessions": sessions,
        "format_pace": format_pace,
        "format_duration": format_duration,
    })


@app.get("/recovery", response_class=HTMLResponse)
async def recovery(request: Request):
    """Recovery metrics charts."""
    db = get_db()

    metrics = rows_to_dicts(db.execute(
        "SELECT * FROM daily_metrics ORDER BY date"
    ).fetchall())

    db.close()

    return templates.TemplateResponse("recovery.html", {
        "request": request,
        "metrics": metrics,
    })


@app.get("/advice", response_class=HTMLResponse)
async def advice(request: Request):
    """Rule-based training advice from last 30 days of data."""
    db = get_db()
    cutoff = (date.today() - timedelta(days=30)).isoformat()

    suggestions = []

    # --- Gym analysis ---
    gym_sessions = rows_to_dicts(db.execute(
        "SELECT * FROM gym_sessions WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall())

    gym_count = len(gym_sessions)
    if gym_count == 0:
        suggestions.append({
            "category": "Gym",
            "icon": "🏋️",
            "level": "warning",
            "message": "No gym sessions logged in the last 30 days. Consider starting with 2-3 sessions per week."
        })
    elif gym_count < 8:
        suggestions.append({
            "category": "Gym",
            "icon": "🏋️",
            "level": "info",
            "message": f"You've logged {gym_count} gym sessions in 30 days (~{gym_count/4:.1f}/week). "
                       f"For muscle growth, aim for 3-5 sessions per week."
        })
    else:
        suggestions.append({
            "category": "Gym",
            "icon": "🏋️",
            "level": "success",
            "message": f"Great consistency! {gym_count} sessions in 30 days (~{gym_count/4:.1f}/week)."
        })

    # Volume by muscle group
    weekly_vol = rows_to_dicts(db.execute(
        """SELECT muscle_group, SUM(total_volume_lb) as vol, SUM(total_reps) as reps
           FROM weekly_volume
           WHERE week_start >= ?
           GROUP BY muscle_group
           ORDER BY vol DESC""",
        (cutoff,),
    ).fetchall())

    if weekly_vol:
        top_muscle = weekly_vol[0]["muscle_group"]
        low_muscles = [v["muscle_group"] for v in weekly_vol if v["vol"] < weekly_vol[0]["vol"] * 0.3]
        if low_muscles:
            suggestions.append({
                "category": "Balance",
                "icon": "⚖️",
                "level": "info",
                "message": f"Your most trained muscle group is {top_muscle}. "
                           f"Consider adding more volume for: {', '.join(low_muscles[:3])}"
            })

    # Exercise progression
    progression = rows_to_dicts(db.execute(
        """SELECT exercise_name,
                  MIN(top_set_weight_lb) as first_weight,
                  MAX(top_set_weight_lb) as last_weight,
                  COUNT(*) as sessions
           FROM exercise_progression
           WHERE date >= ?
           GROUP BY exercise_name
           HAVING sessions >= 2""",
        (cutoff,),
    ).fetchall())

    for p in progression:
        if p["last_weight"] > p["first_weight"]:
            pct = ((p["last_weight"] - p["first_weight"]) / p["first_weight"]) * 100
            suggestions.append({
                "category": "Progress",
                "icon": "📈",
                "level": "success",
                "message": f"{p['exercise_name']}: improved from {p['first_weight']}lb to {p['last_weight']}lb "
                           f"(+{pct:.0f}%) over {p['sessions']} sessions."
            })
        elif p["last_weight"] == p["first_weight"] and p["sessions"] >= 3:
            suggestions.append({
                "category": "Progress",
                "icon": "📊",
                "level": "info",
                "message": f"{p['exercise_name']}: plateaued at {p['last_weight']}lb for {p['sessions']} sessions. "
                           f"Consider varying rep ranges or adding drop sets."
            })

    # --- Cardio analysis ---
    cardio_sessions = rows_to_dicts(db.execute(
        "SELECT * FROM cardio_sessions WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall())

    if not cardio_sessions:
        suggestions.append({
            "category": "Cardio",
            "icon": "🏃",
            "level": "info",
            "message": "No cardio sessions logged. Consider 2-3 cardio sessions per week for heart health."
        })
    else:
        suggestions.append({
            "category": "Cardio",
            "icon": "🏃",
            "level": "success",
            "message": f"{len(cardio_sessions)} cardio sessions in 30 days."
        })

    # --- Recovery analysis ---
    metrics = rows_to_dicts(db.execute(
        "SELECT * FROM daily_metrics WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall())

    if metrics:
        avg_hrv = sum(m["hrv_ms"] for m in metrics if m.get("hrv_ms")) / max(1, sum(1 for m in metrics if m.get("hrv_ms")))
        avg_sleep = sum(m["sleep_duration_min"] for m in metrics if m.get("sleep_duration_min")) / max(1, sum(1 for m in metrics if m.get("sleep_duration_min")))

        if avg_sleep and avg_sleep < 420:  # 7 hours
            suggestions.append({
                "category": "Sleep",
                "icon": "😴",
                "level": "warning",
                "message": f"Average sleep: {avg_sleep:.0f} min ({avg_sleep/60:.1f}h). "
                           f"Aim for 7-9 hours for optimal recovery."
            })

        if avg_hrv:
            suggestions.append({
                "category": "Recovery",
                "icon": "💓",
                "level": "info",
                "message": f"Average HRV: {avg_hrv:.0f}ms over the last 30 days."
            })

        # Check for overtraining signals
        low_bb = [m for m in metrics if m.get("body_battery_wake") and m["body_battery_wake"] < 30]
        if len(low_bb) > 3:
            suggestions.append({
                "category": "Recovery",
                "icon": "⚠️",
                "level": "warning",
                "message": f"Body battery was below 30 on {len(low_bb)} days. Consider adding rest days."
            })
    else:
        suggestions.append({
            "category": "Recovery",
            "icon": "📱",
            "level": "info",
            "message": "No daily metrics imported yet. Import Garmin data for personalized recovery advice."
        })

    # --- General suggestions ---
    if gym_count > 0 and not cardio_sessions:
        suggestions.append({
            "category": "General",
            "icon": "💡",
            "level": "info",
            "message": "You're doing great with strength training! Add 2-3 cardio sessions per week for a balanced program."
        })

    db.close()

    return templates.TemplateResponse("advice.html", {
        "request": request,
        "suggestions": suggestions,
        "gym_count": gym_count,
        "cardio_count": len(cardio_sessions) if cardio_sessions else 0,
        "days_analyzed": 30,
    })
