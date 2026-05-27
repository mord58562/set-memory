"""
Tests for rekordbox_writer.py.

We never touch Rob's real Mac master.db here. Two fixtures cover the
risky paths:
  - a plain-sqlite "Mac" db mirroring djmdContent/djmdArtist/djmdPlaylist/
    djmdSongPlaylist for create_playlist end-to-end coverage
  - subprocess.run mocking for is_rekordbox_running
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Generator
from unittest.mock import patch, MagicMock

import pytest

import ingest
import rekordbox_writer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_mac_db(tmp_path: Path) -> Path:
    """
    Plain sqlite "Mac master.db" mirroring the columns rekordbox_writer
    touches. Mac IDs are intentionally different from canonical IDs to
    prove the dedup_key bridge works.
    """
    db_path = tmp_path / "mac_master.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE djmdArtist (ID TEXT PRIMARY KEY, Name TEXT);
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY,
            Title TEXT,
            ArtistID TEXT,
            rb_local_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE djmdPlaylist (
            ID TEXT PRIMARY KEY,
            Name TEXT,
            ParentID TEXT,
            Attribute INTEGER,
            Seq INTEGER
        );
        CREATE TABLE djmdSongPlaylist (
            ID TEXT PRIMARY KEY,
            PlaylistID TEXT,
            ContentID TEXT,
            TrackNo INTEGER
        );
    """)
    # Three artists, four tracks. Track titles/artists are slightly
    # reformatted versus the Set-Memory side to prove dedup normalisation.
    conn.executemany(
        "INSERT INTO djmdArtist (ID, Name) VALUES (?, ?)",
        [("MA1", "DJ Bounce"), ("MA2", "Club Caviar"), ("MA3", "Woody McBride")],
    )
    conn.executemany(
        "INSERT INTO djmdContent (ID, Title, ArtistID) VALUES (?, ?, ?)",
        [
            # Same dedup_key as SM_A's "Speed It Up - Dr. Bounce Remix"
            # despite the punctuation/case drift.
            ("MAC100", "speed it up - DR. BOUNCE remix", "MA1"),
            ("MAC101", "Off the Ceiling", "MA3"),
            ("MAC102", "Mystery Track", "MA2"),
            # Mac-only track Set Memory hasn't seen; should be ignored.
            ("MAC103", "Mac Only Track", "MA1"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def state_db_with_tracks(tmp_path: Path) -> Path:
    """Real-shape state.db with a handful of Set-Memory canonical tracks."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ingest.ensure_schema(conn)

    def add(content_id: str, title: str, artist: str) -> None:
        key = ingest.dedup_key_for(title, artist)
        conn.execute(
            "INSERT INTO tracks (content_id, title, artist, dedup_key) "
            "VALUES (?, ?, ?, ?)",
            (content_id, title, artist, key),
        )

    add("SM_A", "Speed It Up - Dr. Bounce Remix", "DJ Bounce")
    add("SM_B", "Off the Ceiling", "Woody McBride")
    add("SM_C", "Mystery Track", "Club Caviar")
    # SM-only track: no Mac counterpart.
    add("SM_D", "Set Memory Exclusive", "Some Artist")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def backup_dir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect backup writes to a per-test temp dir."""
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(rekordbox_writer, "BACKUP_DIR", backup_dir)
    return backup_dir


# ---------------------------------------------------------------------------
# is_rekordbox_running
# ---------------------------------------------------------------------------

def test_is_rekordbox_running_true():
    with patch("rekordbox_writer.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert rekordbox_writer.is_rekordbox_running() is True


def test_is_rekordbox_running_false():
    with patch("rekordbox_writer.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert rekordbox_writer.is_rekordbox_running() is False


def test_is_rekordbox_running_no_pgrep():
    with patch("rekordbox_writer.subprocess.run", side_effect=FileNotFoundError()):
        assert rekordbox_writer.is_rekordbox_running() is False


def test_is_rekordbox_running_timeout():
    with patch(
        "rekordbox_writer.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=3),
    ):
        assert rekordbox_writer.is_rekordbox_running() is False


# ---------------------------------------------------------------------------
# mac_master_db_path
# ---------------------------------------------------------------------------

def test_mac_master_db_path_returns_path():
    p = rekordbox_writer.mac_master_db_path()
    assert isinstance(p, Path)
    # Should end with master.db regardless of which container path was hit.
    assert p.name == "master.db"


# ---------------------------------------------------------------------------
# create_playlist refuses when rekordbox is "running"
# ---------------------------------------------------------------------------

def test_create_playlist_refuses_when_rekordbox_running(
    fake_mac_db: Path, state_db_with_tracks: Path,
    backup_dir_tmp: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(rekordbox_writer, "_FAKE_REKORDBOX_RUNNING", True)
    monkeypatch.setattr(rekordbox_writer, "_FAKE_MAC_DB_PATH", fake_mac_db)
    with pytest.raises(rekordbox_writer.RekordboxLocked):
        rekordbox_writer.create_playlist(
            "Test Playlist", ["SM_A", "SM_B"], state_db_with_tracks,
        )


# ---------------------------------------------------------------------------
# create_playlist happy path
# ---------------------------------------------------------------------------

def test_create_playlist_writes_rows(
    fake_mac_db: Path, state_db_with_tracks: Path,
    backup_dir_tmp: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(rekordbox_writer, "_FAKE_REKORDBOX_RUNNING", False)
    monkeypatch.setattr(rekordbox_writer, "_FAKE_MAC_DB_PATH", fake_mac_db)
    # SM_D is the unmatched canonical (not present in fake_mac_db).
    result = rekordbox_writer.create_playlist(
        "Forgotten Favourites",
        ["SM_A", "SM_B", "SM_C", "SM_D"],
        state_db_with_tracks,
    )
    assert result["tracks_added"] == 3
    assert result["unmatched_count"] == 1
    assert Path(result["backup_path"]).exists()

    # Inspect the fake Mac db to confirm rows landed.
    conn = sqlite3.connect(str(fake_mac_db))
    conn.row_factory = sqlite3.Row
    pl_rows = conn.execute(
        "SELECT * FROM djmdPlaylist WHERE Name = 'Forgotten Favourites'"
    ).fetchall()
    assert len(pl_rows) == 1
    playlist_id = pl_rows[0]["ID"]
    assert playlist_id == result["playlist_id"]
    song_rows = conn.execute(
        "SELECT ContentID FROM djmdSongPlaylist WHERE PlaylistID = ? "
        "ORDER BY TrackNo",
        (playlist_id,),
    ).fetchall()
    assert [r["ContentID"] for r in song_rows] == ["MAC100", "MAC101", "MAC102"]
    conn.close()


def test_create_playlist_raises_when_no_matches(
    fake_mac_db: Path, state_db_with_tracks: Path,
    backup_dir_tmp: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(rekordbox_writer, "_FAKE_REKORDBOX_RUNNING", False)
    monkeypatch.setattr(rekordbox_writer, "_FAKE_MAC_DB_PATH", fake_mac_db)
    with pytest.raises(rekordbox_writer.TrackMatchProblem) as exc_info:
        rekordbox_writer.create_playlist(
            "Empty", ["SM_D"], state_db_with_tracks,
        )
    assert exc_info.value.dedup_keys_not_found == ["SM_D"]


# ---------------------------------------------------------------------------
# Backup behaviour
# ---------------------------------------------------------------------------

def test_backup_creates_timestamped_file(
    fake_mac_db: Path, backup_dir_tmp: Path,
):
    backup = rekordbox_writer._backup_master_db(fake_mac_db)
    assert backup.exists()
    assert backup.parent == backup_dir_tmp
    assert backup.name.startswith("master-")
    assert backup.suffix == ".db"
