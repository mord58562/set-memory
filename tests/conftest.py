"""
conftest.py - Shared fixtures for Set Memory tests.

All fixtures use plain sqlite3 (no SQLCipher, no pyrekordbox dependency).
The synthetic_master_db fixture mirrors the djmd* schema from the real USB db.

Fixture inventory:
  synthetic_usb_db    - path to fixtures/synthetic_master.db (built once per session)
  synthetic_usb_conn  - open sqlite3 connection to the synthetic db (read-only copy per test)
  state_db            - in-memory state.db with schema applied, fresh per test function
  default_config      - Config dataclass with known thresholds
  frozen_today        - patches datetime.date.today() to 2026-05-12 for deterministic recency tests
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

# Make the project root importable from tests/
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ingest
from config import Config

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SYNTHETIC_DB_PATH = FIXTURES_DIR / "synthetic_master.db"

# ---------------------------------------------------------------------------
# Frozen date used across all recency-sensitive tests
# ---------------------------------------------------------------------------
FROZEN_TODAY = datetime.date(2026, 5, 12)


# ---------------------------------------------------------------------------
# Synthetic USB db construction
# ---------------------------------------------------------------------------

def _build_synthetic_db(path: Path) -> None:
    """
    Build a plain SQLite file at `path` mirroring the djmd* schema.

    Data layout:
      5 sessions (djmdHistory rows)
      Sessions 1-3: 4 tracks each (djmdSongHistory rows)
      Sessions 4-5: 2 tracks each
      Total djmdContent rows: 20 (IDs 'C001'..'C020')

    Overlap design (for forgotten / never-played testing):
      C001..C004: appear in all 5 sessions -> total_appearances = 5
      C005..C008: appear in sessions 1-3 -> total_appearances = 3
      C009..C010: appear in sessions 4-5 -> total_appearances = 2
      C011..C020: in djmdContent but never in any session -> never-played candidates

    Session dates (for recency testing with FROZEN_TODAY = 2026-05-12):
      Session 1: 2025-09-01  (>= 90 days before frozen today = 2026-02-11; well before)
      Session 2: 2025-10-01
      Session 3: 2025-11-01
      Session 4: 2026-04-01  (41 days before frozen today; recent)
      Session 5: 2026-05-01  (11 days before frozen today; very recent)

    djmdContent date_created for never-played tracks:
      C011..C015: 2026-01-01  (> 30 days before frozen today; qualify for never-played)
      C016..C020: 2026-04-20  (22 days before frozen today; too recent, < 30 days)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE djmdHistory (
            ID          TEXT PRIMARY KEY,
            Seq         INTEGER,
            Name        TEXT,
            Attribute   INTEGER,
            ParentID    TEXT,
            DateCreated TEXT
        );

        CREATE TABLE djmdSongHistory (
            ID          TEXT PRIMARY KEY,
            HistoryID   TEXT NOT NULL,
            ContentID   TEXT,
            TrackNo     INTEGER
        );

        CREATE TABLE djmdContent (
            ID          TEXT PRIMARY KEY,
            Title       TEXT,
            ArtistName  TEXT,
            BPM         REAL,
            Tonality    TEXT,
            ColorID     INTEGER,
            DateCreated TEXT
        );
    """)

    # Sessions
    sessions = [
        ("H001", 1, "2025.09.01", "2025-09-01"),
        ("H002", 2, "2025.10.01", "2025-10-01"),
        ("H003", 3, "2025.11.01", "2025-11-01"),
        ("H004", 4, "2026.04.01", "2026-04-01"),
        ("H005", 5, "2026.05.01", "2026-05-01"),
    ]
    cur.executemany(
        "INSERT INTO djmdHistory (ID, Seq, Name, Attribute, ParentID, DateCreated) "
        "VALUES (?, ?, ?, 0, 'root', ?)",
        sessions,
    )

    # djmdContent rows (20 tracks)
    content_rows = []
    for i in range(1, 21):
        cid = f"C{i:03d}"
        title = f"Track {i}"
        artist = f"Artist {(i % 5) + 1}"
        bpm = 125.0 + i * 0.5
        key = "5A"
        energy = (i % 10) + 1
        # Never-played tracks: C011-C015 old enough, C016-C020 too recent
        if i <= 10:
            date_created = f"2025-0{max(1, i % 9 + 1):02d}-01"
        elif i <= 15:
            date_created = "2026-01-01"
        else:
            date_created = "2026-04-20"
        content_rows.append((cid, title, artist, bpm, key, energy, date_created))
    cur.executemany(
        "INSERT INTO djmdContent (ID, Title, ArtistName, BPM, Tonality, ColorID, DateCreated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        content_rows,
    )

    # djmdSongHistory rows
    sh_rows = []
    sh_id = 1

    def add_song(history_id: str, content_id: str, track_no: int) -> None:
        nonlocal sh_id
        sh_rows.append((f"SH{sh_id:04d}", history_id, content_id, track_no))
        sh_id += 1

    # Sessions 1-3: C001-C004 + session-specific tracks
    for sess_idx, hid in enumerate(["H001", "H002", "H003"]):
        for track_no, cid in enumerate(["C001", "C002", "C003", "C004"], start=1):
            add_song(hid, cid, track_no)
        # Session-specific track (C005-C008, one per session for sessions 1-3)
        extra_cid = f"C{5 + sess_idx:03d}"
        add_song(hid, extra_cid, 5)

    # Sessions 4-5: C001-C004 + C009/C010
    for sess_idx, hid in enumerate(["H004", "H005"]):
        for track_no, cid in enumerate(["C001", "C002"], start=1):
            add_song(hid, cid, track_no)
        extra_cid = f"C{9 + sess_idx:03d}"
        add_song(hid, extra_cid, 3)

    cur.executemany(
        "INSERT INTO djmdSongHistory (ID, HistoryID, ContentID, TrackNo) VALUES (?, ?, ?, ?)",
        sh_rows,
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def synthetic_usb_db() -> Path:
    """
    Build (or reuse) the synthetic USB db at fixtures/synthetic_master.db.

    Rebuilds if the file does not exist. Use scope="session" because the db
    content is read-only - no fixture mutates it.
    """
    _build_synthetic_db(SYNTHETIC_DB_PATH)
    return SYNTHETIC_DB_PATH


@pytest.fixture
def synthetic_usb_conn(synthetic_usb_db: Path) -> Generator[sqlite3.Connection, None, None]:
    """
    Open a plain sqlite3 connection to the synthetic USB db.

    Each test gets its own connection (opened in read-only mode via URI).
    Closed automatically after the test.
    """
    uri = f"file:{synthetic_usb_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def state_db() -> Generator[sqlite3.Connection, None, None]:
    """
    In-memory state.db with schema applied, fresh for each test function.

    Never shared between tests - each call returns a brand-new in-memory db.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ingest.ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def default_config() -> Config:
    """Config dataclass with known, deterministic thresholds for testing."""
    return Config(
        forgotten_min_appearances=5,
        forgotten_days_since_last=90,
        forgotten_limit=10,
        never_played_min_days_since_add=30,
        never_played_limit=10,
        usb_uuid="deadbeef00000000deadbeef00000000",
        usb_pioneer_path="/Volumes/TEST_USB/PIONEER",
        state_db_path="state.db",
        digest_path="digest.md",
    )


@pytest.fixture
def frozen_today() -> Generator[datetime.date, None, None]:
    """
    Patch datetime.date.today() in analyse and digest modules to return FROZEN_TODAY.

    Yields the frozen date so tests can reference it directly.
    """
    fixed = FROZEN_TODAY
    with (
        patch("analyse.datetime") as mock_analyse_dt,
        patch("digest.datetime") as mock_digest_dt,
    ):
        # Preserve other datetime attributes while patching today()
        mock_analyse_dt.date.today.return_value = fixed
        mock_analyse_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_analyse_dt.timedelta = datetime.timedelta

        mock_digest_dt.date.today.return_value = fixed
        mock_digest_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_digest_dt.timedelta = datetime.timedelta

        yield fixed
