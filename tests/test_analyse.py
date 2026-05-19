"""
test_analyse.py - Tests for analyse.py forgotten-favourites and never-played logic.

All tests use in-memory state.db populated by helpers in this file.
No USB db access, no pyrekordbox, no SQLCipher dependency.

The frozen_today fixture (from conftest.py) patches datetime.date.today()
to 2026-05-12 so recency assertions are deterministic.
"""

from __future__ import annotations

import datetime
import sqlite3

import pytest

import analyse
from analyse import AnalysisResult, ForgottenTrack, NeverPlayedTrack, run
from tests.conftest import FROZEN_TODAY


# ---------------------------------------------------------------------------
# Helpers for building state.db content
# ---------------------------------------------------------------------------

def _add_session(
    conn: sqlite3.Connection,
    session_date: str,
    raw_history_id: str = "H001",
    fingerprint: str | None = None,
    source_db_path: str = "test",
    ingested_at: str = "2026-05-12T00:00:00+00:00",
    track_count: int = 1,
) -> int:
    """Insert a session row and return its session_id."""
    if fingerprint is None:
        # Generate a unique fingerprint from the history_id + date
        fingerprint = f"{raw_history_id}_{session_date}"[:16].ljust(16, "0")
    cur = conn.execute(
        "INSERT INTO sessions "
        "(raw_history_id, fingerprint, session_date, source_db_path, ingested_at, track_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (raw_history_id, fingerprint, session_date, source_db_path, ingested_at, track_count),
    )
    conn.commit()
    return cur.lastrowid


def _add_track(
    conn: sqlite3.Connection,
    content_id: str,
    title: str = "Test Track",
    artist: str = "Test Artist",
    date_created: str = "2025-01-01",
    total_appearances: int = 0,
    first_seen_session: int | None = None,
    last_seen_session: int | None = None,
) -> None:
    """Insert a track row."""
    conn.execute(
        "INSERT OR REPLACE INTO tracks "
        "(content_id, title, artist, date_created, "
        "first_seen_session, last_seen_session, total_appearances) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            content_id, title, artist, date_created,
            first_seen_session, last_seen_session, total_appearances,
        ),
    )
    conn.commit()


def _add_appearance(
    conn: sqlite3.Connection,
    session_id: int,
    content_id: str,
    track_no: int = 1,
) -> None:
    """Insert an appearance row."""
    conn.execute(
        "INSERT INTO appearances (session_id, content_id, track_no) VALUES (?, ?, ?)",
        (session_id, content_id, track_no),
    )
    conn.commit()


def _build_forgotten_candidate(
    conn: sqlite3.Connection,
    content_id: str,
    total_appearances: int,
    last_session_date: str,
    title: str = "Forgotten Track",
) -> None:
    """
    Build a track that has total_appearances sessions, with the most recent
    session on last_session_date. Inserts session + track + appearances.
    """
    # Insert sessions for each appearance
    session_ids = []
    for i in range(total_appearances):
        sid = _add_session(
            conn,
            session_date=last_session_date if i == total_appearances - 1 else f"2024-01-{i+1:02d}",
            raw_history_id=f"{content_id}_H{i:03d}",
            fingerprint=f"{content_id}_{i:013d}",
            track_count=1,
        )
        session_ids.append(sid)

    first_sid = session_ids[0]
    last_sid = session_ids[-1]
    _add_track(
        conn,
        content_id=content_id,
        title=title,
        total_appearances=total_appearances,
        first_seen_session=first_sid,
        last_seen_session=last_sid,
        date_created="2024-01-01",
    )
    for i, sid in enumerate(session_ids):
        _add_appearance(conn, sid, content_id, track_no=1)


# ---------------------------------------------------------------------------
# Forgotten-favourites tests
# ---------------------------------------------------------------------------

def test_forgotten_returns_tracks_meeting_both_thresholds(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track with 6 appearances and last session 100 days before frozen_today
    must appear in the forgotten list.
    """
    last_seen = (FROZEN_TODAY - datetime.timedelta(days=100)).isoformat()
    _build_forgotten_candidate(state_db, "C001", total_appearances=6, last_session_date=last_seen)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.forgotten]
    assert "C001" in content_ids, f"C001 should be forgotten; got {content_ids}"


def test_forgotten_excludes_recent_track(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track with 6 appearances but last session only 30 days ago must NOT
    appear in the forgotten list (threshold is 90 days).
    """
    last_seen = (FROZEN_TODAY - datetime.timedelta(days=30)).isoformat()
    _build_forgotten_candidate(state_db, "C002", total_appearances=6, last_session_date=last_seen)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.forgotten]
    assert "C002" not in content_ids, "Recent track should not be in forgotten list"


