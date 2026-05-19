"""
analyse.py - Pure SQL/Python analysis against state.db for Set Memory.

No USB db access, no pyrekordbox, no SQLCipher dependency.
Takes an open sqlite3 connection and a Config, returns two lists.

Fully unit-testable with an in-memory SQLite db populated by conftest.py fixtures.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ForgottenTrack:
    """A track that was played often but not recently."""
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    total_appearances: int
    last_session_date: str  # ISO 8601 date string from sessions.session_date


@dataclass
class NeverPlayedTrack:
    """A track in the library that has never appeared in any session."""
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    date_created: Optional[str]  # djmdContent.DateCreated (file creation date)


@dataclass
class AnalysisResult:
    forgotten: list[ForgottenTrack]
    never_played: list[NeverPlayedTrack]


def run(
    state_conn: sqlite3.Connection,
    forgotten_min_appearances: int,
    forgotten_days_since_last: int,
    forgotten_limit: int,
    never_played_min_days_since_add: int,
    never_played_limit: int,
    today: Optional[datetime.date] = None,
) -> AnalysisResult:
    """
    Compute forgotten-favourites and never-played lists from state.db.

    Parameters
    ----------
    state_conn:
        Open connection to state.db (plain sqlite3, no SQLCipher needed).
    forgotten_min_appearances:
        Track must appear in at least this many sessions to qualify as a "favourite."
    forgotten_days_since_last:
        Track must not have been seen for at least this many days.
    forgotten_limit:
        Maximum number of forgotten tracks to return.
    never_played_min_days_since_add:
        Track must have been in the library for at least this many days.
        Based on djmdContent.DateCreated (file creation date, see DESIGN D5 note).
    never_played_limit:
        Maximum number of never-played tracks to return.
    today:
        Override for today's date. Injected in tests for determinism.
    """
    if today is None:
        today = datetime.date.today()

    forgotten = _query_forgotten(
        state_conn,
        min_appearances=forgotten_min_appearances,
        days_since_last=forgotten_days_since_last,
        limit=forgotten_limit,
        today=today,
    )

    never_played = _query_never_played(
        state_conn,
        min_days_since_add=never_played_min_days_since_add,
        limit=never_played_limit,
        today=today,
    )

    return AnalysisResult(forgotten=forgotten, never_played=never_played)


def _query_forgotten(
    conn: sqlite3.Connection,
    min_appearances: int,
    days_since_last: int,
    limit: int,
    today: datetime.date,
) -> list[ForgottenTrack]:
    """
    Return tracks that meet BOTH conditions:
      - total_appearances >= min_appearances
      - last session date is more than days_since_last days before today

    Sorted by total_appearances descending (most-played forgotten first).
    """
    cutoff_date = (today - datetime.timedelta(days=days_since_last)).isoformat()

    sql = """
        SELECT
            t.content_id,
            t.title,
            t.artist,
            t.total_appearances,
            s.session_date AS last_session_date
        FROM tracks t
        JOIN sessions s ON s.session_id = t.last_seen_session
        WHERE t.total_appearances >= ?
          AND s.session_date < ?
        ORDER BY t.total_appearances DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (min_appearances, cutoff_date, limit)).fetchall()
    return [
        ForgottenTrack(
            content_id=str(row[0]),
            title=str(row[1]) if row[1] else None,
            artist=str(row[2]) if row[2] else None,
            total_appearances=int(row[3]),
            last_session_date=str(row[4]),
        )
        for row in rows
    ]


def _query_never_played(
    conn: sqlite3.Connection,
    min_days_since_add: int,
    limit: int,
    today: datetime.date,
) -> list[NeverPlayedTrack]:
    """
    Return tracks that have total_appearances = 0 AND date_created is old enough.

    Note (DESIGN D5): date_created is the file creation date from djmdContent,
    not the date Rob added the track to rekordbox. This is an approximation.
    A future improvement could use djmdContent.StockDate if its semantics are
    confirmed - left as a note, not a code branch.

    Sorted by date_created ascending (oldest un-played track first).
    """
    cutoff_date = (today - datetime.timedelta(days=min_days_since_add)).isoformat()

    sql = """
        SELECT
            t.content_id,
            t.title,
            t.artist,
            t.date_created
        FROM tracks t
        WHERE t.total_appearances = 0
          AND t.date_created IS NOT NULL
          AND t.date_created < ?
        ORDER BY t.date_created ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff_date, limit)).fetchall()
    return [
        NeverPlayedTrack(
            content_id=str(row[0]),
            title=str(row[1]) if row[1] else None,
            artist=str(row[2]) if row[2] else None,
            date_created=str(row[3]) if row[3] else None,
        )
        for row in rows
    ]


def compute_summary_stats(state_conn: sqlite3.Connection) -> dict[str, int | str]:
    """
    Compute summary statistics from state.db for the digest.

    Returns a dict with: total_sessions, total_unique_tracks,
    library_size (count of tracks in state.db), last_sync_at.
    """
    total_sessions = state_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_unique_tracks = state_conn.execute(
        "SELECT COUNT(DISTINCT content_id) FROM appearances"
    ).fetchone()[0]
    library_size = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    last_sync_at_row = state_conn.execute(
        "SELECT value FROM meta WHERE key = 'last_sync_at'"
    ).fetchone()
    last_sync_at = str(last_sync_at_row[0]) if last_sync_at_row else "never"

    return {
        "total_sessions": total_sessions,
        "total_unique_tracks": total_unique_tracks,
        "library_size": library_size,
        "last_sync_at": last_sync_at,
    }
