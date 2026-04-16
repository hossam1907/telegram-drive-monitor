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

CREATE TABLE IF NOT EXISTS access_requests (
    request_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL UNIQUE,
    username         TEXT,
    first_name       TEXT,
    message          TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    requested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at      TIMESTAMP,
    reviewed_by      INTEGER
);

CREATE TABLE IF NOT EXISTS courses (
    course_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_name        TEXT UNIQUE NOT NULL,
    course_code        TEXT UNIQUE,
    description        TEXT,
    drive_folder_id    TEXT,
    youtube_channel_id TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS youtube_playlists (
    playlist_id    TEXT PRIMARY KEY,
    course_id      INTEGER,
    playlist_name  TEXT NOT NULL,
    playlist_url   TEXT,
    video_count    INTEGER,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_id) REFERENCES courses(course_id)
);

CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id        TEXT PRIMARY KEY,
    playlist_id     TEXT,
    course_id       INTEGER,
    video_title     TEXT NOT NULL,
    video_url       TEXT NOT NULL,
    video_order     INTEGER,
    duration        TEXT,
    view_count      INTEGER,
    thumbnail_url   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (playlist_id) REFERENCES youtube_playlists(playlist_id),
    FOREIGN KEY (course_id) REFERENCES courses(course_id)
);

CREATE TABLE IF NOT EXISTS broadcast_messages (
    message_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id          INTEGER NOT NULL,
    message_text       TEXT NOT NULL,
    target_type        TEXT DEFAULT 'all',
    target_course_id   INTEGER,
    sent_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status             TEXT DEFAULT 'sent',
    delivery_count     INTEGER DEFAULT 0,
    FOREIGN KEY (target_course_id) REFERENCES courses(course_id)
);

CREATE TABLE IF NOT EXISTS user_enrollments (
    enrollment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    course_id        INTEGER NOT NULL,
    enrolled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, course_id),
    FOREIGN KEY (course_id) REFERENCES courses(course_id)
);
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
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(youtube_videos)").fetchall()
        }
        if "view_count" not in columns:
            conn.execute("ALTER TABLE youtube_videos ADD COLUMN view_count INTEGER")
            logger.info("Database migration applied: added youtube_videos.view_count column.")
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


# ---------------------------------------------------------------------------
# Access request operations
# ---------------------------------------------------------------------------

def submit_access_request(user_id: int, username: Optional[str], first_name: Optional[str],
                          message: str) -> None:
    """Create or update an access request for a user."""
    with _transaction() as conn:
        conn.execute(
            """
            INSERT INTO access_requests (user_id, username, first_name, message, status)
            VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                message = excluded.message,
                status = 'pending',
                requested_at = CURRENT_TIMESTAMP,
                reviewed_at = NULL,
                reviewed_by = NULL
            """,
            (user_id, username, first_name, message),
        )


def get_pending_requests() -> List[Dict]:
    """Return all pending access requests ordered by oldest first."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT request_id, user_id, username, first_name, message, status,
                   requested_at, reviewed_at, reviewed_by
            FROM access_requests
            WHERE status = 'pending'
            ORDER BY requested_at ASC, request_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_access_request(user_id: int) -> Optional[Dict]:
    """Return access request details for a user, if present."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT request_id, user_id, username, first_name, message, status,
                   requested_at, reviewed_at, reviewed_by
            FROM access_requests
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def approve_request(user_id: int, reviewed_by: Optional[int] = None) -> bool:
    """Mark a user's access request as approved."""
    with _transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE access_requests
            SET status = 'approved', reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?
            WHERE user_id = ?
            """,
            (reviewed_by, user_id),
        )
        return cursor.rowcount > 0


def reject_request(user_id: int, reviewed_by: Optional[int] = None) -> bool:
    """Mark a user's access request as rejected."""
    with _transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE access_requests
            SET status = 'rejected', reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?
            WHERE user_id = ?
            """,
            (reviewed_by, user_id),
        )
        return cursor.rowcount > 0


def is_user_approved(user_id: int) -> bool:
    """Return whether a user currently has approved access."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM access_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row["status"] == "approved")
    finally:
        conn.close()


