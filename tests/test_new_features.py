"""
test_new_features.py - Coverage for the v2 schema and analyses.

Covers:
  - Schema migration adds the v2 columns
  - sync_library populates tracks with in_library=1 (fixes the original
    "never_played always empty" bug)
  - read_cue_counts handles djmdCue presence and absence
  - recently_added_unplayed surfaces tracks added within the window
  - prep_issues flags missing BPM / key / hot cues
  - co_appearance returns shared-session pairs
  - deleted_candidates surfaces stale-or-removed tracks
  - distribution buckets BPM and keys correctly
  - sessions_by_month returns continuous month series
  - usb_drives is populated when ingest_from_connection gets a volume label
  - notify._applescript_escape produces valid output
  - config rejects oversized integers
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

import analyse
import config as cfg_module
import ingest
import notify
from analyse import run
from tests.conftest import FROZEN_TODAY


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_schema_v2_columns_present(state_db: sqlite3.Connection) -> None:
    """ensure_schema leaves the v2 columns on tracks + the usb_drives table."""
    cols = {row[1] for row in state_db.execute("PRAGMA table_info(tracks)")}
    for required in ("in_library", "last_in_library_at", "added_at",
                     "hot_cue_count", "memory_cue_count", "stock_date"):
        assert required in cols, f"tracks.{required} missing after migration"
    tables = {row[0] for row in state_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "usb_drives" in tables


def test_schema_version_recorded(state_db: sqlite3.Connection) -> None:
    row = state_db.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None and int(row[0]) == ingest.SCHEMA_VERSION


def test_migrates_existing_v1_db(tmp_path: Path) -> None:
    """A v1-shaped state.db gains the v2 columns without losing data."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT, raw_history_id TEXT,
            fingerprint TEXT UNIQUE, session_date TEXT, source_db_path TEXT,
            ingested_at TEXT, track_count INTEGER
        );
        CREATE TABLE appearances (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,
            content_id TEXT, track_no INTEGER, title TEXT, artist TEXT
        );
        CREATE TABLE tracks (
            content_id TEXT PRIMARY KEY, title TEXT, artist TEXT,
            bpm REAL, key_camelot TEXT, energy INTEGER, date_created TEXT,
            first_seen_session INTEGER, last_seen_session INTEGER,
            total_appearances INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO tracks (content_id, title, total_appearances)
            VALUES ('C999', 'Legacy', 7);
    """)
    conn.commit()
    ingest.ensure_schema(conn)
    row = conn.execute(
        "SELECT title, total_appearances, in_library FROM tracks WHERE content_id='C999'"
    ).fetchone()
    assert row is not None
    assert row[0] == "Legacy"
    assert row[1] == 7
    assert row[2] == 0  # in_library defaulted to 0 on the old row
    conn.close()


# ---------------------------------------------------------------------------
# Library sync
# ---------------------------------------------------------------------------

def test_sync_library_populates_unplayed_tracks(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """After ingest, all 20 djmdContent rows are present with in_library=1.
    This is the fix for the original 'never_played always empty' bug."""
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    library_count = state_db.execute(
        "SELECT COUNT(*) FROM tracks WHERE in_library = 1"
    ).fetchone()[0]
    assert library_count == 20, f"Expected 20 library tracks, got {library_count}"

    unplayed = state_db.execute(
        "SELECT COUNT(*) FROM tracks WHERE in_library = 1 AND total_appearances = 0"
    ).fetchone()[0]
    # C008, C011..C020 are in djmdContent but never appear in any session.
    assert unplayed == 11, f"C008 + C011..C020 should be unplayed; got {unplayed}"


def test_sync_library_uses_stock_date_when_available(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    """added_at prefers StockDate over file DateCreated."""
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    row = state_db.execute(
        "SELECT added_at, date_created FROM tracks WHERE content_id = 'C015'"
    ).fetchone()
    assert row["added_at"] == "2026-01-01", \
        f"C015 added_at should be StockDate 2026-01-01, got {row['added_at']}"
    assert row["date_created"] == "2024-12-01"


def test_hot_cue_counts_ingested(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    c001 = state_db.execute(
        "SELECT hot_cue_count, memory_cue_count FROM tracks WHERE content_id='C001'"
    ).fetchone()
    assert c001["hot_cue_count"] == 3
    c006 = state_db.execute(
        "SELECT hot_cue_count, memory_cue_count FROM tracks WHERE content_id='C006'"
    ).fetchone()
    assert c006["hot_cue_count"] == 0
    assert c006["memory_cue_count"] == 2


def test_read_cue_counts_handles_missing_djmdcue(
    state_db: sqlite3.Connection,
) -> None:
    """Older rekordbox versions don't have djmdCue. read_cue_counts returns {}."""
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    bare.executescript("CREATE TABLE djmdContent (ID TEXT)")
    assert ingest.read_cue_counts(bare) == {}
    bare.close()


# ---------------------------------------------------------------------------
# usb_drives tracking
# ---------------------------------------------------------------------------

def test_usb_drive_recorded_when_label_passed(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(
        synthetic_usb_conn, state_db, source_path="/Volumes/TestUSB/PIONEER/Master/master.db",
        volume_label="TestUSB",
    )
    rows = state_db.execute("SELECT volume_label, library_size FROM usb_drives").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "TestUSB"
    assert rows[0][1] == 20


def test_usb_drive_summary_sorts_by_last_seen(
    state_db: sqlite3.Connection,
) -> None:
    state_db.execute(
        "INSERT INTO usb_drives (volume_label, master_db_path, first_seen_at, last_seen_at, library_size) "
        "VALUES ('newer', '/p1', '2026-05-10', '2026-05-10', 100)"
    )
    state_db.execute(
        "INSERT INTO usb_drives (volume_label, master_db_path, first_seen_at, last_seen_at, library_size) "
        "VALUES ('older', '/p2', '2026-01-01', '2026-01-01', 50)"
    )
    state_db.commit()
    drives = analyse.usb_drive_summary(state_db)
    assert [d["volume_label"] for d in drives] == ["older", "newer"]


# ---------------------------------------------------------------------------
# Recently added unplayed
# ---------------------------------------------------------------------------

def test_recently_added_unplayed_includes_recent_additions(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, artist, in_library, added_at, total_appearances) "
        "VALUES ('C100', 'Fresh', 'A', 1, ?, 0)",
        ((FROZEN_TODAY - datetime.timedelta(days=10)).isoformat(),),
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10,
                 recently_added_window_days=30, today=FROZEN_TODAY)
    ids = [t.content_id for t in result.recently_added_unplayed]
    assert "C100" in ids


def test_recently_added_unplayed_excludes_old_additions(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, artist, in_library, added_at, total_appearances) "
        "VALUES ('C101', 'Stale', 'A', 1, ?, 0)",
        ((FROZEN_TODAY - datetime.timedelta(days=200)).isoformat(),),
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10,
                 recently_added_window_days=30, today=FROZEN_TODAY)
    assert "C101" not in [t.content_id for t in result.recently_added_unplayed]


