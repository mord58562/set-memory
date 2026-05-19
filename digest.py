"""
digest.py - Renders digest.md and returns the macOS notification body for Set Memory.

No business logic here - formatting only. Writes atomically (tmp then os.replace).
All output must be free of U+2014 em-dashes (codepoint 0x2014). Tests verify this
with a grep-level codepoint check.

Framing note: Set Memory reads structured session log data from rekordbox's database.
It does NOT perform audio analysis, signal processing, or any form of listening.
Language in the digest must reflect this ("session log", "play data", "track records")
and must never imply audio analysis.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Optional

from analyse import AnalysisResult, ForgottenTrack, NeverPlayedTrack
from ingest import IngestSummary


def render(
    summary: IngestSummary,
    analysis: AnalysisResult,
    stats: dict,
    config_snippet: str,
    output_path: Path,
    today: Optional[datetime.date] = None,
) -> str:
    """
    Write digest.md atomically and return the notification body string.

    Parameters
    ----------
    summary:
        Ingest summary (sessions_found, sessions_new, sessions_skipped).
    analysis:
        AnalysisResult containing forgotten and never_played lists.
    stats:
        Dict from analyse.compute_summary_stats().
    config_snippet:
        Human-readable string describing active thresholds (e.g. "min 5 appearances,
        last seen > 90 days ago"). Shown in the section header.
    output_path:
        Path to write digest.md.
    today:
        Override for today's date. Injected in tests for determinism.
    """
    if today is None:
        today = datetime.date.today()

    content = _build_markdown(summary, analysis, stats, config_snippet, today)
    _write_atomic(output_path, content)
    notification_body = _build_notification(summary, analysis)
    return notification_body


def _build_markdown(
    summary: IngestSummary,
    analysis: AnalysisResult,
    stats: dict,
    config_snippet: str,
    today: datetime.date,
) -> str:
    lines: list[str] = []

    lines.append(f"# Set Memory Digest - {today.isoformat()}")
    lines.append("")
    lines.append(
        "_Set Memory reads rekordbox session log data. "
        "It does not perform audio analysis._"
    )
    lines.append("")

    # Sessions ingested this sync
    lines.append("## Sessions Ingested This Sync")
    lines.append("")
    lines.append(f"- {summary.sessions_new} new session(s) found")
    lines.append(f"- {summary.sessions_skipped} session(s) already in state.db, skipped")
    if summary.sessions_found == 0:
        lines.append("- No sessions found on USB (or USB not mounted)")
    lines.append("")

    # Forgotten favourites
    lines.append(f"## Forgotten Favourites ({config_snippet})")
    lines.append("")
    if analysis.forgotten:
        lines.append("| Title | Artist | Appearances | Last Session |")
        lines.append("|-------|--------|-------------|--------------|")
        for track in analysis.forgotten:
            title = _safe(track.title)
            artist = _safe(track.artist)
            lines.append(
                f"| {title} | {artist} | {track.total_appearances} "
                f"| {track.last_session_date} |"
            )
    else:
        lines.append("_No forgotten favourites meeting current thresholds._")
    lines.append("")

    # Never played after add
    lines.append("## Never Played After Add (in library >= threshold days, never in any session)")
    lines.append("")
    if analysis.never_played:
        lines.append("| Title | Artist | Date Added (file creation date) |")
        lines.append("|-------|--------|---------------------------------|")
        for track in analysis.never_played:
            title = _safe(track.title)
            artist = _safe(track.artist)
            date_added = track.date_created or "unknown"
            lines.append(f"| {title} | {artist} | {date_added} |")
        lines.append("")
        lines.append(
            "_Note: Date Added reflects the file creation date from rekordbox metadata, "
            "which may predate when the track was added to the library._"
        )
    else:
        lines.append("_No un-played tracks meeting current thresholds._")
    lines.append("")

    # Summary stats
    lines.append("## Summary Stats")
    lines.append("")
    lines.append(f"- Total sessions in state.db: {stats.get('total_sessions', 0)}")
    lines.append(f"- Total unique tracks ever played: {stats.get('total_unique_tracks', 0)}")
    lines.append(f"- Tracks in state.db: {stats.get('library_size', 0)}")
    lines.append(f"- State.db last updated: {stats.get('last_sync_at', 'never')}")
    lines.append("")

    return "\n".join(lines)


def _build_notification(summary: IngestSummary, analysis: AnalysisResult) -> str:
    """
    Build the macOS notification body string.

    Per DESIGN D10: body contains only counts, not track titles,
    to avoid displaying library contents in Notification Center.
    """
    if summary.sessions_new == 0:
        return "No new sessions found."
    forgotten_count = len(analysis.forgotten)
    return (
        f"{summary.sessions_new} new session(s). "
        f"{forgotten_count} forgotten track(s) surfaced."
    )


def _write_atomic(output_path: Path, content: str) -> None:
    """Write content to a tmp file then atomically replace the target."""
    tmp_path = output_path.parent / f".{output_path.name}.tmp"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(str(tmp_path), str(output_path))
    except Exception:
        # Clean up tmp on any error
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _safe(value: Optional[str]) -> str:
    """Return a Markdown-table-safe version of an optional string."""
    if value is None:
        return "Unknown"
    # Escape pipe characters to avoid breaking Markdown table formatting
    return value.replace("|", "/")
