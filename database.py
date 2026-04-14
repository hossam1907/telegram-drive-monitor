"""
Database module for Telegram Drive Monitor.

Provides SQLite-backed storage for tracking Google Drive file metadata and
change detection.  All public methods are thread-safe thanks to SQLite's
WAL mode and Python's threading.Lock.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Generator, List, Optional

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

# Single global lock to serialise writes without a connection pool.
_db_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS drive_files (
    file_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    size            INTEGER,
    mime_type       TEXT,
    modified_time   TEXT,
    version         TEXT,
    last_checked    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitoring_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled      INTEGER NOT NULL DEFAULT 1,
    started_at      TEXT,
    last_poll       TEXT,
    total_new       INTEGER NOT NULL DEFAULT 0,
    total_updated   INTEGER NOT NULL DEFAULT 0
);

-- Seed the single monitoring state row if it doesn't exist.
INSERT OR IGNORE INTO monitoring_state (id, is_enabled) VALUES (1, 1);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """Open a new SQLite connection configured for safe concurrent use.

    Returns:
        A configured :class:`sqlite3.Connection` instance.
    """
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def _transaction() -> Generator[sqlite3.Connection, None, None]:
    """Context manager that provides a connection inside a transaction.

    Commits on success, rolls back on exception, and always closes the
    connection.  Acquires the module-level write lock.

    Yields:
        An open :class:`sqlite3.Connection`.
    """
    with _db_lock:
        conn = _get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables and seed initial data if the database is new.

    Should be called once at application startup.
    """
    with _transaction() as conn:
        conn.executescript(_DDL)
    logger.info("Database initialised at '%s'.", DATABASE_PATH)


# ---------------------------------------------------------------------------
# File record operations
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def upsert_file(
    file_id: str,
    name: str,
    size: Optional[int],
    mime_type: Optional[str],
    modified_time: Optional[str],
    version: Optional[str],
) -> str:
    """Insert or update a file record and return its change status.

    Args:
        file_id: Google Drive file ID (primary key).
        name: File name.
        size: File size in bytes, or ``None`` if unavailable.
        mime_type: MIME type string.
        modified_time: ISO-8601 last-modified timestamp from Drive.
        version: Drive revision/version string.

    Returns:
        One of ``"new"``, ``"updated"``, or ``"unchanged"``.
    """
    now = _now_iso()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT version, modified_time FROM drive_files WHERE file_id = ?", (file_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO drive_files
                    (file_id, name, size, mime_type, modified_time, version, last_checked)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, name, size, mime_type, modified_time, version, now),
            )
            logger.debug("New file recorded: %s (%s)", name, file_id)
            return "new"

        # Detect changes by comparing version and modified_time
        if row["version"] != version or row["modified_time"] != modified_time:
            conn.execute(
                """
                UPDATE drive_files
                SET name = ?, size = ?, mime_type = ?, modified_time = ?,
                    version = ?, last_checked = ?
                WHERE file_id = ?
                """,
                (name, size, mime_type, modified_time, version, now, file_id),
            )
            logger.debug("Updated file recorded: %s (%s)", name, file_id)
            return "updated"

        # Just refresh the last_checked timestamp
        conn.execute(
            "UPDATE drive_files SET last_checked = ? WHERE file_id = ?", (now, file_id)
        )
        return "unchanged"


def get_file(file_id: str) -> Optional[Dict]:
    """Retrieve a single file record by its Drive ID.

    Args:
        file_id: Google Drive file ID.

    Returns:
        A dict with file metadata keys, or ``None`` if not found.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM drive_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def search_files(query: str, limit: int = 50) -> List[Dict]:
    """Search for files whose name contains *query* (case-insensitive).

    Args:
        query: Substring to search for.
        limit: Maximum number of results to return.

    Returns:
        List of file metadata dicts ordered by name.
    """
    pattern = f"%{query}%"
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM drive_files WHERE name LIKE ? ORDER BY name LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_files(offset: int = 0, limit: int = 50) -> List[Dict]:
    """Return all tracked files with pagination.

    Args:
        offset: Number of records to skip.
        limit: Maximum records to return.

    Returns:
        List of file metadata dicts ordered by name.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM drive_files ORDER BY name LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_files() -> int:
    """Return the total number of tracked files.

    Returns:
        Integer count.
    """
    conn = _get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM drive_files").fetchone()
        return int(row["cnt"])
    finally:
        conn.close()


def delete_stale_files(known_ids: List[str]) -> int:
    """Remove records whose IDs are not present in *known_ids*.

    Args:
        known_ids: List of Drive file IDs that still exist in the folder.

    Returns:
        Number of records deleted.
    """
    if not known_ids:
        return 0
    placeholders = ",".join("?" * len(known_ids))
    with _transaction() as conn:
        cursor = conn.execute(
            f"DELETE FROM drive_files WHERE file_id NOT IN ({placeholders})", known_ids
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("Removed %d stale file record(s) from database.", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Monitoring state
# ---------------------------------------------------------------------------

def get_monitoring_state() -> Dict:
    """Return the current monitoring state as a dict.

    Returns:
        Dict with keys: ``is_enabled``, ``started_at``, ``last_poll``,
        ``total_new``, ``total_updated``.
    """
    conn = _get_connection()
    try:
        row = conn.execute("SELECT * FROM monitoring_state WHERE id = 1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def set_monitoring_enabled(enabled: bool) -> None:
    """Enable or disable monitoring.

    Args:
        enabled: ``True`` to enable, ``False`` to disable.
    """
    now = _now_iso() if enabled else None
    with _transaction() as conn:
        conn.execute(
            "UPDATE monitoring_state SET is_enabled = ?, started_at = ? WHERE id = 1",
            (1 if enabled else 0, now),
        )
    logger.info("Monitoring %s.", "enabled" if enabled else "disabled")


def record_poll(new_count: int, updated_count: int) -> None:
    """Update statistics after a completed poll cycle.

    Args:
        new_count: Number of new files detected in this cycle.
        updated_count: Number of updated files detected in this cycle.
    """
    now = _now_iso()
    with _transaction() as conn:
        conn.execute(
            """
            UPDATE monitoring_state
            SET last_poll = ?,
                total_new = total_new + ?,
                total_updated = total_updated + ?
            WHERE id = 1
            """,
            (now, new_count, updated_count),
        )
