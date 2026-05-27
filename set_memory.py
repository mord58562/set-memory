"""
set_memory.py - Entry point for Set Memory.

Two modes:
  --on-mount      run the full pipeline (called by launchd on USB mount)
  query <kind>    print one analysis surface to stdout against the current
                  state.db, no USB required. Useful for tweaking thresholds
                  without remounting. kinds: forgotten, never-played,
                  recent-unplayed, prep, together, deleted, distribution,
                  usb, search

Discovery model: every USB mount fires launchd, which invokes this entry
point with --on-mount. The script scans /Volumes/*/PIONEER/Master/master.db,
ingests anything it finds, and exits silently for mounts that aren't
rekordbox USBs. Multiple DJ USBs accumulate into the same state.db.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("set_memory")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set Memory - rekordbox session log analyser")
    sub = parser.add_subparsers(dest="cmd")
    parser.add_argument("--on-mount", action="store_true",
                        help="Run the on-mount pipeline (called by launchd on USB mount)")
    parser.add_argument("--volume", type=str, default=None,
                        help="If set, ingest only from this /Volumes/<label> USB. "
                             "Without this flag every mounted rekordbox USB is ingested.")

    q = sub.add_parser("query", help="Print an analysis to stdout against current state.db")
    q.add_argument("kind", choices=[
        "forgotten", "never-played", "recent-unplayed", "prep",
        "together", "deleted", "distribution", "usb", "search", "sessions",
    ])
    q.add_argument("--limit", type=int, default=None,
                   help="Override the configured limit for this query")
    q.add_argument("--search", type=str, default=None,
                   help="Substring to match against title/artist (kind=search)")
    q.add_argument("--since", type=str, default=None,
                   help="ISO date (YYYY-MM-DD) - only return rows newer than this")

    cp = sub.add_parser(
        "create-playlist",
        help="Create a playlist in Mac rekordbox 6 from a Set Memory surface.",
    )
    cp.add_argument("name", help="Playlist name to create in rekordbox.")
    cp.add_argument(
        "--kind",
        choices=["forgotten", "never-played", "recent-unplayed", "prep",
                 "together", "deleted"],
        default=None,
        help="Pull canonical IDs from this analysis surface.",
    )
    cp.add_argument(
        "--content-ids",
        type=str,
        default=None,
        help="Comma-separated Set Memory canonical content IDs (alternative to --kind).",
    )
    cp.add_argument(
        "--suggestion",
        type=str,
        default=None,
        help="ID of a playlist suggestion (from `set_memory.py suggestions`).",
    )
    cp.add_argument(
        "--limit", type=int, default=None,
        help="Override the configured limit when using --kind.",
    )

    sg = sub.add_parser(
        "suggestions",
        help="List auto-generated playlist suggestions (JSON).",
    )
    sg.add_argument("--limit-per-kind", type=int, default=5)
    sg.add_argument("--id", type=str, default=None,
                    help="If set, print only the suggestion with this id.")
    sg.add_argument("--include-dismissed", action="store_true",
                    help="Also include suggestions previously hidden.")

    dm = sub.add_parser(
        "dismiss-suggestion",
        help="Hide a suggestion from future runs of `suggestions`.",
    )
    dm.add_argument("suggestion_id", help="Suggestion id to hide.")

    un = sub.add_parser(
        "undismiss-suggestion",
        help="Restore a previously-hidden suggestion. Use --all to restore everything.",
    )
    un.add_argument("suggestion_id", nargs="?", default=None,
                    help="Specific suggestion id, or omit and use --all.")
    un.add_argument("--all", action="store_true",
                    help="Restore every dismissed suggestion.")

    args = parser.parse_args()

    if args.cmd == "query":
        return _run_query(args)
    if args.cmd == "create-playlist":
        return _run_create_playlist(args)
    if args.cmd == "suggestions":
        return _run_suggestions(args)
    if args.cmd == "dismiss-suggestion":
        return _run_dismiss_suggestion(args)
    if args.cmd == "undismiss-suggestion":
        return _run_undismiss_suggestion(args)
    if args.on_mount:
        return _run_on_mount(volume_filter=args.volume)
    parser.print_help()
    return 1


def discover_rekordbox_usbs(volumes_root: Path = Path("/Volumes")) -> list[Path]:
    """
    Return every mounted volume that has a rekordbox library on it, as a
    list of paths to the library file (either master.db or export.pdb).

    Two layouts are accepted:
      - Desktop-rekordbox export: PIONEER/Master/master.db (SQLCipher).
      - CDJ export mode:          PIONEER/rekordbox/export.pdb (DeviceSQL).

    No label matching - renaming or reformatting a drive doesn't change
    behaviour. master.db takes priority if both are present (richer schema).
    """
    if not volumes_root.exists():
        return []
    found: list[Path] = []
    for vol in volumes_root.iterdir():
        if not vol.is_dir():
            continue
        master_db = vol / "PIONEER" / "Master" / "master.db"
        rekordbox_dir = vol / "PIONEER" / "rekordbox"
        export_pdb = rekordbox_dir / "export.pdb"
        if master_db.is_file() and rekordbox_dir.is_dir():
            found.append(master_db)
        elif export_pdb.is_file():
            found.append(export_pdb)
    return sorted(found)


def _run_on_mount(volume_filter: Optional[str] = None) -> int:
    import config as cfg_module
    import ingest
    import analyse
    import digest
    import notify

    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        log.error("Config error: %s", exc)
        notify.fire("Set Memory", "Config error - check logs.")
        return 1

    usb_db_paths = discover_rekordbox_usbs()
    if volume_filter:
        usb_db_paths = [p for p in usb_db_paths
                        if p.parent.parent.parent.name == volume_filter]
    if not usb_db_paths:
        log.info("No matching CDJ-export USB mounted - exiting silently.")
        return 0
    log.info("Found %d CDJ-export USB(s): %s", len(usb_db_paths),
             ", ".join(str(p.parent.parent.parent) for p in usb_db_paths))

    # Surface the GUI immediately on mount so Rob sees Set Memory the
    # moment he plugs a USB in - not 5s later when ingest finishes. The
    # app's state.db file watcher repaints automatically when sync writes.
    _surface_gui_app()

    state_db_path = conf.resolved_state_db()
    try:
        state_conn = _open_state_db(state_db_path)
    except Exception as exc:
        log.error("Failed to open state.db: %s", exc)
        notify.fire("Set Memory", "state.db error - check logs.")
        return 1

    combined = ingest.IngestSummary()
    per_usb_errors: list[str] = []
    any_success = False
    for usb_db_path in usb_db_paths:
        usb_label = usb_db_path.parent.parent.parent.name
        is_pdb = usb_db_path.name.endswith(".pdb")
        try:
            if is_pdb:
                usb_summary = ingest.ingest_from_pdb(
                    usb_db_path, state_conn, volume_label=usb_label,
                )
            else:
                usb_summary = ingest.ingest_from_usb(
                    usb_db_path, state_conn, volume_label=usb_label,
                )
            log.info("[%s] %d new session(s) ingested. Library: %d.",
                     usb_label, usb_summary.sessions_new, usb_summary.library_size)
            combined.merge(usb_summary)
            any_success = True
        except ingest.WalLockError as exc:
            log.warning("[%s] WAL lock: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: locked (eject cleanly and replug)")
        except ingest.SchemaError as exc:
            log.warning("[%s] Schema incompatibility: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: rekordbox schema not recognised")
        except RuntimeError as exc:
            log.error("Key/decryption error: %s", exc)
            _write_error_digest(conf.resolved_digest(), str(exc))
            notify.fire("Set Memory", "Key error - check logs.", open_path=conf.resolved_digest())
            state_conn.close()
            return 1
        except Exception as exc:
            log.exception("[%s] Unexpected ingest error: %s", usb_label, exc)
            per_usb_errors.append(f"{usb_label}: {exc}")

    if not any_success:
        log.warning("Every USB ingest failed.")
        notify.fire("Set Memory",
                    f"USB ingest failed ({len(per_usb_errors)} drive(s)) - check logs.",
                    open_path=conf.resolved_digest())
        state_conn.close()
        return 1

    try:
        analysis = analyse.run(
            state_conn=state_conn,
            forgotten_min_appearances=conf.forgotten_min_appearances,
            forgotten_days_since_last=conf.forgotten_days_since_last,
            forgotten_limit=conf.forgotten_limit,
            never_played_min_days_since_add=conf.never_played_min_days_since_add,
            never_played_limit=conf.never_played_limit,
            recently_added_window_days=conf.recently_added_window_days,
            recently_added_limit=conf.recently_added_limit,
            prep_limit=conf.prep_limit,
            co_appearance_min_sessions=conf.co_appearance_min_sessions,
            co_appearance_limit=conf.co_appearance_limit,
            deleted_stale_days=conf.deleted_stale_days,
            deleted_limit=conf.deleted_limit,
            sparkline_months=conf.sparkline_months,
        )
        stats = analyse.compute_summary_stats(state_conn)
        usb_drives = analyse.usb_drive_summary(state_conn)
    except Exception as exc:
        log.exception("Analysis error: %s", exc)
        notify.fire("Set Memory", "Analysis error - check logs.", open_path=conf.resolved_digest())
        state_conn.close()
        return 1

    state_conn.close()

    config_snippet = (
        f"appeared >= {conf.forgotten_min_appearances} times, "
        f"last seen > {conf.forgotten_days_since_last} days ago"
    )
    try:
        notification_body = digest.render(
            summary=combined,
            analysis=analysis,
            stats=stats,
            config_snippet=config_snippet,
            output_path=conf.resolved_digest(),
            usb_drives=usb_drives,
        )
    except Exception as exc:
        log.exception("Digest write error: %s", exc)
        notify.fire("Set Memory", "Digest error - check logs.")
        return 1

    # Notifications are now strictly opt-in: the GUI is the surface for
    # routine results. We only fire a system notification when something
    # actually needs interrupting the user:
    #   - per-USB failure (a drive Rob plugged in didn't work)
    #   - first-ever sync (welcome moment so Rob notices the GUI now has data)
    # Everything else stays silent; the GUI re-renders live via its
    # state.db file watcher.
    is_first_sync = stats.get("total_sessions", 0) == combined.sessions_new
    if per_usb_errors:
        body = f"{len(per_usb_errors)} drive(s) failed to sync."
        notify.fire("Set Memory", body, open_path=conf.resolved_digest())
    elif is_first_sync and combined.sessions_new > 0:
        notify.fire("Set Memory",
                    f"First sync: {combined.sessions_new} session(s), "
                    f"{combined.library_size} track(s) in library. Click to open.",
                    open_path=Path("/Applications/SetMemory.app"))

    log.info("Done. %d new session(s), %d forgotten, %d prep issue(s). Digest: %s",
             combined.sessions_new, len(analysis.forgotten),
             len(analysis.prep_issues), conf.resolved_digest())
    return 0


def _surface_gui_app() -> None:
    """Open SetMemory.app if it exists. -g (background) so the dock icon
    bounces without stealing focus from whatever Rob is doing."""
    import subprocess
    app_path = "/Applications/SetMemory.app"
    if not Path(app_path).exists():
        return
    try:
        subprocess.run(["open", "-g", app_path], check=False, timeout=5)
    except Exception as exc:
        log.debug("Couldn't surface SetMemory.app: %s", exc)


# ---------------------------------------------------------------------------
# query subcommand
# ---------------------------------------------------------------------------

def _run_query(args: argparse.Namespace) -> int:
    import config as cfg_module
    import analyse

    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    db_path = conf.resolved_state_db()
    if not db_path.exists():
        print(f"No state.db at {db_path}. Mount a rekordbox USB to seed it.",
              file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        if args.kind == "search":
            return _query_search(conn, args.search, args.limit or 25)
        if args.kind == "sessions":
            return _query_sessions(conn, args.since, args.limit or 25)
        if args.kind == "usb":
            return _query_usb(conn, analyse)

        result = analyse.run(
            state_conn=conn,
            forgotten_min_appearances=conf.forgotten_min_appearances,
            forgotten_days_since_last=conf.forgotten_days_since_last,
            forgotten_limit=args.limit or conf.forgotten_limit,
            never_played_min_days_since_add=conf.never_played_min_days_since_add,
            never_played_limit=args.limit or conf.never_played_limit,
            recently_added_window_days=conf.recently_added_window_days,
            recently_added_limit=args.limit or conf.recently_added_limit,
            prep_limit=args.limit or conf.prep_limit,
            co_appearance_min_sessions=conf.co_appearance_min_sessions,
            co_appearance_limit=args.limit or conf.co_appearance_limit,
            deleted_stale_days=conf.deleted_stale_days,
            deleted_limit=args.limit or conf.deleted_limit,
            sparkline_months=conf.sparkline_months,
        )
        _print_result(args.kind, result)
        return 0
    finally:
        conn.close()


def _print_result(kind: str, result) -> None:
    if kind == "forgotten":
        for t in result.forgotten:
            print(f"{t.total_appearances:>3}x  {t.last_session_date[:10]}  "
                  f"{(t.title or '?')[:60]:60}  {t.artist or ''}")
    elif kind == "never-played":
        for t in result.never_played:
            added = (t.added_at or t.date_created or "?")[:10]
            print(f"{added}  {(t.title or '?')[:60]:60}  {t.artist or ''}")
    elif kind == "recent-unplayed":
        for t in result.recently_added_unplayed:
            print(f"{t.days_since_added:>3}d  {(t.title or '?')[:60]:60}  {t.artist or ''}")
    elif kind == "prep":
        for p in result.prep_issues:
            tags = ",".join(t for t, on in [
                ("BPM", p.missing_bpm), ("key", p.missing_key), ("cues", p.missing_hot_cues),
            ] if on)
            print(f"{tags:14}  {(p.title or '?')[:60]:60}  {p.artist or ''}")
    elif kind == "together":
        for p in result.co_appearance:
            a = f"{p.a_title} ({p.a_artist})"
            b = f"{p.b_title} ({p.b_artist})"
            print(f"{p.shared_sessions:>3}x  {a[:50]:50} + {b[:50]}")
    elif kind == "deleted":
        for d in result.deleted_candidates:
            print(f"{d.last_in_library_at or '?':>26}  "
                  f"{(d.title or '?')[:50]:50}  {d.artist or ''}  ({d.total_appearances}x)")
    elif kind == "distribution":
        print("BPM:")
        for k, v in result.distribution.bpm.items():
            print(f"  {k:10}  {v}")
        print("Top keys:")
        for k, v in sorted(result.distribution.key.items(), key=lambda x: -x[1])[:8]:
            print(f"  {k:10}  {v}")


def _query_search(conn: sqlite3.Connection, term: str | None, limit: int) -> int:
    if not term:
        print("--search TERM required for search query", file=sys.stderr)
        return 1
    like = f"%{term}%"
    rows = conn.execute("""
        SELECT title, artist, total_appearances, in_library, last_in_library_at
        FROM tracks
        WHERE title LIKE ? OR artist LIKE ?
        ORDER BY total_appearances DESC, title ASC
        LIMIT ?
    """, (like, like, limit)).fetchall()
    for r in rows:
        marker = "*" if r["in_library"] else " "
        print(f"{marker} {r['total_appearances']:>3}x  "
              f"{(r['title'] or '?')[:60]:60}  {r['artist'] or ''}")
    if not rows:
        print(f"(no matches for {term!r})")
    return 0


def _query_sessions(conn: sqlite3.Connection, since: str | None, limit: int) -> int:
    args: list = []
    where = ""
    if since:
        where = "WHERE session_date >= ?"
        args.append(since)
    sql = f"""
        SELECT session_id, session_date, track_count, source_db_path
        FROM sessions {where}
        ORDER BY session_date DESC LIMIT ?
    """
    args.append(limit)
    for r in conn.execute(sql, args):
        print(f"#{r['session_id']:>4}  {r['session_date'][:16]}  "
              f"{r['track_count']:>2} tracks  {Path(r['source_db_path']).parent.parent.parent.name}")
    return 0


def _query_usb(conn: sqlite3.Connection, analyse_module) -> int:
    drives = analyse_module.usb_drive_summary(conn)
    if not drives:
        print("(no USBs recorded yet - mount one to populate)")
        return 0
    for d in drives:
        print(f"{d['volume_label']:30}  last seen {d['last_seen_at'][:19]}  "
              f"{d['library_size']:>4} tracks")
    return 0


# ---------------------------------------------------------------------------
# create-playlist subcommand
# ---------------------------------------------------------------------------

def _run_create_playlist(args: argparse.Namespace) -> int:
    """
    Push a Set Memory track list into Mac rekordbox 6 as a new playlist.
    Emits a JSON document to stdout on success; non-zero exit on any
    error, with a JSON error envelope on stdout for the GUI to parse.
    """
    import json
    import config as cfg_module
    import rekordbox_writer

    sources = [bool(args.kind), bool(args.content_ids), bool(args.suggestion)]
    if sum(sources) == 0:
        print(json.dumps({"error": "Provide --kind, --content-ids or --suggestion."}))
        return 2
    if sum(sources) > 1:
        print(json.dumps({"error": "Use only ONE of --kind / --content-ids / --suggestion."}))
        return 2

    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        print(json.dumps({"error": f"Config error: {exc}"}))
        return 1

    state_db_path = conf.resolved_state_db()
    if not state_db_path.exists():
        print(json.dumps({"error": f"No state.db at {state_db_path}."}))
        return 1

    warnings: list[str] = []
    if args.content_ids:
        content_ids = [s.strip() for s in args.content_ids.split(",") if s.strip()]
    elif args.suggestion:
        import playlist_suggester
        conn = sqlite3.connect(str(state_db_path))
        conn.row_factory = sqlite3.Row
        try:
            suggestions = playlist_suggester.generate_all(conn)
        finally:
            conn.close()
        match = next((s for s in suggestions if s.id == args.suggestion), None)
        if match is None:
            print(json.dumps({"error": f"Unknown suggestion id: {args.suggestion}"}))
            return 1
        content_ids = list(match.content_ids)
    else:
        try:
            content_ids = _content_ids_for_kind(args.kind, conf, state_db_path,
                                                args.limit, warnings)
        except Exception as exc:
            print(json.dumps({"error": f"Failed to load {args.kind} surface: {exc}"}))
            return 1
    if not content_ids:
        print(json.dumps({"error": "No track IDs to add (surface was empty)."}))
        return 1

    try:
        result = rekordbox_writer.create_playlist(
            name=args.name,
            set_memory_content_ids=content_ids,
            state_db_path=state_db_path,
        )
    except rekordbox_writer.RekordboxLocked as exc:
        print(json.dumps({"error": str(exc), "code": "rekordbox_locked"}))
        return 3
    except rekordbox_writer.RekordboxNotFound as exc:
        print(json.dumps({"error": str(exc), "code": "rekordbox_not_found"}))
        return 4
    except rekordbox_writer.TrackMatchProblem as exc:
        print(json.dumps({
            "error": str(exc),
            "code": "no_tracks_matched",
            "unmatched_count": len(exc.dedup_keys_not_found),
        }))
        return 5
    except Exception as exc:
        log.exception("Unexpected playlist write error")
        print(json.dumps({"error": f"Unexpected error: {exc}"}))
        return 1

    payload = {
        "playlist_id": result["playlist_id"],
        "tracks_added": result["tracks_added"],
        "unmatched": result["unmatched_count"],
        "backup_path": result.get("backup_path"),
        "warnings": warnings,
    }
    print(json.dumps(payload))
    return 0


def _run_suggestions(args: argparse.Namespace) -> int:
    """Print auto-generated playlist suggestions as JSON to stdout."""
    import json
    import config as cfg_module
    import playlist_suggester

    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        print(json.dumps({"error": f"Config error: {exc}"}))
        return 1

    state_db_path = conf.resolved_state_db()
    if not state_db_path.exists():
        print(json.dumps({"error": f"No state.db at {state_db_path}."}))
        return 1

    conn = sqlite3.connect(str(state_db_path))
    conn.row_factory = sqlite3.Row
    try:
        playlist_suggester  # ensure imported above; satisfies linters
        import ingest
        ingest.ensure_schema(conn)
        suggestions = playlist_suggester.generate_all(
            conn, limit_per_kind=args.limit_per_kind,
            include_dismissed=getattr(args, "include_dismissed", False),
        )
        dismissed = playlist_suggester.dismissed_ids(conn)
    finally:
        conn.close()

    if args.id:
        match = next((s for s in suggestions if s.id == args.id), None)
        if match is None:
            print(json.dumps({"error": f"No suggestion with id={args.id}"}))
            return 1
        suggestions = [match]

    payload = [
        {
            "id": s.id, "name": s.name, "kind": s.kind,
            "description": s.description, "content_ids": s.content_ids,
            "rationale": s.rationale, "score": s.score,
            "dismissed": s.id in dismissed,
        }
        for s in suggestions
    ]
    print(json.dumps(payload))
    return 0


def _run_dismiss_suggestion(args: argparse.Namespace) -> int:
    import json
    import config as cfg_module
    import ingest
    import playlist_suggester
    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        print(json.dumps({"error": f"Config error: {exc}"})); return 1
    state_db_path = conf.resolved_state_db()
    if not state_db_path.exists():
        print(json.dumps({"error": f"No state.db at {state_db_path}."})); return 1
    conn = sqlite3.connect(str(state_db_path))
    try:
        ingest.ensure_schema(conn)
        playlist_suggester.dismiss(conn, args.suggestion_id)
    finally:
        conn.close()
    print(json.dumps({"dismissed": args.suggestion_id}))
    return 0


def _run_undismiss_suggestion(args: argparse.Namespace) -> int:
    import json
    import config as cfg_module
    import ingest
    import playlist_suggester
    try:
        conf = cfg_module.load()
    except cfg_module.ConfigError as exc:
        print(json.dumps({"error": f"Config error: {exc}"})); return 1
    state_db_path = conf.resolved_state_db()
    if not state_db_path.exists():
        print(json.dumps({"error": f"No state.db at {state_db_path}."})); return 1
    if not args.all and not args.suggestion_id:
        print(json.dumps({"error": "Provide a suggestion_id or --all."})); return 2
    conn = sqlite3.connect(str(state_db_path))
    try:
        ingest.ensure_schema(conn)
        if args.all:
            n = playlist_suggester.clear_dismissed(conn)
            print(json.dumps({"undismissed_all": n}))
        else:
            playlist_suggester.undismiss(conn, args.suggestion_id)
            print(json.dumps({"undismissed": args.suggestion_id}))
    finally:
        conn.close()
    return 0


def _content_ids_for_kind(
    kind: str, conf, state_db_path: Path, limit: Optional[int],
    warnings: list[str],
) -> list[str]:
    """Extract canonical content IDs for the named analysis surface."""
    import analyse

    conn = sqlite3.connect(str(state_db_path))
    conn.row_factory = sqlite3.Row
    try:
        result = analyse.run(
            state_conn=conn,
            forgotten_min_appearances=conf.forgotten_min_appearances,
            forgotten_days_since_last=conf.forgotten_days_since_last,
            forgotten_limit=limit or conf.forgotten_limit,
            never_played_min_days_since_add=conf.never_played_min_days_since_add,
            never_played_limit=limit or conf.never_played_limit,
            recently_added_window_days=conf.recently_added_window_days,
            recently_added_limit=limit or conf.recently_added_limit,
            prep_limit=limit or conf.prep_limit,
            co_appearance_min_sessions=conf.co_appearance_min_sessions,
            co_appearance_limit=limit or conf.co_appearance_limit,
            deleted_stale_days=conf.deleted_stale_days,
            deleted_limit=limit or conf.deleted_limit,
            sparkline_months=conf.sparkline_months,
        )
    finally:
        conn.close()

    if kind == "forgotten":
        return [t.content_id for t in result.forgotten]
    if kind == "never-played":
        return [t.content_id for t in result.never_played]
    if kind == "recent-unplayed":
        return [t.content_id for t in result.recently_added_unplayed]
    if kind == "prep":
        return [p.content_id for p in result.prep_issues]
    if kind == "deleted":
        return [d.content_id for d in result.deleted_candidates]
    if kind == "together":
        # CoAppearancePair stores titles, not IDs. Re-resolve via state.db.
        conn = sqlite3.connect(str(state_db_path))
        conn.row_factory = sqlite3.Row
        try:
            ids: list[str] = []
            seen: set[str] = set()
            for p in result.co_appearance:
                for title, artist in ((p.a_title, p.a_artist), (p.b_title, p.b_artist)):
                    if not title:
                        continue
                    row = conn.execute(
                        "SELECT content_id FROM tracks "
                        "WHERE title = ? AND artist = ? LIMIT 1",
                        (title, artist),
                    ).fetchone()
                    if row and row["content_id"] not in seen:
                        seen.add(row["content_id"])
                        ids.append(row["content_id"])
            return ids
        finally:
            conn.close()
    warnings.append(f"Unknown kind: {kind}")
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_state_db(state_db_path: Path) -> sqlite3.Connection:
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
