"""SQLite database layer for the Fountain bot.

Stores refresh logs only. All timestamps are UTC.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = "fountain.db"


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


@contextmanager
def get_connection():
    """Context manager that commits on success and rolls back on error."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables and indexes if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS refreshes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_refreshes_timestamp ON refreshes(timestamp);
            CREATE INDEX IF NOT EXISTS idx_refreshes_user ON refreshes(user_id);
        """)


# ---------- Refresh operations ----------

def log_refresh(user_id: int, username: str) -> datetime:
    """Insert a refresh log entry and return its UTC timestamp."""
    ts = utc_now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO refreshes (user_id, username, timestamp) VALUES (?, ?, ?)",
            (user_id, username, ts),
        )
    return ts


def get_last_refresh() -> Optional[dict]:
    """Return the most recent refresh, or None if there are no refreshes."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, timestamp FROM refreshes "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def has_refresh_since(since_utc: datetime) -> bool:
    """Return True if any refresh was logged at or after `since_utc`."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refreshes WHERE timestamp >= ? LIMIT 1",
            (since_utc,),
        ).fetchone()
    return row is not None


def get_leaderboard(days: int = 7) -> list[dict]:
    """Return list of {user_id, username, count} ordered by count desc."""
    cutoff = utc_now() - timedelta(days=days)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id,
                   MAX(username) AS username,
                   COUNT(*) AS count
            FROM refreshes
            WHERE timestamp >= ?
            GROUP BY user_id
            ORDER BY count DESC, user_id ASC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_stats(user_id: int) -> dict:
    """Return refresh counts for a user (week, month, all-time)."""
    now = utc_now()
    week_cutoff = now - timedelta(days=7)
    month_cutoff = now - timedelta(days=30)
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM refreshes WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        week = conn.execute(
            "SELECT COUNT(*) AS c FROM refreshes WHERE user_id = ? AND timestamp >= ?",
            (user_id, week_cutoff),
        ).fetchone()["c"]
        month = conn.execute(
            "SELECT COUNT(*) AS c FROM refreshes WHERE user_id = ? AND timestamp >= ?",
            (user_id, month_cutoff),
        ).fetchone()["c"]
    return {"total": total, "week": week, "month": month}


# ---------- Admin / cleanup operations ----------

def clear_refreshes() -> int:
    """Delete all refresh entries. Returns number of rows deleted."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM refreshes")
        return cursor.rowcount
