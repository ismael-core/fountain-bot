"""SQLite database layer for the Fountain bot.

Stores refresh logs and weekly slot assignments. All timestamps are UTC.
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

            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
                hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
                UNIQUE(day_of_week, hour)
            );

            CREATE INDEX IF NOT EXISTS idx_refreshes_timestamp ON refreshes(timestamp);
            CREATE INDEX IF NOT EXISTS idx_refreshes_user ON refreshes(user_id);
            CREATE INDEX IF NOT EXISTS idx_slots_user ON slots(user_id);
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


# ---------- Slot operations ----------

def add_slot(user_id: int, username: str, day_of_week: int, hour: int) -> bool:
    """Assign user to a weekly slot. Returns False if the slot is taken."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM slots WHERE day_of_week = ? AND hour = ?",
            (day_of_week, hour),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO slots (user_id, username, day_of_week, hour) VALUES (?, ?, ?, ?)",
            (user_id, username, day_of_week, hour),
        )
    return True


def remove_slot(user_id: int, day_of_week: int, hour: int) -> bool:
    """Remove a slot owned by the given user. Returns True if a row was deleted."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM slots WHERE user_id = ? AND day_of_week = ? AND hour = ?",
            (user_id, day_of_week, hour),
        )
        return cur.rowcount > 0


def get_all_slots() -> list[dict]:
    """Return all slots ordered by day and hour."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, username, day_of_week, hour "
            "FROM slots ORDER BY day_of_week, hour"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_slots(user_id: int) -> list[dict]:
    """Return all slots assigned to a user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT day_of_week, hour FROM slots "
            "WHERE user_id = ? ORDER BY day_of_week, hour",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_slot_for(day_of_week: int, hour: int) -> Optional[dict]:
    """Return the user assigned to a specific slot, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username FROM slots WHERE day_of_week = ? AND hour = ?",
            (day_of_week, hour),
        ).fetchone()
    return dict(row) if row else None
