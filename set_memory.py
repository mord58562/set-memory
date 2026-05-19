"""
set_memory.py - Entry point for Set Memory.

Thin orchestrator. No business logic here - delegates to ingest, analyse,
digest, notify. All errors are caught at top level; any unhandled exception
writes a fallback notification and exits 1.

Usage:
  python set_memory.py --on-mount
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Project root is wherever this script lives
PROJECT_ROOT = Path(__file__).parent

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("set_memory")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set Memory - rekordbox session log analyser"
    )
    parser.add_argument(
        "--on-mount",
        action="store_true",
        help="Run the on-mount pipeline (called by launchd on USB mount)",
    )
    args = parser.parse_args()

    if not args.on_mount:
        parser.print_help()
        return 1

    return _run_on_mount()


def _run_on_mount() -> int:
    """Execute the on-mount pipeline. Returns exit code (0=success, 1=error)."""
    # Deferred imports so the entry point is fast to parse
    import config as cfg_module
    import ingest
    import analyse
    import digest
    import notify

    # Step 1: Check USB pioneer path
    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        log.error("Config error: %s", exc)
        notify.fire("Set Memory", "Config error - check logs.")
        return 1

    # Step 1a: First-run guard. Empty usb_uuid means the user hasn't set up
    # their device yet. Exit 0 with a clear message rather than failing later
    # on an unrelated mount or crashing on the UUID check.
    if not conf.usb_uuid:
        log.info(
            "usb_uuid is empty in config.json. Set it to your USB's device "
            "UUID (see README) before Set Memory can identify your drive. "
            "Exiting 0."
        )
        return 0

    pioneer_path = Path(conf.usb_pioneer_path)
    if not pioneer_path.exists():
        log.info(
            "Pioneer path %s not found - not the configured USB or USB not mounted. Exiting 0.",
            pioneer_path,
        )
        return 0

    # Step 2: UUID check
    device_backup_dir = pioneer_path / "DeviceLibBackup"
    if not _check_uuid(device_backup_dir, conf.usb_uuid):
        log.info("UUID mismatch or not found - not the configured DJ USB. Exiting 0.")
        return 0

    # Step 3: Locate USB master.db
    usb_db_path = pioneer_path / "Master" / "master.db"
    if not usb_db_path.exists():
        log.warning("master.db not found at %s", usb_db_path)
        notify.fire("Set Memory", "master.db not found on USB.")
        return 1

    # Step 4: Open / create state.db
    state_db_path = conf.resolved_state_db()
    try:
        state_conn = _open_state_db(state_db_path)
    except Exception as exc:
        log.error("Failed to open state.db: %s", exc)
        notify.fire("Set Memory", "state.db error - check logs.")
        return 1

    # Step 5-9: Ingest from USB
    try:
        ingest_summary = ingest.ingest_from_usb(usb_db_path, state_conn)
    except ingest.WalLockError as exc:
        log.error("USB WAL lock: %s", exc)
        _write_error_digest(
            conf.resolved_digest(),
            "USB locked - retry on next mount.",
        )
        notify.fire("Set Memory", "USB locked - retry on next mount.")
        state_conn.close()
        return 1
    except ingest.SchemaError as exc:
        log.error("Schema incompatibility: %s", exc)
        notify.fire("Set Memory", "Schema error - check logs.")
        state_conn.close()
        return 1
    except RuntimeError as exc:
        # SQLCipher key fetch failure
        log.error("Key/decryption error: %s", exc)
        _write_error_digest(conf.resolved_digest(), str(exc))
        notify.fire("Set Memory", "Key error - check logs.")
        state_conn.close()
        return 1
    except Exception as exc:
        log.exception("Unexpected ingest error: %s", exc)
        notify.fire("Set Memory", "Ingest error - check logs.")
        state_conn.close()
        return 1

    # Step 10: Analyse
    try:
        analysis = analyse.run(
            state_conn=state_conn,
            forgotten_min_appearances=conf.forgotten_min_appearances,
            forgotten_days_since_last=conf.forgotten_days_since_last,
            forgotten_limit=conf.forgotten_limit,
            never_played_min_days_since_add=conf.never_played_min_days_since_add,
            never_played_limit=conf.never_played_limit,
        )
        stats = analyse.compute_summary_stats(state_conn)
    except Exception as exc:
        log.exception("Analysis error: %s", exc)
        notify.fire("Set Memory", "Analysis error - check logs.")
        state_conn.close()
        return 1

    state_conn.close()

    # Step 11-12: Write digest
    config_snippet = (
        f"appeared >= {conf.forgotten_min_appearances} times, "
        f"last seen > {conf.forgotten_days_since_last} days ago"
    )
    try:
        notification_body = digest.render(
            summary=ingest_summary,
            analysis=analysis,
            stats=stats,
            config_snippet=config_snippet,
            output_path=conf.resolved_digest(),
        )
    except Exception as exc:
        log.exception("Digest write error: %s", exc)
        notify.fire("Set Memory", "Digest error - check logs.")
        return 1

    # Step 13: Notify
    notify.fire("Set Memory", notification_body)

    log.info(
        "Done. %d new session(s), %d forgotten track(s). Digest: %s",
        ingest_summary.sessions_new,
        len(analysis.forgotten),
        conf.resolved_digest(),
    )
    return 0


def _check_uuid(device_backup_dir: Path, expected_uuid: str) -> bool:
    """
    Check that the USB device UUID matches the expected value.

    Reads rbDevLibBaInfo_*.json under device_backup_dir and looks for the
    UUID field. Returns False if the directory is absent, no JSON is found,
    or the UUID does not match.
    """
    if not device_backup_dir.exists():
        return False
    pattern = str(device_backup_dir / "rbDevLibBaInfo_*.json")
    matches = glob.glob(pattern)
    if not matches:
        log.warning("No rbDevLibBaInfo_*.json found in %s", device_backup_dir)
        return False
    for json_path in matches:
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # The UUID may be stored under various keys; try common ones
            uuid_val = (
                data.get("uuid")
                or data.get("UUID")
                or data.get("deviceUUID")
                or data.get("libraryUUID")
                or ""
            )
            if uuid_val.lower().replace("-", "") == expected_uuid.lower().replace("-", ""):
                return True
        except Exception as exc:
            log.warning("Could not parse %s: %s", json_path, exc)
    return False


def _open_state_db(state_db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) state.db. On corruption, recreate from schema.

    Returns an open connection. Caller is responsible for closing.
    """
    import ingest

    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(state_db_path))
        conn.row_factory = sqlite3.Row
        ingest.ensure_schema(conn)
        return conn
    except sqlite3.DatabaseError as exc:
        log.warning("state.db appears corrupt (%s); recreating...", exc)
        state_db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(state_db_path))
        conn.row_factory = sqlite3.Row
        ingest.ensure_schema(conn)
        log.info("Fresh state.db created at %s", state_db_path)
        return conn


def _write_error_digest(digest_path: Path, error_message: str) -> None:
    """Write a minimal digest.md noting the error condition."""
    import datetime
    today = datetime.date.today().isoformat()
    content = (
        f"# Set Memory Digest - {today}\n\n"
        f"## Error\n\n"
        f"Set Memory encountered an error and could not complete this sync.\n\n"
        f"**Error:** {error_message}\n\n"
        f"Check `~/Downloads/set-memory/logs/stderr.log` for details.\n"
    )
    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not write error digest: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
