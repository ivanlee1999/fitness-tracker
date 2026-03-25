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

import json
import re
import os
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
# Use FITNESS_DB_PATH env var when set (for Docker), otherwise default to local path
DB_PATH = Path(os.environ.get('FITNESS_DB_PATH', BASE_DIR / "fitness.db"))
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

@app.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    """Offline fallback page for PWA."""
    return templates.TemplateResponse("offline.html", {"request": request})


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


@app.get("/running/economy", response_class=HTMLResponse)
async def running_economy(request: Request):
    """Running economy trends: cadence, stride, GCT, vertical oscillation, VO2 max."""
    db = get_db()

    sessions = rows_to_dicts(db.execute(
        """SELECT date, name, distance_meters, duration_seconds,
                  avg_speed_mps, avg_hr_bpm, avg_cadence, max_cadence,
                  avg_stride_length_cm, avg_ground_contact_time_ms,
                  avg_vertical_oscillation_cm, avg_vertical_ratio,
                  avg_ground_contact_balance, avg_respiration_rate,
                  avg_grade_adjusted_speed_mps, vo2_max,
                  avg_power_watts, normalized_power_watts, avg_step_speed_loss_pct,
                  calories, training_effect_aerobic, training_load
           FROM cardio_sessions
           WHERE activity_type = 'running'
           ORDER BY date ASC"""
    ).fetchall())

    db.close()

    return templates.TemplateResponse("running_economy.html", {
        "request": request,
        "sessions": sessions,
        "sessions_json": json.dumps(sessions),
        "format_pace": format_pace,
        "format_duration": format_duration,
    })


def _format_analysis_md(text: str) -> str:
    """Convert simple markdown to HTML for analysis display."""
    import re
    # headers ### -> h3
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$",  r"<h3>\1</h3>", text, flags=re.MULTILINE)
    # bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # bullet points
    lines = text.split("\n")
    result, in_list = [], False
    for line in lines:
        if line.strip().startswith("- ") or line.strip().startswith("* "):
            if not in_list:
                result.append("<ul>"); in_list = True
            result.append(f"<li>{line.strip()[2:]}</li>")
        else:
            if in_list:
                result.append("</ul>"); in_list = False
            if line.strip():
                result.append(f"<p>{line}</p>")
    if in_list:
        result.append("</ul>")
    return "\n".join(result)


def _build_run_summary_for_ai(sessions: list) -> str:
    """Build a compact text summary of running sessions for the AI prompt."""
    lines = []
    for s in sessions:
        pace = format_pace(s.get("avg_speed_mps"))
        gap  = format_pace(s.get("avg_grade_adjusted_speed_mps"))
        parts = [
            f"Date: {s['date']}",
            f"Name: {s.get('name') or 'Run'}",
            f"Dist: {(s.get('distance_meters') or 0)/1000:.1f} km",
            f"Pace: {pace}",
            f"GAP: {gap}",
            f"HR: {s.get('avg_hr_bpm') or '—'} bpm",
            f"Cadence: {round(s['avg_cadence']) if s.get('avg_cadence') else '—'} spm",
            f"Stride: {s['avg_stride_length_cm']/100:.2f} m" if s.get('avg_stride_length_cm') else "Stride: —",
            f"GCT: {round(s['avg_ground_contact_time_ms'])} ms" if s.get('avg_ground_contact_time_ms') else "GCT: —",
            f"VertOsc: {s['avg_vertical_oscillation_cm']:.1f} cm" if s.get('avg_vertical_oscillation_cm') else "VertOsc: —",
            f"VertRatio: {s['avg_vertical_ratio']:.1f}%" if s.get('avg_vertical_ratio') else "VertRatio: —",
            f"Balance: {s['avg_ground_contact_balance']:.1f}%L" if s.get('avg_ground_contact_balance') else "Balance: —",
            f"SpeedLoss: {s['avg_step_speed_loss_pct']:.1f}%" if s.get('avg_step_speed_loss_pct') else "SpeedLoss: —",
            f"Stamina: {round(s['begin_potential_stamina'])}→{round(s['end_potential_stamina'])}" if s.get('begin_potential_stamina') and s.get('end_potential_stamina') else "Stamina: —",
            f"VO2max: {round(s['vo2_max'])}" if s.get('vo2_max') else "VO2max: —",
            f"Power: {round(s['avg_power_watts'])}W (NP {round(s['normalized_power_watts'])}W)" if s.get('avg_power_watts') and s.get('normalized_power_watts') else "",
            f"Load: {round(s['training_load'])}" if s.get('training_load') else "",
            f"Feel/RPE: {s.get('workout_feel')}/{s.get('workout_rpe')}" if s.get('workout_feel') else "",
            f"Compliance: {s.get('workout_compliance_score')}%" if s.get('workout_compliance_score') else "",
            f"Steps: {s.get('total_steps')}" if s.get('total_steps') else "",
            f"BodyBattery Δ: {s.get('body_battery_change'):+d}" if s.get('body_battery_change') else "",
            f"Fastest 1km: {format_duration(int(s['fastest_split_1k_s']))}" if s.get('fastest_split_1k_s') else "",
            f"TrainingEffect: {s.get('training_effect_label') or ''} (aerobic {s.get('training_effect_aerobic') or '—'})",
        ]
        lines.append("\n".join(p for p in parts if p))
        lines.append("")
    return "\n".join(lines)


