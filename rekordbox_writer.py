"""
rekordbox_writer.py - Push Set Memory track lists into the Mac rekordbox 6
library as new playlists.

Why this module exists
----------------------
Set Memory tracks come from USB exports. Each USB carries its own
PIONEER/Master/master.db with its own content IDs. The Mac desktop
rekordbox library is a *different* master.db at
~/Library/Pioneer/rekordbox/master.db, with different content IDs for the
same music files. To create a useful playlist in the Mac library we have
to translate Set Memory canonical IDs into Mac djmdContent IDs.

The bridge is the dedup_key already stored on every Set Memory track:
normalised (title, artist). We compute the same key for every row in the
Mac library and match on it.

Constraints
-----------
- rekordbox 6.x only. pyrekordbox 0.4.4 cannot write rekordbox 7.x.
- rekordbox.app must be quit while we write (SQLCipher exclusive lock).
- master.db is precious - back it up to rekordbox-backups/ before the
  first write of any session.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import ingest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RekordboxLocked(Exception):
    """rekordbox.app is running; the SQLCipher master.db is locked."""


class RekordboxNotFound(Exception):
    """No Mac rekordbox 6 master.db could be located."""


class TrackMatchProblem(Exception):
    """Caller asked for tracks that don't map to anything in the Mac library."""

    def __init__(self, dedup_keys_not_found: list[str]):
        self.dedup_keys_not_found = list(dedup_keys_not_found)
        super().__init__(
            f"{len(self.dedup_keys_not_found)} track(s) could not be matched "
            f"to the Mac rekordbox library."
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent
BACKUP_DIR = PROJECT_ROOT / "rekordbox-backups"


def mac_master_db_path() -> Path:
    """
    Locate the desktop rekordbox 6 master.db on this Mac. Returns the
    standard ~/Library/Pioneer/rekordbox/master.db first; falls back to
    the Mac App Store sandbox container if the standard path is absent.
    Does NOT raise if missing - caller decides.
    """
    home = Path(os.path.expanduser("~"))
    standard = home / "Library" / "Pioneer" / "rekordbox" / "master.db"
    if standard.exists():
        return standard
    sandboxed = (
        home / "Library" / "Containers" / "com.pioneerdj.rekordbox"
        / "Data" / "Library" / "Pioneer" / "rekordbox" / "master.db"
    )
    if sandboxed.exists():
        return sandboxed
    return standard  # return the canonical-looking path so error messages are helpful


# ---------------------------------------------------------------------------
# Process detection
# ---------------------------------------------------------------------------

def is_rekordbox_running() -> bool:
    """True if a process named 'rekordbox' is alive. Uses pgrep -x for an
    exact name match; quiet on systems without pgrep (returns False)."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "rekordbox"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Track ID mapping
# ---------------------------------------------------------------------------

def _open_mac_db_readonly(mac_db_path: Path) -> sqlite3.Connection:
    """
    Open the Mac master.db read-only via pyrekordbox (handles SQLCipher).
    We use it purely to read djmdContent + djmdArtist for dedup_key
    computation. Returns a raw sqlite3 connection on the decrypted db.
    """
    from pyrekordbox import Rekordbox6Database

    if not mac_db_path.exists():
        raise RekordboxNotFound(
            f"No rekordbox 6 master.db at {mac_db_path}. Is rekordbox 6 installed?"
        )
    db = Rekordbox6Database(path=str(mac_db_path))
    # The internal SQLAlchemy engine has the decrypted handle.
    raw = db.engine.raw_connection().driver_connection
    return raw, db


def map_canonical_to_mac(state_db_path: Path) -> dict[str, str]:
    """
    Build {set_memory_canonical_content_id: mac_djmdContent.ID} by joining
    on dedup_key. Tracks without a dedup_key (missing title or artist) or
    without a Mac counterpart are simply absent from the result.
    """
    state_conn = sqlite3.connect(str(state_db_path))
    state_conn.row_factory = sqlite3.Row
    try:
        rows = state_conn.execute(
            "SELECT content_id, dedup_key FROM tracks "
            "WHERE dedup_key IS NOT NULL"
        ).fetchall()
    finally:
        state_conn.close()
    state_by_key: dict[str, str] = {}
    for r in rows:
        # If multiple Set Memory tracks share a key, keep the first; the
        # ingest pipeline already collapses duplicates so this is rare.
        state_by_key.setdefault(r["dedup_key"], r["content_id"])

    mac_db_path = mac_master_db_path()
    raw, db = _open_mac_db_readonly(mac_db_path)
    try:
        cursor = raw.execute(
            "SELECT c.ID, c.Title, a.Name AS ArtistName "
            "FROM djmdContent c LEFT JOIN djmdArtist a ON c.ArtistID = a.ID "
            "WHERE (c.rb_local_deleted IS NULL OR c.rb_local_deleted = 0)"
        )
        mapping: dict[str, str] = {}
        for mac_id, title, artist in cursor:
            key = ingest.dedup_key_for(title, artist)
            if not key:
                continue
            sm_id = state_by_key.get(key)
            if sm_id is not None:
                mapping[sm_id] = str(mac_id)
        return mapping
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _backup_master_db(mac_db_path: Path) -> Path:
    """
    Copy master.db to rekordbox-backups/master-<timestamp>.db (preserves
    permissions via shutil.copy2). Raises if the copy fails.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"master-{timestamp}.db"
    shutil.copy2(str(mac_db_path), str(dest))
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"Backup verification failed for {dest}")
    return dest


