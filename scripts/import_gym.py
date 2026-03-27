#!/usr/bin/env python3
"""
Import a gym workout from Motra text format into the fitness SQLite database.

Usage:
    python import_gym.py workout.txt
    cat workout.txt | python import_gym.py
    python import_gym.py --json workout.json   # JSON format (from workouts.json)

Motra text format example (old format):
    Thursday Evening Upper Body and Core Machine Focus
    Date: 2026-03-19
    Time: 19:31
    Duration: 52 min
    Volume: 9500 lb
    Calories: 216
    Source: https://motra.com/share/workout/45a8ee2953

    1. Machine Rear Delt (Reverse) Fly [rear_delts, shoulders] (machine)
       Warmup: 10 x 50 lb
       Set 1: 10 x 60 lb
       Set 2: 10 x 70 lb
       Set 3: 10 x 70 lb

    2. Barbell Upright Row [traps, shoulders] (barbell)
       Set 1: 10 x 40 lb
       ...

Motra text format example (new format):
    Thursday Evening Cable Back and Arms with Machine Rows
    Mar 26, 2026 at 20:02

    Duration: 41m
    Volume: 7.4K lb
    Calories: 183 cal
    Exercises: 4

    Cable Lat Pull Down Wide-Grip
    1: 10 reps x 70 lb
    2: 8 reps x 85 lb
    3: 8 reps x 100 lb

    Machine Row
    1: 10 reps x 60 lb
    2: 10 reps x 70 lb

    Tracked with Motra.
    https://motra.com/share/workout/815fe1fef5

Idempotent: skips duplicate sessions by (date, name).
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from db import init_db


def infer_muscle_groups(exercise_name: str) -> list[str]:
    """Infer muscle groups from exercise name."""
    name_lower = exercise_name.lower()

    if "lat pull" in name_lower or "pull-up" in name_lower or "pullup" in name_lower:
        return ["lats", "biceps"]
    elif "row" in name_lower and "upright" not in name_lower:
        return ["lats", "rhomboids", "biceps"]
    elif "tricep" in name_lower:
        return ["triceps"]
    elif "bicep curl" in name_lower or "biceps curl" in name_lower:
        return ["biceps"]
    elif "rear delt" in name_lower:
        return ["rear_delts", "shoulders"]
    elif "upright row" in name_lower:
        return ["traps", "shoulders"]
    elif "dip" in name_lower and "tricep" not in name_lower:
        return ["chest", "triceps"]
    elif "ab crunch" in name_lower or "crunch" in name_lower:
        return ["core", "abs"]
    else:
        return []


def infer_equipment(exercise_name: str) -> str:
    """Infer equipment from exercise name."""
    name_lower = exercise_name.lower()

    if "cable" in name_lower:
        return "cable"
    elif "machine" in name_lower or "assisted" in name_lower:
        return "machine"
    elif "barbell" in name_lower:
        return "barbell"
    elif "dumbbell" in name_lower:
        return "dumbbell"
    else:
        return ""


def parse_motra_text(text: str) -> dict:
    """Parse Motra-style workout text into a structured dict.

    Supports two formats:
    1. Old format: Date/Time fields, numbered exercises "1. Exercise Name [muscles] (equipment)"
    2. New format: "Mar 26, 2026 at 20:02", plain exercise names, "1: 10 reps x 70 lb" sets
    """
    lines = [l.rstrip() for l in text.strip().splitlines()]
    if not lines:
        raise ValueError("Empty workout text")

    session = {"exercises": []}

    # First non-empty line is the session name
    session["name"] = lines[0].strip()

    # Detect format by checking line 2
    # New format: "Mar 26, 2026 at 20:02"
    # Old format: "Date: 2026-03-19" or blank
    is_new_format = False
    if len(lines) > 1:
        date_match = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2})", lines[1])
        if date_match:
            is_new_format = True
            # Parse date and time: "Mar 26, 2026 at 20:02"
            dt = datetime.strptime(lines[1].strip(), "%b %d, %Y at %H:%M")
            session["date"] = dt.strftime("%Y-%m-%d")
            session["time"] = dt.strftime("%H:%M")

    # Parse header fields
    i = 2 if is_new_format else 1
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Skip footer lines
        if line.startswith("Tracked with") or line.startswith("http"):
            if line.startswith("http"):
                session["source_url"] = line
            i += 1
            continue

        # Old format: "Date: 2026-03-19"
        m = re.match(r"Date:\s*(.+)", line, re.I)
        if m:
            session["date"] = m.group(1).strip()
            i += 1
            continue

        # Old format: "Time: 19:31"
        m = re.match(r"Time:\s*(.+)", line, re.I)
        if m:
            session["time"] = m.group(1).strip()
            i += 1
            continue

        # Duration: "41m" or "Duration: 52 min"
        m = re.match(r"Duration:\s*(\d+)\s*m(?:in)?", line, re.I)
        if m:
            session["duration_min"] = int(m.group(1))
            i += 1
            continue

        # Volume: "7.4K lb" or "Volume: 9500 lb"
        m = re.match(r"Volume:\s*([\d.]+)([KMk]?)\s*lb", line, re.I)
        if m:
            volume = float(m.group(1))
            suffix = m.group(2).upper() if m.group(2) else ""
            if suffix == "K":
                volume *= 1000
            elif suffix == "M":
                volume *= 1000000
            session["volume_lb"] = volume
            i += 1
            continue

        # Calories: "183 cal" or "Calories: 216"
        m = re.match(r"Calories:\s*(\d+)", line, re.I)
        if m:
            session["calories"] = int(m.group(1))
            i += 1
            continue

        # Exercises count (new format only, skip it)
        m = re.match(r"Exercises:\s*(\d+)", line, re.I)
        if m:
            i += 1
            continue

        # Old format: "Source: https://..."
        m = re.match(r"Source:\s*(.+)", line, re.I)
        if m:
            session["source_url"] = m.group(1).strip()
            i += 1
            continue

        # Notes
        m = re.match(r"Notes?:\s*(.+)", line, re.I)
        if m:
            session["notes"] = m.group(1).strip()
            i += 1
            continue

        # Old format exercise line: "1. Exercise Name [muscles] (equipment)"
        m = re.match(
            r"\d+\.\s+(.+?)(?:\s*\[([^\]]+)\])?\s*(?:\(([^)]+)\))?\s*$", line
        )
        if m:
            exercise = {
                "name": m.group(1).strip(),
                "muscle_groups": m.group(2).strip() if m.group(2) else "",
                "equipment": m.group(3).strip() if m.group(3) else "",
                "sets": [],
            }

            # Infer muscle groups and equipment if not provided
            if not exercise["muscle_groups"]:
                inferred = infer_muscle_groups(exercise["name"])
                exercise["muscle_groups"] = ",".join(inferred) if inferred else ""
            if not exercise["equipment"]:
                exercise["equipment"] = infer_equipment(exercise["name"])

            # Check for superset annotation
            sm = re.match(r"Superset(?:\s+with)?:\s*(.+)", lines[i + 1].strip(), re.I) if i + 1 < len(lines) else None
            if sm:
                exercise["superset_group"] = sm.group(1).strip()
                i += 1

            session["exercises"].append(exercise)
            i += 1
            continue

        # New format set line: "1: 10 reps x 70 lb" or "Warm Up: 10 reps x 70 lb"
        set_match = re.match(
            r"\s*(?:(Warm\s*Up|\d+)):\s*(\d+)\s*reps?\s*x\s*([\d.]+)\s*lb",
            line,
            re.I,
        )
        if set_match and session["exercises"]:
            set_label = set_match.group(1).strip()
            if re.match(r"\d+", set_label):
                set_type = "working"
            elif set_label.lower().replace(" ", "") == "warmup":
                set_type = "warmup"
            else:
                set_type = "working"

            s = {
                "set_type": set_type,
                "reps": int(set_match.group(2)),
                "weight_lb": float(set_match.group(3)),
            }

            session["exercises"][-1]["sets"].append(s)
            i += 1
            continue

        # Old format set line: "Set 1: 10 x 60 lb" or "Warmup: 10 x 50 lb"
        m = re.match(
            r"\s*(?:(Warmup|Dropset|Failure|Set\s*\d+)):\s*(\d+)\s*(?:x|×)\s*([\d.]+)\s*lb"
            r"(?:\s*\(assist\s*([\d.]+)\s*lb\))?",
            line,
            re.I,
        )
        if m and session["exercises"]:
            set_label = m.group(1).strip().lower()
            if set_label.startswith("set"):
                set_type = "working"
            elif set_label == "warmup":
                set_type = "warmup"
            elif set_label == "dropset":
                set_type = "dropset"
            elif set_label == "failure":
                set_type = "failure"
            else:
                set_type = "working"

            s = {
                "set_type": set_type,
                "reps": int(m.group(2)),
                "weight_lb": float(m.group(3)),
            }
            if m.group(4):
                s["assist_lb"] = float(m.group(4))

            session["exercises"][-1]["sets"].append(s)
            i += 1
            continue

        # New format: Exercise name (not a set, not a header field)
        # This must be checked last, after all other patterns
        if is_new_format and not re.match(r"\d+:", line):
            exercise = {
                "name": line,
                "muscle_groups": "",
                "equipment": "",
                "sets": [],
            }

            # Infer muscle groups and equipment
            inferred_muscles = infer_muscle_groups(exercise["name"])
            exercise["muscle_groups"] = ",".join(inferred_muscles) if inferred_muscles else ""
            exercise["equipment"] = infer_equipment(exercise["name"])

            session["exercises"].append(exercise)
            i += 1
            continue

        i += 1

    return session


def insert_session(conn: sqlite3.Connection, session: dict) -> int | None:
    """Insert a gym session. Returns session_id or None if duplicate."""
    date = session.get("date")
    name = session.get("name")
    if not date or not name:
        raise ValueError("Session must have 'date' and 'name'")

    # Check for duplicate
    existing = conn.execute(
        "SELECT id FROM gym_sessions WHERE date = ? AND name = ?", (date, name)
    ).fetchone()
    if existing:
        return None

    cur = conn.execute(
        """INSERT INTO gym_sessions (date, time, name, duration_min, volume_lb, calories, source_url, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date,
            session.get("time"),
            name,
            session.get("duration_min"),
            session.get("volume_lb"),
            session.get("calories"),
            session.get("source_url"),
            session.get("notes"),
        ),
    )
    session_id = cur.lastrowid

    for order, ex in enumerate(session.get("exercises", []), 1):
        muscle_groups = ex.get("muscle_groups", "")
        if isinstance(muscle_groups, list):
            muscle_groups = ",".join(muscle_groups)

        cur_ex = conn.execute(
            """INSERT INTO gym_exercises (session_id, exercise_order, name, muscle_groups, equipment, superset_group, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                order,
                ex["name"],
                muscle_groups,
                ex.get("equipment"),
                ex.get("superset_group"),
                ex.get("notes") or ex.get("note"),
            ),
        )
        exercise_id = cur_ex.lastrowid

        for set_num, s in enumerate(ex.get("sets", []), 1):
            set_type = s.get("type", s.get("set_type", "working"))
            conn.execute(
                """INSERT INTO gym_sets (exercise_id, set_number, set_type, reps, weight_lb, assist_lb, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    exercise_id,
                    set_num,
                    set_type,
                    s.get("reps"),
                    s.get("weight_lb"),
                    s.get("assist_lb"),
                    s.get("notes"),
                ),
            )

    conn.commit()
    return session_id