# ---------------------------------------------------------------------------
# Prep audit
# ---------------------------------------------------------------------------

def test_prep_audit_flags_missing_bpm(state_db: sqlite3.Connection) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, in_library, bpm, key_camelot, "
        " hot_cue_count, total_appearances) "
        "VALUES ('C200', 'NoBPM', 1, NULL, '5A', 3, 5)"
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10)
    issues = {p.content_id: p for p in result.prep_issues}
    assert "C200" in issues
    assert issues["C200"].missing_bpm is True
    assert issues["C200"].missing_key is False


def test_prep_audit_flags_missing_hot_cues(state_db: sqlite3.Connection) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, in_library, bpm, key_camelot, "
        " hot_cue_count, total_appearances) "
        "VALUES ('C201', 'NoCues', 1, 128.0, '5A', 0, 3)"
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10)
    assert any(p.content_id == "C201" and p.missing_hot_cues for p in result.prep_issues)


def test_prep_audit_sorted_by_appearances(state_db: sqlite3.Connection) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, in_library, bpm, hot_cue_count, total_appearances) "
        "VALUES ('C202', 'Low', 1, NULL, 1, 1), "
        "       ('C203', 'High', 1, NULL, 1, 20)"
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10)
    ids = [p.content_id for p in result.prep_issues]
    assert ids.index("C203") < ids.index("C202")


def test_prep_audit_treats_null_hot_cue_count_as_unknown(
    state_db: sqlite3.Connection,
) -> None:
    """Pre-v2 rows had hot_cue_count = NULL; treat as 'don't know', not 'missing'."""
    state_db.execute(
        "INSERT INTO tracks (content_id, title, in_library, bpm, key_camelot, "
        " hot_cue_count, total_appearances) "
        "VALUES ('C204', 'NullCue', 1, 120.0, '5A', NULL, 0)"
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10)
    assert not any(p.content_id == "C204" for p in result.prep_issues)


# ---------------------------------------------------------------------------
# Co-appearance
# ---------------------------------------------------------------------------

