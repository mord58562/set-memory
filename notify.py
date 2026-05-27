"""
notify.py - macOS notification for Set Memory.

fire(title, body, open_path=None) tries terminal-notifier first (clickable,
opens open_path on click); falls back to osascript (no click action).

Notification failure is logged and never raised. The body should contain
counts, not track titles (DESIGN D10).

History note: an earlier version single-quoted the AppleScript string,
which doesn't compile (AppleScript strings need double quotes); every
notification silently failed with syntax error -2741. This is the fix.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def fire(title: str, body: str, open_path: Optional[Path] = None) -> None:
    """
    Display a macOS notification.

    If terminal-notifier is installed (brew install terminal-notifier) and
    open_path is given, the notification is clickable and opens that file.
    Otherwise falls back to osascript (no click action).
    """
    if shutil.which("terminal-notifier") and open_path is not None:
        if _fire_terminal_notifier(title, body, open_path):
            return
    _fire_osascript(title, body)


def _fire_terminal_notifier(title: str, body: str, open_path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "terminal-notifier",
                "-title", title,
                "-message", body,
                "-open", f"file://{open_path}",
                "-sender", "com.apple.Terminal",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.debug("terminal-notifier fired: %r / %r", title, body)
            return True
        log.warning("terminal-notifier non-zero exit %d: %s",
                    result.returncode, result.stderr.strip())
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("terminal-notifier failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("terminal-notifier unexpected error: %s", exc)
        return False


def _fire_osascript(title: str, body: str) -> None:
    # AppleScript strings are double-quoted only. Escape backslashes first,
    # then double quotes. Single-quoted forms (any flavour) don't compile.
    safe_title = _applescript_escape(title)
    safe_body = _applescript_escape(body)
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("osascript non-zero exit %d: %s",
                        result.returncode, result.stderr.strip())
        else:
            log.debug("osascript notification fired: %r / %r", title, body)
    except FileNotFoundError:
        log.warning("osascript not found - notification not sent. "
                    "Expected on non-macOS environments.")
    except subprocess.TimeoutExpired:
        log.warning("osascript timed out after 10 seconds - notification not sent.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Unexpected error firing notification: %s", exc)


def _applescript_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
