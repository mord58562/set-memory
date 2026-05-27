# Set Memory

Reads your rekordbox session log data from the DJ USB drive and surfaces, on
every mount:

1. **Headline + activity sparkline** - one-line summary of what changed plus
   a sparkline of sessions-per-month over the last year.
2. **Forgotten favourites** - tracks you played often but haven't touched
   in months.
3. **Never played after add** - tracks that have been in your library for a
   while but have never appeared in any recorded session. Uses rekordbox's
   `StockDate` (true library-add date) when populated, falling back to file
   creation date.
4. **Recently added, not played yet** - buy-regret signal for the last
   ~30 days. Different question from #3.
5. **Prep audit** - library tracks missing BPM, key analysis, or hot cues.
   Sorted by most-played first so the tracks already in rotation bubble up.
6. **Played together** - track pairs that appeared in many sessions
   together. Useful for set planning.
7. **Distribution** - histogram of plays across BPM buckets and Camelot keys.
   Quick set-diversity awareness.
8. **Possibly deleted** - tracks recorded in state.db but no longer in any
   recently-synced USB library.
9. **USB drives** - per-USB last-mounted timestamp and library size, so a
   forgotten drive is obvious.

Results land in `~/Downloads/set-memory/digest.md` and fire a macOS
notification (clickable when `terminal-notifier` is installed, which the
installer adds). All data stays on your machine.

## GUI

`scripts/install.sh` builds and installs **SetMemory.app**, a native
SwiftUI window app at `/Applications/SetMemory.app`. The app reads
`state.db` directly (plain SQLite - the SQLCipher boundary stays in
Python), auto-refreshes whenever the launchd agent writes a new digest,
and offers all of the analyses above in a three-pane layout:

- **Sidebar:** every surface (Forgotten / Recent / Never / Prep /
  Together / Distribution / USB Drives / Sessions / Possibly Deleted)
  with live row counts.
- **Centre:** the active surface as a sortable table or chart, with a
  one-line description so you don't have to memorise what each section
  means.
- **Right:** detail pane with all metadata for the selected track,
  plus copy-title / copy-artist / copy-"title - artist" buttons.

Top toolbar: live search box, mounted-USB indicator (green when a
rekordbox USB is currently plugged in), Sync Now button (greyed until
a USB is mounted), last-sync relative time, and a Settings sheet for
every threshold. Settings write back to `config.json` so the launchd
agent picks them up on the next mount.

Cmd-R = Sync now. Cmd-Shift-R = re-read state.db without syncing.

## CLI

A CLI is also available for tweaking thresholds without remounting:

```bash
~/miniconda3/bin/python ~/Downloads/set-memory/set_memory.py query forgotten
~/miniconda3/bin/python ~/Downloads/set-memory/set_memory.py query prep
~/miniconda3/bin/python ~/Downloads/set-memory/set_memory.py query together
~/miniconda3/bin/python ~/Downloads/set-memory/set_memory.py query search --search "marlon"
~/miniconda3/bin/python ~/Downloads/set-memory/set_memory.py query sessions --since 2026-01-01
```

---

## What's new in 0.4.0

- **CDJ-export USBs (`.pdb`) now ingestable.** The original release only
  read the legacy `PIONEER/Master/master.db` (SQLCipher) layout. Modern
  rekordbox exports to `PIONEER/rekordbox/export.pdb` (Pioneer's
  reverse-engineered DeviceSQL format), and the existing pyrekordbox
  couldn't touch it. New pure-Python `pdb_reader.py` (no extra pip
  deps) parses the .pdb directly; discovery accepts either layout and
  dispatches to the right ingest path automatically.
- **GUI overhaul against the AI-design-tells ban list.** Custom
  wordmark, dense rekordbox-inspired row layout, inline-expand on
  selection (no separate inspector pane), bottom status bar with all
  stats (no sidebar footer), collapsible sidebar groups (Library /
  Patterns / Maintenance), split-role accents (cyan = selection,
  amber = action, coral = danger), tempo-coloured BPM column,
  Camelot-wheel-coloured key chips, deliberately tight spacing.
- **USB picker.** Multiple CDJ USBs mounted? Sync targets one or all,
  selectable from the top bar.
- **App auto-surfaces on USB mount.** When launchd fires the ingest,
  Set Memory opens in the background dock so results are one click
  away.