def test_forgotten_excludes_infrequent_track(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track with only 2 appearances (below threshold of 5) and last session
    200 days ago must NOT appear in the forgotten list.
    """
    last_seen = (FROZEN_TODAY - datetime.timedelta(days=200)).isoformat()
    _build_forgotten_candidate(state_db, "C003", total_appearances=2, last_session_date=last_seen)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.forgotten]
    assert "C003" not in content_ids, "Infrequent track should not be in forgotten list"


def test_forgotten_respects_limit(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """15 qualifying tracks with limit=10: result must have exactly 10 items."""
    last_seen = (FROZEN_TODAY - datetime.timedelta(days=150)).isoformat()
    for i in range(1, 16):
        _build_forgotten_candidate(
            state_db,
            f"CX{i:03d}",
            total_appearances=5 + i,
            last_session_date=last_seen,
            title=f"Track {i}",
        )

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    assert len(result.forgotten) == 10, (
        f"Expected 10 forgotten tracks (limit), got {len(result.forgotten)}"
    )


def test_forgotten_sorted_by_appearances_desc(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """Forgotten list must be sorted by total_appearances descending."""
    last_seen = (FROZEN_TODAY - datetime.timedelta(days=150)).isoformat()
    for i, appearances in enumerate([5, 8, 6], start=1):
        _build_forgotten_candidate(
            state_db,
            f"CY{i:03d}",
            total_appearances=appearances,
            last_session_date=last_seen,
        )

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    apps = [t.total_appearances for t in result.forgotten]
    assert apps == sorted(apps, reverse=True), f"Not sorted desc: {apps}"


# ---------------------------------------------------------------------------
# Never-played tests
# ---------------------------------------------------------------------------

def test_never_played_includes_old_unplayed_track(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track with date_created 60 days before frozen_today and no appearances
    must appear in the never_played list.
    """
    old_date = (FROZEN_TODAY - datetime.timedelta(days=60)).isoformat()
    _add_track(state_db, "C010", date_created=old_date, total_appearances=0)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.never_played]
    assert "C010" in content_ids, f"Old unplayed track should be in never_played; got {content_ids}"


def test_never_played_excludes_recent_add(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track added 10 days ago (below 30-day threshold) with no appearances
    must NOT appear in the never_played list.
    """
    recent_date = (FROZEN_TODAY - datetime.timedelta(days=10)).isoformat()
    _add_track(state_db, "C011", date_created=recent_date, total_appearances=0)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.never_played]
    assert "C011" not in content_ids, "Recently added track should not appear in never_played"


def test_never_played_excludes_played_track(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """
    A track added 60 days ago with 1 appearance must NOT appear in never_played.
    """
    old_date = (FROZEN_TODAY - datetime.timedelta(days=60)).isoformat()
    sid = _add_session(state_db, session_date="2026-04-01", raw_history_id="HP01",
                       fingerprint="HP01test0000000", track_count=1)
    _add_track(state_db, "C012", date_created=old_date, total_appearances=1,
               first_seen_session=sid, last_seen_session=sid)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    content_ids = [t.content_id for t in result.never_played]
    assert "C012" not in content_ids, "Played track should not appear in never_played"


def test_never_played_respects_limit(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """15 never-played tracks with limit=10: result capped at 10."""
    old_date = (FROZEN_TODAY - datetime.timedelta(days=60)).isoformat()
    for i in range(1, 16):
        _add_track(state_db, f"CN{i:03d}", date_created=old_date, total_appearances=0)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    assert len(result.never_played) == 10, (
        f"Expected 10 never_played tracks (limit), got {len(result.never_played)}"
    )


def test_both_lists_empty_on_fresh_db(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """Fresh state.db with zero sessions and zero tracks: both lists empty, no crash."""
    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    assert result.forgotten == []
    assert result.never_played == []


def test_compute_summary_stats_on_fresh_db(state_db: sqlite3.Connection) -> None:
    """compute_summary_stats returns zero counts on a fresh db and does not crash."""
    stats = analyse.compute_summary_stats(state_db)
    assert stats["total_sessions"] == 0
    assert stats["total_unique_tracks"] == 0
    assert stats["library_size"] == 0
    assert stats["last_sync_at"] == "never"


def test_never_played_sorted_by_date_asc(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    """never_played list is sorted by date_created ascending (oldest un-played first)."""
    dates = ["2024-01-01", "2023-06-01", "2024-06-01"]
    for i, d in enumerate(dates, start=1):
        _add_track(state_db, f"CO{i:03d}", date_created=d, total_appearances=0)

    result = run(
        state_db,
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        today=FROZEN_TODAY,
    )
    result_dates = [t.date_created for t in result.never_played]
    assert result_dates == sorted(result_dates), f"Not sorted asc: {result_dates}"