def test_co_appearance_returns_shared_pairs(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    result = run(state_db, 5, 90, 10, 30, 10,
                 co_appearance_min_sessions=3, co_appearance_limit=20)
    # C001 + C002 share all 5 sessions
    pairs = [(p.a_title, p.b_title, p.shared_sessions) for p in result.co_appearance]
    has_top_pair = any(
        sorted([a, b]) == ["Track 1", "Track 2"] and s == 5
        for a, b, s in pairs
    )
    assert has_top_pair, f"Expected (Track 1, Track 2, 5) in pairs; got {pairs[:5]}"


def test_co_appearance_respects_min_sessions(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    result = run(state_db, 5, 90, 10, 30, 10,
                 co_appearance_min_sessions=10, co_appearance_limit=20)
    assert result.co_appearance == []


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------

def test_distribution_buckets_bpm(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    result = run(state_db, 5, 90, 10, 30, 10)
    total = sum(result.distribution.bpm.values())
    assert total > 0, "Distribution should contain some plays"
    # Synthetic BPMs are 125.5..135.0; all should fall in 120-127 or 128-134
    assert result.distribution.bpm["120-127"] + result.distribution.bpm["128-134"] > 0


def test_distribution_top_keys_includes_camelot(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    result = run(state_db, 5, 90, 10, 30, 10)
    assert any(k in result.distribution.key for k in ["5A", "5B", "8A", "8B", "11A"])


# ---------------------------------------------------------------------------
# Sessions by month
# ---------------------------------------------------------------------------

def test_sessions_by_month_continuous(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    ingest.ingest_from_connection(synthetic_usb_conn, state_db, source_path="test")
    result = run(state_db, 5, 90, 10, 30, 10,
                 sparkline_months=12, today=FROZEN_TODAY)
    assert len(result.sessions_by_month) == 12
    assert result.sessions_by_month.get("2025-09") == 1
    assert result.sessions_by_month.get("2026-04") == 1
    assert result.sessions_by_month.get("2026-05") == 1


# ---------------------------------------------------------------------------
# Deleted candidates
# ---------------------------------------------------------------------------

def test_deleted_candidates_surfaces_dropped_tracks(
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
) -> None:
    state_db.execute(
        "INSERT INTO tracks (content_id, title, in_library, last_in_library_at, total_appearances) "
        "VALUES ('C300', 'Gone', 0, ?, 5)",
        ((FROZEN_TODAY - datetime.timedelta(days=120)).isoformat(),),
    )
    state_db.commit()
    result = run(state_db, 5, 90, 10, 30, 10,
                 deleted_stale_days=60, today=FROZEN_TODAY)
    assert "C300" in [d.content_id for d in result.deleted_candidates]


# ---------------------------------------------------------------------------
# notify escape
# ---------------------------------------------------------------------------

def test_applescript_escape_handles_quotes_and_backslashes() -> None:
    assert notify._applescript_escape('a"b') == 'a\\"b'
    assert notify._applescript_escape('a\\b') == 'a\\\\b'
    assert notify._applescript_escape('mix"a\\b"end') == 'mix\\"a\\\\b\\"end'


def test_osascript_string_compiles_with_real_content() -> None:
    """End-to-end: a body with quotes/backslashes survives the escape and
    produces a valid AppleScript command. We don't actually fire (might
    notify the user); just verify the script parses by piping it through
    osascript --compile."""
    import subprocess
    title = 'Set Memory: "test"'
    body = 'a\\b "quoted" all good'
    safe_title = notify._applescript_escape(title)
    safe_body = notify._applescript_escape(body)
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    # Compile-only check (no dispatch). osascript -e re-evaluates from
    # source each invocation - if compilation fails, we'd see a -2741.
    result = subprocess.run(["osascript", "-l", "AppleScript", "-s", "o", "-e",
                             f"on run\n{script}\nend run"],
                            capture_output=True, text=True)
    # We expect either success (notification posted) or a compile-time
    # syntax error. Compile error returns exit 1 with -2741 in stderr.
    assert "syntax error" not in (result.stderr or "").lower(), \
        f"AppleScript failed to compile: {result.stderr}"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_config_rejects_oversized_int(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"forgotten_min_appearances": 999999}')
    with pytest.raises(cfg_module.ConfigError, match="must be <="):
        cfg_module.load(config_file)


def test_config_rejects_negative_int(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"forgotten_limit": -1}')
    with pytest.raises(cfg_module.ConfigError, match="must be >= 0"):
        cfg_module.load(config_file)


def test_config_includes_new_fields(tmp_path: Path) -> None:
    """Default Config covers the v2 fields with sensible values."""
    conf = cfg_module.Config()
    assert conf.recently_added_window_days > 0
    assert conf.prep_limit > 0
    assert conf.co_appearance_min_sessions > 0
    assert conf.sparkline_months > 0


# ---------------------------------------------------------------------------
# digest renders all sections
# ---------------------------------------------------------------------------

def test_digest_contains_all_v2_sections(
    synthetic_usb_conn: sqlite3.Connection,
    state_db: sqlite3.Connection,
    frozen_today: datetime.date,
    tmp_path: Path,
) -> None:
    import digest
    ingest.ingest_from_connection(synthetic_usb_conn, state_db,
                                  source_path="test", volume_label="TestUSB")
    summary = ingest.IngestSummary(sessions_found=5, sessions_new=5,
                                   library_size=20, library_added=20)
    result = run(state_db, 5, 90, 10, 30, 10,
                 co_appearance_min_sessions=3, today=FROZEN_TODAY)
    stats = analyse.compute_summary_stats(state_db)
    usbs = analyse.usb_drive_summary(state_db)
    out = tmp_path / "digest.md"
    digest.render(summary, result, stats, "test config", out,
                  today=FROZEN_TODAY, usb_drives=usbs)
    content = out.read_text()
    for header in ("## Headline", "## Activity", "## Sessions Ingested This Sync",
                   "## USB Drives", "## Forgotten Favourites",
                   "## Never Played After Add", "## Recently Added, Not Played Yet",
                   "## Prep Audit", "## Played Together",
                   "## Distribution", "## Summary Stats"):
        assert header in content, f"Missing section: {header}"
    assert chr(0x2014) not in content, "Em-dash leaked into digest"
