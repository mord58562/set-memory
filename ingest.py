"""
ingest.py - USB master.db -> state.db ingestion for Set Memory.

Architecture (two-layer, per DESIGN D1):

  Layer A (low-level, testable):
    connect_master_db(path, key=None) -> sqlite3.Connection
      Uses plain sqlite3 when key is None; uses pysqlcipher3 when key is given.
      This is the ONLY point that knows about SQLCipher vs plain SQLite.

    read_sessions(conn) -> list[RawSession]
    read_song_history(conn, history_id) -> list[RawSongEntry]
    read_content(conn, content_ids) -> dict[str, RawTrack]

    ingest_from_connection(usb_conn, state_conn, source_path, ingested_at) -> IngestSummary
      Pure business logic against two open connections. Fully testable with
      synthetic plain SQLite. No filesystem ops, no temp dirs, no network.

  Layer B (high-level, production-only):
    ingest_from_usb(usb_db_path, state_conn, config) -> IngestSummary
      Fetches the SQLCipher key from pyrekordbox's cache, snapshots the USB
      master.db to a temp dir (db + WAL + SHM), opens via connect_master_db,
      calls ingest_from_connection, deletes the temp dir.

Tests cover Layer A exclusively. Layer B is exercised only by install.sh + real usage.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Sentinel value used for content_id when the song history row has no ContentID
_MISSING_CONTENT_ID = "__missing__"


class SchemaError(Exception):
    """Raised when the USB master.db is missing expected djmd* tables or columns."""


class WalLockError(Exception):
    """Raised when the USB master.db WAL cannot be copied cleanly after retries."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RawSession:
    """One djmdHistory row from the USB db."""
    history_id: str
    name: str
    date_created: str  # ISO 8601 string


@dataclass
class RawSongEntry:
    """One djmdSongHistory row."""
    song_history_id: str
    history_id: str
    content_id: str
    track_no: int


@dataclass
class RawTrack:
    """Key fields from djmdContent."""
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    bpm: Optional[float]
    key_camelot: Optional[str]
    energy: Optional[int]
    date_created: Optional[str]  # yyyy-mm-dd


@dataclass
class IngestSummary:
    sessions_found: int = 0
    sessions_new: int = 0
    sessions_skipped: int = 0
    new_session_ids: list[int] = field(default_factory=list)  # state.db session_id values


# ---------------------------------------------------------------------------
# Layer A - Low-level, testable functions
# ---------------------------------------------------------------------------

def connect_master_db(path: str | Path, key: Optional[str] = None) -> sqlite3.Connection:
    """
    Open a rekordbox master.db.

    When key is None: plain sqlite3 (used for synthetic test fixture and any
    unencrypted copy).
    When key is provided: uses pysqlcipher3 to open a SQLCipher-encrypted db.
    This is the single boundary between encrypted and unencrypted access.
    """
    if key is None:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn
    else:
        try:
            from pysqlcipher3 import dbapi2 as sqlcipher  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "pysqlcipher3 is not installed. Run scripts/install.sh to set up "
                "the full SQLCipher environment."
            ) from exc
        conn = sqlcipher.connect(str(path))
        conn.execute(f"PRAGMA key = '{key}'")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA kdf_iter = 64000")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1")
        conn.row_factory = sqlite3.Row
        return conn


