# Set Memory - Design Document

**Date:** 2026-05-12
**Status:** Pre-implementation design. No code written yet.

---

## 1. File Layout

```
~/Downloads/set-memory/
  set_memory.py          # entry point: --on-mount flag, orchestrates the pipeline
  ingest.py              # USB master.db -> state.db: session + appearance ingestion
  analyse.py             # forgotten-favourites + never-played-after-add computation
  digest.py              # writes digest.md in structured markdown
  notify.py              # fires macOS notification via osascript
  config.py              # loads/validates config.json; typed dataclass
  state.db               # SQLite log; accumulates across all syncs; never leaves machine
  digest.md              # latest digest; overwritten on each sync
  config.json            # user-editable thresholds (forgotten days, min appearances, etc.)
  launchd/
    com.mord58562.setmemory.plist   # StartOnMount launchd agent; calls set_memory.py --on-mount
  scripts/
    install.sh           # brew deps, pip install, key download, plist install, smoke test
    uninstall.sh         # removes plist, leaves state.db and digest.md intact
  tests/
    conftest.py          # shared fixtures: in-memory state.db, synthetic USB db
    test_ingest.py       # ingest module unit + integration tests
    test_analyse.py      # forgotten and never-played logic tests
    test_digest.py       # digest output format tests
    test_notify.py       # notify module tests (mocked osascript)
    test_smoke.py        # smoke test: real USB mount path if available
    fixtures/
      synthetic_master.db      # minimal SQLCipher-free SQLite DB with djmd* tables
      config_default.json      # reference config
  README.md
  RECON.md
  DESIGN.md
```

---

## 2. Architecture Decisions

### D1 - SQLCipher Key Acquisition

