"""
config.py - load and validate config.json.

Returns a typed Config dataclass. Creates config.json with defaults on
first run. Raises ConfigError on type or range mismatch so bad edits are
caught immediately rather than silently producing empty results.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


class ConfigError(Exception):
    """config.json contains an invalid type, range, or unrecognised value."""


DEFAULTS: dict[str, Any] = {
    # Forgotten favourites
    "forgotten_min_appearances": 5,
    "forgotten_days_since_last": 90,
    "forgotten_limit": 10,
    # Never played (old library, never in a session)
    "never_played_min_days_since_add": 30,
    "never_played_limit": 10,
    # Recently added but unplayed (buy-regret signal)
    "recently_added_window_days": 30,
    "recently_added_limit": 10,
    # Prep audit (BPM / key / hot cues missing)
    "prep_limit": 15,
    # Co-appearance pairs
    "co_appearance_min_sessions": 3,
    "co_appearance_limit": 10,
    # Deleted-track detection
    "deleted_stale_days": 60,
    "deleted_limit": 10,
    # Sparkline depth
    "sparkline_months": 12,
    # State + digest paths
    "state_db_path": "state.db",
    "digest_path": "digest.md",
    # Optional Jury integration
    "append_to_jury_digest": False,
    "jury_digest_path": "~/Documents/cleanup-digest.md",
}


# (type, optional max). int max guards against typos producing silently-empty lists.
_FIELD_SPECS: dict[str, tuple[type, int | None]] = {
    "forgotten_min_appearances": (int, 10_000),
    "forgotten_days_since_last": (int, 36_500),
    "forgotten_limit": (int, 10_000),
    "never_played_min_days_since_add": (int, 36_500),
    "never_played_limit": (int, 10_000),
    "recently_added_window_days": (int, 36_500),
    "recently_added_limit": (int, 10_000),
    "prep_limit": (int, 10_000),
    "co_appearance_min_sessions": (int, 10_000),
    "co_appearance_limit": (int, 10_000),
    "deleted_stale_days": (int, 36_500),
    "deleted_limit": (int, 10_000),
    "sparkline_months": (int, 120),
    "state_db_path": (str, None),
    "digest_path": (str, None),
    "append_to_jury_digest": (bool, None),
    "jury_digest_path": (str, None),
}


@dataclass
class Config:
    forgotten_min_appearances: int = 5
    forgotten_days_since_last: int = 90
    forgotten_limit: int = 10
    never_played_min_days_since_add: int = 30
    never_played_limit: int = 10
    recently_added_window_days: int = 30
    recently_added_limit: int = 10
    prep_limit: int = 15
    co_appearance_min_sessions: int = 3
    co_appearance_limit: int = 10
    deleted_stale_days: int = 60
    deleted_limit: int = 10
    sparkline_months: int = 12
    state_db_path: str = "state.db"
    digest_path: str = "digest.md"
    append_to_jury_digest: bool = False
    jury_digest_path: str = "~/Documents/cleanup-digest.md"

    def resolved_state_db(self) -> Path:
        p = Path(self.state_db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def resolved_digest(self) -> Path:
        p = Path(self.digest_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load(config_path: Path = CONFIG_PATH) -> Config:
    """
    Load config.json (create with defaults if absent), validate.

    Raises ConfigError on type / range error. Unknown keys are ignored so
    older configs stay loadable after schema changes.
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
        recently_added_window_days=int(merged["recently_added_window_days"]),
        recently_added_limit=int(merged["recently_added_limit"]),
        prep_limit=int(merged["prep_limit"]),
        co_appearance_min_sessions=int(merged["co_appearance_min_sessions"]),
        co_appearance_limit=int(merged["co_appearance_limit"]),
        deleted_stale_days=int(merged["deleted_stale_days"]),
        deleted_limit=int(merged["deleted_limit"]),
        sparkline_months=int(merged["sparkline_months"]),
        state_db_path=str(merged["state_db_path"]),
        digest_path=str(merged["digest_path"]),
        append_to_jury_digest=bool(merged["append_to_jury_digest"]),
        jury_digest_path=str(merged["jury_digest_path"]),
    )


def _validate(data: dict[str, Any]) -> None:
    for key, (expected_type, max_value) in _FIELD_SPECS.items():
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
        if expected_type is int:
            if val < 0:
                raise ConfigError(f"config.json: '{key}' must be >= 0, got {val!r}")
            if max_value is not None and val > max_value:
                raise ConfigError(
                    f"config.json: '{key}' must be <= {max_value}, got {val!r}"
                )


def _write_defaults(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(DEFAULTS, fh, indent=2)
        fh.write("\n")
