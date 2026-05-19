# Set Memory

Reads your rekordbox session log data from the DJ USB drive and surfaces two things
on every mount:

1. **Forgotten favourites** - tracks you played often but haven't touched in months.
2. **Never played after add** - tracks that have been in your library for a while
   but have never appeared in any recorded session.

Results land in `~/Downloads/set-memory/digest.md` and fire a macOS notification.
All data stays on your machine (no cloud, no uploads).

---

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

1. You plug in your DJ USB drive.
2. macOS fires the launchd agent (`com.mord58562.setmemory`).
3. Set Memory checks that it is your USB (via device UUID, not volume label).
4. It opens the `master.db` on the USB (decrypted via SQLCipher + pyrekordbox)
   and reads all `djmdHistory` sessions.
5. New sessions (not yet in `state.db`) are ingested - their track appearances
   are recorded by content ID.
6. Forgotten-favourites and never-played lists are computed from `state.db`.
7. `digest.md` is written atomically. A macOS notification fires.
8. Total expected runtime: under 10 seconds for a typical sync.

---

## Install

Prerequisites: Homebrew, miniconda3 at `~/miniconda3/`.

```bash
# Step 1: Install system SQLCipher library
brew install sqlcipher

# Step 2: Install pyrekordbox into miniconda3
~/miniconda3/bin/pip install pyrekordbox==0.4.4

# Step 3: Download the rekordbox SQLCipher key from Pioneer's CDN (one-time)
#   This is a one-time network call. The key is cached at ~/.pyrekordbox/key.
#   On subsequent mounts, Set Memory reads the cache - no network needed.
~/miniconda3/bin/python3 -m pyrekordbox download-key

# Step 4: Install the SQLCipher adapter
~/miniconda3/bin/python3 -m pyrekordbox install-sqlcipher

# Step 5: Install the launchd agent and run the full setup
bash ~/Downloads/set-memory/scripts/install.sh
```

After install, the launchd agent is live. Mount your USB drive to trigger
the first real sync.

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

On first run, Set Memory writes a default `config.json` next to the script.
Open it and fill in two values before mounting your USB:

- `usb_uuid` - the UUID inside `<USB>/PIONEER/DeviceLibBackup/rbDevLibBaInfo_*.json`.
  Mount the drive once, run `cat /Volumes/*/PIONEER/DeviceLibBackup/rbDevLibBaInfo_*.json | grep -i uuid`,
  and copy the value across.
- `usb_pioneer_path` - the `/Volumes/<name>/PIONEER` folder on your drive.

Until `usb_uuid` is set, Set Memory exits cleanly on mount with a log note;
nothing is read or written. Changes take effect on the next mount - no
restart needed.

```json
{
  "forgotten_min_appearances": 5,
  "forgotten_days_since_last": 90,
  "forgotten_limit": 10,
  "never_played_min_days_since_add": 30,
  "never_played_limit": 10,
  "usb_uuid": "<YOUR_USB_UUID>",
  "usb_pioneer_path": "/Volumes/<YOUR_USB>/PIONEER",
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

### SQLCipher key fetch failure

**Symptom:** digest.md contains "Key error" or logs show `RuntimeError: Failed to
download SQLCipher key`.

**Cause:** Pioneer's CDN was unreachable when `download-key` was called, OR
the key cache at `~/.pyrekordbox/key` was deleted.

**Fix:**
```bash
# Requires network access
~/miniconda3/bin/python3 -m pyrekordbox download-key

# Verify the cache exists and is non-empty
cat ~/.pyrekordbox/key
```

If the CDN is permanently unreachable (Pioneer changes their distribution),
check the pyrekordbox issue tracker for a manual key entry workaround.

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

Not a problem. Set Memory identifies your USB by device UUID
(`<YOUR_USB_UUID>`), not volume label. If you reformat
and the UUID changes, update `usb_uuid` in `config.json` and run
`install.sh` again to recheck the UUID.

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
