"""
pdb_reader.py - Pure-stdlib reader for Pioneer's PIONEER/rekordbox/export.pdb.

CDJ-exported USBs (rekordbox export mode) write a DeviceSQL binary database
called export.pdb instead of the SQLCipher master.db that desktop rekordbox
uses. pyrekordbox doesn't understand .pdb, so for those USBs we parse the
file ourselves.

The format was reverse-engineered by Deep-Symmetry (henrybetts, flesniak,
James Elliott). This is a focused subset that pulls just the tables Set
Memory needs:

  - tracks (table type 0)            -> id, title, artist_id, key_id, bpm,
                                        date_added, file_path
  - artists (table type 2)           -> id, name
  - keys (table type 5)              -> id, name (Camelot/musical key)
  - history_playlists (table type 11)-> id, name
  - history_entries (table type 12)  -> per playlist, ordered track_ids

Everything else (genres, albums, labels, colors, regular playlists, artwork,
columns, etc.) is skipped. Read-only; never writes to the input file.

Spec sources:
  - https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/exports.html
  - https://github.com/Deep-Symmetry/crate-digger (kaitai struct)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Page type enum (from rekordbox_pdb.ksy)
# ---------------------------------------------------------------------------

PT_TRACKS = 0
PT_ARTISTS = 2
PT_KEYS = 5
PT_HISTORY_PLAYLISTS = 11
PT_HISTORY_ENTRIES = 12

PAGE_HEADER_LEN = 0x28  # bytes consumed by the page header before heap


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class PdbTrack:
    track_id: int
    title: Optional[str]
    artist_id: int
    key_id: int
    bpm: Optional[float]          # real BPM (tempo / 100), None if 0
    date_added: Optional[str]     # ISO YYYY-MM-DD or None
    file_path: Optional[str]


@dataclass
class PdbSession:
    history_id: int
    name: str
    date_added: Optional[str]     # .pdb history_playlist_row has no date;
                                  # always None - kept for API symmetry
    track_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DeviceSQL string decoding
# ---------------------------------------------------------------------------

def _read_device_sql_string(buf: bytes, pos: int) -> Optional[str]:
    """
    Decode a DeviceSQL string at absolute byte position `pos` in `buf`.

    Three encodings:
      - Short ASCII: length_and_kind is odd, total field = (lk >> 1) bytes,
        text bytes = (lk >> 1) - 1.
      - Long ASCII (lk == 0x40): u2 length follows, then 1 pad byte, then
        (length - 4) ASCII bytes.
      - Long UTF-16-LE (lk == 0x90): same header, (length - 4) UTF-16-LE bytes.

    Returns None on malformed/empty strings rather than raising; pdb files in
    the wild contain plenty of empty/padding strings and we want to keep
    reading.
    """
    if pos < 0 or pos >= len(buf):
        return None
    lk = buf[pos]
    try:
        if lk == 0x40:  # long ASCII
            if pos + 4 > len(buf):
                return None
            length = struct.unpack_from("<H", buf, pos + 1)[0]
            text_len = length - 4
            if text_len <= 0 or pos + 4 + text_len > len(buf):
                return None
            return buf[pos + 4 : pos + 4 + text_len].decode("ascii", errors="replace")
        if lk == 0x90:  # long UTF-16-LE
            if pos + 4 > len(buf):
                return None
            length = struct.unpack_from("<H", buf, pos + 1)[0]
            text_len = length - 4
            if text_len <= 0 or pos + 4 + text_len > len(buf):
                return None
            return buf[pos + 4 : pos + 4 + text_len].decode("utf-16-le", errors="replace")
        if lk & 0x01:  # short ASCII (low bit set)
            total = lk >> 1
            text_len = total - 1
            if text_len <= 0 or pos + 1 + text_len > len(buf):
                return "" if text_len == 0 else None
            return buf[pos + 1 : pos + 1 + text_len].decode("ascii", errors="replace")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Row layout decoders (one per table type we care about)
# ---------------------------------------------------------------------------

def _decode_track_row(buf: bytes, row_base: int) -> Optional[PdbTrack]:
    """Track row is 0x5E bytes of fixed fields + 21 u2 string offsets."""
    if row_base + 0x5E + 42 > len(buf):
        return None
    # tempo at +0x38, key_id at +0x20, artist_id at +0x44, id at +0x48
    tempo, = struct.unpack_from("<I", buf, row_base + 0x38)
    key_id, = struct.unpack_from("<I", buf, row_base + 0x20)
    artist_id, = struct.unpack_from("<I", buf, row_base + 0x44)
    track_id, = struct.unpack_from("<I", buf, row_base + 0x48)
    str_offsets = struct.unpack_from("<21H", buf, row_base + 0x5E)
    title = _read_device_sql_string(buf, row_base + str_offsets[17]) if str_offsets[17] else None
    date_added = _read_device_sql_string(buf, row_base + str_offsets[10]) if str_offsets[10] else None
    file_path = _read_device_sql_string(buf, row_base + str_offsets[20]) if str_offsets[20] else None
    bpm = (tempo / 100.0) if tempo > 0 else None
    return PdbTrack(
        track_id=track_id,
        title=title or None,
        artist_id=artist_id,
        key_id=key_id,
        bpm=bpm,
        date_added=date_added or None,
        file_path=file_path or None,
    )


def _decode_artist_row(buf: bytes, row_base: int) -> Optional[tuple[int, str]]:
    """
    Artist row: u2 subtype, u2 index_shift, u4 id, u1 (=0x03), u1 ofs_name_near.
    If subtype & 0x04, a u2 ofs_name_far lives at row_base + 0x0A.
    """
    if row_base + 0x0A > len(buf):
        return None
    subtype, _shift, artist_id = struct.unpack_from("<HHI", buf, row_base)
    if subtype & 0x04:
        if row_base + 0x0C > len(buf):
            return None
        ofs = struct.unpack_from("<H", buf, row_base + 0x0A)[0]
    else:
        ofs = buf[row_base + 0x09]
    name = _read_device_sql_string(buf, row_base + ofs) if ofs else None
    if name is None:
        return None
    return (artist_id, name)


def _decode_key_row(buf: bytes, row_base: int) -> Optional[tuple[int, str]]:
    """Key row: u4 id, u4 id2, then device_sql_string name."""
    if row_base + 8 > len(buf):
        return None
    key_id, _id2 = struct.unpack_from("<II", buf, row_base)
    name = _read_device_sql_string(buf, row_base + 8)
    if name is None:
        return None
    return (key_id, name)


def _decode_history_playlist_row(buf: bytes, row_base: int) -> Optional[tuple[int, str]]:
    """History playlist row: u4 id, then device_sql_string name."""
    if row_base + 4 > len(buf):
        return None
    history_id = struct.unpack_from("<I", buf, row_base)[0]
    name = _read_device_sql_string(buf, row_base + 4)
    if name is None:
        return None
    return (history_id, name)


def _decode_history_entry_row(buf: bytes, row_base: int) -> Optional[tuple[int, int, int]]:
    """History entry row: u4 track_id, u4 playlist_id, u4 entry_index."""
    if row_base + 12 > len(buf):
        return None
    track_id, playlist_id, entry_index = struct.unpack_from("<III", buf, row_base)
    return (track_id, playlist_id, entry_index)


# ---------------------------------------------------------------------------
# Page + table traversal
# ---------------------------------------------------------------------------

def _iter_table_pages(buf: bytes, len_page: int, first_page_idx: int,
                      last_page_idx: int, table_type: int):
    """
    Yield (page_bytes, page_start_in_buf) for each page in a table chain.

    Follows next_page links; stops at last_page_idx, when next_page goes
    past the file, or when a page changes type (defensive guard from kaitai
    notes). The first page of a table is often a sentinel with zero rows.
    """
    page_idx = first_page_idx
    seen: set[int] = set()
    while True:
        if page_idx in seen:
            return  # cycle guard
        seen.add(page_idx)
        page_start = page_idx * len_page
        if page_start + len_page > len(buf):
            return
        page = buf[page_start : page_start + len_page]
        # page header layout (little-endian):
        # 0x00 4 zeros, 0x04 page_index u4, 0x08 type u4, 0x0C next_page u4
        if len(page) < 0x10:
            return
        page_type = struct.unpack_from("<I", page, 0x08)[0]
        next_page = struct.unpack_from("<I", page, 0x0C)[0]
        if page_type == table_type:
            yield page, page_start
        if page_idx == last_page_idx:
            return
        if next_page * len_page >= len(buf):
            return
        if next_page == page_idx:
            return
        page_idx = next_page


def _iter_rows(page: bytes, len_page: int):
    """
    Yield row_base byte offsets (within the page) for every present row.

    Row index is built backwards from the page end in groups of 16. For
    group g, base = len_page - (g * 0x24). At base-4 sits a u2 presence
    bitmap, and at base-(6 + 2*i) sits row i's u2 offset (relative to
    heap_pos = end of page header = 0x28).
    """
    if len(page) < 0x20:
        return
    # page_flags at 0x1B, bit 0x40 set => index page (no data rows)
    page_flags = page[0x1B]
    if page_flags & 0x40:
        return
    # 24-bit packed row counts at 0x18 (little-endian bit packing).
    # Kaitai parses these as b13 (num_row_offsets) then b11 (num_rows) in
    # little-endian bit-endian. Equivalent to reading a u4, masking, etc.
    rc_word = struct.unpack_from("<I", page, 0x18)[0] & 0x00FFFFFF
    # b13 = bits 0..12, b11 = bits 13..23
    num_row_offsets = rc_word & 0x1FFF
    if num_row_offsets == 0:
        return
    num_groups = (num_row_offsets - 1) // 16 + 1
    for g in range(num_groups):
        base = len_page - (g * 0x24)
        if base - 4 < 0 or base - 4 + 2 > len(page):
            continue
        presence = struct.unpack_from("<H", page, base - 4)[0]
        # rows in this group: up to 16, but the last group may be partial
        rows_in_group = min(16, num_row_offsets - g * 16)
        for i in range(rows_in_group):
            if not (presence >> i) & 1:
                continue
            ofs_pos = base - (6 + 2 * i)
            if ofs_pos < 0 or ofs_pos + 2 > len(page):
                continue
            ofs_row = struct.unpack_from("<H", page, ofs_pos)[0]
            yield PAGE_HEADER_LEN + ofs_row


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def read_pdb(path: str | Path) -> dict:
    """
    Parse an export.pdb file and return:

        {
            'tracks':   dict[int, PdbTrack],   # keyed by track_id
            'artists':  dict[int, str],        # keyed by artist_id
            'keys':     dict[int, str],        # keyed by key_id
            'sessions': list[PdbSession],      # one per history_playlist
        }

    Sessions' track_ids are sorted by entry_index ascending.
    """
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 0x20:
        raise ValueError(f"{path}: file too small to be a pdb ({len(data)} bytes)")

    # File header: u4 zero, u4 len_page, u4 num_tables, u4 next_unused_page,
    # u4, u4 sequence, u4 zero. Tables start at 0x1C.
    len_page = struct.unpack_from("<I", data, 0x04)[0]
    num_tables = struct.unpack_from("<I", data, 0x08)[0]
    if len_page == 0 or len_page > 0x100000:
        raise ValueError(f"{path}: implausible len_page {len_page}")

    # Each table entry: u4 type, u4 empty_candidate, u4 first_page, u4 last_page.
    tables: dict[int, tuple[int, int]] = {}
    for i in range(num_tables):
        off = 0x1C + i * 16
        if off + 16 > len(data):
            break
        t_type, _empty, first_page, last_page = struct.unpack_from("<IIII", data, off)
        tables[t_type] = (first_page, last_page)

    tracks: dict[int, PdbTrack] = {}
    artists: dict[int, str] = {}
    keys: dict[int, str] = {}
    history_playlists: dict[int, str] = {}
    history_entries: list[tuple[int, int, int]] = []  # (track_id, pl_id, idx)

    def walk(table_type: int, decode_fn, sink):
        if table_type not in tables:
            return
        first_page, last_page = tables[table_type]
        for page, _start in _iter_table_pages(data, len_page, first_page,
                                              last_page, table_type):
            for row_base in _iter_rows(page, len_page):
                # row_base is offset within the page; convert to absolute
                # by re-finding the page start. Cheaper: pass the page slice
                # itself and use page-relative row_base directly.
                decoded = decode_fn(page, row_base)
                if decoded is not None:
                    sink(decoded)

    walk(PT_TRACKS, _decode_track_row,
         lambda t: tracks.__setitem__(t.track_id, t))
    walk(PT_ARTISTS, _decode_artist_row,
         lambda pair: artists.__setitem__(pair[0], pair[1]))
    walk(PT_KEYS, _decode_key_row,
         lambda pair: keys.__setitem__(pair[0], pair[1]))
    walk(PT_HISTORY_PLAYLISTS, _decode_history_playlist_row,
         lambda pair: history_playlists.__setitem__(pair[0], pair[1]))
    walk(PT_HISTORY_ENTRIES, _decode_history_entry_row,
         lambda triple: history_entries.append(triple))

    # Group entries by playlist, sort by entry_index, build PdbSession list.
    per_pl: dict[int, list[tuple[int, int]]] = {}  # pl_id -> [(idx, track_id)]
    for track_id, pl_id, idx in history_entries:
        per_pl.setdefault(pl_id, []).append((idx, track_id))

    sessions: list[PdbSession] = []
    for pl_id, name in history_playlists.items():
        entries = sorted(per_pl.get(pl_id, []), key=lambda x: x[0])
        sessions.append(PdbSession(
            history_id=pl_id,
            name=name,
            date_added=None,
            track_ids=[tid for _, tid in entries],
        ))
    # Sort sessions by history_id so digest ordering is stable across runs.
    sessions.sort(key=lambda s: s.history_id)

    return {
        "tracks": tracks,
        "artists": artists,
        "keys": keys,
        "sessions": sessions,
    }
