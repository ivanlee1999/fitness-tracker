#!/usr/bin/env python3
"""
One-shot migration: reads /home/ivan/.openclaw/workspace/memory/workouts.json
and imports existing gym sessions into the fitness SQLite database.

Usage:
    python migrate.py
    python migrate.py --source /path/to/workouts.json
    python migrate.py --db /path/to/fitness.db
"""

import argparse
import json
import sys
from pathlib import Path

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import init_db
from import_gym import insert_session

DEFAULT_SOURCE = Path.home() / ".openclaw" / "workspace" / "memory" / "workouts.json"


def migrate_workouts(source_path: Path, db_path=None):
    """Migrate workouts.json into the fitness database."""
    if not source_path.exists():
        print(f"Source file not found: {source_path}")
        sys.exit(1)

    data = json.loads(source_path.read_text())
    sessions = data.get("sessions", [])

    if not sessions:
        print("No sessions found in source file.")
        return

    print(f"Found {len(sessions)} session(s) in {source_path}")
    print(f"Schema version: {data.get('schema_version', 'unknown')}")
    print()

    conn = init_db(db_path)

    inserted = 0
    skipped = 0

    for raw_session in sessions:
        # Normalize from workouts.json format to import_gym format
        session = {
            "date": raw_session.get("date"),
            "time": raw_session.get("time"),
            "name": raw_session.get("name"),
            "duration_min": raw_session.get("duration_min"),
            "volume_lb": raw_session.get("volume_lb"),
            "calories": raw_session.get("calories"),
            "source_url": raw_session.get("source"),
            "notes": raw_session.get("notes"),
            "exercises": [],
        }

        # Build superset mapping
        superset_map = {}
        for ex in raw_session.get("exercises", []):
            if ex.get("superset_with"):
                pair_name = ex["superset_with"]
                # Create a group name from the pair
                group = tuple(sorted([ex["name"], pair_name]))
                superset_map[ex["name"]] = f"SS-{hash(group) % 10000}"
                superset_map[pair_name] = f"SS-{hash(group) % 10000}"

        for ex in raw_session.get("exercises", []):
            muscle_groups = ex.get("muscle_group", ex.get("muscle_groups", []))
            if isinstance(muscle_groups, list):
                muscle_groups = ",".join(muscle_groups)

            exercise = {
                "name": ex["name"],
                "muscle_groups": muscle_groups,
                "equipment": ex.get("equipment", ""),
                "superset_group": superset_map.get(ex["name"]),
                "note": ex.get("note"),
                "sets": [],
            }

            for s in ex.get("sets", []):
                set_data = {
                    "type": s.get("type", "working"),
                    "reps": s.get("reps"),
                    "weight_lb": abs(s.get("weight_lb", 0)),
                    "assist_lb": s.get("assist_lb"),
                }
                exercise["sets"].append(set_data)

            session["exercises"].append(exercise)

        sid = insert_session(conn, session)
        if sid is not None:
            n_ex = len(session["exercises"])
            n_sets = sum(len(e["sets"]) for e in session["exercises"])
            print(f"  Imported: '{session['name']}' ({session['date']})")
            print(f"           {n_ex} exercises, {n_sets} sets, {session.get('volume_lb', 0)} lb volume")
            inserted += 1
        else:
            print(f"  Skipped (duplicate): '{session['name']}' ({session['date']})")
            skipped += 1

    # Verify data
    print("\n--- Verification ---")
    row = conn.execute("SELECT COUNT(*) as cnt FROM gym_sessions").fetchone()
    print(f"  Total gym sessions in DB: {row['cnt']}")
    row = conn.execute("SELECT COUNT(*) as cnt FROM gym_exercises").fetchone()
    print(f"  Total exercises in DB:    {row['cnt']}")
    row = conn.execute("SELECT COUNT(*) as cnt FROM gym_sets").fetchone()
    print(f"  Total sets in DB:         {row['cnt']}")

    # Show exercise progression view
    print("\n--- Exercise Progression (from view) ---")
    rows = conn.execute(
        "SELECT date, exercise_name, top_set_weight_lb, top_set_reps FROM exercise_progression ORDER BY date, exercise_name"
    ).fetchall()
    for r in rows:
        print(f"  {r['date']} | {r['exercise_name']:40s} | {r['top_set_weight_lb']:>6.0f} lb x {r['top_set_reps']} reps")

    print(f"\nMigration complete: {inserted} inserted, {skipped} skipped")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate workouts.json to fitness DB")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Source JSON file")
    parser.add_argument("--db", default=None, help="Database path")
    args = parser.parse_args()

    migrate_workouts(Path(args.source), args.db)


if __name__ == "__main__":
    main()
