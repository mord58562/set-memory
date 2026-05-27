"""
analyse.py - Pure SQL/Python analysis against state.db.

No USB / SQLCipher / pyrekordbox dependency. Open a sqlite3 connection,
pass it in, get back dataclasses.

Surfaces:
  - forgotten             tracks played often, not seen for a while
  - never_played          old library tracks with zero appearances
  - recently_added_unplayed   buys from the last ~N days that didn't make it in
  - prep_audit            library tracks missing BPM / key / cues
  - bpm_key_distribution  set-diversity report from session appearances
  - co_appearance         top track pairs that share many sessions
  - deleted_candidates    tracks in state.db not seen in any USB library lately
  - sessions_by_month     {YYYY-MM: count} for sparkline rendering
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ForgottenTrack:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    total_appearances: int
    last_session_date: str


@dataclass
class NeverPlayedTrack:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    date_created: Optional[str]
    added_at: Optional[str] = None


@dataclass
class RecentlyAddedUnplayed:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    added_at: str
    days_since_added: int


@dataclass
class PrepIssue:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    missing_bpm: bool
    missing_key: bool
    missing_hot_cues: bool


@dataclass
class CoAppearancePair:
    a_title: Optional[str]
    a_artist: Optional[str]
    b_title: Optional[str]
    b_artist: Optional[str]
    shared_sessions: int


@dataclass
class DeletedCandidate:
    content_id: str
    title: Optional[str]
    artist: Optional[str]
    last_in_library_at: Optional[str]
    total_appearances: int


@dataclass
class DistributionBuckets:
    """Histogram of plays across BPM buckets and Camelot keys."""
    bpm: dict[str, int] = field(default_factory=dict)
    key: dict[str, int] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    forgotten: list[ForgottenTrack] = field(default_factory=list)
    never_played: list[NeverPlayedTrack] = field(default_factory=list)
    recently_added_unplayed: list[RecentlyAddedUnplayed] = field(default_factory=list)
    prep_issues: list[PrepIssue] = field(default_factory=list)
    co_appearance: list[CoAppearancePair] = field(default_factory=list)
    deleted_candidates: list[DeletedCandidate] = field(default_factory=list)
    distribution: DistributionBuckets = field(default_factory=DistributionBuckets)
    sessions_by_month: dict[str, int] = field(default_factory=dict)


def run(
    state_conn: sqlite3.Connection,
    forgotten_min_appearances: int,
    forgotten_days_since_last: int,
    forgotten_limit: int,
    never_played_min_days_since_add: int,
    never_played_limit: int,
    recently_added_window_days: int = 30,
    recently_added_limit: int = 10,
    prep_limit: int = 15,
    co_appearance_min_sessions: int = 3,
    co_appearance_limit: int = 10,
    deleted_stale_days: int = 60,
    deleted_limit: int = 10,
    sparkline_months: int = 12,
    today: Optional[datetime.date] = None,
) -> AnalysisResult:
    if today is None:
        today = datetime.date.today()

    return AnalysisResult(
        forgotten=_query_forgotten(
            state_conn,
            min_appearances=forgotten_min_appearances,
            days_since_last=forgotten_days_since_last,
            limit=forgotten_limit,
            today=today,
        ),
        never_played=_query_never_played(
            state_conn,
            min_days_since_add=never_played_min_days_since_add,
            limit=never_played_limit,
            today=today,
        ),
        recently_added_unplayed=_query_recently_added_unplayed(
            state_conn,
            window_days=recently_added_window_days,
            limit=recently_added_limit,
            today=today,
        ),
        prep_issues=_query_prep_issues(state_conn, limit=prep_limit),
        co_appearance=_query_co_appearance(
            state_conn,
            min_sessions=co_appearance_min_sessions,
            limit=co_appearance_limit,
        ),
        deleted_candidates=_query_deleted_candidates(
            state_conn,
            stale_days=deleted_stale_days,
            limit=deleted_limit,
            today=today,
        ),
        distribution=_query_distribution(state_conn),
        sessions_by_month=_query_sessions_by_month(state_conn, months=sparkline_months, today=today),
    )


# ---------------------------------------------------------------------------
# Forgotten (played often, not recently)
# ---------------------------------------------------------------------------

def _query_forgotten(
    conn: sqlite3.Connection, min_appearances: int, days_since_last: int,
    limit: int, today: datetime.date,
) -> list[ForgottenTrack]:
    cutoff = (today - datetime.timedelta(days=days_since_last)).isoformat()
    sql = """
        SELECT t.content_id, t.title, t.artist, t.total_appearances,
               s.session_date AS last_session_date
        FROM tracks t
        JOIN sessions s ON s.session_id = t.last_seen_session
        WHERE t.total_appearances >= ?
          AND s.session_date < ?
        ORDER BY t.total_appearances DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (min_appearances, cutoff, limit)).fetchall()
    return [
        ForgottenTrack(
            content_id=str(r[0]),
            title=str(r[1]) if r[1] else None,
            artist=str(r[2]) if r[2] else None,
            total_appearances=int(r[3]),
            last_session_date=str(r[4]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Never played (old library, zero sessions)
# ---------------------------------------------------------------------------

def _query_never_played(
    conn: sqlite3.Connection, min_days_since_add: int, limit: int,
    today: datetime.date,
) -> list[NeverPlayedTrack]:
    """
    Uses COALESCE(added_at, date_created) as the "when added" signal:
    - added_at comes from djmdContent.StockDate when populated (true library add date)
    - date_created falls back to file mtime (DESIGN D5 approximation)
    """
    cutoff = (today - datetime.timedelta(days=min_days_since_add)).isoformat()
    sql = """
        SELECT content_id, title, artist, date_created, added_at,
               COALESCE(added_at, date_created) AS effective_add
        FROM tracks
        WHERE total_appearances = 0
          AND in_library = 1
          AND effective_add IS NOT NULL
          AND effective_add < ?
        ORDER BY effective_add ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return [
        NeverPlayedTrack(
            content_id=str(r[0]),
            title=str(r[1]) if r[1] else None,
            artist=str(r[2]) if r[2] else None,
            date_created=str(r[3]) if r[3] else None,
            added_at=str(r[4]) if r[4] else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Recently added but unplayed - "buy regret" signal
# ---------------------------------------------------------------------------

def _query_recently_added_unplayed(
    conn: sqlite3.Connection, window_days: int, limit: int,
    today: datetime.date,
) -> list[RecentlyAddedUnplayed]:
    cutoff = (today - datetime.timedelta(days=window_days)).isoformat()
    today_str = today.isoformat()
    sql = """
        SELECT content_id, title, artist,
               COALESCE(added_at, date_created) AS effective_add
        FROM tracks
        WHERE total_appearances = 0
          AND in_library = 1
          AND effective_add IS NOT NULL
          AND effective_add >= ?
        ORDER BY effective_add DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff, limit)).fetchall()
    result = []
    for r in rows:
        added = str(r[3])
        try:
            days = (today - datetime.date.fromisoformat(added[:10])).days
        except ValueError:
            days = 0
        result.append(RecentlyAddedUnplayed(
            content_id=str(r[0]),
            title=str(r[1]) if r[1] else None,
            artist=str(r[2]) if r[2] else None,
            added_at=added,
            days_since_added=days,
        ))
    return result


# ---------------------------------------------------------------------------
# Prep audit - tracks missing BPM / key / hot cues
# ---------------------------------------------------------------------------

def _query_prep_issues(conn: sqlite3.Connection, limit: int) -> list[PrepIssue]:
    """
    Library tracks Rob would want to fix before playing out:
      - BPM missing -> MIK / rekordbox didn't analyse
      - Key missing -> same
      - Zero hot cues -> nothing prepared for the deck

    Sorted by most-played first (so already-loved tracks bubble up - they're
    the ones Rob most wants prepped). The hot_cue_count column may be NULL on
    pre-v2 rows; treat NULL as "unknown" rather than "missing" to avoid
    flagging the entire library after upgrade.
    """
    sql = """
        SELECT content_id, title, artist, bpm, key_camelot, hot_cue_count,
               total_appearances
        FROM tracks
        WHERE in_library = 1
          AND (
            bpm IS NULL
            OR key_camelot IS NULL
            OR hot_cue_count = 0
          )
        ORDER BY total_appearances DESC, title ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    return [
        PrepIssue(
            content_id=str(r[0]),
            title=str(r[1]) if r[1] else None,
            artist=str(r[2]) if r[2] else None,
            missing_bpm=r[3] is None,
            missing_key=r[4] is None,
            missing_hot_cues=(r[5] == 0),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Co-appearance - track pairs that share many sessions
# ---------------------------------------------------------------------------

def _query_co_appearance(
    conn: sqlite3.Connection, min_sessions: int, limit: int,
) -> list[CoAppearancePair]:
    """
    Pairs of tracks that appear together in >= min_sessions distinct sessions.
    Useful for set planning ("these two always work back-to-back").
    """
    sql = """
        SELECT a.content_id, b.content_id, COUNT(DISTINCT a.session_id) AS shared
        FROM appearances a
        JOIN appearances b
          ON a.session_id = b.session_id AND a.content_id < b.content_id
        GROUP BY a.content_id, b.content_id
        HAVING shared >= ?
        ORDER BY shared DESC, a.content_id
        LIMIT ?
    """
    rows = conn.execute(sql, (min_sessions, limit)).fetchall()
    pairs = []
    for r in rows:
        ta = conn.execute(
            "SELECT title, artist FROM tracks WHERE content_id = ?", (r[0],)
        ).fetchone()
        tb = conn.execute(
            "SELECT title, artist FROM tracks WHERE content_id = ?", (r[1],)
        ).fetchone()
        pairs.append(CoAppearancePair(
            a_title=str(ta[0]) if ta and ta[0] else None,
            a_artist=str(ta[1]) if ta and ta[1] else None,
            b_title=str(tb[0]) if tb and tb[0] else None,
            b_artist=str(tb[1]) if tb and tb[1] else None,
            shared_sessions=int(r[2]),
        ))
    return pairs


# ---------------------------------------------------------------------------
# Deleted candidates - tracks no longer in any synced USB library
# ---------------------------------------------------------------------------

def _query_deleted_candidates(
    conn: sqlite3.Connection, stale_days: int, limit: int,
    today: datetime.date,
) -> list[DeletedCandidate]:
    cutoff = (today - datetime.timedelta(days=stale_days)).isoformat()
    sql = """
        SELECT content_id, title, artist, last_in_library_at, total_appearances
        FROM tracks
        WHERE in_library = 0 OR (last_in_library_at IS NOT NULL AND last_in_library_at < ?)
        ORDER BY total_appearances DESC, last_in_library_at ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return [
        DeletedCandidate(
            content_id=str(r[0]),
            title=str(r[1]) if r[1] else None,
            artist=str(r[2]) if r[2] else None,
            last_in_library_at=str(r[3]) if r[3] else None,
            total_appearances=int(r[4]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Distribution - BPM buckets + key plays
# ---------------------------------------------------------------------------

_BPM_BUCKETS = [
    ("<100", 0, 100),
    ("100-119", 100, 120),
    ("120-127", 120, 128),
    ("128-134", 128, 135),
    ("135-144", 135, 145),
    ("145-159", 145, 160),
    ("160+", 160, 10_000),
]


def _query_distribution(conn: sqlite3.Connection) -> DistributionBuckets:
    """
    Histogram across appearances (counts how often each BPM bucket / Camelot
    key was played). Tracks with NULL BPM/key are bucketed under "unknown".
    """
    bpm: dict[str, int] = {label: 0 for label, _, _ in _BPM_BUCKETS}
    bpm["unknown"] = 0
    key: dict[str, int] = {}

    rows = conn.execute("""
        SELECT t.bpm, t.key_camelot, COUNT(*) AS n
        FROM appearances a
        JOIN tracks t ON t.content_id = a.content_id
        GROUP BY t.bpm, t.key_camelot
    """).fetchall()
    for r in rows:
        bpm_val = r[0]
        k = r[1]
        n = int(r[2])
        if bpm_val is None:
            bpm["unknown"] += n
        else:
            placed = False
            for label, lo, hi in _BPM_BUCKETS:
                if lo <= bpm_val < hi:
                    bpm[label] += n
                    placed = True
                    break
            if not placed:
                bpm["unknown"] += n
        key_label = str(k) if k else "unknown"
        key[key_label] = key.get(key_label, 0) + n
    return DistributionBuckets(bpm=bpm, key=key)


# ---------------------------------------------------------------------------
# Sessions per month (sparkline source)
# ---------------------------------------------------------------------------

def _query_sessions_by_month(
    conn: sqlite3.Connection, months: int, today: datetime.date,
) -> dict[str, int]:
    """
    Return {YYYY-MM: session_count} for the last `months` calendar months
    ending in today's month. Months with zero sessions are included so the
    sparkline shows continuity.
    """
    raw = {
        str(r[0])[:7]: int(r[1])
        for r in conn.execute("""
            SELECT substr(session_date, 1, 7) AS ym, COUNT(*)
            FROM sessions GROUP BY ym
        """).fetchall()
    }
    out: dict[str, int] = {}
    year, month = today.year, today.month
    for _ in range(months):
        ym = f"{year:04d}-{month:02d}"
        out[ym] = raw.get(ym, 0)
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return dict(reversed(out.items()))


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------

def compute_summary_stats(state_conn: sqlite3.Connection) -> dict[str, int | str]:
    total_sessions = state_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_unique_tracks = state_conn.execute(
        "SELECT COUNT(DISTINCT content_id) FROM appearances"
    ).fetchone()[0]
    library_size = state_conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE in_library = 1"
    ).fetchone()[0]
    state_size = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    last_sync_at_row = state_conn.execute(
        "SELECT value FROM meta WHERE key = 'last_sync_at'"
    ).fetchone()
    last_sync_at = str(last_sync_at_row[0]) if last_sync_at_row else "never"

    usb_count = state_conn.execute("SELECT COUNT(*) FROM usb_drives").fetchone()[0]

    return {
        "total_sessions": total_sessions,
        "total_unique_tracks": total_unique_tracks,
        "library_size": library_size,
        "state_track_count": state_size,
        "last_sync_at": last_sync_at,
        "usb_drives_seen": usb_count,
    }


def usb_drive_summary(state_conn: sqlite3.Connection) -> list[dict]:
    """List of every USB drive ever ingested, oldest-mounted first."""
    rows = state_conn.execute(
        "SELECT volume_label, last_seen_at, library_size FROM usb_drives "
        "ORDER BY last_seen_at ASC"
    ).fetchall()
    return [
        {
            "volume_label": str(r[0]),
            "last_seen_at": str(r[1]),
            "library_size": int(r[2]),
        }
        for r in rows
    ]
