# Set Memory - Reconnaissance Report
Date: 2026-05-12

## 1. rekordbox Database Accessibility

**Mac `master.db`** (`~/Library/Pioneer/rekordbox/master.db`, 1.49 MB):
- `sqlite3 master.db ".tables"` → `Error: file is not a database`. SQLCipher-encrypted.
- `strings` returns garbage; no schema leak.
- Last modified June 30 2025 - **stale, no sync in nearly a year**.
- Backups (`master.backup.db`, `master.backup2.db`) also June 30 2025, also encrypted.

**USB `master.db`** (`/Volumes/<YOUR_USB>/PIONEER/Master/master.db`, 53.6 MB):
- Also SQLCipher-encrypted. Misidentified by `file` as "OpenPGP Public Key" - SQLCipher's page-level encryption produces non-standard magic bytes.
- Last modified May 9 2026 (**active**). WAL file (`master.db-wal` 257 KB, May 11 2026) confirms live writes from XDJ-RX3.
- 3 backups span May 2-May 9 2026 - hardware rotates backups per session.

**`exportLibrary.db`** (`/Volumes/<YOUR_USB>/PIONEER/rekordbox/exportLibrary.db`, 221 KB) - also encrypted; secondary cache, not main DB.

**Schema via pyrekordbox docs (confirmed against source)**:
- `djmdContent` - main track table (~70 columns). Fields: `DJPlayCount` ("not sure if rekordbox plays count"), `DateCreated` (yyyy-mm-dd file creation), `StockDate` (purpose unclear), `ReleaseDate`. **No `LastPlayTime` field exists.**
- `djmdHistory` - history playlist metadata: ID, Seq, Name, Attribute, ParentID, DateCreated.
- `djmdSongHistory` - track membership: ID, HistoryID, ContentID, TrackNo. **No per-track timestamp** - only ordinal within playlist.

**Critical implication**: no LastPlayTime in schema. Only per-track play data is `DJPlayCount` (cumulative, hardware-increment uncertain) + membership in `djmdSongHistory`. "When" inferred from `djmdHistory.DateCreated` + sequence position.

## 2. XDJ USB-Side Data

| Path | Format | Status |
|---|---|---|
| `Master/master.db` | SQLCipher | Encrypted, active (May 9 2026) |
| `rekordbox/export.pdb` | Pioneer PDB | Plaintext binary, parseable |
| `rekordbox/exportExt.pdb` | Pioneer PDB | Plaintext binary, parseable |
| `rekordbox/exportLibrary.db` | SQLCipher | Encrypted |
| `USBANLZ/Pxxx/xxxxxxxx/ANLZ0000.DAT/EXT/2EX` | PMAI binary | Plaintext, parseable |
| `Master/masterPlaylists6.xml` | XML | Plaintext playlist tree |
| `Master/automixPlaylist6.xml` | XML | Plaintext automix content IDs |
| `rekordbox/playlists3.sync` | XML | Plaintext sync state w/ timestamps |
| `DeviceLibBackup/rbDevLibBaInfo_*.json` | JSON | Plaintext device backup metadata |

**export.pdb** (303 KB) - primary accessible source. Per-track records: title, artist, filepath (`/Rob's songs/...mp3`), USBANLZ path, key/energy (`5A - Energy 6`), DateCreated in yyyy-mm-dd. ~177 visible track references via grep. **No play count / play history** - PDB is performance lookup snapshot, not session log.

**ANLZ files** (161 DAT + 161 EXT + 161 2EX = 483 total). Magic `PMAI`. Sections: `PPTH` (UTF-16LE path), `PVBR`, `PQTZ` (beatgrid), `PWAV/PWV2/PWV3` (waveform), `PCOB` (cue points). **No history/count/timestamps in ANLZ.**

**masterPlaylists6.xml** - full playlist tree (IDs, parents, Timestamps in Unix ms). Readable without decryption. Shows any HISTORY folder playlists XDJ creates.

**XDJ-RX3 history mechanism**: when playing tracks, XDJ creates History playlists inside USB `master.db` (`djmdHistory` + `djmdSongHistory`). Each playlist named with a date (e.g. "2026.05.09"), contains track IDs in play order. **Primary record of what was played and when.** Lives in the encrypted USB `master.db`, not PDB or ANLZ.

## 3. pyrekordbox Viability + Alternatives

**pyrekordbox v0.4.4 (Aug 2025) - RECOMMENDED**:
- Decrypts and queries `master.db` via SQLCipher. Auto key extraction via `python -m pyrekordbox download-key`.
- Tested against 5.8.6, 6.7.7, 7.0.9. **Rob's 6.8.5 not in explicit test matrix** but close to 6.7.7 - schema should be compatible.
- ORM models: `DjmdContent`, `DjmdHistory`, `DjmdSongHistory`. History query: all `DjmdHistory` rows (each session playlist with Name + DateCreated) → for each, `DjmdSongHistory` rows by `TrackNo` → each ContentID → `DjmdContent`.
- Also parses ANLZ + PDB.
- **Install**: not currently in miniconda3. `pip install pyrekordbox` + `python -m pyrekordbox download-key` + `brew install sqlcipher` + `python -m pyrekordbox install-sqlcipher`. One-time.
- **CRITICAL**: must open the USB `master.db`, NOT the Mac one (Mac stale since June 2025, no hardware history).

**rekordcrate** (Rust, Holzhaus) - reads `export.pdb` + ANLZ. No SQLCipher dep. Doesn't expose session history. Useful as lightweight track-metadata fallback only.

