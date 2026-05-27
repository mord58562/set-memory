"""
playlist_suggester.py - Deterministic playlist suggestions from state.db.

Draws across every track + session Set Memory has ever ingested (per-USB
data is already collapsed into canonical rows by the v3 dedup migration).

Each suggestion has:
  - id        slug used to refer to the suggestion in the CLI
  - name      human-readable name (used as the rekordbox playlist name)
  - kind      one of "clique" | "forgotten" | "recent" | "key_chain" |
              "bpm_ramp" | "prep" | "seed_companions"
  - description  one-sentence what-this-is
  - content_ids  ordered list of canonical track content_ids
  - rationale    short why-these-tracks string
  - score        sort key (higher = more confident / interesting)

Algorithms favour deterministic, explainable picks over clever ML.
Music is taste; the tool surfaces options, the DJ picks the keepers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class Suggestion:
    id: str
    name: str
    kind: str
    description: str
    content_ids: list[str]
    rationale: str = ""
    score: float = 0.0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_all(state_conn: sqlite3.Connection,
                 limit_per_kind: int = 5,
                 include_dismissed: bool = False) -> list[Suggestion]:
    """
    Run every suggestion algorithm against state.db and return all
    suggestions sorted by score desc. Dismissed suggestion ids are
    excluded by default; pass include_dismissed=True to surface them
    (used by the GUI's "Show dismissed" toggle).
    """
    out: list[Suggestion] = []
    out.append(_forgotten_pack(state_conn))
    out.append(_recently_added_pack(state_conn))
    out.append(_prep_pack(state_conn))
    out.extend(_cliques(state_conn, limit=limit_per_kind))
    out.extend(_seed_companions(state_conn, limit=limit_per_kind))
    out.extend(_key_chains(state_conn, limit=limit_per_kind))
    out.append(_bpm_ramp(state_conn))
    out = [s for s in out if s and s.content_ids]
    if not include_dismissed:
        dismissed = dismissed_ids(state_conn)
        out = [s for s in out if s.id not in dismissed]
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def dismissed_ids(state_conn: sqlite3.Connection) -> set[str]:
    rows = state_conn.execute(
        "SELECT suggestion_id FROM dismissed_suggestions"
    ).fetchall()
    return {str(r[0]) for r in rows}


def dismiss(state_conn: sqlite3.Connection, suggestion_id: str) -> None:
    """Hide a suggestion from future generate_all() calls."""
    import datetime
    state_conn.execute(
        "INSERT OR REPLACE INTO dismissed_suggestions "
        "(suggestion_id, dismissed_at) VALUES (?, ?)",
        (suggestion_id, datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    state_conn.commit()


def undismiss(state_conn: sqlite3.Connection, suggestion_id: str) -> None:
    """Unhide one suggestion."""
    state_conn.execute(
        "DELETE FROM dismissed_suggestions WHERE suggestion_id = ?",
        (suggestion_id,),
    )
    state_conn.commit()


def clear_dismissed(state_conn: sqlite3.Connection) -> int:
    """Drop every dismissal; returns the count."""
    cur = state_conn.execute("DELETE FROM dismissed_suggestions")
    state_conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Forgotten favourites pack
# ---------------------------------------------------------------------------

def _forgotten_pack(conn: sqlite3.Connection,
                    min_appearances: int = 5,
                    days_since_last: int = 90,
                    limit: int = 30) -> Optional[Suggestion]:
    cutoff = _iso_offset(-days_since_last)
    rows = conn.execute("""
        SELECT t.content_id, t.total_appearances
        FROM tracks t
        JOIN sessions s ON s.session_id = t.last_seen_session
        WHERE t.total_appearances >= ?
          AND s.session_date < ?
        ORDER BY t.total_appearances DESC
        LIMIT ?
    """, (min_appearances, cutoff, limit)).fetchall()
    if not rows:
        return None
    ids = [str(r[0]) for r in rows]
    return Suggestion(
        id="forgotten_pack",
        name="Forgotten Favourites",
        kind="forgotten",
        description=f"Top {len(ids)} tracks you played a lot, then forgot.",
        content_ids=ids,
        rationale=f"Each track had ≥{min_appearances} plays and was last seen >{days_since_last}d ago.",
        score=80.0 + min(20, len(ids)),
    )


# ---------------------------------------------------------------------------
# Recently-added pack
# ---------------------------------------------------------------------------

def _recently_added_pack(conn: sqlite3.Connection,
                          window_days: int = 30,
                          limit: int = 30) -> Optional[Suggestion]:
    cutoff = _iso_offset(-window_days)
    rows = conn.execute("""
        SELECT content_id
        FROM tracks
        WHERE total_appearances = 0
          AND in_library = 1
          AND COALESCE(added_at, date_created) IS NOT NULL
          AND COALESCE(added_at, date_created) >= ?
        ORDER BY COALESCE(added_at, date_created) DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()
    if not rows:
        return None
    ids = [str(r[0]) for r in rows]
    return Suggestion(
        id="recently_added_pack",
        name="Try These (recent buys, unplayed)",
        kind="recent",
        description=f"{len(ids)} tracks you added in the last {window_days} days but haven't played yet.",
        content_ids=ids,
        rationale="Buy-regret signal. Either rotate these in or admit you're not feeling them.",
        score=70.0,
    )


# ---------------------------------------------------------------------------
# Prep audit pack
# ---------------------------------------------------------------------------

def _prep_pack(conn: sqlite3.Connection, limit: int = 50) -> Optional[Suggestion]:
    rows = conn.execute("""
        SELECT content_id
        FROM tracks
        WHERE in_library = 1
          AND (bpm IS NULL OR key_camelot IS NULL OR hot_cue_count = 0)
        ORDER BY total_appearances DESC, title ASC
        LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        return None
    ids = [str(r[0]) for r in rows]
    return Suggestion(
        id="prep_pack",
        name="Prep Me (missing BPM / key / cues)",
        kind="prep",
        description=f"{len(ids)} library tracks missing analysis or hot cues. Sorted most-played first.",
        content_ids=ids,
        rationale="Load this playlist in rekordbox, hit Analyse, set hot cues, you're done.",
        score=60.0,
    )


# ---------------------------------------------------------------------------
# Co-occurrence cliques - groups of 3-8 tracks that all play together
# ---------------------------------------------------------------------------

def _cliques(conn: sqlite3.Connection,
             min_pair_sessions: int = 4,
             min_clique: int = 3,
             max_clique: int = 8,
             limit: int = 5) -> list[Suggestion]:
    """
    Greedy clique discovery on the track co-occurrence graph.
    Builds an adjacency map from `appearances`, picks the heaviest
    edge as a seed, grows the clique by adding the node whose minimum
    co-session-count with current members is highest, stops when no
    candidate meets the pair threshold or the clique hits max size.
    Excludes already-included tracks from later seeds to keep results
    distinct.
    """
    pairs = conn.execute("""
        SELECT a.content_id, b.content_id, COUNT(DISTINCT a.session_id) AS shared
        FROM appearances a
        JOIN appearances b
          ON a.session_id = b.session_id AND a.content_id < b.content_id
        GROUP BY a.content_id, b.content_id
        HAVING shared >= ?
    """, (min_pair_sessions,)).fetchall()
    if not pairs:
        return []

    adj: dict[str, dict[str, int]] = {}
    for a, b, n in pairs:
        a, b, n = str(a), str(b), int(n)
        adj.setdefault(a, {})[b] = n
        adj.setdefault(b, {})[a] = n

    sorted_pairs = sorted(pairs, key=lambda r: -r[2])
    used: set[str] = set()
    suggestions: list[Suggestion] = []

    for a_raw, b_raw, _ in sorted_pairs:
        if len(suggestions) >= limit:
            break
        a, b = str(a_raw), str(b_raw)
        if a in used or b in used:
            continue
        clique = [a, b]
        clique_set = {a, b}
        while len(clique) < max_clique:
            best: tuple[Optional[str], int] = (None, 0)
            candidates = set(adj.get(clique[0], {}).keys())
            for m in clique[1:]:
                candidates &= set(adj.get(m, {}).keys())
            for cand in candidates - clique_set - used:
                min_shared = min(adj[cand].get(m, 0) for m in clique)
                if min_shared > best[1]:
                    best = (cand, min_shared)
            if best[0] is None or best[1] < min_pair_sessions:
                break
            clique.append(best[0])
            clique_set.add(best[0])
        if len(clique) < min_clique:
            continue
        used.update(clique)
        titles = _titles_for(conn, clique[:3])
        teaser = " + ".join(titles)
        suggestions.append(Suggestion(
            id=f"clique_{len(suggestions) + 1}",
            name=f"Cluster: {teaser}",
            kind="clique",
            description=f"{len(clique)} tracks that keep showing up together.",
            content_ids=clique,
            rationale="Greedy clique on co-session graph; min pairwise overlap "
                      f"≥{min_pair_sessions} sessions.",
            score=50.0 + len(clique),
        ))
    return suggestions


# ---------------------------------------------------------------------------
# Seed + companions - take a top forgotten track and surround it with its
# most frequent session mates
# ---------------------------------------------------------------------------

def _seed_companions(conn: sqlite3.Connection,
                     limit: int = 5,
                     companions_per_seed: int = 14) -> list[Suggestion]:
    seeds = conn.execute("""
        SELECT t.content_id, t.title, t.artist, t.total_appearances
        FROM tracks t
        WHERE t.total_appearances >= 6
          AND t.in_library = 1
        ORDER BY t.total_appearances DESC
        LIMIT ?
    """, (limit * 2,)).fetchall()
    out: list[Suggestion] = []
    seen_seeds: set[str] = set()
    for r in seeds:
        seed_id, seed_title, seed_artist, _plays = str(r[0]), r[1], r[2], int(r[3])
        if seed_id in seen_seeds:
            continue
        seen_seeds.add(seed_id)
        # Pull the seed's most-frequent session-mates
        rows = conn.execute("""
            SELECT b.content_id, COUNT(DISTINCT a.session_id) AS shared
            FROM appearances a
            JOIN appearances b
              ON a.session_id = b.session_id AND b.content_id != a.content_id
            WHERE a.content_id = ?
            GROUP BY b.content_id
            ORDER BY shared DESC
            LIMIT ?
        """, (seed_id, companions_per_seed)).fetchall()
        ids = [seed_id] + [str(r[0]) for r in rows]
        if len(ids) < 4:
            continue
        out.append(Suggestion(
            id=f"seed_{len(out) + 1}",
            name=f"Around {seed_title or 'Unknown'}",
            kind="seed_companions",
            description=f"{seed_title or 'Unknown'} + the {len(ids) - 1} tracks that most often share sessions with it.",
            content_ids=ids,
            rationale=f"Anchored on {seed_title or 'Unknown'} ({seed_artist or 'Unknown'}); "
                      "companions ranked by co-session count.",
            score=40.0 + len(ids),
        ))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Key chain - walk the Camelot wheel from a popular seed
# ---------------------------------------------------------------------------

def _key_chains(conn: sqlite3.Connection,
                target_length: int = 18,
                limit: int = 3) -> list[Suggestion]:
    """
    Build harmonically-compatible chains. From a seed track, alternate:
      - same key
      - +1 hour same letter (energy step)
      - -1 hour same letter
      - relative major/minor (swap A<->B same number)
    Greedy: pick the most-played unused track in any compatible key.
    """
    tracks = conn.execute("""
        SELECT content_id, title, key_camelot, total_appearances
        FROM tracks
        WHERE in_library = 1 AND key_camelot IS NOT NULL
          AND key_camelot GLOB '[0-9]*'
    """).fetchall()
    if not tracks:
        return []
    by_key: dict[str, list[tuple[str, str, int]]] = {}
    for cid, title, key, plays in tracks:
        by_key.setdefault(str(key), []).append((str(cid), title or "", int(plays)))
    for k in by_key:
        by_key[k].sort(key=lambda t: -t[2])
    # Heuristic seeds: most-played track in each of a few keys
    seed_keys = sorted(by_key, key=lambda k: -by_key[k][0][2])[:limit]
    out: list[Suggestion] = []
    used_globally: set[str] = set()
    for k in seed_keys:
        chain: list[str] = []
        used: set[str] = set()
        key = k
        for _ in range(target_length):
            candidates = []
            for compat in _camelot_compatible(key):
                for cid, _title, _plays in by_key.get(compat, []):
                    if cid in used or cid in used_globally:
                        continue
                    candidates.append((compat, cid, _plays))
            if not candidates:
                break
            candidates.sort(key=lambda c: -c[2])
            picked_key, picked_id, _ = candidates[0]
            chain.append(picked_id)
            used.add(picked_id)
            key = picked_key
        if len(chain) < 8:
            continue
        used_globally.update(chain)
        seed_title = by_key[k][0][1] if by_key[k] else ""
        out.append(Suggestion(
            id=f"key_chain_{len(out) + 1}",
            name=f"Key chain from {k} ({seed_title})",
            kind="key_chain",
            description=f"{len(chain)} harmonically-compatible tracks anchored on Camelot {k}.",
            content_ids=chain,
            rationale="Walks Camelot ±1 hour and relative major/minor. Greedy by total plays.",
            score=30.0 + len(chain),
        ))
    return out


def _camelot_compatible(key: str) -> list[str]:
    """Adjacent keys on the Camelot wheel: same, ±1 hour, swap A↔B."""
    if not key or len(key) < 2:
        return [key]
    try:
        n = int("".join(c for c in key if c.isdigit()))
    except ValueError:
        return [key]
    letter = key[-1].upper()
    other_letter = "B" if letter == "A" else "A"
    plus = ((n - 1 + 1) % 12) + 1
    minus = ((n - 1 - 1) % 12) + 1
    return [
        f"{n}{letter}",
        f"{plus}{letter}",
        f"{minus}{letter}",
        f"{n}{other_letter}",
    ]


# ---------------------------------------------------------------------------
# BPM ramp - warm-up curve
# ---------------------------------------------------------------------------

def _bpm_ramp(conn: sqlite3.Connection,
              target_length: int = 24) -> Optional[Suggestion]:
    """
    Build a tempo arc: low → peak → cooldown. Picks the most-played
    track in each BPM bucket along the curve so the result is at least
    half familiar.
    """
    arc = [110, 115, 120, 123, 125, 127, 128, 130, 132, 134, 138, 142,
           146, 148, 150, 152, 150, 145, 138, 130, 125, 120, 115, 110]
    arc = arc[:target_length]
    out: list[str] = []
    used: set[str] = set()
    for target_bpm in arc:
        row = conn.execute("""
            SELECT content_id
            FROM tracks
            WHERE in_library = 1 AND bpm IS NOT NULL
              AND content_id NOT IN (SELECT alias_content_id FROM track_aliases)
              AND ABS(bpm - ?) <= 1.5
            ORDER BY total_appearances DESC, ABS(bpm - ?) ASC
            LIMIT 5
        """, (target_bpm, target_bpm)).fetchall()
        for r in row:
            cid = str(r[0])
            if cid in used:
                continue
            out.append(cid)
            used.add(cid)
            break
    if len(out) < 6:
        return None
    return Suggestion(
        id="bpm_ramp",
        name="Warm-up → Peak → Cooldown",
        kind="bpm_ramp",
        description=f"{len(out)} tracks ramped through a BPM arc.",
        content_ids=out,
        rationale="Greedy by most-played within each BPM target band (±1.5 BPM).",
        score=25.0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _titles_for(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    for cid in ids:
        row = conn.execute(
            "SELECT title FROM tracks WHERE content_id = ?", (cid,)
        ).fetchone()
        out.append(str(row[0]) if row and row[0] else "Unknown")
    return out


def _iso_offset(days: int) -> str:
    import datetime
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
