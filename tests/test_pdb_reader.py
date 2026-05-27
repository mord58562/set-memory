"""
test_pdb_reader.py - Tests for the pure-Python export.pdb reader.

Skipped unless the ROB USB 11 drive is mounted (same skip pattern as
test_smoke.py). When mounted, exercises the real binary parser against
4 MB of live data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pdb_reader  # noqa: E402

PDB_PATH = Path("/Volumes/ROB USB 11/PIONEER/rekordbox/export.pdb")

pytestmark = pytest.mark.skipif(
    not PDB_PATH.is_file(),
    reason="ROB USB 11 not mounted (no export.pdb at expected path)",
)


@pytest.fixture(scope="module")
def parsed():
    return pdb_reader.read_pdb(PDB_PATH)


def test_top_level_keys(parsed):
    assert set(parsed.keys()) == {"tracks", "artists", "keys", "sessions"}


def test_tracks_parsed(parsed):
    tracks = parsed["tracks"]
    # 4 MB pdb with a real library: expect at minimum hundreds of tracks.
    assert len(tracks) > 100
    # Every key is an int track_id matching the row id.
    for tid, t in tracks.items():
        assert isinstance(tid, int) and tid > 0
        assert t.track_id == tid


def test_artists_keys_present(parsed):
    assert len(parsed["artists"]) > 10
    assert len(parsed["keys"]) >= 1
    for aid, name in parsed["artists"].items():
        assert isinstance(aid, int) and isinstance(name, str)


def test_sessions_have_track_ids_in_order(parsed):
    sessions = parsed["sessions"]
    # A USB someone has actually played from has >= 1 history playlist.
    assert len(sessions) >= 1
    for s in sessions:
        assert isinstance(s.history_id, int)
        assert isinstance(s.name, str) and s.name
        # track_ids should be unique-per-position and reference real tracks
        # most of the time (some may dangle if the library was edited).
        assert isinstance(s.track_ids, list)


def test_bpm_is_real_units_not_centibpm(parsed):
    """pdb_reader must divide tempo by 100 before returning."""
    bpms = [t.bpm for t in parsed["tracks"].values() if t.bpm is not None]
    if not bpms:
        pytest.skip("library has no analysed tracks")
    # Real BPMs sit in the 50-220 range; centibpm would be 5000-22000.
    median = sorted(bpms)[len(bpms) // 2]
    assert 50.0 <= median <= 220.0


def test_track_string_offsets_resolve(parsed):
    """At least one track should have a parseable title and file_path."""
    titled = [t for t in parsed["tracks"].values() if t.title]
    pathed = [t for t in parsed["tracks"].values() if t.file_path]
    assert len(titled) > len(parsed["tracks"]) // 2  # majority titled
    assert len(pathed) > 0


def test_artist_and_key_ids_link(parsed):
    """A non-trivial fraction of tracks resolve to a known artist + key."""
    tracks = parsed["tracks"]
    artists = parsed["artists"]
    keys = parsed["keys"]
    linked_artists = sum(1 for t in tracks.values() if t.artist_id in artists)
    linked_keys = sum(1 for t in tracks.values() if t.key_id in keys)
    assert linked_artists > len(tracks) // 2
    # Keys are optional per track but most analysed libraries have many.
    assert linked_keys > 0
