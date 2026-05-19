"""
config.py - loads and validates config.json for Set Memory.

Returns a typed Config dataclass. Creates config.json with defaults if absent.
Raises ConfigError immediately on type mismatch so bad edits are caught early.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# Default USB UUID from RECON (device-specific; change in config.json if USB changes)
_DEFAULT_USB_UUID = ""


class ConfigError(Exception):
    """Raised when config.json contains an invalid type or unrecognised value."""


DEFAULTS: dict[str, Any] = {
    # Forgotten-favourites thresholds
    # Q4 note: DESIGN recommends 3 for lower-frequency players; default 5 means
    # a track needs ~5 session appearances before it qualifies as a "favourite."
    # If the forgotten list is always empty, lower this to 3 in config.json.
    "forgotten_min_appearances": 5,
    "forgotten_days_since_last": 90,
    "forgotten_limit": 10,
    # Never-played-after-add thresholds
    "never_played_min_days_since_add": 30,
    "never_played_limit": 10,
    # USB identity
    "usb_uuid": _DEFAULT_USB_UUID,
    "usb_pioneer_path": "/Volumes/PIONEER",
    # state.db location (relative to project root unless absolute)
    "state_db_path": "state.db",
    # digest.md location
    "digest_path": "digest.md",
    # Optional Jury integration (off by default)
    "append_to_jury_digest": False,
    "jury_digest_path": "~/Documents/cleanup-digest.md",
}

_TYPE_MAP: dict[str, type] = {
    "forgotten_min_appearances": int,
    "forgotten_days_since_last": int,
    "forgotten_limit": int,
    "never_played_min_days_since_add": int,
    "never_played_limit": int,
    "usb_uuid": str,
    "usb_pioneer_path": str,
    "state_db_path": str,
    "digest_path": str,
    "append_to_jury_digest": bool,
    "jury_digest_path": str,
}


@dataclass
class Config:
    forgotten_min_appearances: int = 5
    forgotten_days_since_last: int = 90
    forgotten_limit: int = 10
    never_played_min_days_since_add: int = 30
    never_played_limit: int = 10
    usb_uuid: str = _DEFAULT_USB_UUID
    usb_pioneer_path: str = "/Volumes/PIONEER"
    state_db_path: str = "state.db"
    digest_path: str = "digest.md"
    append_to_jury_digest: bool = False
    jury_digest_path: str = "~/Documents/cleanup-digest.md"

    def resolved_state_db(self) -> Path:
        """Return absolute path to state.db, resolving relative paths from project root."""
        p = Path(self.state_db_path)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p

    def resolved_digest(self) -> Path:
        """Return absolute path to digest.md."""
        p = Path(self.digest_path)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p


def load(config_path: Path = CONFIG_PATH) -> Config:
    """
    Load config.json, create with defaults if absent, validate types.

    Raises ConfigError on type mismatch. Never silently ignores bad values.
    """
    if not config_path.exists():
        log.info("config.json not found at %s - creating with defaults", config_path)
        _write_defaults(config_path)

    with config_path.open("r", encoding="utf-8") as fh:
        try:
            raw: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json is not valid JSON: {exc}") from exc

    merged = {**DEFAULTS, **raw}
    _validate(merged)

    return Config(
        forgotten_min_appearances=int(merged["forgotten_min_appearances"]),
        forgotten_days_since_last=int(merged["forgotten_days_since_last"]),
        forgotten_limit=int(merged["forgotten_limit"]),
        never_played_min_days_since_add=int(merged["never_played_min_days_since_add"]),
        never_played_limit=int(merged["never_played_limit"]),
        usb_uuid=str(merged["usb_uuid"]),
        usb_pioneer_path=str(merged["usb_pioneer_path"]),
        state_db_path=str(merged["state_db_path"]),
        digest_path=str(merged["digest_path"]),
        append_to_jury_digest=bool(merged["append_to_jury_digest"]),
        jury_digest_path=str(merged["jury_digest_path"]),
    )


def _validate(data: dict[str, Any]) -> None:
    for key, expected_type in _TYPE_MAP.items():
        if key not in data:
            continue
        val = data[key]
        # bool is a subtype of int in Python; check bool first to avoid false pass
        if expected_type is int and isinstance(val, bool):
            raise ConfigError(
                f"config.json: '{key}' must be an integer, got bool {val!r}"
            )
        if not isinstance(val, expected_type):
            raise ConfigError(
                f"config.json: '{key}' must be {expected_type.__name__}, "
                f"got {type(val).__name__} ({val!r})"
            )
        if expected_type is int and val < 0:
            raise ConfigError(
                f"config.json: '{key}' must be >= 0, got {val!r}"
            )


def _write_defaults(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(DEFAULTS, fh, indent=2)
        fh.write("\n")