## What's new in 0.3.0

- **Native SwiftUI GUI.** Set Memory now ships with
  `/Applications/SetMemory.app`: three-pane window with sidebar
  surfaces, live search, table views per analysis, distribution charts
  (BPM histogram, Camelot key bars, monthly sparkline), a track-detail
  inspector with copy-to-clipboard, a Sync Now toolbar button (live
  mounted-USB indicator), and an inline Settings sheet for every
  threshold. App reads `state.db` directly via plain SQLite; auto-
  refreshes whenever the launchd agent writes a new digest.
- `scripts/install.sh` now builds + installs the GUI alongside the
  launchd agent.

## What's new in 0.2.0

- **Never-played actually works.** The original release had a structural
  bug: the `tracks` table only got populated from session appearances, so
  the "never played" query (filtering for zero appearances against that
  table) was always empty by construction. Set Memory now syncs the full
  `djmdContent` library on every mount, then layers play counts on top.
- **Notifications actually fire.** Single-quoted AppleScript strings don't
  compile; every notification has silently failed since release with
  `syntax error -2741` (visible only in stderr.log). Fixed; click-through
  added via `terminal-notifier` when present.
- **BPM correctly normalised** from rekordbox's `value * 100` integer
  encoding, with zero treated as "not analysed."
- **Prep audit** flags library tracks missing BPM, key, or hot cues.
- **Played-together pairs** from co-appearance across sessions.
- **Recently-added-unplayed** as a distinct buy-regret signal.
- **Distribution stats** across BPM buckets and Camelot keys.
- **Per-USB tracking** in a new `usb_drives` table.
- **Possibly-deleted tracks** surfaced by staleness.
- **Sparkline + headline** in the digest.
- **`query` CLI subcommand** for all of the above without remounting.
- **Schema migrations** with `schema_version` in `meta`; upgrades a v1
  state.db in place without data loss.

## What this is NOT

- **Not audio analysis.** Set Memory reads structured rows from rekordbox's
  `djmdHistory` and `djmdSongHistory` tables. It does not process audio signals,
  waveforms, or any binary media. "Played" means "appeared in a session log row,"
  not "was heard by the system."
- **Not a real-time DJ critic.** Runs once per USB mount. Does not touch a live
  session in progress.
- **Not a replacement for rekordbox.** Set Memory is a read-only companion that
  accumulates a local history log (`state.db`) across syncs.

---

## How it works

1. You plug in any rekordbox USB drive.
2. macOS fires the launchd agent (`com.mord58562.setmemory`).
3. Set Memory scans every mounted volume for `PIONEER/Master/master.db`.
   Non-rekordbox volumes are ignored silently; multiple DJ drives mounted
   at once are all processed in the same run.
4. For each rekordbox USB, it opens `master.db` (decrypted via SQLCipher +
   pyrekordbox) and reads `djmdHistory`.
5. New sessions (not yet in `state.db`) are ingested by content fingerprint,
   so re-mounting the same drive or two USBs mirroring each other never
   double-count.
6. Forgotten-favourites and never-played lists are computed across the
   union of every session ever recorded, regardless of which drive it
   came from.
7. `digest.md` is written atomically. A macOS notification fires.
8. Total expected runtime: under 10 seconds for a typical sync.

---

## Install

Prerequisites: Homebrew, miniconda3 at `~/miniconda3/`.

```bash
# Step 1: Install system SQLCipher library
brew install sqlcipher

# Step 2: Install pyrekordbox into miniconda3 (pulls sqlcipher3-wheels
# in as a dependency; no separate adapter build step needed)
~/miniconda3/bin/pip install pyrekordbox==0.4.4

# Step 3: Run the installer (verifies the toolchain, builds the
# launchd plist, smoke-tests the pipeline)
bash ~/Downloads/set-memory/scripts/install.sh
```

After install, the launchd agent is live. Mount any rekordbox USB to
trigger the first real sync.

---

## Uninstall

```bash
bash ~/Downloads/set-memory/scripts/uninstall.sh
```

This removes the launchd agent only. `state.db`, `digest.md`, and `logs/` are
preserved. Delete them manually if you want a clean slate:

```bash
rm ~/Downloads/set-memory/state.db
rm ~/Downloads/set-memory/digest.md
rm -rf ~/Downloads/set-memory/logs/
```

---

