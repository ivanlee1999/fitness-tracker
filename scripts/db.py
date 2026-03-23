"""Shared database utilities for the fitness tracker."""

import os
import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent
# Use FITNESS_DB_PATH env var when set (for Docker), otherwise default to local path
DB_PATH = Path(os.environ.get('FITNESS_DB_PATH', DB_DIR / "fitness.db"))
SCHEMA_PATH = DB_DIR / "schema.sql"

# Idempotent column migrations for tables created before schema updates.
# Each entry is (table_name, column_name, column_definition).
MIGRATIONS: list[tuple[str, str, str]] = [
    ("cardio_sessions", "moving_duration_seconds", "INTEGER"),
]


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a connection to the fitness database, creating schema if needed."""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add any missing columns to existing tables (safe to run multiple times)."""
    for table, column, col_def in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
            conn.commit()
            print(f"Migration: added {table}.{column} ({col_def})")


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Initialize the database with schema.sql if tables don't exist."""
    conn = get_connection(db_path)
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    migrate_schema(conn)
    return conn
