"""
test_digest.py - Tests for digest.py output format and em-dash compliance.

All tests use in-memory/tmp paths. No USB db access.
The em-dash codepoint check (U+2014 = 0xe2 0x80 0x94) is a hard requirement
per feedback_no_em_dashes and the em_dash_codepoint_grep memory.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import digest
from analyse import AnalysisResult, ForgottenTrack, NeverPlayedTrack
from ingest import IngestSummary
from tests.conftest import FROZEN_TODAY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EM_DASH_CODEPOINT = chr(0x2014)  # U+2014


def _make_summary(sessions_new: int = 2, sessions_found: int = 3, sessions_skipped: int = 1) -> IngestSummary:
    s = IngestSummary(
        sessions_found=sessions_found,
        sessions_new=sessions_new,
        sessions_skipped=sessions_skipped,
    )
    s.new_session_ids = list(range(1, sessions_new + 1))
    return s


def _make_analysis(
    forgotten_count: int = 2,
    never_played_count: int = 1,
) -> AnalysisResult:
    forgotten = [
        ForgottenTrack(
            content_id=f"C{i:03d}",
            title=f"Forgotten Track {i}",
            artist=f"Artist {i}",
            total_appearances=5 + i,
            last_session_date="2025-09-01",
        )
        for i in range(1, forgotten_count + 1)
    ]
    never_played = [
        NeverPlayedTrack(
            content_id=f"N{i:03d}",
            title=f"Never Track {i}",
            artist=f"Artist {i}",
            date_created="2026-01-15",
        )
        for i in range(1, never_played_count + 1)
    ]
    return AnalysisResult(forgotten=forgotten, never_played=never_played)


def _make_stats() -> dict:
    return {
        "total_sessions": 10,
        "total_unique_tracks": 50,
        "library_size": 75,
        "last_sync_at": "2026-05-12T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_digest_written_atomically(tmp_path: Path) -> None:
    """
    digest.md must be written via a tmp file then os.replace (atomic write).

    We verify that os.replace is called, confirming the atomic write path.
    """
    output = tmp_path / "digest.md"
    summary = _make_summary()
    analysis = _make_analysis()
    stats = _make_stats()

    replace_calls = []
    original_replace = os.replace

    def capturing_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        original_replace(src, dst)

    with patch("digest.os.replace", side_effect=capturing_replace):
        digest.render(
            summary=summary,
            analysis=analysis,
            stats=stats,
            config_snippet="test config",
            output_path=output,
            today=FROZEN_TODAY,
        )

    assert len(replace_calls) == 1, "os.replace should be called exactly once"
    src_path, dst_path = replace_calls[0]
    assert dst_path == str(output), f"Destination should be {output}"
    assert ".tmp" in src_path, f"Source should be a .tmp file, got {src_path}"
    assert output.exists(), "digest.md should exist after render"


def test_digest_contains_all_sections(tmp_path: Path) -> None:
    """Given known inputs, all four section headers must be present in digest.md."""
    output = tmp_path / "digest.md"
    summary = _make_summary()
    analysis = _make_analysis()
    stats = _make_stats()

    digest.render(
        summary=summary,
        analysis=analysis,
        stats=stats,
        config_snippet="appeared >= 5 times, last seen > 90 days ago",
        output_path=output,
        today=FROZEN_TODAY,
    )

    content = output.read_text(encoding="utf-8")
    assert "## Sessions Ingested This Sync" in content
    assert "## Forgotten Favourites" in content
    assert "## Never Played After Add" in content
    assert "## Summary Stats" in content


def test_digest_session_count_in_notification_body(tmp_path: Path) -> None:
    """Notification body must contain 'N new session' matching the ingest count."""
    output = tmp_path / "digest.md"
    summary = _make_summary(sessions_new=3)
    analysis = _make_analysis(forgotten_count=2)

    notification_body = digest.render(
        summary=summary,
        analysis=analysis,
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )

    assert "3 new session" in notification_body.lower() or "3 new session" in notification_body, (
        f"Expected '3 new session' in notification body: {notification_body!r}"
    )


def test_digest_zero_new_sessions_message(tmp_path: Path) -> None:
    """When sessions_new == 0, notification body must contain 'No new sessions found'."""
    output = tmp_path / "digest.md"
    summary = _make_summary(sessions_new=0, sessions_found=5, sessions_skipped=5)
    analysis = _make_analysis(forgotten_count=0, never_played_count=0)

    notification_body = digest.render(
        summary=summary,
        analysis=analysis,
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )

    assert "no new sessions" in notification_body.lower(), (
        f"Expected 'No new sessions found' in: {notification_body!r}"
    )


def test_digest_no_em_dashes(tmp_path: Path) -> None:
    """
    U+2014 em-dash must not appear anywhere in digest output.

    This is a codepoint-level check (not visual scan) per the em-dash feedback memory.
    The check covers both the written file and the notification body string.
    """
    output = tmp_path / "digest.md"
    summary = _make_summary()
    analysis = _make_analysis(forgotten_count=3, never_played_count=3)
    stats = _make_stats()

    notification_body = digest.render(
        summary=summary,
        analysis=analysis,
        stats=stats,
        config_snippet="appeared >= 5 times, last seen > 90 days ago",
        output_path=output,
        today=FROZEN_TODAY,
    )

    file_content = output.read_text(encoding="utf-8")

    # Codepoint check on file
    em_dash_count_file = file_content.count(EM_DASH_CODEPOINT)
    assert em_dash_count_file == 0, (
        f"Found {em_dash_count_file} em-dash(es) (U+2014) in digest.md"
    )

    # Codepoint check on notification body
    em_dash_count_body = notification_body.count(EM_DASH_CODEPOINT)
    assert em_dash_count_body == 0, (
        f"Found {em_dash_count_body} em-dash(es) (U+2014) in notification body"
    )


def test_digest_contains_date(tmp_path: Path) -> None:
    """The digest header must contain the frozen date."""
    output = tmp_path / "digest.md"
    digest.render(
        summary=_make_summary(),
        analysis=_make_analysis(),
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )
    content = output.read_text(encoding="utf-8")
    assert FROZEN_TODAY.isoformat() in content, (
        f"Expected date {FROZEN_TODAY.isoformat()} in digest header"
    )


def test_digest_empty_forgotten_shows_placeholder(tmp_path: Path) -> None:
    """When forgotten list is empty, placeholder text is shown instead of a table."""
    output = tmp_path / "digest.md"
    analysis = AnalysisResult(forgotten=[], never_played=[])
    digest.render(
        summary=_make_summary(),
        analysis=analysis,
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )
    content = output.read_text(encoding="utf-8")
    assert "No forgotten favourites" in content


def test_digest_empty_never_played_shows_placeholder(tmp_path: Path) -> None:
    """When never_played list is empty, placeholder text is shown instead of a table."""
    output = tmp_path / "digest.md"
    analysis = AnalysisResult(forgotten=[], never_played=[])
    digest.render(
        summary=_make_summary(),
        analysis=analysis,
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )
    content = output.read_text(encoding="utf-8")
    assert "No un-played tracks" in content


def test_digest_notification_body_contains_forgotten_count(tmp_path: Path) -> None:
    """Notification body must include forgotten track count."""
    output = tmp_path / "digest.md"
    summary = _make_summary(sessions_new=2)
    analysis = _make_analysis(forgotten_count=4)

    body = digest.render(
        summary=summary,
        analysis=analysis,
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )
    assert "4 forgotten" in body.lower() or "4" in body, (
        f"Expected forgotten count in notification body: {body!r}"
    )


def test_digest_does_not_claim_audio_analysis(tmp_path: Path) -> None:
    """
    Digest must not contain language CLAIMING audio listening or analysis.

    Set Memory reads structured session log data; it does not analyse audio.
    Per feedback_no_fake_listening: never claim the system listened or analysed audio.

    We check for phrases that assert audio analysis happened (active voice),
    not for phrases that disclaim it (which is fine and expected in the footer note).
    """
    output = tmp_path / "digest.md"
    digest.render(
        summary=_make_summary(),
        analysis=_make_analysis(),
        stats=_make_stats(),
        config_snippet="test",
        output_path=output,
        today=FROZEN_TODAY,
    )
    content = output.read_text(encoding="utf-8").lower()

    # Phrases that would assert audio analysis occurred (these must be absent)
    # Note: "does not perform audio analysis" is a disclaimer, not a claim -
    # so we check for positive/active-voice assertions only.
    forbidden_claim_phrases = [
        "analysed audio",
        "listened to",
        "by ear",
        "matched loudness",
        "reverse-engineered from listening",
        "audio signal",
    ]
    for phrase in forbidden_claim_phrases:
        assert phrase not in content, (
            f"Digest must not claim audio analysis; found forbidden phrase: {phrase!r}"
        )

    # The digest should positively disclaim audio analysis
    assert "does not perform audio analysis" in content, (
        "Digest should contain the standard audio-analysis disclaimer"
    )
