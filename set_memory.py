"""
set_memory.py - Entry point for Set Memory.

Thin orchestrator. No business logic here - delegates to ingest, analyse,
digest, notify. All errors are caught at top level; any unhandled exception
writes a fallback notification and exits 1.

Discovery model: every USB mount fires launchd, which calls this entry
point with --on-mount. The script scans /Volumes/*/PIONEER/Master/master.db,
ingests anything it finds, and exits silently for mounts that aren't
rekordbox USBs (no PIONEER folder). Multiple DJ USBs accumulate into
the same state.db; sessions are deduplicated by content fingerprint, so
re-mounting or mirrored drives never double-count.

Usage:
  python set_memory.py --on-mount
"""

from __future__ import annotations

import argparse
import glob
import logging
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


def discover_rekordbox_usbs(volumes_root: Path = Path("/Volumes")) -> list[Path]:
    """
    Return every currently mounted volume that has a rekordbox library
    on it, as a list of master.db paths. A drive counts as rekordbox-
    bearing if it has both PIONEER/Master/master.db and PIONEER/rekordbox/
    on disk. We deliberately don't filter by volume name or UUID - users
    rename drives, reformat them, swap them out, and the tool should
    pick up whatever is plugged in without configuration.
    """
    if not volumes_root.exists():
        return []
    found: list[Path] = []
    for vol in volumes_root.iterdir():
        if not vol.is_dir():
            continue
        pioneer = vol / "PIONEER"
        master_db = pioneer / "Master" / "master.db"
        rekordbox_dir = pioneer / "rekordbox"
        # Require BOTH master.db and the rekordbox subfolder. A bare
        # /PIONEER/ on a random drive (e.g. a backup folder a user
        # happens to have created) without the rekordbox export tree
        # isn't a real DJ drive.
        if master_db.is_file() and rekordbox_dir.is_dir():
            found.append(master_db)
    return sorted(found)


def _run_on_mount() -> int:
    """Execute the on-mount pipeline. Returns exit code (0=success, 1=error)."""
    # Deferred imports so the entry point is fast to parse
    import config as cfg_module
    import ingest
    import analyse
    import digest
    import notify

    # Step 1: Load config
    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        log.error("Config error: %s", exc)
        notify.fire("Set Memory", "Config error - check logs.")
        return 1

    # Step 2: Discover rekordbox USBs across all mounted volumes
    usb_db_paths = discover_rekordbox_usbs()
    if not usb_db_paths:
        log.info("No rekordbox USB mounted - exiting silently.")
        return 0
    log.info("Found %d rekordbox USB(s): %s",
             len(usb_db_paths),
             ", ".join(str(p.parent.parent.parent) for p in usb_db_paths))

    # Step 3: Open / create state.db
    state_db_path = conf.resolved_state_db()
    try:
        state_conn = _open_state_db(state_db_path)
    except Exception as exc:
        log.error("Failed to open state.db: %s", exc)
        notify.fire("Set Memory", "state.db error - check logs.")
        return 1

    # Step 4: Ingest each discovered USB. Collect per-USB summaries
    # so the analysis + digest can report the total work done in
    # one run. A per-USB failure (locked WAL, decryption error,
    # schema drift) is logged and the loop continues - one bad
    # drive shouldn't abort the others.
    combined_summary = None
    per_usb_errors: list[str] = []
    for usb_db_path in usb_db_paths:
        usb_label = usb_db_path.parent.parent.parent.name  # /Volumes/<label>
        try:
            usb_summary = ingest.ingest_from_usb(usb_db_path, state_conn)
            log.info("[%s] %d new session(s) ingested.",
                     usb_label, usb_summary.sessions_new)
            combined_summary = _merge_summary(combined_summary, usb_summary)
        except ingest.WalLockError as exc:
            log.warning("[%s] WAL lock: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: locked (eject cleanly and replug)")
        except ingest.SchemaError as exc:
            log.warning("[%s] Schema incompatibility: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: rekordbox schema not recognised")
        except RuntimeError as exc:
            # SQLCipher key fetch failure - applies to every USB this run
            log.error("Key/decryption error: %s", exc)
            _write_error_digest(conf.resolved_digest(), str(exc))
            notify.fire("Set Memory", "Key error - check logs.")
            state_conn.close()
            return 1
        except Exception as exc:
            log.exception("[%s] Unexpected ingest error: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: {exc}")

    # If every USB failed, surface that as a notification.
    if combined_summary is None:
        log.warning("Every USB ingest failed.")
        notify.fire("Set Memory",
                    f"USB ingest failed ({len(per_usb_errors)} drive(s)) - check logs.")
        state_conn.close()
        return 1

    # Step 5: Analyse
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

    # Step 6: Write digest
    config_snippet = (
        f"appeared >= {conf.forgotten_min_appearances} times, "
        f"last seen > {conf.forgotten_days_since_last} days ago"
    )
    try:
        notification_body = digest.render(
            summary=combined_summary,
            analysis=analysis,
            stats=stats,
            config_snippet=config_snippet,
            output_path=conf.resolved_digest(),
        )
    except Exception as exc:
        log.exception("Digest write error: %s", exc)
        notify.fire("Set Memory", "Digest error - check logs.")
        return 1

    # Step 7: Notify. Per-USB errors get appended to the body so
    # the user notices a partial failure without having to open the log.
    if per_usb_errors:
        notification_body += f"\n{len(per_usb_errors)} drive(s) failed."
    notify.fire("Set Memory", notification_body)

    log.info(
        "Done. %d new session(s), %d forgotten track(s). Digest: %s",
        combined_summary.sessions_new,
        len(analysis.forgotten),
        conf.resolved_digest(),
    )
    return 0


def _merge_summary(existing, new):
    """
    Combine two ingest summaries into one. The summary type comes from
    ingest.py and is a simple dataclass; we widen the totals additively
    so the digest can report "N new sessions" across every USB in one
    run rather than emitting one digest per drive.
    """
    if existing is None:
        return new
    # Both summaries are simple dataclasses with integer counters and
    # list fields. Replace with the union; ingest produces these so we
    # mirror its shape rather than importing the type here.
    existing.sessions_new = getattr(existing, "sessions_new", 0) + getattr(new, "sessions_new", 0)
    existing.sessions_seen = getattr(existing, "sessions_seen", 0) + getattr(new, "sessions_seen", 0)
    existing.appearances_inserted = getattr(existing, "appearances_inserted", 0) + getattr(new, "appearances_inserted", 0)
    existing.tracks_upserted = getattr(existing, "tracks_upserted", 0) + getattr(new, "tracks_upserted", 0)
    # Extend list-typed fields if they exist
    for attr in ("new_session_ids",):
        if hasattr(existing, attr) and hasattr(new, attr):
            getattr(existing, attr).extend(getattr(new, attr))
    return existing


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
