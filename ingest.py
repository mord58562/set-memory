"""
ingest.py - USB master.db -> state.db ingestion for Set Memory.

Two-layer split:

  Layer A (testable):
    connect_master_db(path, key=None)
    read_sessions / read_song_history / read_content / read_cues
    ingest_from_connection(usb_conn, state_conn, ...)
      - sync_library: upsert every djmdContent row into tracks (in_library=1)
      - process sessions: increment total_appearances + record appearances

  Layer B (production-only):
    ingest_from_usb(usb_db_path, state_conn)
      - fetch SQLCipher key from pyrekordbox, snapshot the USB db, delegate

Tests cover Layer A exclusively.
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

_MISSING_CONTENT_ID = "__missing__"
SCHEMA_VERSION = 2


class SchemaError(Exception):
    """USB master.db missing expected djmd* tables/columns."""


class WalLockError(Exception):
    """Could not snapshot the USB master.db after retries."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RawSession:
    history_id: str
    name: str
    date_created: str


@dataclass
class RawSongEntry:
    song_history_id: str
    history_id: str
    content_id: str
    track_no: int


@dataclass
class RawTrack:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    bpm: Optional[float]
    key_camelot: Optional[str]
    energy: Optional[int]
    date_created: Optional[str]
    stock_date: Optional[str] = None


@dataclass
class IngestSummary:
    sessions_found: int = 0
    sessions_new: int = 0
    sessions_skipped: int = 0
    library_size: int = 0
    library_added: int = 0
    library_removed_flagged: int = 0
    new_session_ids: list[int] = field(default_factory=list)

    def merge(self, other: "IngestSummary") -> "IngestSummary":
        self.sessions_found += other.sessions_found
        self.sessions_new += other.sessions_new
        self.sessions_skipped += other.sessions_skipped
        self.library_size = max(self.library_size, other.library_size)
        self.library_added += other.library_added
        self.library_removed_flagged += other.library_removed_flagged
        self.new_session_ids.extend(other.new_session_ids)
        return self


# ---------------------------------------------------------------------------
# Layer A - Low-level, testable
# ---------------------------------------------------------------------------

def connect_master_db(path: str | Path, key: Optional[str] = None) -> sqlite3.Connection:
    """
    Open a rekordbox master.db. key=None: plain sqlite3 (test fixture / unencrypted copy).
    key=str: sqlcipher3 with the deobfuscated SQLCipher passphrase. Single boundary.
    """
    if key is None:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore[import]
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlcipher  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "sqlcipher3 is not installed. Run scripts/install.sh "
                "(it pulls sqlcipher3-wheels in as a pyrekordbox dep)."
            ) from exc
    conn = sqlcipher.connect(str(path))
    # pyrekordbox passes the deobfuscated 64-char key as a passphrase and lets
    # SQLCipher 4 defaults do the rest. Explicit cipher_page_size / kdf_iter
    # pragmas (mimicking SQLCipher 3 tuning) make sqlcipher3-wheels 0.5+ refuse
    # the file with "file is not a database". Don't touch the defaults.
    conn.execute(f"PRAGMA key = '{key}'")
    # Row factory must come from the SAME module as the connection.
    conn.row_factory = sqlcipher.Row
    return conn


