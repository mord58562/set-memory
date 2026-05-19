"""
notify.py - macOS notification via osascript for Set Memory.

Single public function: fire(title, body).

Failure (osascript not found, non-zero exit, any OS error) is logged but never
raised - a notification failure must never block the digest write or pipeline exit.

The notification body is always a count string (never track titles) per DESIGN D10.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def fire(title: str, body: str) -> None:
    """
    Display a macOS notification via osascript.

    Safe to call in any environment - if osascript is missing or fails,
    the error is logged and the function returns normally.

    Parameters
    ----------
    title:
        Notification title (shown in bold). Keep short.
    body:
        Notification body text. Should contain only counts, not track titles,
        to avoid surfacing library contents in Notification Center.
    """
    # Escape single quotes in title and body to avoid breaking the AppleScript string
    safe_title = title.replace("'", "\\'")
    safe_body = body.replace("'", "\\'")
    script = (
        f"display notification '{safe_body}' with title '{safe_title}'"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning(
                "osascript returned non-zero exit %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
        else:
            log.debug("Notification fired: %r / %r", title, body)
    except FileNotFoundError:
        log.warning(
            "osascript not found - notification not sent. "
            "This is expected in non-macOS environments."
        )
    except subprocess.TimeoutExpired:
        log.warning("osascript timed out after 10 seconds - notification not sent.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Unexpected error firing notification: %s", exc)