def main():
    parser = argparse.ArgumentParser(description="Import gym workout into fitness DB")
    parser.add_argument("file", nargs="?", help="Workout file (text or JSON with --json)")
    parser.add_argument("--json", action="store_true", help="Input is JSON format")
    parser.add_argument("--db", default=None, help="Database path (default: fitness.db)")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text()
    else:
        text = sys.stdin.read()

    conn = init_db(args.db)

    if args.json:
        data = json.loads(text)
        # Support both single session and {sessions: [...]} format
        sessions = data.get("sessions", [data]) if isinstance(data, dict) else [data]
    else:
        sessions = [parse_motra_text(text)]

    inserted = 0
    skipped = 0
    for session in sessions:
        # Normalize JSON format fields
        if "source" in session and "source_url" not in session:
            session["source_url"] = session["source"]
        if "muscle_group" in session.get("exercises", [{}])[0] if session.get("exercises") else False:
            for ex in session["exercises"]:
                if "muscle_group" in ex and "muscle_groups" not in ex:
                    ex["muscle_groups"] = ex["muscle_group"]

        sid = insert_session(conn, session)
        if sid is not None:
            n_ex = len(session.get("exercises", []))
            n_sets = sum(len(e.get("sets", [])) for e in session.get("exercises", []))
            print(f"  Inserted session '{session['name']}' (id={sid}): {n_ex} exercises, {n_sets} sets")
            inserted += 1
        else:
            print(f"  Skipped duplicate: '{session['name']}' on {session['date']}")
            skipped += 1

    print(f"\nDone: {inserted} inserted, {skipped} skipped")
    conn.close()


if __name__ == "__main__":
    main()
