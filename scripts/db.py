"""Shared database utilities for the fitness tracker."""

import os
import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DB_DIR / "fitness.db"
SCHEMA_PATH = DB_DIR / "schema.sql"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a connection to the fitness database, creating schema if needed."""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Initialize the database with schema.sql if tables don't exist."""
    conn = get_connection(db_path)
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    return conn