**crate-digger** (Java, Deep Symmetry) - same scope as rekordcrate. JRE dep. Inappropriate for lightweight Mac script.

## 4. Sync Detection Mechanism

**Recommended: launchd `StartOnMount`**.
A launchd plist with `StartOnMount=true` fires on every volume mount. Script checks for `/Volumes/<YOUR_USB>/PIONEER/Master/master.db` before doing anything (safe for non-USB mounts). Native macOS, zero deps, instant.

Verify USB identity via `/Volumes/<YOUR_USB>/PIONEER/DeviceLibBackup/rbDevLibBaInfo_*.json` UUID (`<YOUR_USB_UUID>`) rather than volume label (exFAT reformat could change label).

There's already one Pioneer launchd daemon on the system (`com.pioneerdj.FwUpdateManagerd.plist`) - same pattern.

**Not recommended**: rekordbox launch trigger (Rob opens infrequently), Mac master.db watch (stale), interval polling (latency + constant CPU).

## 5. Forgotten-Track Signals + Computation

**Available signals** (from USB `master.db` via pyrekordbox):

| Signal | Source | Availability |
|---|---|---|
| Track played in session X | `djmdSongHistory.ContentID` + `djmdHistory.DateCreated` | Direct |
| Session date | `djmdHistory.DateCreated` | Direct |
| Play order within session | `djmdSongHistory.TrackNo` | Direct |
| Track added to library | `djmdContent.DateCreated` | Direct yyyy-mm-dd |
| Cumulative play count | `djmdContent.DJPlayCount` | Present; hardware-increment uncertain |
| Track title/artist/BPM/key | `djmdContent.*` | Full metadata |

**Not directly available**: `LastPlayTime` per track, exact clock time within session.

**Derived signals** (via Set Memory's own log):
- **Last played**: max `djmdHistory.DateCreated` joined to track via `djmdSongHistory`.
- **Forgotten** ("played often, not recently"): count `ContentID` occurrences across all `djmdSongHistory` (= total session appearances); find max session date; filter `appearances ≥ N` AND `last_session < today - 90 days`.
- **Never played** ("in library, never appeared in history"): all `djmdContent.ID` minus distinct `ContentID` in `djmdSongHistory`. Filter by `DateCreated` if needed.
- **Historical trend**: requires Set Memory's persisted log built over multiple syncs. First sync baselines from existing `djmdHistory` accumulated by XDJ.

**One subtlety**: `DJPlayCount` not reliable as sole signal (docs note uncertainty). Use `djmdSongHistory` membership as authoritative.

## 6. Output Channel

**Recommended**: macOS notification (`osascript -e 'display notification ...'` or `terminal-notifier`) at sync time + companion digest file written to `~/Downloads/set-memory/digest.md` (latest, overwritten each sync).

Push notification = trigger. Digest file = pull-detail layer.

**Sunday digest integration** (with Jury): not ideal - USB may not have synced since previous Sunday. Event-driven > scheduled. Standalone digest cleaner; can optionally append a section to Jury's `cleanup-digest.md`.

## 7. Standalone vs Integrated Architecture

Own folder at `~/Downloads/set-memory/`:
```
set-memory/
  set_memory.py          # main ingestion + analysis
  state.db               # Set Memory's own SQLite log (track appearances/session)
  digest.md              # latest output, overwritten on each sync
  launchd/
    com.mord58562.setmemory.plist   # StartOnMount agent
  scripts/
    install.sh
    uninstall.sh
  tests/
  README.md
```

Does not depend on Jury. May optionally append a "DJ section" to Jury's digest as a file write - not a structural dependency.

`state.db` accumulates: every session ingested (session_id, date, track count) + every track appearance (session_id, content_id, track_no, title, artist). On each USB mount, compare `djmdHistory` against state.db, ingest new sessions only, recompute forgotten signals.

## 8. Risks and Open Questions

**R1 - SQLCipher key for 6.8.5**: pyrekordbox tested through 6.7.7 / 7.0.9; 6.8.5 in-between. `download-key` fetches from Pioneer infra. If Pioneer changes distribution, breaks. Mitigation: test extraction first; pin pyrekordbox version.

**R2 - USB master.db locked during XDJ session**: WAL file present suggests WAL mode. Eject-clean from XDJ before re-insert in Mac. Mitigation: copy db+wal+shm atomically before opening, or open read-only with WAL.

**R3 - DJPlayCount accuracy**: docs explicit caveat. Use `djmdSongHistory` membership as primary, not DJPlayCount.

**R4 - History creation conditional on Performance mode**: browse-only on XDJ doesn't write history. Expected behaviour.

**R5 - USB volume label change**: detect by UUID in `rbDevLibBaInfo_*.json`, not label.

**Q1 - Does djmdHistory on USB carry Mac-sync'd history or only hardware?** USB DB 53.6 MB vs Mac 1.49 MB → USB is primary library; Mac is subset. Likely USB carries full hardware history. Verify on first connection by listing all `djmdHistory` rows.

**Q2 - MIK integration**: MIK Collection10.mikdb at `~/Library/Application Support/Mixedinkey/`. ZSONG records (ZKEY, ZTAGENERGY, ZTEMPO, ZANALYSISDATE, ZSYNCDATE) - analysis metadata only, no play history. MIK does not need merging - rekordbox stores MIK-written key/energy values in djmdContent after MIK "Sync to rekordbox".

**Q3 - exportLibrary.db purpose**: encrypted, dated May 2024. Likely legacy Device Library format used by older CDJ firmware. Not active session history. Ignore for Set Memory.
