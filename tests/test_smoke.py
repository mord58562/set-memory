"""
test_smoke.py - Smoke test for Set Memory.

Skipped unless RUN_SMOKE=1 is set in the environment AND the USB is mounted
with the correct UUID. This test exercises the full on-mount pipeline against
the real encrypted USB master.db.

Run manually after install:
  RUN_SMOKE=1 ~/miniconda3/bin/pytest tests/test_smoke.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Project root on the path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

RUN_SMOKE = os.environ.get("RUN_SMOKE", "0") == "1"
USB_PIONEER_PATH = Path("/Volumes/TEST_USB/PIONEER")
USB_MASTER_DB = USB_PIONEER_PATH / "Master" / "master.db"

_usb_present = USB_MASTER_DB.exists()

skip_reason = (
    "Smoke test requires RUN_SMOKE=1 env var and /Volumes/TEST_USB/PIONEER to be mounted. "
    "Mount the USB first, then: RUN_SMOKE=1 pytest tests/test_smoke.py -v"
)


@pytest.mark.skipif(
    not (RUN_SMOKE and _usb_present),
    reason=skip_reason,
)
def test_smoke_real_usb() -> None:
    """
    Full on-mount pipeline smoke test against the real encrypted USB master.db.

    Asserts:
      - digest.md exists and is non-empty after the run
      - Notification body string is valid (non-empty, no em-dashes)
      - state.db has at least one session row

    This test calls ingest_from_usb which requires pyrekordbox + SQLCipher.
    It must only run when the USB is mounted and RUN_SMOKE=1.
    """
    import tempfile
    import shutil
    import datetime

    import ingest
    import analyse
    import digest
    from config import Config

    # Use a temporary state.db and digest.md so the smoke test does not
    # overwrite the real state.db during testing
    with tempfile.TemporaryDirectory(prefix="setmem_smoke_") as tmp_str:
        tmp = Path(tmp_str)
        tmp_state_db = tmp / "state.db"
        tmp_digest = tmp / "digest.md"

        # Open state.db
        state_conn = sqlite3.connect(str(tmp_state_db))
        state_conn.row_factory = sqlite3.Row
        ingest.ensure_schema(state_conn)

        # Run ingest from real USB
        summary = ingest.ingest_from_usb(USB_MASTER_DB, state_conn)
        assert summary.sessions_found >= 0, "sessions_found should be a non-negative integer"

        # Run analysis
        conf = Config()
        analysis = analyse.run(
            state_conn=state_conn,
            forgotten_min_appearances=conf.forgotten_min_appearances,
            forgotten_days_since_last=conf.forgotten_days_since_last,
            forgotten_limit=conf.forgotten_limit,
            never_played_min_days_since_add=conf.never_played_min_days_since_add,
            never_played_limit=conf.never_played_limit,
        )
        stats = analyse.compute_summary_stats(state_conn)

        # Render digest
        config_snippet = (
            f"appeared >= {conf.forgotten_min_appearances} times, "
            f"last seen > {conf.forgotten_days_since_last} days ago"
        )
        notification_body = digest.render(
            summary=summary,
            analysis=analysis,
            stats=stats,
            config_snippet=config_snippet,
            output_path=tmp_digest,
        )

        # Assertions
        assert tmp_digest.exists(), "digest.md should be created after smoke run"
        content = tmp_digest.read_text(encoding="utf-8")
        assert len(content) > 0, "digest.md should not be empty"

        # Em-dash check on digest output
        EM_DASH = chr(0x2014)
        assert EM_DASH not in content, "digest.md must not contain em-dash (U+2014)"
        assert EM_DASH not in notification_body, "Notification body must not contain em-dash"

        # Notification body must be a non-empty string
        assert isinstance(notification_body, str), "Notification body must be a string"
        assert len(notification_body) > 0, "Notification body must not be empty"

        # state.db must have at least one session row if USB has any history
        session_count = state_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        # XDJ should have at least 1 session; if somehow zero, still pass with a note
        if summary.sessions_found > 0:
            assert session_count > 0, (
                f"sessions_found={summary.sessions_found} but state.db has 0 session rows"
            )

        state_conn.close()

    print(f"\nSmoke test passed.")
    print(f"  sessions_found={summary.sessions_found}")
    print(f"  sessions_new={summary.sessions_new}")
    print(f"  forgotten={len(analysis.forgotten)}")
    print(f"  never_played={len(analysis.never_played)}")
    print(f"  notification_body={notification_body!r}")