Key is fetched via `python -m pyrekordbox download-key` on first run only. The key is written to `~/.pyrekordbox/key` (pyrekordbox's own cache path). On subsequent runs, check that the cache file exists and is non-empty before attempting a network call. If the cache is absent, fetch it. This avoids a Pioneer network round-trip on every USB mount (adds latency, risks failure at a moment when USB is the trigger). Document the cache path explicitly in README so Rob can locate it if needed.

Reasoning: network calls during an on-mount trigger are a single point of failure. The key does not rotate between rekordbox versions in practice; the Pioneer CDN has been stable. Caching eliminates that risk after first run. See D8 for the cache-miss failure path.

### D2 - state.db Schema

See Section 5 for full DDL. Core tables: `sessions`, `appearances`, `tracks`, `meta`. The `tracks` table is a denormalized view of what Set Memory has seen - it is not authoritative; `djmdContent` on the USB master.db is authoritative. The `tracks` table carries the last-seen title/artist so the digest can be generated without reopening the USB db after ingestion.

### D3 - Conflict Handling

A `djmdHistory` row identity for Set Memory's purposes is the tuple `(DateCreated, fingerprint)` where `fingerprint` is a SHA-256 (first 16 hex chars) of the sorted set of `ContentID` values in that session's `djmdSongHistory` rows. This survives playlist renames (the XDJ renames date-keyed history playlists occasionally) because the date + track set together are stable. The `sessions` table stores both `raw_history_id` (the `djmdHistory.ID` from the USB db) and `fingerprint`. On re-ingestion, match first by `fingerprint`; if a fingerprint matches an already-ingested session, skip it regardless of whether the `raw_history_id` changed. Log any `raw_history_id` change at INFO level.

### D4 - "Forgotten Favourites" Threshold Defaults

Defaults in `config.json`:

- `forgotten_min_appearances`: 5 (track must have appeared in at least 5 sessions)
- `forgotten_days_since_last`: 90 (last appearance must be > 90 days before today)
- `forgotten_limit`: 10 (max rows in digest section)

Both thresholds read from config on every run. Changing config.json takes effect on the next sync without touching any code.

### D5 - "Never Played After Add" Filter Defaults

Defaults in `config.json`:

- `never_played_min_days_since_add`: 30 (track must have been in `djmdContent` for at least 30 days)
- `never_played_limit`: 10 (max rows in digest)

"In library" = present in `djmdContent` on the USB db. "Never played" = `ContentID` never appears in any `djmdSongHistory` row across all ingested sessions. The 30-day grace period avoids surfacing tracks Rob just added and hasn't had a chance to play.

Note: since `djmdContent.DateCreated` reflects the file creation date, not the date Rob added the track to rekordbox, a track added to the library today from an old file will show its original creation date. This means the 30-day filter is an approximation. Document this in README. A future improvement could use `djmdContent.StockDate` if its semantics are confirmed - leave as a comment, not a code branch.

### D6 - Output Format

**digest.md** - overwritten on each sync, human-readable:

```
# Set Memory Digest - <date>

## Sessions Ingested This Sync
- <N> new sessions found (IDs: ...)
- <K> sessions already in state.db, skipped

## Forgotten Favourites (appeared >= N times, last seen > D days ago)
| Title | Artist | Appearances | Last Session |
...
(up to 10 rows)

## Never Played After Add (in library >= D days, never in any session)
| Title | Artist | Date Added |
...
(up to 10 rows)

## Summary Stats
- Total sessions in state.db: ...
- Total unique tracks ever played: ...
- Library size (djmdContent rows): ...
- State.db last updated: ...
```

**macOS notification** - fires after digest is written:

- Title: "Set Memory"
- Body: "N new sessions. K forgotten tracks surfaced."
- If 0 new sessions: "No new sessions found." (still fires so Rob knows the mount ran)

### D7 - launchd Plist

Location: `~/Library/LaunchAgents/com.mord58562.setmemory.plist`

Key plist keys:

- `Label`: `com.mord58562.setmemory`
- `ProgramArguments`: `["/path/to/miniconda3/bin/python", "~/Downloads/set-memory/set_memory.py", "--on-mount"]`
- `StartOnMount`: `true`
- `RunAtLoad`: `false` (only fire on mount, not at login)
- `StandardOutPath`: `~/Downloads/set-memory/logs/stdout.log`
- `StandardErrorPath`: `~/Downloads/set-memory/logs/stderr.log`

The Python path must be the absolute miniconda3 path (not `python3` or a shebang-resolved path) because launchd does not inherit the user's PATH. `install.sh` detects the miniconda3 python path with `which python` after activating base, writes it into the plist.

`launchctl load` / `launchctl bootout` + `launchctl bootstrap` handled by install.sh / uninstall.sh.

### D8 - Failure Modes

| Failure | Behaviour |
|---|---|
| Mount fires but no volume in `/Volumes/*` has both `PIONEER/Master/master.db` and `PIONEER/rekordbox/` | Exit 0 silently. This fires on every volume mount; non-rekordbox mounts must be cheap to dismiss. |
| One drive in a multi-drive run hits a WAL lock or schema mismatch | Skip that drive, append its label to the notification body, keep processing the others. One bad drive shouldn't lose work on the rest. |
| `master.db` present but WAL lock detected (XDJ mid-write) | Wait 2 seconds, retry once. On second failure, log to stderr, write a digest noting "USB locked, skipped ingestion," fire notification: "USB locked - retry on next mount." |
| SQLCipher key cache absent | Attempt download-key. If download fails (no network), write error to digest and notification. Do not crash. Exit 1 (signals launchd to record the failure). |
| SQLCipher key present but decryption fails | Log full error. Write digest noting decryption failure. Notify. Exit 1. |
| state.db missing or corrupt | Recreate from DDL (schema-only, no data). Ingest from scratch. Note "fresh state.db created" in digest. |
| djmdContent or djmdHistory table missing in USB db | Schema incompatibility. Log, notify, exit 1. Do not partially ingest. |

### D9 - MIK Non-Integration

MIK is explicitly read-only metadata that rekordbox has already absorbed. After "Sync to rekordbox," MIK's key and energy values live in `djmdContent.ColorID`/`Tonality`/`Rating` columns on the USB db. Set Memory reads those columns as part of track metadata in `ingest.py`. There is no separate MIK database query, no MIK db connection, no dependency on `~/Library/Application Support/Mixedinkey/`. MIK data is present in the source of truth (USB master.db) by the time Set Memory runs. This is not a limitation - it is the correct data flow.

MIK does not provide play history, session data, or hot cue positions relevant to this tool.

### D10 - Privacy Posture

- `state.db` stays at `~/Downloads/set-memory/state.db`. No sync, no upload, no cloud.
- `digest.md` is a local file at `~/Downloads/set-memory/digest.md`.
- No analytics, no telemetry, no network calls except the one-time (or cache-miss) pyrekordbox key download from Pioneer's CDN.
- Logs at `~/Downloads/set-memory/logs/` are local and contain only metadata (track titles, artists, session dates) - no audio content.
- The macOS notification body contains only counts, not track titles, to avoid displaying library contents in Notification Center.

---

## 3. Module Breakdown

### set_memory.py (entry point)

Parses `--on-mount` flag. Calls `config.load()`, then runs the on-mount pipeline in sequence: discover rekordbox USBs across `/Volumes/*`, ingest each, analyse the union, write the digest, notify. All errors caught at top level; any unhandled exception writes a fallback notification and exits 1. Designed to be a thin orchestrator - no business logic.

### ingest.py

Responsibilities: open USB `master.db` via pyrekordbox, enumerate `DjmdHistory` rows, compute session fingerprint, compare against `state.db` to find new sessions, insert new sessions + appearances + track records into `state.db`. Returns a summary struct: sessions_found, sessions_new, sessions_skipped.

Key design point: copies `master.db` + WAL + SHM to a temp directory before opening, so pyrekordbox holds a snapshot rather than locking the live USB file. This avoids holding an open handle to the USB volume (which could interfere with later ejection) and sidesteps WAL inconsistency.

### analyse.py

Pure computation against `state.db` only - no USB db access. Takes a db connection and config. Returns two lists: `forgotten` (tracks meeting the appearances + recency thresholds) and `never_played` (tracks in the tracks table with zero appearances and date_added older than threshold). Both lists are sorted: forgotten by appearances descending; never_played by date_added ascending (oldest un-played first).

The separation of ingest (USB) and analyse (state.db only) makes analyse unit-testable without any pyrekordbox or SQLCipher dependency.

### digest.py

Takes the ingest summary, forgotten list, never_played list, and overall stats from state.db. Renders `digest.md` as a string and writes it atomically (write to `.digest.md.tmp`, then `os.replace`). Returns the notification body string. No logic - formatting only.

### notify.py

Wraps `subprocess.run(["osascript", "-e", ...])`. One function: `fire(title, body)`. On failure (osascript not found, non-zero exit), logs and returns without raising - a notification failure must never block the digest write. Contains an optional `terminal_notifier` path for testing environments without a full macOS notification stack.

### config.py

Reads `config.json` from the project directory. Returns a typed dataclass `Config` with all threshold fields and their defaults. If `config.json` is absent, creates it with defaults. Validates types; raises `ConfigError` (not caught by the orchestrator silently) if a field is the wrong type, so bad edits are surfaced immediately.

---

## 4. On-Mount Workflow

When launchd fires `set_memory.py --on-mount`:

1. Load `config.json` (created with defaults if absent).

2. Scan `/Volumes/*` for any volume that has both `PIONEER/Master/master.db` and a `PIONEER/rekordbox/` subtree. If none, exit 0 silently - the mount that triggered us isn't a rekordbox USB. Multiple matches mean multiple DJ drives are plugged in; each gets processed in the same run.

4. Check `~/.pyrekordbox/key` (or pyrekordbox's resolved cache path). If absent or empty, run `python -m pyrekordbox download-key`. If that fails, write a digest noting the failure and fire a notification, then exit 1.

5. Snapshot USB `master.db` (+WAL+SHM) to a temp directory with retry logic: attempt once, if file is locked (OperationalError or WalError), wait 2 seconds, try once more. On second failure, fall through to failure-mode handling in D8.

6. Open the snapshot via pyrekordbox. Query all `DjmdHistory` rows sorted by `DateCreated` ascending.

7. For each history row, compute the session fingerprint from its `DjmdSongHistory` ContentID set. Check state.db for a matching fingerprint. Skip if already ingested.

8. For each new session: insert a `sessions` row, then insert one `appearances` row per `DjmdSongHistory` entry. For each ContentID, upsert a `tracks` row from `DjmdContent` (update `last_seen_session`, increment `total_appearances`).

9. Delete the temp snapshot directory.

10. Run `analyse.py` against `state.db` to produce forgotten + never_played lists.

11. Compute summary stats from state.db (total sessions, unique tracks, library size from last ingest).

12. Write `digest.md` via `digest.py`.

13. Fire macOS notification via `notify.py`.

14. Exit 0.

Total expected runtime for a typical sync (1-5 new sessions, ~200-track library): well under 10 seconds.

---

## 5. state.db Schema (full DDL)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Stores: schema_version, last_sync_at

CREATE TABLE IF NOT EXISTS sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_history_id   TEXT    NOT NULL,  -- djmdHistory.ID from USB db (may change on rename)
    fingerprint      TEXT    NOT NULL UNIQUE,  -- SHA-256[:16] of sorted ContentID set
    session_date     TEXT    NOT NULL,  -- djmdHistory.DateCreated, ISO 8601
    source_db_path   TEXT    NOT NULL,  -- path to USB master.db at time of ingest
    ingested_at      TEXT    NOT NULL,  -- UTC ISO 8601 timestamp of this ingest run
    track_count      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions (session_date);

CREATE TABLE IF NOT EXISTS appearances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions (session_id),
    content_id   TEXT    NOT NULL,  -- djmdContent.ID (string, Pioneer uses string IDs)
    track_no     INTEGER NOT NULL,  -- djmdSongHistory.TrackNo (play order)
    title        TEXT,
    artist       TEXT
);

CREATE INDEX IF NOT EXISTS idx_appearances_content ON appearances (content_id);
CREATE INDEX IF NOT EXISTS idx_appearances_session ON appearances (session_id);

CREATE TABLE IF NOT EXISTS tracks (
    content_id           TEXT PRIMARY KEY,  -- djmdContent.ID
    title                TEXT,
    artist               TEXT,
    bpm                  REAL,
    key_camelot          TEXT,  -- e.g. "5A"; from djmdContent after MIK sync
    energy               INTEGER,  -- 1-10 from MIK via djmdContent
    date_created         TEXT,   -- djmdContent.DateCreated (file creation date; yyyy-mm-dd)
    first_seen_session   INTEGER REFERENCES sessions (session_id),
    last_seen_session    INTEGER REFERENCES sessions (session_id),
    total_appearances    INTEGER NOT NULL DEFAULT 0  -- count of appearances rows
);
```

Note: `DJPlayCount` from `djmdContent` is intentionally omitted. The RECON confirms its increment semantics are uncertain. `total_appearances` (count of `djmdSongHistory` memberships across ingested sessions) is the authoritative signal.

---

## 6. Test Plan

**Test shape:** Trophy pyramid. Static analysis (mypy + ruff) is the base. Unit tests for pure logic dominate. Integration tests against a real in-process SQLite DB (no mocking of SQLite). The pyrekordbox/SQLCipher layer is tested with a synthetic plaintext SQLite fixture that mirrors the djmd* schema - avoiding a SQLCipher dependency in the test suite.

### conftest.py fixtures

- `synthetic_usb_db`: creates `fixtures/synthetic_master.db` as a plain SQLite file with `djmdHistory`, `djmdSongHistory`, and `djmdContent` tables populated with known data (5 sessions, 20 tracks, deliberate overlaps). Used in place of the real encrypted USB db in all unit/integration tests.
- `state_db`: in-memory SQLite connection with schema applied fresh for each test function.
- `default_config`: `Config` dataclass with known thresholds.
- `frozen_today`: patches `datetime.date.today()` to a fixed date so recency assertions are deterministic.

### test_ingest.py

- `test_ingest_new_sessions_populates_sessions_table`: given 3 sessions in synthetic_usb_db and empty state.db, ingest produces 3 sessions rows.
- `test_ingest_skips_already_ingested_sessions`: second ingest of same data produces 0 new sessions.
- `test_ingest_fingerprint_survives_history_id_rename`: change raw_history_id on a session, re-ingest; still skips (fingerprint match).
- `test_ingest_appearances_correct_order`: appearances.track_no matches djmdSongHistory.TrackNo order.
- `test_ingest_tracks_upsert_increments_total_appearances`: a track appearing in 2 sessions has total_appearances = 2.
- `test_ingest_wal_lock_retry`: mock the first copy attempt to raise OperationalError; assert retry succeeds on second attempt.
- `test_ingest_missing_tables_raises_schema_error`: synthetic db with no djmdHistory raises a named exception, not a raw SQLite error.

### test_analyse.py

- `test_forgotten_returns_tracks_meeting_both_thresholds`: seed state.db with a track with 6 appearances, last session 100 days ago; assert it appears in forgotten list.
- `test_forgotten_excludes_recent_track`: same appearances count, last session 30 days ago; assert NOT in forgotten list.
- `test_forgotten_excludes_infrequent_track`: 2 appearances, last session 200 days ago; assert NOT in forgotten list.
- `test_forgotten_respects_limit`: 15 qualifying tracks; assert len(forgotten) == 10 with default config.
- `test_forgotten_sorted_by_appearances_desc`: assert result is ordered.
- `test_never_played_includes_old_unplayed_track`: track with date_created 60 days ago, no appearances; assert in never_played.
- `test_never_played_excludes_recent_add`: track added 10 days ago, no appearances; assert NOT in never_played.
- `test_never_played_excludes_played_track`: track added 60 days ago, 1 appearance; assert NOT in never_played.
- `test_never_played_respects_limit`: assert capped at config limit.
- `test_both_lists_empty_on_fresh_db`: zero sessions, zero tracks; both lists empty, no crash.

### test_digest.py

- `test_digest_written_atomically`: assert digest.md does not exist in partial-write state (mock os.replace to verify tmp file used).
- `test_digest_contains_all_sections`: given known inputs, assert all four section headers present.
- `test_digest_session_count_in_notification_body`: assert notification body string matches "N new sessions" pattern.
- `test_digest_zero_new_sessions_message`: ingest summary with 0 new sessions produces "No new sessions found" body.
- `test_digest_no_em_dashes`: assert U+2014 not present anywhere in output (codepoint grep per feedback).

### test_notify.py

- `test_notify_calls_osascript_with_correct_args`: mock subprocess.run; assert it is called with expected osascript string.
- `test_notify_failure_does_not_raise`: mock subprocess.run to raise FileNotFoundError; assert no exception propagates.

### test_smoke.py (skipped unless `RUN_SMOKE=1` env var set)

- `test_smoke_real_usb`: marks `pytest.mark.skipif(not usb_present, ...)`. If a rekordbox USB is mounted (anywhere in `/Volumes/*`), runs the full on-mount pipeline and asserts: `digest.md` exists and is non-empty, notification body string is valid, state.db has at least one session row.

---

## 7. Smoke Test (Post-Install)

`install.sh` ends with:

```sh
echo "Running smoke test..."
python set_memory.py --on-mount
echo "Exit code: $?"
echo "Digest preview:"
head -20 ~/Downloads/set-memory/digest.md
```

If USB is not mounted at install time, the script detects the missing `/PIONEER/` directory and exits 0 cleanly. `install.sh` then prints: "USB not mounted - smoke test exited cleanly (expected). Mount USB to trigger first real sync."

If USB is mounted, the full pipeline runs and `head -20 digest.md` serves as visual confirmation. The `open` command is not appropriate here (digest.md is a text file Rob may not have a viewer configured for); instead, `cat digest.md` shows the full output at install time.

---

## 8. Integration with Jury's Sunday Digest

Set Memory is a standalone tool. The Sunday digest (Jury) runs on a schedule; Set Memory runs event-driven on USB mount. They operate independently.

**Recommended optional pattern:** at the end of `digest.py`, if `~/Documents/cleanup-digest.md` exists, append a fenced DJ section to it:

```
## DJ Library (from Set Memory - <date>)
<N sessions since last Sunday. Top forgotten: ...>
```

This is a simple file append, gated on the other file's existence. No import of Jury's code. No structural dependency. Controlled by a config flag `append_to_jury_digest: true` (default false until Rob enables it).

---

## 9. Risks and Mitigations

**R1 - SQLCipher + rekordbox 6.8.5 untested ground**

pyrekordbox's explicit test matrix is 5.8.6, 6.7.7, 7.0.9. Rob's version is 6.8.5 - between two tested versions. Schema changes between minor versions are uncommon in rekordbox's history. The RECON confirms the `djmdHistory`/`djmdSongHistory`/`djmdContent` table structure is known from pyrekordbox's ORM source.

Mitigation: `install.sh` runs a table-existence check against the USB master.db as part of setup. If tables are missing or column names differ, it reports the specific mismatch. Do not proceed to install the launchd agent if the schema check fails. Pin `pyrekordbox==0.4.4` in requirements to avoid silent upstream changes.

**R2 - Pioneer CDN key distribution changes**

If Pioneer changes how they distribute the SQLCipher key, `download-key` will fail. The key is stable per rekordbox major version; it is not a per-user or per-installation value.

Mitigation: cache on first successful fetch. If cache exists, never call the network again (D1). Document the cache path so Rob can manually back it up. If cache is lost and CDN is down, the digest notes this explicitly.

**R3 - USB master.db WAL state from XDJ mid-session**

The WAL file is 257 KB (active). If Rob mounts the USB on Mac before the XDJ has cleanly committed, the WAL may be in a partially-written state.

Mitigation: copy db + wal + shm together atomically. Opening a WAL-mode SQLite file with both the db and wal present will replay committed WAL frames correctly. Uncommitted frames are ignored. This is standard SQLite WAL recovery behavior - no special handling needed.

**R4 - DJPlayCount unreliability**

The RECON flags this explicitly. Set Memory does not use DJPlayCount anywhere. `djmdSongHistory` membership is the sole play signal.

**R5 - USB volume label change**

Mitigated by the discovery scan: Set Memory walks `/Volumes/*` looking for the `PIONEER/Master/master.db` + `PIONEER/rekordbox/` shape rather than matching on label or UUID. Reformatting or renaming a drive keeps it working without any reconfiguration; new drives are picked up the moment they're plugged in.

**R6 - state.db grows without bound over many years**

With ~1 session/week and ~50 tracks/session, state.db at 10 years would hold ~2,600 sessions and ~130,000 appearance rows. At roughly 100 bytes per appearance row, that is ~13 MB. This is negligible; no pruning strategy is needed.

---

## 10. Open Questions for Rob

**Q1 - Does the USB master.db djmdHistory include only XDJ-created sessions, or does it also include Mac rekordbox history?**

The USB db is 53.6 MB vs Mac's 1.49 MB, strongly suggesting the USB carries the full live library. But it is possible some early sessions from Mac-side rekordbox are present too. Recommended default: ingest everything in djmdHistory on first sync regardless of source; this gives the fullest baseline.

**Q2 - What is the correct miniconda3 Python path on this machine?**

`install.sh` should detect it automatically via `which python` in the conda base environment. If that path is unusual, the plist will need manual adjustment. Verify during install.

**Q3 - Should the digest.md overwrite on every sync, or append new sync sections?**

Recommended default: overwrite (current design). Keeps the file readable at a glance. If Rob wants history, state.db has it. An append-mode config flag could be added later. For now, overwrite is simpler.

**Q4 - Forgotten-favourites threshold: is 5 sessions the right floor, or should it be lower given Rob's play frequency?**

If Rob plays 1-2 sessions per week, a track needs ~2-5 weeks of use to hit 5 appearances. That is reasonable for "established favourite." If Rob's play frequency is lower, the threshold might filter too aggressively. Recommended: start at 3 and adjust. This is in config.json and costs nothing to change.

**Q5 - Should the Jury append be on by default?**

Recommended: off by default. It requires knowing Jury's digest path is stable and that Rob wants the two digests linked. Enable it once both tools are running cleanly.