def _require_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    """Raise SchemaError if any of the required tables are absent."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = [t for t in tables if t not in existing]
    if missing:
        raise SchemaError(
            f"USB master.db is missing expected tables: {missing}. "
            f"Schema may be incompatible with this version of Set Memory."
        )


def read_sessions(conn: sqlite3.Connection) -> list[RawSession]:
    """Return all djmdHistory rows sorted by DateCreated ascending."""
    _require_tables(conn, ["djmdHistory"])
    rows = conn.execute(
        "SELECT ID, Name, DateCreated FROM djmdHistory ORDER BY DateCreated ASC"
    ).fetchall()
    return [
        RawSession(
            history_id=str(row["ID"]),
            name=str(row["Name"]) if row["Name"] else "",
            date_created=str(row["DateCreated"]) if row["DateCreated"] else "",
        )
        for row in rows
    ]


def read_song_history(conn: sqlite3.Connection, history_id: str) -> list[RawSongEntry]:
    """Return all djmdSongHistory rows for a given HistoryID, sorted by TrackNo."""
    _require_tables(conn, ["djmdSongHistory"])
    rows = conn.execute(
        "SELECT ID, HistoryID, ContentID, TrackNo "
        "FROM djmdSongHistory WHERE HistoryID = ? ORDER BY TrackNo ASC",
        (history_id,),
    ).fetchall()
    return [
        RawSongEntry(
            song_history_id=str(row["ID"]),
            history_id=str(row["HistoryID"]),
            content_id=str(row["ContentID"]) if row["ContentID"] else _MISSING_CONTENT_ID,
            track_no=int(row["TrackNo"]) if row["TrackNo"] is not None else 0,
        )
        for row in rows
    ]


def read_content(
    conn: sqlite3.Connection, content_ids: list[str]
) -> dict[str, RawTrack]:
    """Return djmdContent rows for the given content_ids, keyed by content_id."""
    if not content_ids:
        return {}
    _require_tables(conn, ["djmdContent"])
    placeholders = ",".join("?" * len(content_ids))
    rows = conn.execute(
        f"SELECT ID, Title, ArtistName, BPM, Tonality, ColorID, DateCreated "
        f"FROM djmdContent WHERE ID IN ({placeholders})",
        content_ids,
    ).fetchall()
    result: dict[str, RawTrack] = {}
    for row in rows:
        cid = str(row["ID"])
        result[cid] = RawTrack(
            content_id=cid,
            title=str(row["Title"]) if row["Title"] else None,
            artist=str(row["ArtistName"]) if row["ArtistName"] else None,
            bpm=float(row["BPM"]) if row["BPM"] is not None else None,
            key_camelot=str(row["Tonality"]) if row["Tonality"] else None,
            energy=int(row["ColorID"]) if row["ColorID"] is not None else None,
            date_created=str(row["DateCreated"]) if row["DateCreated"] else None,
        )
    return result


def compute_fingerprint(content_ids: list[str]) -> str:
    """
    SHA-256 (first 16 hex chars) of the sorted set of content_ids.

    Sorting + dedup makes the fingerprint stable even if TrackNo order changes.
    Per DESIGN D3: fingerprint is the collision-resistant session identity.
    """
    unique_sorted = sorted(set(content_ids))
    payload = ",".join(unique_sorted).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _apply_schema(state_conn: sqlite3.Connection) -> None:
    """Create state.db tables if they don't exist."""
    state_conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_history_id   TEXT    NOT NULL,
            fingerprint      TEXT    NOT NULL UNIQUE,
            session_date     TEXT    NOT NULL,
            source_db_path   TEXT    NOT NULL,
            ingested_at      TEXT    NOT NULL,
            track_count      INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions (session_date);

        CREATE TABLE IF NOT EXISTS appearances (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES sessions (session_id),
            content_id   TEXT    NOT NULL,
            track_no     INTEGER NOT NULL,
            title        TEXT,
            artist       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_appearances_content ON appearances (content_id);
        CREATE INDEX IF NOT EXISTS idx_appearances_session ON appearances (session_id);

        CREATE TABLE IF NOT EXISTS tracks (
            content_id           TEXT PRIMARY KEY,
            title                TEXT,
            artist               TEXT,
            bpm                  REAL,
            key_camelot          TEXT,
            energy               INTEGER,
            date_created         TEXT,
            first_seen_session   INTEGER REFERENCES sessions (session_id),
            last_seen_session    INTEGER REFERENCES sessions (session_id),
            total_appearances    INTEGER NOT NULL DEFAULT 0
        );
    """)
    state_conn.commit()


def ensure_schema(state_conn: sqlite3.Connection) -> None:
    """Public alias for _apply_schema, used by set_memory.py on startup."""
    _apply_schema(state_conn)


def ingest_from_connection(
    usb_conn: sqlite3.Connection,
    state_conn: sqlite3.Connection,
    source_path: str,
    ingested_at: Optional[str] = None,
) -> IngestSummary:
    """
    Core ingestion logic - Layer A.

    Reads all djmdHistory sessions from usb_conn, compares fingerprints against
    state_conn, inserts new sessions + appearances + tracks. Returns a summary.

    This function is fully testable with synthetic plain SQLite connections.
    """
    if ingested_at is None:
        ingested_at = datetime.now(timezone.utc).isoformat()

    _apply_schema(state_conn)

    existing_fingerprints: set[str] = {
        row[0]
        for row in state_conn.execute("SELECT fingerprint FROM sessions").fetchall()
    }

    raw_sessions = read_sessions(usb_conn)
    summary = IngestSummary(sessions_found=len(raw_sessions))

    for raw_session in raw_sessions:
        songs = read_song_history(usb_conn, raw_session.history_id)
        content_ids = [s.content_id for s in songs if s.content_id != _MISSING_CONTENT_ID]
        fingerprint = compute_fingerprint(content_ids)

        if fingerprint in existing_fingerprints:
            # Check if raw_history_id changed (rename detection, per D3)
            existing_row = state_conn.execute(
                "SELECT raw_history_id FROM sessions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing_row and existing_row[0] != raw_session.history_id:
                log.info(
                    "Session fingerprint %s: raw_history_id changed from %s to %s "
                    "(likely playlist rename on XDJ) - skipping as already ingested",
                    fingerprint,
                    existing_row[0],
                    raw_session.history_id,
                )
            summary.sessions_skipped += 1
            continue

        # New session - insert
        cur = state_conn.execute(
            "INSERT INTO sessions "
            "(raw_history_id, fingerprint, session_date, source_db_path, ingested_at, track_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                raw_session.history_id,
                fingerprint,
                raw_session.date_created,
                source_path,
                ingested_at,
                len(songs),
            ),
        )
        session_id = cur.lastrowid
        existing_fingerprints.add(fingerprint)
        summary.sessions_new += 1
        summary.new_session_ids.append(session_id)

        # Fetch track metadata for all content_ids in this session
        track_meta = read_content(usb_conn, content_ids) if content_ids else {}

        for song in songs:
            cid = song.content_id
            if cid == _MISSING_CONTENT_ID:
                continue
            meta = track_meta.get(cid)
            title = meta.title if meta else None
            artist = meta.artist if meta else None

            state_conn.execute(
                "INSERT INTO appearances (session_id, content_id, track_no, title, artist) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, cid, song.track_no, title, artist),
            )

            # Upsert tracks row
            existing_track = state_conn.execute(
                "SELECT first_seen_session, total_appearances FROM tracks WHERE content_id = ?",
                (cid,),
            ).fetchone()

            if existing_track is None:
                state_conn.execute(
                    "INSERT INTO tracks "
                    "(content_id, title, artist, bpm, key_camelot, energy, date_created, "
                    "first_seen_session, last_seen_session, total_appearances) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        cid,
                        meta.title if meta else None,
                        meta.artist if meta else None,
                        meta.bpm if meta else None,
                        meta.key_camelot if meta else None,
                        meta.energy if meta else None,
                        meta.date_created if meta else None,
                        session_id,
                        session_id,
                    ),
                )
            else:
                state_conn.execute(
                    "UPDATE tracks SET "
                    "last_seen_session = ?, "
                    "total_appearances = total_appearances + 1, "
                    "title = COALESCE(?, title), "
                    "artist = COALESCE(?, artist) "
                    "WHERE content_id = ?",
                    (session_id, title, artist, cid),
                )

        state_conn.commit()
        log.info(
            "Ingested session %s (%s, %d tracks) -> state session_id %d",
            raw_session.history_id,
            raw_session.date_created,
            len(songs),
            session_id,
        )

    # Update meta
    state_conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync_at', ?)",
        (ingested_at,),
    )
    state_conn.commit()

    return summary


# ---------------------------------------------------------------------------
# Layer B - High-level, production-only (not tested directly)
# ---------------------------------------------------------------------------

def _get_pyrekordbox_key() -> str:
    """
    Retrieve the SQLCipher key from pyrekordbox's cache.

    If the cache is absent or empty, attempts to download it from Pioneer's CDN.
    Raises RuntimeError if the key cannot be obtained.
    """
    try:
        import pyrekordbox  # type: ignore[import]
        from pyrekordbox.config import get_config  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "pyrekordbox is not installed. Run scripts/install.sh first."
        ) from exc

    import subprocess
    import sys

    # pyrekordbox stores the key in its config; try reading it first
    try:
        cfg = get_config()
        key = getattr(cfg, "db_key", None) or getattr(cfg, "key", None)
        if key:
            return str(key)
    except Exception:
        pass

    # Fallback: check the conventional cache file path
    key_cache = Path.home() / ".pyrekordbox" / "key"
    if key_cache.exists() and key_cache.stat().st_size > 0:
        return key_cache.read_text(encoding="utf-8").strip()

    # Cache missing - attempt download
    log.info("SQLCipher key cache absent; attempting download from Pioneer CDN...")
    result = subprocess.run(
        [sys.executable, "-m", "pyrekordbox", "download-key"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to download SQLCipher key: {result.stderr.strip()}. "
            "Ensure you have network access and pyrekordbox is installed."
        )

    # Re-check cache after download
    if key_cache.exists() and key_cache.stat().st_size > 0:
        return key_cache.read_text(encoding="utf-8").strip()

    raise RuntimeError(
        "download-key succeeded but key file not found at expected location. "
        f"Checked: {key_cache}"
    )


def _snapshot_usb_db(usb_db_path: Path, tmp_dir: Path) -> Path:
    """
    Copy master.db + WAL + SHM to tmp_dir with one retry on lock.

    Returns path to the copy of master.db.
    """
    dest = tmp_dir / "master.db"

    def _copy() -> None:
        shutil.copy2(str(usb_db_path), str(dest))
        for suffix in ("-wal", "-shm"):
            src = usb_db_path.parent / (usb_db_path.name + suffix)
            if src.exists():
                shutil.copy2(str(src), str(tmp_dir / ("master.db" + suffix)))

    try:
        _copy()
    except (OSError, PermissionError) as exc:
        log.warning("First copy attempt failed (%s); retrying in 2 seconds...", exc)
        time.sleep(2)
        try:
            _copy()
        except (OSError, PermissionError) as exc2:
            raise WalLockError(
                f"Cannot snapshot USB master.db after 2 attempts: {exc2}"
            ) from exc2

    return dest


def ingest_from_usb(
    usb_db_path: Path,
    state_conn: sqlite3.Connection,
    source_path: Optional[str] = None,
) -> IngestSummary:
    """
    Layer B: Production ingestion from the real encrypted USB master.db.

    1. Gets the SQLCipher key from pyrekordbox's cache (downloading if needed).
    2. Snapshots the USB db to a temp directory.
    3. Opens the snapshot via connect_master_db(key=...).
    4. Delegates to ingest_from_connection.
    5. Cleans up the temp directory.
    """
    key = _get_pyrekordbox_key()
    ingested_at = datetime.now(timezone.utc).isoformat()

    with tempfile.TemporaryDirectory(prefix="setmem_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        snapshot_path = _snapshot_usb_db(usb_db_path, tmp_dir)

        usb_conn = connect_master_db(snapshot_path, key=key)
        try:
            return ingest_from_connection(
                usb_conn=usb_conn,
                state_conn=state_conn,
                source_path=source_path or str(usb_db_path),
                ingested_at=ingested_at,
            )
        finally:
            usb_conn.close()
