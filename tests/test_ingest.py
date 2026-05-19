"""
test_ingest.py - Unit and integration tests for ingest.py (Layer A only).

All tests use the synthetic plain SQLite db. No SQLCipher, no pyrekordbox.
The connect_master_db boundary is tested via connect_master_db(path, key=None).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import ingest
from ingest import (
    IngestSummary,
    SchemaError,
    WalLockError,
    compute_fingerprint,
    connect_master_db,
    ingest_from_connection,
    read_sessions,
    read_song_history,
)


# ---------------------------------------------------------------------------
# connect_master_db
# ---------------------------------------------------------------------------

def test_connect_master_db_plain_sqlite(synthetic_usb_db: Path) -> None:
    """connect_master_db with key=None returns a working sqlite3 connection."""
    conn = connect_master_db(synthetic_usb_db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "djmdHistory" in tables
        assert "djmdSongHistory" in tables
        assert "djmdContent" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# read_sessions
# ---------------------------------------------------------------------------

def test_read_sessions_returns_all_sessions(synthetic_usb_conn: sqlite3.Connection) -> None:
    """read_sessions returns all 5 sessions sorted by DateCreated ascending."""
    sessions = read_sessions(synthetic_usb_conn)
    assert len(sessions) == 5
    dates = [s.date_created for s in sessions]
    assert dates == sorted(dates), "Sessions must be sorted by date ascending"


def test_read_sessions_missing_table_raises_schema_error(state_db: sqlite3.Connection) -> None:
    """read_sessions raises SchemaError when djmdHistory table is absent."""
    with pytest.raises(SchemaError, match="djmdHistory"):
        read_sessions(state_db)


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_regardless_of_input_order() -> None:
    """Fingerprint is order-independent (uses sorted set)."""
    ids_a = ["C001", "C002", "C003"]
    ids_b = ["C003", "C001", "C002"]
    assert compute_fingerprint(ids_a) == compute_fingerprint(ids_b)


def test_fingerprint_is_16_hex_chars() -> None:
    """Fingerprint is exactly 16 hex characters."""
    fp = compute_fingerprint(["C001", "C002"])
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_different_for_different_track_sets() -> None:
    """Different track sets produce different fingerprints."""
    fp1 = compute_fingerprint(["C001", "C002"])
    fp2 = compute_fingerprint(["C001", "C003"])
    assert fp1 != fp2


def test_fingerprint_deduplicates_content_ids() -> None:
    """Duplicate content_ids are collapsed before hashing."""
    fp1 = compute_fingerprint(["C001", "C001", "C002"])
    fp2 = compute_fingerprint(["C001", "C002"])
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# ingest_from_connection - new sessions
# ---------------------------------------------------------------------------

def test_ingest_new_sessions_populates_sessions_table(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """Given 5 sessions in synthetic_usb_db and empty state.db, ingest produces 5 rows."""
    summary = ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    assert summary.sessions_found == 5
    assert summary.sessions_new == 5
    assert summary.sessions_skipped == 0
    row_count = state_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert row_count == 5


def test_ingest_skips_already_ingested_sessions(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """Second ingest of the same data produces 0 new sessions."""
    ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    # Re-open the same synthetic db connection (it's read-only; just reconnect)
    from tests.conftest import SYNTHETIC_DB_PATH
    second_conn = sqlite3.connect(str(SYNTHETIC_DB_PATH))
    second_conn.row_factory = sqlite3.Row
    summary2 = ingest_from_connection(second_conn, state_db, source_path="test")
    second_conn.close()
    assert summary2.sessions_new == 0
    assert summary2.sessions_skipped == 5


def test_ingest_fingerprint_survives_history_id_rename(
    state_db: sqlite3.Connection,
) -> None:
    """
    A session with a changed raw_history_id is still skipped if the fingerprint matches.

    Simulates an XDJ playlist rename (DESIGN D3).
    """
    # Build a minimal synthetic db with one session. Schema mirrors the
    # real rekordbox 6.x layout: ArtistID + KeyID on djmdContent, with
    # djmdArtist + djmdKey holding the actual names.
    schema = """
        CREATE TABLE djmdHistory (ID TEXT PRIMARY KEY, Seq INTEGER, Name TEXT,
            Attribute INTEGER, ParentID TEXT, DateCreated TEXT);
        CREATE TABLE djmdSongHistory (ID TEXT PRIMARY KEY, HistoryID TEXT,
            ContentID TEXT, TrackNo INTEGER);
        CREATE TABLE djmdArtist (ID TEXT PRIMARY KEY, Name TEXT);
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        CREATE TABLE djmdContent (ID TEXT PRIMARY KEY, Title TEXT, ArtistID TEXT,
            KeyID TEXT, BPM REAL, ColorID INTEGER, DateCreated TEXT);
        INSERT INTO djmdArtist VALUES ('A1', 'Artist A');
        INSERT INTO djmdKey VALUES ('K1', '5A');
        INSERT INTO djmdContent VALUES ('C001', 'Track 1', 'A1', 'K1', 120.0, 7, '2025-01-01');
    """
    conn_a = sqlite3.connect(":memory:")
    conn_a.row_factory = sqlite3.Row
    conn_a.executescript(schema + """
        INSERT INTO djmdHistory VALUES ('H001', 1, '2025.09.01', 0, 'root', '2025-09-01');
        INSERT INTO djmdSongHistory VALUES ('SH001', 'H001', 'C001', 1);
    """)
    ingest_from_connection(conn_a, state_db, source_path="test")
    conn_a.close()

    # Second db: same tracks but different history ID (rename)
    conn_b = sqlite3.connect(":memory:")
    conn_b.row_factory = sqlite3.Row
    conn_b.executescript(schema + """
        INSERT INTO djmdHistory VALUES ('H999', 1, 'Renamed Session', 0, 'root', '2025-09-01');
        INSERT INTO djmdSongHistory VALUES ('SH001', 'H999', 'C001', 1);
    """)
    summary = ingest_from_connection(conn_b, state_db, source_path="test")
    conn_b.close()

    assert summary.sessions_new == 0, "Renamed session should be skipped via fingerprint"
    assert summary.sessions_skipped == 1


def test_ingest_appearances_correct_order(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """appearances.track_no matches djmdSongHistory.TrackNo order."""
    ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    # Get first session
    session = state_db.execute(
        "SELECT session_id FROM sessions ORDER BY session_date ASC LIMIT 1"
    ).fetchone()
    rows = state_db.execute(
        "SELECT track_no FROM appearances WHERE session_id = ? ORDER BY track_no ASC",
        (session[0],),
    ).fetchall()
    track_nos = [r[0] for r in rows]
    assert track_nos == sorted(track_nos), "track_no must be monotonically non-decreasing"
    assert len(track_nos) > 0, "Session should have at least one appearance"


def test_ingest_tracks_upsert_increments_total_appearances(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """
    A track appearing in multiple sessions has total_appearances equal to that count.

    C001 appears in all 5 sessions, so total_appearances should be 5.
    """
    ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    row = state_db.execute(
        "SELECT total_appearances FROM tracks WHERE content_id = 'C001'"
    ).fetchone()
    assert row is not None, "C001 should be in tracks table"
    assert row[0] == 5, f"C001 expected 5 appearances, got {row[0]}"


def test_ingest_wal_lock_retry(
    synthetic_usb_db: Path,
    state_db: sqlite3.Connection,
) -> None:
    """
    Snapshot retry: first copy attempt raises OSError, second succeeds.

    Tests the Layer B retry logic in _snapshot_usb_db via mock.
    """
    import shutil
    import tempfile

    call_count = 0
    original_copy2 = shutil.copy2

    def mock_copy2(src: str, dst: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("Simulated WAL lock on first attempt")
        original_copy2(src, dst)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch("ingest.shutil.copy2", side_effect=mock_copy2):
            with patch("ingest.time.sleep"):  # Skip the 2-second wait
                result = ingest._snapshot_usb_db(synthetic_usb_db, tmp_dir)
        assert result.exists(), "Snapshot should exist after successful retry"
    assert call_count >= 2, "copy2 should have been called at least twice"


def test_ingest_wal_lock_raises_on_second_failure(
    synthetic_usb_db: Path,
) -> None:
    """If both snapshot attempts fail, WalLockError is raised."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch("ingest.shutil.copy2", side_effect=OSError("always locked")):
            with patch("ingest.time.sleep"):
                with pytest.raises(WalLockError):
                    ingest._snapshot_usb_db(synthetic_usb_db, tmp_dir)


def test_ingest_missing_tables_raises_schema_error(
    state_db: sqlite3.Connection,
) -> None:
    """Synthetic db with no djmdHistory raises SchemaError, not a raw sqlite3 error."""
    empty_conn = sqlite3.connect(":memory:")
    empty_conn.row_factory = sqlite3.Row
    with pytest.raises(SchemaError):
        ingest_from_connection(empty_conn, state_db, source_path="test")
    empty_conn.close()


def test_ingest_track_metadata_stored(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """After ingest, tracks table contains title, artist, bpm from djmdContent."""
    ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    row = state_db.execute(
        "SELECT title, artist, bpm FROM tracks WHERE content_id = 'C001'"
    ).fetchone()
    assert row is not None
    assert row["title"] == "Track 1"
    assert row["artist"] is not None
    assert row["bpm"] is not None


def test_ingest_summary_new_session_ids_populated(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """IngestSummary.new_session_ids contains the state.db session_id for each new session."""
    summary = ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    assert len(summary.new_session_ids) == 5
    # Each ID should be a valid session_id in the sessions table
    for sid in summary.new_session_ids:
        row = state_db.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        assert row is not None, f"session_id {sid} not found in sessions table"