def get_approved_users() -> List[int]:
    """Return IDs of all users with approved access."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id FROM access_requests WHERE status = 'approved' ORDER BY user_id"
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Course management
# ---------------------------------------------------------------------------

def add_course(course_name: str, course_code: Optional[str], drive_folder_id: Optional[str] = None,
               youtube_channel_id: Optional[str] = None,
               description: Optional[str] = None) -> int:
    """Add or update a course and return its ID."""
    with _transaction() as conn:
        conn.execute(
            """
            INSERT INTO courses (course_name, course_code, description, drive_folder_id, youtube_channel_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(course_name) DO UPDATE SET
                course_code = excluded.course_code,
                description = excluded.description,
                drive_folder_id = excluded.drive_folder_id,
                youtube_channel_id = excluded.youtube_channel_id
            """,
            (course_name, course_code, description, drive_folder_id, youtube_channel_id),
        )
        row = conn.execute(
            "SELECT course_id FROM courses WHERE course_name = ?",
            (course_name,),
        ).fetchone()
        return int(row["course_id"])


def get_all_courses() -> List[Dict]:
    """Get all courses ordered by course code then name."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT course_id, course_name, course_code, description, drive_folder_id,
                   youtube_channel_id, created_at
            FROM courses
            ORDER BY course_code IS NULL, course_code, course_name
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_course_by_code(course_code: str) -> Optional[Dict]:
    """Get course by course code (case-insensitive)."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT course_id, course_name, course_code, description, drive_folder_id,
                   youtube_channel_id, created_at
            FROM courses
            WHERE UPPER(course_code) = UPPER(?)
            """,
            (course_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_course_by_id(course_id: int) -> Optional[Dict]:
    """Get course by ID."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT course_id, course_name, course_code, description, drive_folder_id,
                   youtube_channel_id, created_at
            FROM courses
            WHERE course_id = ?
            """,
            (course_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def add_youtube_playlist(playlist_id: str, course_id: Optional[int], playlist_name: str,
                         playlist_url: Optional[str], video_count: Optional[int]) -> None:
    """Add or update a YouTube playlist."""
    with _transaction() as conn:
        conn.execute(
            """
            INSERT INTO youtube_playlists (playlist_id, course_id, playlist_name, playlist_url, video_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
                course_id = excluded.course_id,
                playlist_name = excluded.playlist_name,
                playlist_url = excluded.playlist_url,
                video_count = excluded.video_count
            """,
            (playlist_id, course_id, playlist_name, playlist_url, video_count),
        )


def get_course_playlists(course_id: int) -> List[Dict]:
    """Get all playlists for a course."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT playlist_id, course_id, playlist_name, playlist_url, video_count, created_at
            FROM youtube_playlists
            WHERE course_id = ?
            ORDER BY playlist_name
            """,
            (course_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def add_youtube_video(video_id: str, playlist_id: Optional[str], course_id: Optional[int], video_title: str,
                      video_url: str, video_order: Optional[int], duration: Optional[str] = None,
                      thumbnail_url: Optional[str] = None, view_count: Optional[int] = None) -> None:
    """Add or update a YouTube video."""
    with _transaction() as conn:
        conn.execute(
            """
            INSERT INTO youtube_videos (
                video_id, playlist_id, course_id, video_title, video_url, video_order, duration,
                thumbnail_url, view_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                playlist_id = excluded.playlist_id,
                course_id = excluded.course_id,
                video_title = excluded.video_title,
                video_url = excluded.video_url,
                video_order = excluded.video_order,
                duration = excluded.duration,
                thumbnail_url = excluded.thumbnail_url,
                view_count = excluded.view_count
            """,
            (
                video_id,
                playlist_id,
                course_id,
                video_title,
                video_url,
                video_order,
                duration,
                thumbnail_url,
                view_count,
            ),
        )


def get_playlist_videos(playlist_id: str) -> List[Dict]:
    """Get all videos in a playlist."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT video_id, playlist_id, course_id, video_title, video_url, video_order,
                   duration, thumbnail_url, view_count, created_at
            FROM youtube_videos
            WHERE playlist_id = ?
            ORDER BY video_order IS NULL, video_order, video_title
            """,
            (playlist_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_course_video_count(course_id: int) -> int:
    """Return total number of videos linked to a course."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM youtube_videos WHERE course_id = ?",
            (course_id,),
        ).fetchone()
        return int(row["cnt"])
    finally:
        conn.close()


def send_broadcast(sender_id: int, message_text: str, target_type: str,
                   target_course_id: Optional[int] = None, delivery_count: int = 0) -> int:
    """Record broadcast message and return message ID."""
    with _transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO broadcast_messages (sender_id, message_text, target_type, target_course_id, delivery_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sender_id, message_text, target_type, target_course_id, delivery_count),
        )
        return int(cursor.lastrowid)


def get_recent_broadcasts(limit: int = 10) -> List[Dict]:
    """Return recent broadcast messages with course metadata."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT b.message_id, b.sender_id, b.message_text, b.target_type, b.target_course_id,
                   b.sent_at, b.status, b.delivery_count, c.course_name, c.course_code
            FROM broadcast_messages b
            LEFT JOIN courses c ON c.course_id = b.target_course_id
            ORDER BY b.sent_at DESC, b.message_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_user_courses(user_id: int) -> List[Dict]:
    """Get courses for a user."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT c.course_id, c.course_name, c.course_code, c.description, c.drive_folder_id,
                   c.youtube_channel_id, c.created_at
            FROM user_enrollments e
            JOIN courses c ON c.course_id = e.course_id
            WHERE e.user_id = ?
            ORDER BY c.course_code IS NULL, c.course_code, c.course_name
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def enroll_user_in_course(user_id: int, course_id: int) -> bool:
    """Enroll user in course. Returns True if inserted, False if already enrolled."""
    with _transaction() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO user_enrollments (user_id, course_id) VALUES (?, ?)",
            (user_id, course_id),
        )
        return cursor.rowcount > 0


def get_course_enrolled_users(course_id: int) -> List[int]:
    """Get all user IDs enrolled in a course."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id FROM user_enrollments WHERE course_id = ? ORDER BY user_id",
            (course_id,),
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    finally:
        conn.close()
