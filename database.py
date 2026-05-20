"""SQLite database layer for the Fountain bot.

Tables:
- refreshes: every approved refresh (linked to a ticket when applicable)
- tickets: lifecycle of each ticket flow
- config: runtime config (game_link, etc) settable via /set_link
- blacklist: users banned from re-entering, with strike progression
- audit_log_entries: structured record of every event the bot handles
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = "fountain.db"


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def _aware(ts):
    """Force UTC-awareness on a timestamp coming from SQLite."""
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


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
    """Create tables and run migrations for older DBs."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS refreshes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                proof_url TEXT,
                ticket_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                expires_at TIMESTAMP,
                refresh_count INTEGER NOT NULL DEFAULT 0,
                last_approved_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                updated_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                banned_at TIMESTAMP NOT NULL,
                banned_until TIMESTAMP,
                strike_count INTEGER NOT NULL DEFAULT 1,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id INTEGER,
                mod_id INTEGER,
                ticket_id INTEGER,
                details TEXT,
                created_at TIMESTAMP NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_refreshes_timestamp ON refreshes(timestamp);
            CREATE INDEX IF NOT EXISTS idx_refreshes_user ON refreshes(user_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log_entries(created_at);
        """)

        # Migrations for older versions of the DB
        cols = [r[1] for r in conn.execute("PRAGMA table_info(refreshes)").fetchall()]
        if "proof_url" not in cols:
            conn.execute("ALTER TABLE refreshes ADD COLUMN proof_url TEXT")
        if "ticket_id" not in cols:
            conn.execute("ALTER TABLE refreshes ADD COLUMN ticket_id INTEGER")


# ====================================================================
# Refresh history (used by /leaderboard and /stats)
# ====================================================================

def log_refresh(
    user_id: int,
    username: str,
    proof_url: str,
    ticket_id: Optional[int] = None,
) -> datetime:
    """Insert a refresh log entry and return its UTC timestamp."""
    ts = utc_now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO refreshes (user_id, username, timestamp, proof_url, ticket_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, ts, proof_url, ticket_id),
        )
    return ts


def get_last_refresh() -> Optional[dict]:
    """Return the most recent refresh, or None if there are no refreshes."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, timestamp FROM refreshes "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["timestamp"] = _aware(d["timestamp"])
    return d


def get_leaderboard(days: int = 7) -> list[dict]:
    """Return list of {user_id, username, count} ordered by count desc."""
    cutoff = utc_now() - timedelta(days=days)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id, MAX(username) AS username, COUNT(*) AS count
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


def clear_refreshes() -> int:
    """Delete all refresh entries. Returns number of rows deleted."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM refreshes")
        return cursor.rowcount


# ====================================================================
# Tickets
# ====================================================================

# Status values used in the tickets table
TICKET_CREATED = "created"            # /start_ticket ran, link+button posted, waiting for user click
TICKET_WAITING_PROOF = "waiting"      # user clicked button, waiting for screenshot
TICKET_PENDING = "pending"            # screenshot uploaded, waiting for mod approval
TICKET_ACTIVE = "active"              # mod approved, AFK timer running
TICKET_EXPIRED_GRACE = "expired"      # AFK ran out, in 10-min grace before blacklist
TICKET_CLOSED = "closed"              # ticket finished (clean exit or blacklist)


def create_ticket(user_id: int, channel_id: int) -> int:
    """Insert a new ticket in CREATED state. Returns the ticket id."""
    ts = utc_now()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tickets (user_id, channel_id, status, created_at, refresh_count)
            VALUES (?, ?, ?, ?, 0)
            """,
            (user_id, channel_id, TICKET_CREATED, ts),
        )
        return cursor.lastrowid


def get_ticket(ticket_id: int) -> Optional[dict]:
    """Get a ticket by id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["created_at"] = _aware(d["created_at"])
    d["started_at"] = _aware(d["started_at"])
    d["expires_at"] = _aware(d["expires_at"])
    return d


def get_ticket_by_channel(channel_id: int) -> Optional[dict]:
    """Get the most recent ticket for a given channel."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["created_at"] = _aware(d["created_at"])
    d["started_at"] = _aware(d["started_at"])
    d["expires_at"] = _aware(d["expires_at"])
    return d


def set_ticket_status(ticket_id: int, status: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?",
            (status, ticket_id),
        )


