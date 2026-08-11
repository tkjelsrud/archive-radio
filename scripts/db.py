"""
Shared SQLite access for the pipeline (DESIGN.md §20).

archive.db is the only coordination point between jotta_sync.py, scan.py,
analyze.py, render.py, and the web app — no script keeps its own
side-state. Import this module rather than opening sqlite3 directly.
"""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).parent.parent / "archive.db"


def connect(db_path=DEFAULT_DB_PATH):
    """Open a connection with foreign keys enabled and dict-like rows."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=DEFAULT_DB_PATH):
    """Create tables if they don't exist yet. Safe to call every run."""
    conn = connect(db_path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    return conn