def _require_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    existing = {
        row[0] for row in conn.execute(
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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _normalise_bpm(raw_bpm) -> Optional[float]:
    """
    rekordbox stores BPM as integer x 100 (128 BPM => 12800). Tracks that
    haven't been BPM-analysed store 0.0 (not NULL). The test fixture stores
    plain BPM (120.0 etc) so anything <= 300 is treated as already in real
    BPM units; anything else is divided by 100. Zero collapses to None
    ("not analysed").
    """
    if raw_bpm is None:
        return None
    val = float(raw_bpm)
    if val <= 0:
        return None
    if val > 300:
        val = val / 100.0
    return val


def read_all_content(conn: sqlite3.Connection) -> dict[str, RawTrack]:
    """
    Return every djmdContent row keyed by content_id. Drives library sync.

    Uses StockDate if available (rekordbox >= 6.x, post-2022) as a closer
    approximation of "date added to library" than DateCreated (file creation
    date, often years before import). Falls back to DateCreated.
    """
    _require_tables(conn, ["djmdContent"])
    has_stock = _has_column(conn, "djmdContent", "StockDate")
    stock_select = "c.StockDate" if has_stock else "NULL AS StockDate"
    rows = conn.execute(
        f"SELECT c.ID, c.Title, a.Name AS ArtistName, c.BPM, "
        f"       k.ScaleName AS Tonality, c.ColorID, c.DateCreated, {stock_select} "
        f"FROM djmdContent c "
        f"LEFT JOIN djmdArtist a ON a.ID = c.ArtistID "
        f"LEFT JOIN djmdKey    k ON k.ID = c.KeyID"
    ).fetchall()
    result: dict[str, RawTrack] = {}
    for row in rows:
        cid = str(row["ID"])
        result[cid] = RawTrack(
            content_id=cid,
            title=str(row["Title"]) if row["Title"] else None,
            artist=str(row["ArtistName"]) if row["ArtistName"] else None,
            bpm=_normalise_bpm(row["BPM"]),
            key_camelot=str(row["Tonality"]) if row["Tonality"] else None,
            energy=int(row["ColorID"]) if row["ColorID"] is not None else None,
            date_created=str(row["DateCreated"]) if row["DateCreated"] else None,
            stock_date=str(row["StockDate"]) if has_stock and row["StockDate"] else None,
        )
    return result


def read_content(conn: sqlite3.Connection, content_ids: list[str]) -> dict[str, RawTrack]:
    """Targeted lookup. Used inside session ingest to enrich appearances."""
    if not content_ids:
        return {}
    _require_tables(conn, ["djmdContent"])
    has_stock = _has_column(conn, "djmdContent", "StockDate")
    stock_select = "c.StockDate" if has_stock else "NULL AS StockDate"
    placeholders = ",".join("?" * len(content_ids))
    rows = conn.execute(
        f"SELECT c.ID, c.Title, a.Name AS ArtistName, c.BPM, "
        f"       k.ScaleName AS Tonality, c.ColorID, c.DateCreated, {stock_select} "
        f"FROM djmdContent c "
        f"LEFT JOIN djmdArtist a ON a.ID = c.ArtistID "
        f"LEFT JOIN djmdKey    k ON k.ID = c.KeyID "
        f"WHERE c.ID IN ({placeholders})",
        content_ids,
    ).fetchall()
    result: dict[str, RawTrack] = {}
    for row in rows:
        cid = str(row["ID"])
        result[cid] = RawTrack(
            content_id=cid,
            title=str(row["Title"]) if row["Title"] else None,
            artist=str(row["ArtistName"]) if row["ArtistName"] else None,
            bpm=_normalise_bpm(row["BPM"]),
            key_camelot=str(row["Tonality"]) if row["Tonality"] else None,
            energy=int(row["ColorID"]) if row["ColorID"] is not None else None,
            date_created=str(row["DateCreated"]) if row["DateCreated"] else None,
            stock_date=str(row["StockDate"]) if has_stock and row["StockDate"] else None,
        )
    return result


def read_cue_counts(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """
    Return {content_id: (hot_cue_count, memory_cue_count)} from djmdCue.

    Rekordbox uses Kind=0 for memory cues, Kind=1 for hot cues (A-H). Tracks
    with no cue rows aren't in the result. Used by the prep audit. Returns
    empty dict if djmdCue doesn't exist (older rekordbox versions).
    """
    has_table = bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='djmdCue'"
    ).fetchone())
    if not has_table:
        return {}
    rows = conn.execute(
        "SELECT ContentID, Kind, COUNT(*) FROM djmdCue "
        "GROUP BY ContentID, Kind"
    ).fetchall()
    result: dict[str, tuple[int, int]] = {}
    for row in rows:
        cid = str(row[0])
        kind = int(row[1]) if row[1] is not None else 0
        count = int(row[2])
        hot, mem = result.get(cid, (0, 0))
        if kind == 1:
            hot += count
        else:
            mem += count
        result[cid] = (hot, mem)
    return result


def compute_fingerprint(content_ids: list[str]) -> str:
    """SHA-256[:16] of sorted unique content_ids. Order-independent."""
    unique_sorted = sorted(set(content_ids))
    payload = ",".join(unique_sorted).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Schema + migrations
# ---------------------------------------------------------------------------

_BASE_SCHEMA = """
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

    CREATE TABLE IF NOT EXISTS usb_drives (
        volume_label     TEXT PRIMARY KEY,
        master_db_path   TEXT,
        first_seen_at    TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL,
        last_sync_at     TEXT,
        library_size     INTEGER NOT NULL DEFAULT 0
    );
"""

_V2_COLUMNS = [
    ("tracks", "stock_date", "TEXT"),
    ("tracks", "in_library", "INTEGER NOT NULL DEFAULT 0"),
    ("tracks", "last_in_library_at", "TEXT"),
    ("tracks", "added_at", "TEXT"),
    ("tracks", "hot_cue_count", "INTEGER"),
    ("tracks", "memory_cue_count", "INTEGER"),
]


def _apply_schema(state_conn: sqlite3.Connection) -> None:
    state_conn.executescript(_BASE_SCHEMA)
    # v2 migration: add columns conditionally
    for table, col, decl in _V2_COLUMNS:
        if not _has_column(state_conn, table, col):
            state_conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    state_conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    state_conn.commit()


def ensure_schema(state_conn: sqlite3.Connection) -> None:
    _apply_schema(state_conn)


# ---------------------------------------------------------------------------
# Library + session ingest
# ---------------------------------------------------------------------------

def _upsert_library_track(
    state_conn: sqlite3.Connection,
    track: RawTrack,
    ingested_at: str,
    cue_counts: dict[str, tuple[int, int]],
) -> bool:
    """
    Upsert one djmdContent row into tracks. Returns True if a brand-new row.

    Preserves total_appearances and first/last_seen_session if the track was
    already present (e.g. ingested earlier from session data). Always refreshes
    in_library, last_in_library_at, and the latest metadata.
    """
    hot, mem = cue_counts.get(track.content_id, (0, 0))
    # Prefer StockDate (date added to library) over DateCreated (file mtime)
    # when StockDate is populated. Fall back when not.
    added_at = track.stock_date or track.date_created
    cur = state_conn.execute(
        "INSERT INTO tracks "
        "(content_id, title, artist, bpm, key_camelot, energy, date_created, "
        " stock_date, in_library, last_in_library_at, added_at, "
        " hot_cue_count, memory_cue_count, total_appearances) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0) "
        "ON CONFLICT(content_id) DO UPDATE SET "
        "  title = excluded.title, "
        "  artist = excluded.artist, "
        "  bpm = COALESCE(excluded.bpm, tracks.bpm), "
        "  key_camelot = COALESCE(excluded.key_camelot, tracks.key_camelot), "
        "  energy = COALESCE(excluded.energy, tracks.energy), "
        "  date_created = COALESCE(excluded.date_created, tracks.date_created), "
        "  stock_date = COALESCE(excluded.stock_date, tracks.stock_date), "
        "  in_library = 1, "
        "  last_in_library_at = excluded.last_in_library_at, "
        "  added_at = COALESCE(tracks.added_at, excluded.added_at), "
        "  hot_cue_count = excluded.hot_cue_count, "
        "  memory_cue_count = excluded.memory_cue_count",
        (
            track.content_id, track.title, track.artist, track.bpm,
            track.key_camelot, track.energy, track.date_created, track.stock_date,
            ingested_at, added_at, hot, mem,
        ),
    )
    return cur.rowcount == 1 and state_conn.total_changes  # crude "new row" hint


def sync_library(
    usb_conn: sqlite3.Connection,
    state_conn: sqlite3.Connection,
    ingested_at: str,
) -> tuple[int, int]:
    """
    Sync the entire djmdContent + djmdCue into state.db tracks.

    Returns (total_library_size, newly_added_rows). Newly_added = rows whose
    content_id wasn't in tracks before this call. Tracks dropped from the
    library aren't deleted; their in_library is left as-is and the deleted-
    track analysis surfaces them via last_in_library_at staleness.
    """
    library = read_all_content(usb_conn)
    cue_counts = read_cue_counts(usb_conn)
    existing = {
        row[0] for row in state_conn.execute(
            "SELECT content_id FROM tracks WHERE in_library = 1"
        ).fetchall()
    }
    new_count = 0
    for track in library.values():
        if track.content_id not in existing:
            new_count += 1
        _upsert_library_track(state_conn, track, ingested_at, cue_counts)
    state_conn.commit()
    return len(library), new_count


def ingest_from_connection(
    usb_conn: sqlite3.Connection,
    state_conn: sqlite3.Connection,
    source_path: str,
    ingested_at: Optional[str] = None,
    volume_label: Optional[str] = None,
) -> IngestSummary:
    """
    Core ingestion - Layer A.

    1. Apply / migrate schema
    2. Sync full library (every djmdContent row -> tracks, with in_library=1)
    3. For each new session: insert session + appearances, bump total_appearances
    4. Record this USB drive in usb_drives
    """
    if ingested_at is None:
        ingested_at = datetime.now(timezone.utc).isoformat()

    _apply_schema(state_conn)

    library_size, library_added = sync_library(usb_conn, state_conn, ingested_at)

    existing_fingerprints: set[str] = {
        row[0]
        for row in state_conn.execute("SELECT fingerprint FROM sessions").fetchall()
    }

    raw_sessions = read_sessions(usb_conn)
    summary = IngestSummary(
        sessions_found=len(raw_sessions),
        library_size=library_size,
        library_added=library_added,
    )

    for raw_session in raw_sessions:
        songs = read_song_history(usb_conn, raw_session.history_id)
        content_ids = [s.content_id for s in songs if s.content_id != _MISSING_CONTENT_ID]
        fingerprint = compute_fingerprint(content_ids)

        if fingerprint in existing_fingerprints:
            existing_row = state_conn.execute(
                "SELECT raw_history_id FROM sessions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing_row and existing_row[0] != raw_session.history_id:
                log.debug(
                    "Session fingerprint %s: raw_history_id changed %s -> %s",
                    fingerprint, existing_row[0], raw_session.history_id,
                )
            summary.sessions_skipped += 1
            continue

        cur = state_conn.execute(
            "INSERT INTO sessions "
            "(raw_history_id, fingerprint, session_date, source_db_path, ingested_at, track_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                raw_session.history_id, fingerprint, raw_session.date_created,
                source_path, ingested_at, len(songs),
            ),
        )
        session_id = cur.lastrowid
        existing_fingerprints.add(fingerprint)
        summary.sessions_new += 1
        summary.new_session_ids.append(session_id)

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

            # Track may already exist from sync_library; bump counters in place.
            state_conn.execute(
                "INSERT INTO tracks "
                "(content_id, title, artist, bpm, key_camelot, energy, date_created, "
                " stock_date, first_seen_session, last_seen_session, total_appearances) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(content_id) DO UPDATE SET "
                "  last_seen_session = excluded.last_seen_session, "
                "  first_seen_session = COALESCE(tracks.first_seen_session, excluded.first_seen_session), "
                "  total_appearances = tracks.total_appearances + 1, "
                "  title = COALESCE(excluded.title, tracks.title), "
                "  artist = COALESCE(excluded.artist, tracks.artist)",
                (
                    cid,
                    meta.title if meta else None,
                    meta.artist if meta else None,
                    meta.bpm if meta else None,
                    meta.key_camelot if meta else None,
                    meta.energy if meta else None,
                    meta.date_created if meta else None,
                    meta.stock_date if meta else None,
                    session_id,
                    session_id,
                ),
            )

        state_conn.commit()
        log.info(
            "Ingested session %s (%s, %d tracks) -> state session_id %d",
            raw_session.history_id, raw_session.date_created, len(songs), session_id,
        )

    # Record / refresh USB drive entry
    if volume_label:
        state_conn.execute(
            "INSERT INTO usb_drives "
            "(volume_label, master_db_path, first_seen_at, last_seen_at, last_sync_at, library_size) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(volume_label) DO UPDATE SET "
            "  master_db_path = excluded.master_db_path, "
            "  last_seen_at = excluded.last_seen_at, "
            "  last_sync_at = excluded.last_sync_at, "
            "  library_size = excluded.library_size",
            (volume_label, source_path, ingested_at, ingested_at, ingested_at, library_size),
        )

    state_conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync_at', ?)",
        (ingested_at,),
    )
    state_conn.commit()

    return summary


# ---------------------------------------------------------------------------
# Layer B - Production-only
# ---------------------------------------------------------------------------

def _get_pyrekordbox_key() -> str:
    """
    Deobfuscate the SQLCipher key from pyrekordbox. The key is a global
    Pioneer constant - same for every install - so once you have a working
    pyrekordbox you have the key.
    """
    try:
        from pyrekordbox.db6.database import BLOB, deobfuscate  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "pyrekordbox is not installed. Run scripts/install.sh first."
        ) from exc

    key = deobfuscate(BLOB)
    if not key or not key.startswith("402fd"):
        raise RuntimeError(
            "pyrekordbox returned an unexpected key shape. "
            "The package may have changed; check its release notes."
        )
    return key


def _snapshot_usb_db(usb_db_path: Path, tmp_dir: Path) -> Path:
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
    volume_label: Optional[str] = None,
) -> IngestSummary:
    """Layer B: production ingestion against the real encrypted USB master.db."""
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
                volume_label=volume_label,
            )
        finally:
            usb_conn.close()