# ---------------------------------------------------------------------------
# Playlist creation
# ---------------------------------------------------------------------------

# Test hook: set to True to simulate rekordbox running without invoking pgrep.
_FAKE_REKORDBOX_RUNNING = False

# Test hook: when set, create_playlist treats this path as the Mac master.db
# and skips the SQLCipher path entirely (writes via plain sqlite3).
_FAKE_MAC_DB_PATH: Optional[Path] = None


def create_playlist(
    name: str,
    set_memory_content_ids: list[str],
    state_db_path: Path,
) -> dict:
    """
    Create a top-level playlist in the Mac rekordbox library containing
    the Mac djmdContent rows matching the given Set Memory canonical IDs.

    Returns {"playlist_id", "tracks_added", "unmatched_count",
             "backup_path"}.

    Raises:
      RekordboxLocked    - rekordbox.app is running.
      RekordboxNotFound  - no master.db located.
      TrackMatchProblem  - none of the requested tracks mapped to the Mac
                           library (zero matches is treated as a hard
                           error so the caller can surface it).
    """
    if _FAKE_REKORDBOX_RUNNING or is_rekordbox_running():
        raise RekordboxLocked(
            "rekordbox is running. Quit rekordbox before creating a playlist."
        )

    # Test mode: fixture-driven plain sqlite path.
    if _FAKE_MAC_DB_PATH is not None:
        return _create_playlist_plain_sqlite(
            _FAKE_MAC_DB_PATH, name, set_memory_content_ids, state_db_path,
        )

    mac_db_path = mac_master_db_path()
    if not mac_db_path.exists():
        raise RekordboxNotFound(f"No rekordbox 6 master.db at {mac_db_path}.")

    # Translate IDs first so we can fail fast on zero matches.
    canonical_to_mac = map_canonical_to_mac(state_db_path)
    matched_mac_ids: list[str] = []
    unmatched_canonical: list[str] = []
    for sm_id in set_memory_content_ids:
        mac_id = canonical_to_mac.get(sm_id)
        if mac_id is None:
            unmatched_canonical.append(sm_id)
        else:
            matched_mac_ids.append(mac_id)
    if not matched_mac_ids:
        raise TrackMatchProblem(unmatched_canonical)

    backup_path = _backup_master_db(mac_db_path)
    log.info("Backed up Mac master.db -> %s", backup_path)

    from pyrekordbox import Rekordbox6Database

    db = Rekordbox6Database(path=str(mac_db_path))
    try:
        pl = db.create_playlist(name=name)
        for mac_id in matched_mac_ids:
            db.add_to_playlist(pl, mac_id)
        db.commit()
        playlist_id = str(pl.ID)
    finally:
        db.close()

    return {
        "playlist_id": playlist_id,
        "tracks_added": len(matched_mac_ids),
        "unmatched_count": len(unmatched_canonical),
        "backup_path": str(backup_path),
    }


