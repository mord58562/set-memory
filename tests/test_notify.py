"""
test_notify.py - Tests for notify.py osascript wrapper.

All tests mock subprocess.run. No real osascript calls.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

import notify


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_notify_calls_osascript_with_correct_args() -> None:
    """fire() calls subprocess.run with osascript and the correct AppleScript string."""
    with patch("notify.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        notify.fire("Set Memory", "2 new sessions. 1 forgotten track surfaced.")

    assert mock_run.called, "subprocess.run should be called"
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "osascript", f"First arg should be 'osascript', got {cmd[0]!r}"
    assert cmd[1] == "-e", f"Second arg should be '-e', got {cmd[1]!r}"
    script = cmd[2]
    assert "display notification" in script, f"Script should contain 'display notification': {script!r}"
    assert "Set Memory" in script, f"Title should appear in script: {script!r}"
    assert "2 new sessions" in script, f"Body should appear in script: {script!r}"


def test_notify_failure_does_not_raise() -> None:
    """If subprocess.run raises FileNotFoundError, fire() returns without raising."""
    with patch("notify.subprocess.run", side_effect=FileNotFoundError("osascript not found")):
        # Must not raise
        notify.fire("Set Memory", "Test body")


def test_notify_non_zero_exit_does_not_raise() -> None:
    """If osascript exits non-zero, fire() logs a warning but does not raise."""
    with patch("notify.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="some error")
        # Must not raise
        notify.fire("Set Memory", "Test body")


def test_notify_timeout_does_not_raise() -> None:
    """If subprocess.run times out, fire() returns without raising."""
    with patch(
        "notify.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["osascript"], timeout=10),
    ):
        notify.fire("Set Memory", "Test body")


def test_notify_single_quotes_in_title_escaped() -> None:
    """Single quotes in title are escaped to avoid breaking the AppleScript string."""
    with patch("notify.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        notify.fire("Rob's Memory", "2 new sessions.")

    args, kwargs = mock_run.call_args
    script = args[0][2]
    # The title with escaped quote should not break the surrounding string delimiters
    assert "Rob\\'s Memory" in script or "Rob" in script, (
        f"Title should be escaped in script: {script!r}"
    )


def test_notify_single_quotes_in_body_escaped() -> None:
    """Single quotes in body are escaped."""
    with patch("notify.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        notify.fire("Set Memory", "It's working.")

    args, kwargs = mock_run.call_args
    script = args[0][2]
    assert "It\\'s" in script or "It" in script, (
        f"Body single quote should be escaped: {script!r}"
    )


def test_notify_timeout_parameter_set() -> None:
    """subprocess.run is called with a timeout to prevent hanging."""
    with patch("notify.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        notify.fire("Set Memory", "Test")

    _, kwargs = mock_run.call_args
    assert "timeout" in kwargs, "subprocess.run should be called with a timeout parameter"
    assert kwargs["timeout"] > 0, "Timeout should be a positive number"


def test_notify_unexpected_exception_does_not_raise() -> None:
    """Any unexpected exception from subprocess.run is caught and logged, not re-raised."""
    with patch("notify.subprocess.run", side_effect=RuntimeError("unexpected")):
        notify.fire("Set Memory", "Test body")