def approve_refresh(ticket_id: int, mod_id: int, afk_hours: int) -> datetime:
    """Mark a ticket as ACTIVE (or extend it), bumping refresh_count and expires_at.

    Returns the new expires_at.
    """
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, expires_at, refresh_count FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Ticket {ticket_id} not found")

        current_expires = _aware(row["expires_at"])
        new_base = max(current_expires, now) if current_expires else now
        new_expires = new_base + timedelta(hours=afk_hours)
        new_count = row["refresh_count"] + 1

        conn.execute(
            """
            UPDATE tickets
            SET status = ?, started_at = COALESCE(started_at, ?),
                expires_at = ?, refresh_count = ?, last_approved_by = ?
            WHERE id = ?
            """,
            (TICKET_ACTIVE, now, new_expires, new_count, mod_id, ticket_id),
        )
    return new_expires


def get_active_tickets() -> list[dict]:
    """Return all tickets in ACTIVE or EXPIRED_GRACE state."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status IN (?, ?) ORDER BY expires_at ASC",
            (TICKET_ACTIVE, TICKET_EXPIRED_GRACE),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = _aware(d["created_at"])
        d["started_at"] = _aware(d["started_at"])
        d["expires_at"] = _aware(d["expires_at"])
        out.append(d)
    return out


def get_waiting_queue() -> list[dict]:
    """Return tickets that have a ticket created but no approved refresh yet."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status IN (?, ?, ?)
            ORDER BY created_at ASC
            """,
            (TICKET_CREATED, TICKET_WAITING_PROOF, TICKET_PENDING),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = _aware(d["created_at"])
        d["started_at"] = _aware(d["started_at"])
        d["expires_at"] = _aware(d["expires_at"])
        out.append(d)
    return out


# ====================================================================
# Config (key/value, settable at runtime)
# ====================================================================

def get_config(key: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_config(key: str, value: str, updated_by: Optional[int] = None):
    ts = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO config (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by
            """,
            (key, value, ts, updated_by),
        )


# ====================================================================
# Blacklist
# ====================================================================

def get_blacklist_entry(user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM blacklist WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["banned_at"] = _aware(d["banned_at"])
    d["banned_until"] = _aware(d["banned_until"])
    return d


def is_blacklisted(user_id: int) -> bool:
    entry = get_blacklist_entry(user_id)
    if entry is None:
        return False
    if entry["banned_until"] is None:
        return True  # permanent
    return entry["banned_until"] > utc_now()


def add_blacklist_strike(user_id: int, durations_hours: list[int], reason: str = "") -> dict:
    """Add a strike to a user. Returns the new blacklist entry.

    durations_hours: list like [24, 48, 168]. If strike_count exceeds the
    list length, ban is permanent (banned_until = NULL).
    """
    now = utc_now()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT strike_count FROM blacklist WHERE user_id = ?", (user_id,)
        ).fetchone()
        new_strike = (existing["strike_count"] + 1) if existing else 1

        if new_strike <= len(durations_hours):
            banned_until = now + timedelta(hours=durations_hours[new_strike - 1])
        else:
            banned_until = None  # permanent

        conn.execute(
            """
            INSERT INTO blacklist (user_id, banned_at, banned_until, strike_count, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned_at = excluded.banned_at,
                banned_until = excluded.banned_until,
                strike_count = excluded.strike_count,
                reason = excluded.reason
            """,
            (user_id, now, banned_until, new_strike, reason),
        )
    return {
        "user_id": user_id,
        "banned_at": now,
        "banned_until": banned_until,
        "strike_count": new_strike,
        "reason": reason,
    }


def remove_from_blacklist(user_id: int) -> bool:
    """Manually remove a user from the blacklist. Returns True if a row was deleted."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0


# ====================================================================
# Audit log
# ====================================================================

def write_audit_entry(
    event_type: str,
    user_id: Optional[int] = None,
    mod_id: Optional[int] = None,
    ticket_id: Optional[int] = None,
    details: Optional[dict] = None,
):
    """Insert an audit log entry. Details are stored as JSON for flexibility."""
    ts = utc_now()
    details_json = json.dumps(details) if details else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_log_entries (event_type, user_id, mod_id, ticket_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, user_id, mod_id, ticket_id, details_json, ts),
        )
