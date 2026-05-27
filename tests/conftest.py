"""
conftest.py - Shared fixtures for Set Memory tests.

Plain sqlite3 throughout. No SQLCipher, no pyrekordbox dependency. The
synthetic_master_db fixture mirrors the djmd* schema used by rekordbox 6.x
(normalised artist + key tables, optional djmdCue table for prep audit).

Fixtures:
  synthetic_usb_db    - path to fixtures/synthetic_master.db (session-scoped)
  synthetic_usb_conn  - open sqlite3 connection to the synthetic db
  state_db            - in-memory state.db with schema applied, fresh per test
  default_config      - Config dataclass with known thresholds
  frozen_today        - patches datetime.date.today() to 2026-05-12

Data layout (FROZEN_TODAY = 2026-05-12):
  5 sessions, 20 tracks in djmdContent
  C001..C004 in all 5 sessions
  C005..C008 in sessions 1-3 only
  C009/C010 in sessions 4-5 only
  C011..C015 in djmdContent only, added 2026-01-01 (old, unplayed)
  C016..C020 in djmdContent only, added 2026-04-20 (recent, unplayed)
  djmdCue: C001..C005 have 3 hot cues each; C006..C010 have memory cues only;
    C011..C020 have no cue rows (prep-audit candidates)
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ingest
from config import Config

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SYNTHETIC_DB_PATH = FIXTURES_DIR / "synthetic_master.db"

FROZEN_TODAY = datetime.date(2026, 5, 12)


def _build_synthetic_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE djmdHistory (
            ID TEXT PRIMARY KEY, Seq INTEGER, Name TEXT, Attribute INTEGER,
            ParentID TEXT, DateCreated TEXT
        );
        CREATE TABLE djmdSongHistory (
            ID TEXT PRIMARY KEY, HistoryID TEXT NOT NULL,
            ContentID TEXT, TrackNo INTEGER
        );
        CREATE TABLE djmdArtist (ID TEXT PRIMARY KEY, Name TEXT);
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY, Title TEXT, ArtistID TEXT, KeyID TEXT,
            BPM REAL, ColorID INTEGER, DateCreated TEXT, StockDate TEXT
        );
        CREATE TABLE djmdCue (
            ID TEXT PRIMARY KEY, ContentID TEXT, Kind INTEGER, InMsec INTEGER
        );
    """)

    cur.executemany(
        "INSERT INTO djmdArtist (ID, Name) VALUES (?, ?)",
        [(f"A{n}", f"Artist {n}") for n in range(1, 6)],
    )
    # Keys cover several Camelot wheel positions so distribution stats are nontrivial.
    keys = [("K001", "5A"), ("K002", "5B"), ("K003", "8A"), ("K004", "8B"), ("K005", "11A")]
    cur.executemany("INSERT INTO djmdKey (ID, ScaleName) VALUES (?, ?)", keys)

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

    content_rows = []
    for i in range(1, 21):
        cid = f"C{i:03d}"
        title = f"Track {i}"
        artist_id = f"A{(i % 5) + 1}"
        key_id = f"K{(i % 5) + 1:03d}"
        bpm = 125.0 + i * 0.5
        # Sprinkle in tracks with NULL BPM/key so prep audit has work to do.
        if i in (7, 13):
            bpm = None
        if i in (8, 14):
            key_id = None
        energy = (i % 10) + 1
        if i <= 10:
            date_created = f"2025-0{max(1, i % 9 + 1):02d}-01"
            stock_date = date_created
        elif i <= 15:
            date_created = "2024-12-01"  # file older than library-add
            stock_date = "2026-01-01"    # actual library-add date
        else:
            date_created = "2024-12-01"
            stock_date = "2026-04-20"
        content_rows.append((cid, title, artist_id, key_id, bpm, energy, date_created, stock_date))
    cur.executemany(
        "INSERT INTO djmdContent (ID, Title, ArtistID, KeyID, BPM, ColorID, DateCreated, StockDate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        content_rows,
    )

    # djmdCue: hot cues for C001..C005 (Kind=1), memory cues for C006..C010 (Kind=0).
    cue_rows = []
    cue_id = 1
    for cid_num in range(1, 6):
        for _ in range(3):
            cue_rows.append((f"CU{cue_id:04d}", f"C{cid_num:03d}", 1, 0))
            cue_id += 1
    for cid_num in range(6, 11):
        for _ in range(2):
            cue_rows.append((f"CU{cue_id:04d}", f"C{cid_num:03d}", 0, 0))
            cue_id += 1
    cur.executemany(
        "INSERT INTO djmdCue (ID, ContentID, Kind, InMsec) VALUES (?, ?, ?, ?)",
        cue_rows,
    )

    sh_rows = []
    sh_id = 1

    def add_song(history_id: str, content_id: str, track_no: int) -> None:
        nonlocal sh_id
        sh_rows.append((f"SH{sh_id:04d}", history_id, content_id, track_no))
        sh_id += 1

    for sess_idx, hid in enumerate(["H001", "H002", "H003"]):
        for track_no, cid in enumerate(["C001", "C002", "C003", "C004"], start=1):
            add_song(hid, cid, track_no)
        extra_cid = f"C{5 + sess_idx:03d}"
        add_song(hid, extra_cid, 5)

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


@pytest.fixture(scope="session")
def synthetic_usb_db() -> Path:
    _build_synthetic_db(SYNTHETIC_DB_PATH)
    return SYNTHETIC_DB_PATH


@pytest.fixture
def synthetic_usb_conn(synthetic_usb_db: Path) -> Generator[sqlite3.Connection, None, None]:
    uri = f"file:{synthetic_usb_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def state_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ingest.ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def default_config() -> Config:
    return Config()


@pytest.fixture
def frozen_today() -> Generator[datetime.date, None, None]:
    fixed = FROZEN_TODAY
    with (
        patch("analyse.datetime") as mock_analyse_dt,
        patch("digest.datetime") as mock_digest_dt,
    ):
        mock_analyse_dt.date.today.return_value = fixed
        mock_analyse_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_analyse_dt.date.fromisoformat.side_effect = datetime.date.fromisoformat
        mock_analyse_dt.timedelta = datetime.timedelta

        mock_digest_dt.date.today.return_value = fixed
        mock_digest_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_digest_dt.timedelta = datetime.timedelta

        yield fixed
