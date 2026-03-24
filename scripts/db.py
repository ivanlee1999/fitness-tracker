"""Shared database utilities for the fitness tracker."""

import os
import re
import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent
# Use FITNESS_DB_PATH env var when set (for Docker), otherwise default to local path
DB_PATH = Path(os.environ.get('FITNESS_DB_PATH', DB_DIR / "fitness.db"))
SCHEMA_PATH = DB_DIR / "schema.sql"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a connection to the fitness database, creating schema if needed."""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _parse_schema_columns(schema_sql: str) -> dict[str, list[tuple[str, str]]]:
    """
    Parse CREATE TABLE statements from schema SQL and return:
    { table_name: [(col_name, col_type), ...] }
    Only returns regular columns (not constraints/indexes).
    """
    tables: dict[str, list[tuple[str, str]]] = {}
    for match in re.finditer(
        r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)\s*\((.*?)\);',
        schema_sql, re.DOTALL | re.IGNORECASE,
    ):
        table_name = match.group(1)
        body = match.group(2)
        cols: list[tuple[str, str]] = []
        for line in body.split('\n'):
            line = line.strip().rstrip(',')
            if not line:
                continue
            # Skip constraints
            if re.match(
                r'(PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK|CONSTRAINT)',
                line, re.IGNORECASE,
            ):
                continue
            # Extract column name and type
            col_match = re.match(r'(\w+)\s+(.+)', line)
            if col_match:
                col_name = col_match.group(1)
                col_def = col_match.group(2).strip()
                col_type = col_def.split()[0]  # e.g. INTEGER, TEXT, REAL
                cols.append((col_name, col_type))
        if cols:
            tables[table_name] = cols
    return tables


def migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Auto-migrate: compare live DB tables against schema.sql and ADD COLUMN
    for any missing columns.  Safe to run multiple times (idempotent).
    """
    schema_sql = SCHEMA_PATH.read_text()
    schema_tables = _parse_schema_columns(schema_sql)

    for table_name, schema_cols in schema_tables.items():
        # Check if table exists in DB
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            continue  # Table not created yet; executescript handles it

        # Get existing columns
        existing_cols = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")
        }

        for col_name, col_type in schema_cols:
            if col_name not in existing_cols:
                try:
                    conn.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                    )
                    conn.commit()
                    print(f"Migration: added {table_name}.{col_name} ({col_type})")
                except Exception as e:
                    print(
                        f"Migration warning: could not add {table_name}.{col_name}: {e}"
                    )


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Initialize the database with schema.sql if tables don't exist."""
    conn = get_connection(db_path)
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    migrate_schema(conn)
    return conn
