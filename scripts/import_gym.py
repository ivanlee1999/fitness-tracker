#!/usr/bin/env python3
"""
Import a gym workout from Motra text format into the fitness SQLite database.

Usage:
    python import_gym.py workout.txt
    cat workout.txt | python import_gym.py
    python import_gym.py --json workout.json   # JSON format (from workouts.json)

Motra text format example:
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

Idempotent: skips duplicate sessions by (date, name).
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from db import init_db


def parse_motra_text(text: str) -> dict:
    """Parse Motra-style workout text into a structured dict."""
    lines = [l.rstrip() for l in text.strip().splitlines()]
    if not lines:
        raise ValueError("Empty workout text")

    session = {"exercises": []}

    # First non-empty line is the session name
    session["name"] = lines[0].strip()

    # Parse header fields
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        m = re.match(r"Date:\s*(.+)", line, re.I)
        if m:
            session["date"] = m.group(1).strip()
            i += 1
            continue

        m = re.match(r"Time:\s*(.+)", line, re.I)
        if m:
            session["time"] = m.group(1).strip()
            i += 1
            continue

        m = re.match(r"Duration:\s*(\d+)", line, re.I)
        if m:
            session["duration_min"] = int(m.group(1))
            i += 1
            continue

        m = re.match(r"Volume:\s*([\d,.]+)", line, re.I)
        if m:
            session["volume_lb"] = float(m.group(1).replace(",", ""))
            i += 1
            continue

        m = re.match(r"Calories:\s*(\d+)", line, re.I)
        if m:
            session["calories"] = int(m.group(1))
            i += 1
            continue

        m = re.match(r"Source:\s*(.+)", line, re.I)
        if m:
            session["source_url"] = m.group(1).strip()
            i += 1
            continue

        m = re.match(r"Notes?:\s*(.+)", line, re.I)
        if m:
            session["notes"] = m.group(1).strip()
            i += 1
            continue

        # Exercise line: "1. Exercise Name [muscles] (equipment)"
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

            # Check for superset annotation
            sm = re.match(r"Superset(?:\s+with)?:\s*(.+)", lines[i + 1].strip(), re.I) if i + 1 < len(lines) else None
            if sm:
                exercise["superset_group"] = sm.group(1).strip()
                i += 1

            session["exercises"].append(exercise)
            i += 1
            continue

        # Set line: "Set 1: 10 x 60 lb" or "Warmup: 10 x 50 lb"
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
