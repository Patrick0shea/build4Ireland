"""SQLite database setup and connection helpers for the transport MCP project."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "transport.sqlite3"
SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


def database_path() -> Path:
    """Return the configured database path (TRANSPORT_DB_PATH or local default)."""
    return Path(os.environ.get("TRANSPORT_DB_PATH", str(DEFAULT_DATABASE))).expanduser()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection. Call initialize() once before use."""
    db_path = Path(path) if path is not None else database_path()
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize(path: str | Path | None = None) -> Path:
    """Create the schema and enable WAL mode; safe to call on every startup."""
    db_path = Path(path) if path is not None else database_path()
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema)
    return db_path


@contextmanager
def transaction(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection in a transaction, committing on success or rolling back."""
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