## Configuration

There is no per-drive setup. Set Memory discovers any mounted volume that
has a rekordbox library on it (`PIONEER/Master/master.db` + a `PIONEER/
rekordbox/` export tree) and ingests sessions from each. Plug in a new
USB and it just works; reformat or rename a drive and nothing breaks.

`config.json` holds only the analysis thresholds, written next to the
script on first run. Changes take effect on the next mount, no restart
needed.

```json
{
  "forgotten_min_appearances": 5,
  "forgotten_days_since_last": 90,
  "forgotten_limit": 10,
  "never_played_min_days_since_add": 30,
  "never_played_limit": 10,
  "state_db_path": "state.db",
  "digest_path": "digest.md",
  "append_to_jury_digest": false
}
```

**First-run tuning note (Q4 from DESIGN):** `forgotten_min_appearances: 5` means
a track needs to appear in at least 5 recorded sessions to qualify as a "favourite."
If your sessions are infrequent (one per week or less), the forgotten list may be
empty for the first few months while the history log builds up. In that case,
lower the threshold to 3 in `config.json` - it costs nothing to change and you
can raise it again later once the log has depth.

---

## Running tests

Unit tests use a synthetic plain-SQLite fixture (no SQLCipher, no USB needed):

```bash
~/miniconda3/bin/pytest ~/Downloads/set-memory/tests/ -v
```

Smoke test (requires USB mounted and pyrekordbox installed):

```bash
RUN_SMOKE=1 ~/miniconda3/bin/pytest ~/Downloads/set-memory/tests/test_smoke.py -v
```

---

## Troubleshooting

### "Schema incompatibility" notification

**Symptom:** Notification body is "Schema error - check logs."

**Cause:** The rekordbox version on your XDJ changed and the `djmdHistory`,
`djmdSongHistory`, or `djmdContent` table structure no longer matches what
Set Memory expects.

**Fix:** Check `logs/stderr.log` for the specific missing column or table name.
Open a GitHub issue with the error message if this happens after a rekordbox update.

### "USB locked - retry on next mount"

**Cause:** The USB was disconnected from the XDJ before it finished writing the
WAL file. Set Memory retried once and still could not get a clean snapshot.

**Fix:** Eject the USB cleanly from the XDJ (hold eject button until the disk
icon disappears), then re-mount on Mac.

### state.db is corrupt

Set Memory detects a corrupt `state.db` on open and recreates it automatically.
The new file starts fresh (no history). Re-mounting the USB will re-ingest all
`djmdHistory` sessions from scratch.

### Logs

```
~/Downloads/set-memory/logs/stdout.log   - standard output from the launchd run
~/Downloads/set-memory/logs/stderr.log   - errors and warnings
```

### USB volume label changed

Not a problem. Set Memory walks `/Volumes/*` and only cares whether each
volume has a rekordbox library on it. Renaming or reformatting a drive
doesn't change behaviour.

---

## Data notes

- `djmdContent.DateCreated` is the **file creation date**, not the date you added
  the track to rekordbox. A track ripped in 2022 but added to your library this
  week will show 2022. The never-played threshold is therefore an approximation.
  This is a known limitation documented in the DESIGN (D5).

- `DJPlayCount` in rekordbox has uncertain hardware-increment semantics (per RECON).
  Set Memory ignores it entirely. "Played" means "appeared in at least one
  `djmdSongHistory` row," which corresponds to a confirmed session log entry.

---

## File layout

```
~/Downloads/set-memory/
  set_memory.py          - entry point (--on-mount flag)
  ingest.py              - reads USB djmd* tables -> state.db
  analyse.py             - forgotten + never-played computation (pure SQL/Python)
  digest.py              - writes digest.md
  notify.py              - macOS notification via osascript
  config.py              - loads/validates config.json
  config.json            - user-editable thresholds
  state.db               - accumulated session log (never leaves machine)
  digest.md              - latest digest (overwritten on each sync)
  launchd/
    com.mord58562.setmemory.plist
  scripts/
    install.sh
    uninstall.sh
  tests/
    conftest.py
    test_ingest.py
    test_analyse.py
    test_digest.py
    test_notify.py
    test_smoke.py
    fixtures/
      synthetic_master.db     - built at test-time; plain SQLite, no encryption
      config_default.json
  logs/
    stdout.log
    stderr.log
```