@app.get("/running/analysis", response_class=HTMLResponse)
async def running_analysis(request: Request, runs: str = "10", refresh: str = "0"):
    import anthropic as _anthropic
    from datetime import datetime

    db = get_db()
    limit_clause = "" if runs == "all" else f"LIMIT {int(runs)}"
    sessions = rows_to_dicts(db.execute(
        f"""SELECT date, name, distance_meters, duration_seconds,
                   avg_speed_mps, avg_grade_adjusted_speed_mps,
                   avg_hr_bpm, max_hr_bpm,
                   avg_cadence, max_cadence,
                   avg_stride_length_cm, avg_ground_contact_time_ms,
                   avg_vertical_oscillation_cm, avg_vertical_ratio,
                   avg_ground_contact_balance, avg_step_speed_loss_pct,
                   begin_potential_stamina, end_potential_stamina,
                   vo2_max, avg_power_watts, normalized_power_watts,
                   training_load, training_effect_label, training_effect_aerobic,
                   workout_feel, workout_rpe, workout_compliance_score,
                   total_steps, body_battery_change,
                   fastest_split_1k_s, fastest_split_5k_s,
                   avg_respiration_rate, elevation_gain_m
            FROM cardio_sessions
            WHERE activity_type = 'running'
            ORDER BY date DESC {limit_clause}"""
    ).fetchall())

    if not sessions:
        db.close()
        return templates.TemplateResponse("running_analysis.html", {
            "request": request, "sessions": [], "analysis": None,
            "run_count": runs, "analysis_date": "", "cached": False,
            "format_pace": format_pace, "format_duration": format_duration,
        })

    latest_run_date = sessions[0]["date"]  # sessions ordered DESC so first = latest

    # --- Cache lookup ---
    cached_row = None
    if refresh != "1":
        cached_row = db.execute(
            """SELECT analysis_html, analysis_text, created_at, num_runs
               FROM running_analyses
               WHERE run_count = ? AND latest_run_date = ?
               ORDER BY created_at DESC LIMIT 1""",
            (runs, latest_run_date)
        ).fetchone()

    if cached_row:
        db.close()
        analysis_date = cached_row["created_at"][:10]
        return templates.TemplateResponse("running_analysis.html", {
            "request": request,
            "sessions": list(reversed(sessions)),
            "analysis": cached_row["analysis_html"],
            "run_count": runs,
            "analysis_date": analysis_date,
            "cached": True,
            "format_pace": format_pace,
            "format_duration": format_duration,
        })

    # --- Generate fresh analysis ---
    summary_text = _build_run_summary_for_ai(list(reversed(sessions)))
    prompt = f"""You are a running coach analyzing an athlete's Garmin data.

Here are their last {len(sessions)} runs (oldest to newest):

{summary_text}

Please provide a detailed analysis covering:

### 1. Overall Fitness Trend
Summarize pace, HR, VO₂ max, and fitness trajectory over time. Are they getting faster? Is aerobic efficiency improving (same pace at lower HR)?

### 2. Running Economy Assessment
Analyze cadence, stride length, ground contact time, vertical oscillation, and vertical ratio. Compare to optimal ranges:
- Cadence: 170–180 spm (elite) / 160–170 spm (recreational)
- GCT: <250ms elite, 250–300ms good, >300ms needs work
- Vertical ratio: <8% elite, 8–10% good, >10% inefficient
- Vertical oscillation: <8cm good, 8–10cm ok, >10cm excessive

### 3. Stamina & Fatigue Profile
Use the stamina start→end values and speed loss % to assess how well they hold pace through runs. Is fatigue a limiting factor?

### 4. Workout Compliance & Perceived Effort
Analyze feel/RPE/compliance scores. Are they training at the right intensity for each session type?

### 5. Key Strengths
What is this runner doing well?

### 6. Top 3 Actionable Improvements
Specific, prioritized recommendations with concrete targets.

Be specific with numbers. Keep it practical and motivating."""

    client = _anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw_analysis = message.content[0].text
    html_analysis = _format_analysis_md(raw_analysis)

    # --- Persist to cache ---
    db.execute(
        """INSERT OR REPLACE INTO running_analyses
               (run_count, latest_run_date, num_runs, analysis_html, analysis_text, model)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (runs, latest_run_date, len(sessions), html_analysis, raw_analysis, "claude-opus-4-6")
    )
    db.commit()
    db.close()

    return templates.TemplateResponse("running_analysis.html", {
        "request": request,
        "sessions": list(reversed(sessions)),
        "analysis": html_analysis,
        "run_count": runs,
        "analysis_date": datetime.now().strftime("%b %d, %Y"),
        "cached": False,
        "format_pace": format_pace,
        "format_duration": format_duration,
    })


@app.get("/gym/stats", response_class=HTMLResponse)
async def gym_stats(request: Request):
    from datetime import datetime, timedelta
    db = get_db()

    # ── Date range ──
    bounds = db.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM gym_sessions WHERE volume_lb > 0").fetchone()
    total_sessions = bounds[2] or 0
    date_range = f"{bounds[0]} → {bounds[1]}" if bounds[0] else "No data"

    # ── Radar data: sets per muscle group per period ──
    def muscle_sets(days):
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days < 9999 else "2000-01-01"
        rows = db.execute("""
            SELECT e.muscle_groups, COUNT(*) as sets
            FROM gym_exercises e
            JOIN gym_sets s ON s.exercise_id = e.id
            JOIN gym_sessions gs ON gs.id = e.session_id
            WHERE gs.date >= ? AND s.set_type = 'working'
            GROUP BY e.muscle_groups
        """, (since,)).fetchall()
        # Map to canonical muscle groups
        buckets = {"Chest": 0, "Back": 0, "Shoulders": 0, "Arms": 0, "Core": 0, "Legs": 0, "Traps": 0}
        MAP = {
            "chest": "Chest", "pec": "Chest",
            "lats": "Back", "back": "Back", "rhomboids": "Back",
            "shoulders": "Shoulders", "rear_delts": "Shoulders", "delts": "Shoulders",
            "biceps": "Arms", "triceps": "Arms", "forearms": "Arms",
            "abs": "Core", "core": "Core", "obliques": "Core", "hipflexors": "Core",
            "quads": "Legs", "glutes": "Legs", "hamstrings": "Legs", "calves": "Legs",
            "traps": "Traps",
        }
        for mg, sets in rows:
            for part in (mg or "").lower().replace(" ", "").split(","):
                for key, bucket in MAP.items():
                    if key in part:
                        buckets[bucket] += sets
                        break
        return buckets

    radar_data = {
        7: muscle_sets(7),
        30: muscle_sets(30),
        90: muscle_sets(90),
        365: muscle_sets(9999),
    }

    # ── Weekly volume (last 26 weeks) ──
    weekly_vol = rows_to_dicts(db.execute("""
        SELECT strftime('%Y-W%W', date) as week, ROUND(SUM(volume_lb)) as vol
        FROM gym_sessions WHERE volume_lb > 0
        AND date >= date('now', '-182 days')
        GROUP BY week ORDER BY week
    """).fetchall())

    # ── Weekly frequency (last 12 weeks) ──
    weekly_freq = rows_to_dicts(db.execute("""
        SELECT strftime('%Y-W%W', date) as week, COUNT(*) as count
        FROM gym_sessions WHERE volume_lb > 0
        AND date >= date('now', '-84 days')
        GROUP BY week ORDER BY week
    """).fetchall())

    # ── Muscle bar (last 30d) ──
    mb_rows = db.execute("""
        SELECT e.muscle_groups, COUNT(*) as sets
        FROM gym_exercises e
        JOIN gym_sets s ON s.exercise_id = e.id
        JOIN gym_sessions gs ON gs.id = e.session_id
        WHERE gs.date >= date('now', '-30 days') AND s.set_type = 'working'
        GROUP BY e.muscle_groups ORDER BY sets DESC LIMIT 12
    """).fetchall()
    # Canonicalize
    from collections import defaultdict
    mbuckets = defaultdict(int)
    MAP2 = {
        "chest": "Chest", "pec": "Chest",
        "lats": "Back / Lats", "back": "Back / Lats", "rhomboids": "Back / Lats",
        "shoulders": "Shoulders", "rear_delts": "Shoulders", "delts": "Shoulders",
        "biceps": "Biceps", "triceps": "Triceps", "forearms": "Forearms",
        "abs": "Core / Abs", "core": "Core / Abs", "obliques": "Core / Abs", "hipflexors": "Core / Abs",
        "quads": "Legs", "glutes": "Legs", "hamstrings": "Legs", "calves": "Legs",
        "traps": "Traps",
    }
    for mg, sets in mb_rows:
        for part in (mg or "").lower().replace(" ", "").split(","):
            matched = False
            for key, bucket in MAP2.items():
                if key in part:
                    mbuckets[bucket] += sets
                    matched = True
                    break
            if not matched and part:
                mbuckets[part.title()] += sets
    muscle_bar = [{"muscle": k, "sets": v} for k, v in sorted(mbuckets.items(), key=lambda x: -x[1])]

    # ── PRs ──
    prs = rows_to_dicts(db.execute("""
        SELECT e.name, MAX(s.weight_lb) as weight_lb, s.reps,
               gs.date
        FROM gym_exercises e
        JOIN gym_sets s ON s.exercise_id = e.id
        JOIN gym_sessions gs ON gs.id = e.session_id
        WHERE s.set_type = 'working' AND s.weight_lb > 0
        GROUP BY e.name
        ORDER BY weight_lb DESC LIMIT 15
    """).fetchall())

    # ── Heatmap data ──
    heatmap_rows = db.execute("""
        SELECT date, COUNT(*) as count
        FROM gym_sessions WHERE volume_lb > 0
        AND date >= date('now', '-370 days')
        GROUP BY date
    """).fetchall()
    heatmap_data = [{"date": r[0], "count": r[1]} for r in heatmap_rows]

    # ── KPIs ──
    kpi = db.execute("""
        SELECT AVG(duration_min) as avg_dur, AVG(volume_lb) as avg_vol, AVG(calories) as avg_cal
        FROM gym_sessions WHERE volume_lb > 0
    """).fetchone()

    # Longest streak
    dates = [r[0] for r in db.execute("SELECT DISTINCT date FROM gym_sessions WHERE volume_lb > 0 ORDER BY date").fetchall()]
    streak = cur_streak = 0
    prev = None
    for d in dates:
        cur = datetime.strptime(d, "%Y-%m-%d")
        if prev and (cur - prev).days == 1:
            cur_streak += 1
        else:
            cur_streak = 1
        streak = max(streak, cur_streak)
        prev = cur

    # Top muscle
    top_muscle = muscle_bar[0]["muscle"] if muscle_bar else "—"

    db.close()

    return templates.TemplateResponse("gym_stats.html", {
        "request": request,
        "total_sessions": total_sessions,
        "date_range": date_range,
        "radar_data": {str(k): v for k, v in radar_data.items()},
        "weekly_vol": weekly_vol,
        "weekly_freq": weekly_freq,
        "muscle_bar": muscle_bar,
        "prs": prs,
        "heatmap_data": heatmap_data,
        "avg_duration": round(kpi[0] or 0),
        "avg_volume": kpi[1] or 0,
        "avg_calories": round(kpi[2] or 0),
        "longest_streak": streak,
        "total_volume": sum(r["vol"] for r in weekly_vol) if weekly_vol else 0,
        "top_muscle": top_muscle,
    })


def _format_plan_md(text: str) -> str:
    import re
    text = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$",  r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Tables: | col | col |
    def convert_table(m):
        rows = [r.strip() for r in m.group(0).strip().split("\n") if r.strip()]
        html = "<table>"
        for i, row in enumerate(rows):
            if re.match(r"^\|[-| ]+\|$", row): continue
            cells = [c.strip() for c in row.strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            html += "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
        return html + "</table>"
    text = re.sub(r"(\|.+\|\n)+", convert_table, text)
    # Lists
    lines = text.split("\n")
    result, in_list = [], False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^[-*•] ", stripped):
            if not in_list: result.append("<ul>"); in_list = True
            result.append(f"<li>{stripped[2:]}</li>")
        elif re.match(r"^\d+\. ", stripped):
            if not in_list: result.append("<ol>"); in_list = True
            result.append(f"<li>{re.sub(r'^\d+\. ', '', stripped)}</li>")
        else:
            if in_list: result.append("</ul>" if not result[-2].startswith("<ol") else "</ol>"); in_list = False
            if stripped and not stripped.startswith("<"):
                result.append(f"<p>{line}</p>")
            elif stripped:
                result.append(line)
    if in_list: result.append("</ul>")
    return "\n".join(result)


@app.get("/today", response_class=HTMLResponse)
async def today_plan(request: Request, refresh: str = "0"):
    import anthropic as _anthropic
    from datetime import datetime, timedelta
    from collections import defaultdict

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    today_dow = now.strftime("%A")
    since7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    db = get_db()

    # ── Last 7d data ──
    gym_rows = rows_to_dicts(db.execute("""
        SELECT gs.date, gs.name, gs.duration_min, gs.volume_lb, gs.calories,
               GROUP_CONCAT(DISTINCT e.muscle_groups) as muscles,
               COUNT(s.id) as total_sets
        FROM gym_sessions gs
        LEFT JOIN gym_exercises e ON e.session_id = gs.id
        LEFT JOIN gym_sets s ON s.exercise_id = e.id AND s.set_type = 'working'
        WHERE gs.date >= ? AND gs.volume_lb > 0
        GROUP BY gs.id ORDER BY gs.date DESC
    """, (since7,)).fetchall())

    cardio_rows = rows_to_dicts(db.execute("""
        SELECT date, name, activity_type, distance_meters, duration_seconds,
               avg_hr_bpm, training_effect_label, training_load, calories,
               avg_speed_mps, normalized_power_watts, training_effect_aerobic
        FROM cardio_sessions WHERE date >= ? ORDER BY date DESC
    """, (since7,)).fetchall())

    # Top exercises for context (strength levels)
    top_lifts = rows_to_dicts(db.execute("""
        SELECT e.name, MAX(s.weight_lb) as max_w, e.muscle_groups
        FROM gym_exercises e JOIN gym_sets s ON s.exercise_id=e.id
        WHERE s.set_type='working' AND s.weight_lb > 0
        GROUP BY e.name ORDER BY max_w DESC LIMIT 12
    """).fetchall())

    # PR context for common exercises
    freq_exercises = rows_to_dicts(db.execute("""
        SELECT e.name, COUNT(*) as sessions, MAX(s.weight_lb) as max_w
        FROM gym_exercises e JOIN gym_sets s ON s.exercise_id=e.id
        JOIN gym_sessions gs ON gs.id=e.session_id
        WHERE s.set_type='working' AND s.weight_lb > 0
        AND gs.date >= date('now', '-60 days')
        GROUP BY e.name ORDER BY sessions DESC LIMIT 15
    """).fetchall())

    # Recovery if available
    recovery = rows_to_dicts(db.execute(
        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT 2"
    ).fetchall())

    db.close()

    # ── Cache key: today + data fingerprint ──
    cache_key = today_str
    db2 = get_db()
    cached_row = None
    if refresh != "1":
        cached_row = db2.execute(
            "SELECT analysis_html, created_at FROM running_analyses WHERE run_count='today' AND latest_run_date=? ORDER BY created_at DESC LIMIT 1",
            (cache_key,)
        ).fetchone()

    if cached_row:
        db2.close()
        return templates.TemplateResponse("today.html", {
            "request": request,
            "today_str": today_str, "today_dow": today_dow,
            "plan": cached_row["analysis_html"],
            "cached": True,
            "generated_at": cached_row["created_at"][:10],
            "gym_sessions_count": len(gym_rows),
            "gym_muscles_hit": _muscles_summary(gym_rows),
            "run_count": sum(1 for r in cardio_rows if r["activity_type"] == "running"),
            "run_km": round(sum((r.get("distance_meters") or 0)/1000 for r in cardio_rows if r["activity_type"] == "running"), 1),
            "ride_count": sum(1 for r in cardio_rows if r["activity_type"] == "cycling"),
            "ride_km": round(sum((r.get("distance_meters") or 0)/1000 for r in cardio_rows if r["activity_type"] == "cycling"), 1),
            "total_load": sum(r.get("training_load") or 0 for r in cardio_rows),
            "recent_gym": _enrich_gym(gym_rows[:5]),
            "recent_cardio": cardio_rows[:5],
        })

    # ── Build prompt ──
    gym_text = ""
    for r in gym_rows:
        gym_text += f"- {r['date']} ({r['name']}): {r['total_sets']} working sets, muscles: {r['muscles']}, volume: {r.get('volume_lb',0):,.0f} lb, {r.get('duration_min',0)} min\n"

    cardio_text = ""
    for r in cardio_rows:
        km = (r.get("distance_meters") or 0)/1000
        pace = format_pace(r.get("avg_speed_mps"))
        cardio_text += f"- {r['date']} ({r['activity_type']}): {r['name']}, {km:.1f} km, pace {pace}, HR {r.get('avg_hr_bpm','—')}, training load {r.get('training_load',0):.0f}, effect: {r.get('training_effect_label','—')}\n"

    lifts_text = ", ".join(f"{r['name']} ({r['max_w']} lb)" for r in top_lifts[:8])
    freq_text = ", ".join(f"{r['name']} x{r['sessions']} sessions" for r in freq_exercises[:8])

    rec_text = ""
    for r in recovery:
        rec_text += f"- {r['date']}: body battery {r.get('body_battery_wake','—')}, HRV {r.get('hrv_ms','—')}ms ({r.get('hrv_status','—')}), sleep {round((r.get('sleep_duration_min') or 0)/60,1)}h, resting HR {r.get('resting_hr','—')}\n"
    if not rec_text:
        rec_text = "No recovery data available.\n"

    prompt = f"""You are a personal trainer and running coach creating a workout plan.

Today is {today_dow}, {today_str}.

## Last 7 Days — Gym
{gym_text or "No gym sessions in last 7 days."}

## Last 7 Days — Cardio
{cardio_text or "No cardio in last 7 days."}

## Recovery Data
{rec_text}

## Athlete Strength Levels (recent PRs)
{lifts_text}

## Most Frequently Trained Exercises (last 60d)
{freq_text}

---

Based on this data, create a detailed workout plan for TODAY. Consider:
1. What muscle groups have been recently worked (need rest)
2. Training load from cardio (running load was {sum(r.get("training_load",0) for r in cardio_rows if r["activity_type"]=="running"):.0f} this week)
3. What the athlete should prioritize given gaps in their training
4. Recovery status

## Format your response exactly like this:

### Today's Focus
One sentence on the goal/theme for today.

### Readiness Assessment
2-3 sentences on recovery status and whether to go hard, moderate, or easy today.

### Gym Workout
A table with: Exercise | Sets × Reps | Weight (lb) | Rest | Notes
Include 5-8 exercises with specific weights based on their PR data. Include warm-up notes.

### Cardio
Specific cardio recommendation: type, duration, intensity (easy/moderate/hard), target HR zone or pace. Or "Rest/recovery walk" if warranted.

### Weekly Balance Check
One short paragraph on what's been neglected (e.g. legs, pulling movements) and what to prioritize later this week.

Be specific with numbers. Use their actual lift numbers, not generic advice."""

    client = _anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text
    html_plan = _format_plan_md(raw)

    # Cache it
    db2.execute(
        "INSERT OR REPLACE INTO running_analyses (run_count, latest_run_date, num_runs, analysis_html, analysis_text, model) VALUES (?,?,?,?,?,?)",
        ("today", cache_key, len(gym_rows) + len(cardio_rows), html_plan, raw, "claude-opus-4-6")
    )
    db2.commit()
    db2.close()

    return templates.TemplateResponse("today.html", {
        "request": request,
        "today_str": today_str, "today_dow": today_dow,
        "plan": html_plan,
        "cached": False,
        "generated_at": today_str,
        "gym_sessions_count": len(gym_rows),
        "gym_muscles_hit": _muscles_summary(gym_rows),
        "run_count": sum(1 for r in cardio_rows if r["activity_type"] == "running"),
        "run_km": round(sum((r.get("distance_meters") or 0)/1000 for r in cardio_rows if r["activity_type"] == "running"), 1),
        "ride_count": sum(1 for r in cardio_rows if r["activity_type"] == "cycling"),
        "ride_km": round(sum((r.get("distance_meters") or 0)/1000 for r in cardio_rows if r["activity_type"] == "cycling"), 1),
        "total_load": sum(r.get("training_load") or 0 for r in cardio_rows),
        "recent_gym": _enrich_gym(gym_rows[:5]),
        "recent_cardio": cardio_rows[:5],
    })


def _muscles_summary(gym_rows):
    """Clean comma list of muscle groups hit."""
    muscles = set()
    for r in gym_rows:
        for m in (r.get("muscles") or "").lower().replace(" ", "").split(","):
            m = m.strip()
            MAP = {"chest":"Chest","lats":"Back","back":"Back","shoulders":"Shoulders","shoulder":"Shoulders",
                   "biceps":"Arms","triceps":"Arms","abs":"Core","core":"Core","quads":"Legs","glutes":"Legs",
                   "traps":"Traps","rhomboids":"Back","rear_delts":"Shoulders","hipflexors":"Core","forearms":"Arms"}
            for k, v in MAP.items():
                if k in m:
                    muscles.add(v)
                    break
    return ", ".join(sorted(muscles)) or "—"


def _enrich_gym(rows):
    for r in rows:
        r["muscles_clean"] = _muscles_summary([r])
    return rows



# Garmin exercise name → (category, exerciseName) for strength workout uploads
_GARMIN_EXERCISE_MAP: dict[str, tuple[str, str]] = {
    # Squats
    "squat": ("SQUAT", "SQUAT"),
    "back squat": ("SQUAT", "BARBELL_BACK_SQUAT"),
    "barbell squat": ("SQUAT", "BARBELL_BACK_SQUAT"),
    "barbell back squat": ("SQUAT", "BARBELL_BACK_SQUAT"),
    "front squat": ("SQUAT", "BARBELL_FRONT_SQUAT"),
    "goblet squat": ("SQUAT", "GOBLET_SQUAT"),
    "leg press": ("LEG_PRESS", "LEG_PRESS"),
    # Deadlifts
    "deadlift": ("DEADLIFT", "BARBELL_DEADLIFT"),
    "barbell deadlift": ("DEADLIFT", "BARBELL_DEADLIFT"),
    "romanian deadlift": ("DEADLIFT", "ROMANIAN_DEADLIFT"),
    "rdl": ("DEADLIFT", "ROMANIAN_DEADLIFT"),
    "sumo deadlift": ("DEADLIFT", "SUMO_DEADLIFT"),
    # Bench press
    "bench press": ("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
    "barbell bench press": ("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
    "incline bench press": ("BENCH_PRESS", "BARBELL_INCLINE_BENCH_PRESS"),
    "incline dumbbell press": ("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS"),
    "dumbbell bench press": ("BENCH_PRESS", "DUMBBELL_BENCH_PRESS"),
    "dumbbell press": ("BENCH_PRESS", "DUMBBELL_BENCH_PRESS"),
    # Rows
    "row": ("ROW", "BARBELL_ROW"),
    "barbell row": ("ROW", "BARBELL_ROW"),
    "bent-over row": ("ROW", "BARBELL_ROW"),
    "bent over row": ("ROW", "BARBELL_ROW"),
    "dumbbell row": ("ROW", "DUMBBELL_ROW"),
    "seated cable row": ("ROW", "SEATED_CABLE_ROW"),
    "cable row": ("ROW", "SEATED_CABLE_ROW"),
    "face pull": ("ROW", "FACE_PULL"),
    # Pull-ups / Lat pulldown
    "pull-up": ("PULL_UP", "PULL_UP"),
    "pull up": ("PULL_UP", "PULL_UP"),
    "pullup": ("PULL_UP", "PULL_UP"),
    "chin-up": ("PULL_UP", "CHIN_UP"),
    "chin up": ("PULL_UP", "CHIN_UP"),
    "lat pulldown": ("PULL_UP", "WIDE_GRIP_LAT_PULLDOWN"),
    "lat pull-down": ("PULL_UP", "WIDE_GRIP_LAT_PULLDOWN"),
    "wide grip lat pulldown": ("PULL_UP", "WIDE_GRIP_LAT_PULLDOWN"),
    "close grip lat pulldown": ("PULL_UP", "CLOSE_GRIP_LAT_PULLDOWN"),
    # Shoulder press
    "shoulder press": ("SHOULDER_PRESS", "DUMBBELL_SHOULDER_PRESS"),
    "overhead press": ("SHOULDER_PRESS", "BARBELL_OVERHEAD_PRESS"),
    "ohp": ("SHOULDER_PRESS", "BARBELL_OVERHEAD_PRESS"),
    "military press": ("SHOULDER_PRESS", "BARBELL_OVERHEAD_PRESS"),
    "dumbbell shoulder press": ("SHOULDER_PRESS", "DUMBBELL_SHOULDER_PRESS"),
    "arnold press": ("SHOULDER_PRESS", "ARNOLD_PRESS"),
    # Lateral raise
    "lateral raise": ("LATERAL_RAISE", "DUMBBELL_LATERAL_RAISE"),
    "dumbbell lateral raise": ("LATERAL_RAISE", "DUMBBELL_LATERAL_RAISE"),
    # Curls
    "bicep curl": ("CURL", "BARBELL_CURL"),
    "bicep curls": ("CURL", "BARBELL_CURL"),
    "barbell curl": ("CURL", "BARBELL_CURL"),
    "dumbbell curl": ("CURL", "DUMBBELL_CURL"),
    "hammer curl": ("CURL", "HAMMER_CURL"),
    "preacher curl": ("CURL", "BARBELL_PREACHER_CURL"),
    # Triceps
    "tricep pushdown": ("TRICEP_EXTENSION", "TRICEPS_PUSHDOWN"),
    "triceps pushdown": ("TRICEP_EXTENSION", "TRICEPS_PUSHDOWN"),
    "tricep extension": ("TRICEP_EXTENSION", "TRICEPS_EXTENSION"),
    "skull crusher": ("TRICEP_EXTENSION", "LYING_BARBELL_TRICEPS_EXTENSION"),
    "close grip bench press": ("BENCH_PRESS", "CLOSE_GRIP_BARBELL_BENCH_PRESS"),
    # Lunges
    "lunge": ("LUNGE", "WALKING_LUNGE"),
    "walking lunge": ("LUNGE", "WALKING_LUNGE"),
    "reverse lunge": ("LUNGE", "REVERSE_LUNGE"),
    "split squat": ("LUNGE", "SPLIT_SQUAT"),
    "bulgarian split squat": ("LUNGE", "BULGARIAN_SPLIT_SQUAT"),
    # Core
    "plank": ("PLANK", "PLANK"),
    "crunch": ("CRUNCH", "CRUNCH"),
    "sit-up": ("CRUNCH", "SIT_UP"),
    "sit up": ("CRUNCH", "SIT_UP"),
    "leg raise": ("LEG_RAISE", "HANGING_LEG_RAISE"),
    # Calf
    "calf raise": ("CALF_RAISE", "STANDING_CALF_RAISE"),
    "standing calf raise": ("CALF_RAISE", "STANDING_CALF_RAISE"),
    # Push / other
    "push-up": ("PUSH_UP", "PUSH_UP"),
    "push up": ("PUSH_UP", "PUSH_UP"),
    "dip": ("PUSH_UP", "TRICEP_DIP"),
    "cable flye": ("FLYE", "CABLE_CROSSOVER"),
    "cable crossover": ("FLYE", "CABLE_CROSSOVER"),
    "chest flye": ("FLYE", "DUMBBELL_FLYE"),
    "hip thrust": ("HIP_RAISE", "BARBELL_HIP_THRUST"),
    "glute bridge": ("HIP_RAISE", "GLUTE_BRIDGE"),
    "leg curl": ("LEG_CURL", "LYING_LEG_CURL"),
    "leg extension": ("LEG_EXTENSION", "LEG_EXTENSION"),
}


def _get_garmin_exercise(name: str) -> tuple[str | None, str | None]:
    """Map an exercise name to Garmin (category, exerciseName). Case-insensitive, fuzzy."""
    key = name.lower().strip("* ").strip()
    # Exact match
    if key in _GARMIN_EXERCISE_MAP:
        return _GARMIN_EXERCISE_MAP[key]
    # Partial match — find the first entry whose key is contained in the name
    for map_key, val in _GARMIN_EXERCISE_MAP.items():
        if map_key in key or key in map_key:
            return val
    return None, None

def _parse_plan_to_garmin_workout(plan_text: str, workout_name: str) -> dict:
    """Parse AI plan markdown table into a Garmin workout JSON."""
    import re

    steps = []
    step_order = 1

    # Find table rows (skip header and separator)
    table_pattern = re.compile(r"^\|(.+)\|$", re.MULTILINE)
    rows = table_pattern.findall(plan_text)

    exercise_rows = []
    for row in rows:
        cells = [c.strip() for c in row.split("|")]
        # Skip header row (contains "Exercise" or "Sets") and separator rows
        if not cells or cells[0].lower() in ("exercise", "") or re.match(r"^[-:]+$", cells[0]):
            continue
        if cells[0].lower() == "exercise":
            continue
        if all(c.replace("-","").replace(":","").replace(" ","") == "" for c in cells):
            continue
        exercise_rows.append(cells)

    for cells in exercise_rows:
        if len(cells) < 2:
            continue

        exercise_name = cells[0].strip("* ").strip()
        sets_reps_raw = cells[1] if len(cells) > 1 else ""
        weight_raw    = cells[2] if len(cells) > 2 else ""
        rest_raw      = cells[3] if len(cells) > 3 else ""
        notes         = cells[4] if len(cells) > 4 else ""

        # Parse sets and reps: "4 × 6", "3 × 10 each leg", "2 × 45 sec"
        sets_match = re.search(r"(\d+)\s*[×x×]\s*(\d+)", sets_reps_raw)
        if not sets_match:
            # Try "2 × 45 sec" or similar
            sets_match = re.search(r"(\d+)", sets_reps_raw)
            num_sets = int(sets_match.group(1)) if sets_match else 3
            num_reps = 10
        else:
            num_sets = int(sets_match.group(1))
            num_reps = int(sets_match.group(2))

        # Parse weight (take lower bound for ranges like "100–110")
        weight_match = re.search(r"(\d+(?:\.\d+)?)", weight_raw)
        weight_lb = float(weight_match.group(1)) if weight_match else None
        weight_kg = round(weight_lb * 0.453592, 1) if weight_lb else None

        # Parse rest: "2:30" → 150s, "1:30" → 90s, "1:00" → 60s
        rest_s = 90  # default
        rest_match = re.search(r"(\d+):(\d{2})", rest_raw)
        if rest_match:
            rest_s = int(rest_match.group(1)) * 60 + int(rest_match.group(2))
        else:
            rest_num = re.search(r"(\d+)", rest_raw)
            if rest_num:
                rest_s = int(rest_num.group(1)) * 60

        # Build RepeatGroup + ExecutableStep + RestStep
        child_steps = []
        child_id = 1

        # Work step
        ex_category, ex_name = _get_garmin_exercise(exercise_name)
        work_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": step_order + 1,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "childStepId": child_id,
            "description": f"{notes[:100]}" if notes else None,
            "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
            "endConditionValue": float(num_reps),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            "category": ex_category,
            "exerciseName": ex_name,
        }
        # Add weight if available
        if weight_kg:
            work_step["weightValue"] = weight_kg
            work_step["weightUnit"] = {"unitKey": "kilogram"}
        child_steps.append(work_step)
        child_id += 1

        # Rest step
        if rest_s > 0:
            rest_step = {
                "type": "ExecutableStepDTO",
                "stepOrder": step_order + 2,
                "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                "childStepId": child_id,
                "description": None,
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(rest_s),
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            }
            child_steps.append(rest_step)
            child_id += 1

        repeat_group = {
            "type": "RepeatGroupDTO",
            "stepOrder": step_order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "childStepId": 1,
            "numberOfIterations": num_sets,
            "workoutSteps": child_steps,
        }
        steps.append(repeat_group)
        step_order += len(child_steps) + 1

    workout = {
        "workoutName": workout_name,
        "description": f"AI-generated workout plan from FitTrack",
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
            "workoutSteps": steps,
        }]
    }
    return workout


@app.post("/today/sync-garmin")
async def sync_to_garmin(request: Request):
    """Parse today's AI plan and upload it as a Garmin workout."""
    import os
    from garminconnect import Garmin
    from datetime import datetime

    db = get_db()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Get today's plan
    row = db.execute(
        "SELECT analysis_text FROM running_analyses WHERE run_count='today' AND latest_run_date=? ORDER BY created_at DESC LIMIT 1",
        (today_str,)
    ).fetchone()
    db.close()

    if not row:
        return {"success": False, "error": "No plan found for today. Generate a plan first."}

    plan_text = row["analysis_text"]
    workout_name = f"FitTrack Plan · {today_str}"

    # Parse plan into workout
    workout_json = _parse_plan_to_garmin_workout(plan_text, workout_name)
    num_exercises = len(workout_json["workoutSegments"][0]["workoutSteps"])

    if num_exercises == 0:
        return {"success": False, "error": "Could not parse any exercises from the plan. The plan may not contain a workout table."}

    # Upload to Garmin
    try:
        g = Garmin()
        g.garth.load(os.path.expanduser("~/.garminconnect"))
        try:
            profile = g.garth.profile
            if profile and profile.get("displayName"):
                g.display_name = profile["displayName"]
        except Exception:
            pass
        result = g.upload_workout(workout_json)
        workout_id = result.get("workoutId")
        return {
            "success": True,
            "workoutId": workout_id,
            "workoutName": workout_name,
            "exercises": num_exercises,
            "message": f"Uploaded {num_exercises} exercises to Garmin Connect. Open the Garmin Connect app → Workouts to find it.",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/sync/garmin")
async def sync_garmin():
    """Run Garmin sync: import new activities and daily metrics."""
    import sys
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    from garmin_sync import run_sync

    try:
        result = run_sync(str(DB_PATH))
    except Exception as e:
        result = {"synced_activities": 0, "synced_daily": 0, "errors": [str(e)]}
    return result


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
