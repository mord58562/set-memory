"""
digest.py - Render digest.md and produce the macOS notification body.

Atomic write (tmp + os.replace). No em-dashes anywhere (U+2014 banned;
test enforces).

Framing: Set Memory reads structured session log data. It never analyses
audio. Language in the digest must reflect that ("session log", "play
data", "track records") and must never imply audio analysis.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Optional

from analyse import (
    AnalysisResult,
    CoAppearancePair,
    DeletedCandidate,
    DistributionBuckets,
    ForgottenTrack,
    NeverPlayedTrack,
    PrepIssue,
    RecentlyAddedUnplayed,
)
from ingest import IngestSummary


_SPARK_GLYPHS = " ▁▂▃▄▅▆▇█"


def render(
    summary: IngestSummary,
    analysis: AnalysisResult,
    stats: dict,
    config_snippet: str,
    output_path: Path,
    today: Optional[datetime.date] = None,
    usb_drives: Optional[list[dict]] = None,
) -> str:
    """
    Write digest.md atomically. Return the macOS notification body string.

    config_snippet describes the active forgotten-favourites thresholds for
    the section header. usb_drives is the list returned by
    analyse.usb_drive_summary() and renders the per-USB section when given.
    """
    if today is None:
        today = datetime.date.today()

    content = _build_markdown(summary, analysis, stats, config_snippet, today, usb_drives or [])
    _write_atomic(output_path, content)
    return _build_notification(summary, analysis)


def _build_markdown(
    summary: IngestSummary,
    analysis: AnalysisResult,
    stats: dict,
    config_snippet: str,
    today: datetime.date,
    usb_drives: list[dict],
) -> str:
    lines: list[str] = []

    # Header + headline
    lines.append(f"# Set Memory Digest - {today.isoformat()}")
    lines.append("")
    lines.append(
        "_Set Memory reads rekordbox session log data. "
        "It does not perform audio analysis._"
    )
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(_headline(summary, analysis, stats))
    lines.append("")

    # Sparkline of session activity over the last N months
    if analysis.sessions_by_month:
        lines.append("## Activity")
        lines.append("")
        lines.append(_sparkline_line(analysis.sessions_by_month))
        lines.append("")

    # Sessions ingested this sync
    lines.append("## Sessions Ingested This Sync")
    lines.append("")
    lines.append(f"- {summary.sessions_new} new session(s) found")
    lines.append(f"- {summary.sessions_skipped} session(s) already in state.db, skipped")
    lines.append(
        f"- Library: {summary.library_size} track(s) on USB, "
        f"{summary.library_added} new to state.db this sync"
    )
    if summary.sessions_found == 0:
        lines.append("- No sessions found on USB (or USB not mounted)")
    lines.append("")

    # USB drives
    if usb_drives:
        lines.append("## USB Drives")
        lines.append("")
        lines.append("| Drive | Last Mounted | Library Size |")
        lines.append("|-------|--------------|--------------|")
        for d in usb_drives:
            lines.append(
                f"| {_safe(d.get('volume_label'))} "
                f"| {_safe(d.get('last_seen_at'))} "
                f"| {d.get('library_size', 0)} |"
            )
        lines.append("")

    # Forgotten favourites
    lines.append(f"## Forgotten Favourites ({config_snippet})")
    lines.append("")
    if analysis.forgotten:
        lines.append("| Title | Artist | Appearances | Last Session |")
        lines.append("|-------|--------|-------------|--------------|")
        for t in analysis.forgotten:
            lines.append(
                f"| {_safe(t.title)} | {_safe(t.artist)} | {t.total_appearances} "
                f"| {t.last_session_date} |"
            )
    else:
        lines.append("_No forgotten favourites meeting current thresholds._")
    lines.append("")

    # Never played after add
    lines.append("## Never Played After Add (in library >= threshold days, never in any session)")
    lines.append("")
    if analysis.never_played:
        lines.append("| Title | Artist | Added |")
        lines.append("|-------|--------|-------|")
        for t in analysis.never_played:
            added = t.added_at or t.date_created or "unknown"
            lines.append(f"| {_safe(t.title)} | {_safe(t.artist)} | {added} |")
        lines.append("")
        lines.append(
            "_Added uses rekordbox StockDate when populated; otherwise "
            "file creation date (may predate library import)._"
        )
    else:
        lines.append("_No un-played tracks meeting current thresholds._")
    lines.append("")

    # Recently added unplayed (buy regret)
    lines.append("## Recently Added, Not Played Yet")
    lines.append("")
    if analysis.recently_added_unplayed:
        lines.append("| Title | Artist | Added | Days |")
        lines.append("|-------|--------|-------|------|")
        for t in analysis.recently_added_unplayed:
            lines.append(
                f"| {_safe(t.title)} | {_safe(t.artist)} | {t.added_at} "
                f"| {t.days_since_added} |"
            )
    else:
        lines.append("_All recent additions have been played, or no recent additions._")
    lines.append("")

    # Prep audit
    lines.append("## Prep Audit (BPM / key / hot cues missing)")
    lines.append("")
    if analysis.prep_issues:
        lines.append("| Title | Artist | Missing |")
        lines.append("|-------|--------|---------|")
        for p in analysis.prep_issues:
            tags = []
            if p.missing_bpm: tags.append("BPM")
            if p.missing_key: tags.append("key")
            if p.missing_hot_cues: tags.append("hot cues")
            lines.append(
                f"| {_safe(p.title)} | {_safe(p.artist)} | {', '.join(tags) or '-'} |"
            )
        lines.append("")
        lines.append(
            "_Sorted by most-played first. Tracks already in rotation are "
            "the most valuable to prep._"
        )
    else:
        lines.append("_Library is fully prepped (BPM, key, and at least one hot cue per track)._")
    lines.append("")

    # Co-appearance pairs
    lines.append("## Played Together (track pairs sharing many sessions)")
    lines.append("")
    if analysis.co_appearance:
        lines.append("| A | B | Sessions |")
        lines.append("|---|---|----------|")
        for p in analysis.co_appearance:
            a = f"{_safe(p.a_title)} ({_safe(p.a_artist)})"
            b = f"{_safe(p.b_title)} ({_safe(p.b_artist)})"
            lines.append(f"| {a} | {b} | {p.shared_sessions} |")
    else:
        lines.append("_Not enough overlap yet for co-appearance pairs._")
    lines.append("")

    # Distribution
    lines.append("## Distribution (plays across BPM and key)")
    lines.append("")
    lines.extend(_distribution_lines(analysis.distribution))
    lines.append("")

    # Deleted candidates
    if analysis.deleted_candidates:
        lines.append("## Possibly Deleted (in state.db, not seen in any synced library lately)")
        lines.append("")
        lines.append("| Title | Artist | Last in Library | Appearances |")
        lines.append("|-------|--------|-----------------|-------------|")
        for d in analysis.deleted_candidates:
            lines.append(
                f"| {_safe(d.title)} | {_safe(d.artist)} "
                f"| {d.last_in_library_at or 'unknown'} | {d.total_appearances} |"
            )
        lines.append("")

    # Summary stats
    lines.append("## Summary Stats")
    lines.append("")
    lines.append(f"- Total sessions in state.db: {stats.get('total_sessions', 0)}")
    lines.append(f"- Total unique tracks ever played: {stats.get('total_unique_tracks', 0)}")
    lines.append(f"- Tracks currently in library: {stats.get('library_size', 0)}")
    lines.append(f"- Tracks in state.db (incl. deleted): {stats.get('state_track_count', 0)}")
    lines.append(f"- USB drives seen: {stats.get('usb_drives_seen', 0)}")
    lines.append(f"- State.db last updated: {stats.get('last_sync_at', 'never')}")
    lines.append("")

    return "\n".join(lines)


def _headline(summary: IngestSummary, analysis: AnalysisResult, stats: dict) -> str:
    bits = []
    if summary.sessions_new > 0:
        bits.append(f"{summary.sessions_new} new session(s) ingested")
    if summary.library_added > 0:
        bits.append(f"{summary.library_added} new track(s) in library")
    if analysis.forgotten:
        bits.append(f"{len(analysis.forgotten)} forgotten favourite(s) surfaced")
    if analysis.recently_added_unplayed:
        bits.append(f"{len(analysis.recently_added_unplayed)} recent buy(s) still unplayed")
    if analysis.prep_issues:
        bits.append(f"{len(analysis.prep_issues)} prep issue(s)")
    if not bits:
        return "_Nothing new this sync._"
    return ". ".join(bits) + "."


def _sparkline_line(by_month: dict[str, int]) -> str:
    """Render a sparkline of session counts across months."""
    values = list(by_month.values())
    if not values:
        return "_No session data yet._"
    peak = max(values) or 1
    glyphs = "".join(
        _SPARK_GLYPHS[min(len(_SPARK_GLYPHS) - 1, int(round(v / peak * (len(_SPARK_GLYPHS) - 1))))]
        for v in values
    )
    first = next(iter(by_month))
    last = list(by_month)[-1]
    total = sum(values)
    return f"`{glyphs}` ({first} to {last}, {total} sessions, peak {peak}/mo)"


def _distribution_lines(dist: DistributionBuckets) -> list[str]:
    total = sum(dist.bpm.values()) or 1
    bpm_lines = ["**BPM (share of plays):**", ""]
    for label, count in dist.bpm.items():
        pct = 100 * count / total
        bpm_lines.append(f"- {label}: {count} ({pct:.0f}%)")
    top_keys = sorted(dist.key.items(), key=lambda x: x[1], reverse=True)[:8]
    bpm_lines.append("")
    bpm_lines.append("**Top keys (Camelot, by plays):**")
    bpm_lines.append("")
    for key_label, count in top_keys:
        bpm_lines.append(f"- {key_label}: {count}")
    return bpm_lines


def _build_notification(summary: IngestSummary, analysis: AnalysisResult) -> str:
    if summary.sessions_new == 0 and summary.library_added == 0:
        return "No new sessions found."
    parts = []
    if summary.sessions_new:
        parts.append(f"{summary.sessions_new} new session(s)")
    if summary.library_added:
        parts.append(f"{summary.library_added} new track(s) in library")
    if analysis.forgotten:
        parts.append(f"{len(analysis.forgotten)} forgotten track(s) surfaced")
    return ". ".join(parts) + "."


def _write_atomic(output_path: Path, content: str) -> None:
    tmp_path = output_path.parent / f".{output_path.name}.tmp"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(str(tmp_path), str(output_path))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _safe(value: Optional[str]) -> str:
    if value is None:
        return "Unknown"
    # Backslash-escape pipes so Markdown tables don't shatter.
    return str(value).replace("|", r"\|")