# ---------------------------------------------------------------------------
# Test-only plain-sqlite path
# ---------------------------------------------------------------------------

def _create_playlist_plain_sqlite(
    fake_mac_db_path: Path,
    name: str,
    set_memory_content_ids: list[str],
    state_db_path: Path,
) -> dict:
    """
    Mirror of create_playlist that uses a synthetic plain-sqlite Mac db
    (mirroring djmdContent / djmdArtist / djmdPlaylist / djmdSongPlaylist).
    Used by tests so we don't need a real SQLCipher rekordbox library.
    """
    # Build dedup_key -> mac_id from the fake db.
    mac_conn = sqlite3.connect(str(fake_mac_db_path))
    mac_conn.row_factory = sqlite3.Row
    mac_by_key: dict[str, str] = {}
    for r in mac_conn.execute(
        "SELECT c.ID, c.Title, a.Name AS ArtistName "
        "FROM djmdContent c LEFT JOIN djmdArtist a ON c.ArtistID = a.ID"
    ):
        key = ingest.dedup_key_for(r["Title"], r["ArtistName"])
        if key:
            mac_by_key[key] = str(r["ID"])

    state_conn = sqlite3.connect(str(state_db_path))
    state_conn.row_factory = sqlite3.Row
    sm_keys: dict[str, str] = {}
    for r in state_conn.execute(
        "SELECT content_id, dedup_key FROM tracks WHERE dedup_key IS NOT NULL"
    ):
        sm_keys[r["content_id"]] = r["dedup_key"]
    state_conn.close()

    matched_mac_ids: list[str] = []
    unmatched: list[str] = []
    for sm_id in set_memory_content_ids:
        key = sm_keys.get(sm_id)
        mac_id = mac_by_key.get(key) if key else None
        if mac_id is None:
            unmatched.append(sm_id)
        else:
            matched_mac_ids.append(mac_id)
    if not matched_mac_ids:
        mac_conn.close()
        raise TrackMatchProblem(unmatched)

    backup_path = _backup_master_db(fake_mac_db_path)
    log.info("Backed up fake Mac master.db -> %s", backup_path)

    cur = mac_conn.cursor()
    # Synthesize a playlist ID. Real rekordbox uses numeric strings.
    next_pid = cur.execute(
        "SELECT COALESCE(MAX(CAST(ID AS INTEGER)), 0) + 1 FROM djmdPlaylist"
    ).fetchone()[0]
    playlist_id = str(next_pid)
    cur.execute(
        "INSERT INTO djmdPlaylist (ID, Name, ParentID, Attribute, Seq) "
        "VALUES (?, ?, 'root', 0, 1)",
        (playlist_id, name),
    )
    next_sp_id = cur.execute(
        "SELECT COALESCE(MAX(CAST(ID AS INTEGER)), 0) + 1 FROM djmdSongPlaylist"
    ).fetchone()[0]
    for track_no, mac_id in enumerate(matched_mac_ids, start=1):
        cur.execute(
            "INSERT INTO djmdSongPlaylist (ID, PlaylistID, ContentID, TrackNo) "
            "VALUES (?, ?, ?, ?)",
            (str(next_sp_id), playlist_id, mac_id, track_no),
        )
        next_sp_id += 1
    mac_conn.commit()
    mac_conn.close()

    return {
        "playlist_id": playlist_id,
        "tracks_added": len(matched_mac_ids),
        "unmatched_count": len(unmatched),
        "backup_path": str(backup_path),
    }
