"""
test_discovery.py - unit tests for the auto-discovery scanner that
replaces the old UUID-gated single-USB model. discover_rekordbox_usbs
walks /Volumes/* and returns master.db paths for every drive that
has both PIONEER/Master/master.db and a PIONEER/rekordbox/ subtree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from set_memory import discover_rekordbox_usbs


def _make_fake_rekordbox_drive(root: Path, name: str) -> Path:
    """Lay out the minimum directory structure that should look like a
    real rekordbox USB to the scanner: PIONEER/Master/master.db plus a
    PIONEER/rekordbox/ folder. Returns the path to master.db."""
    drive = root / name
    (drive / "PIONEER" / "Master").mkdir(parents=True)
    (drive / "PIONEER" / "rekordbox").mkdir(parents=True)
    master_db = drive / "PIONEER" / "Master" / "master.db"
    master_db.write_bytes(b"\x00")  # presence-only fixture; not parsed by discovery
    return master_db


def test_discover_returns_empty_when_no_volumes(tmp_path: Path) -> None:
    fake_root = tmp_path / "nonexistent"
    assert discover_rekordbox_usbs(fake_root) == []


def test_discover_returns_empty_when_no_rekordbox_drives(tmp_path: Path) -> None:
    volumes = tmp_path / "Volumes"
    (volumes / "BackupDrive").mkdir(parents=True)
    (volumes / "PhotoCard" / "DCIM").mkdir(parents=True)
    assert discover_rekordbox_usbs(volumes) == []


def test_discover_finds_single_rekordbox_drive(tmp_path: Path) -> None:
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    master = _make_fake_rekordbox_drive(volumes, "DJ-USB")
    assert discover_rekordbox_usbs(volumes) == [master]


def test_discover_finds_multiple_drives(tmp_path: Path) -> None:
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    m1 = _make_fake_rekordbox_drive(volumes, "DJ-USB-A")
    m2 = _make_fake_rekordbox_drive(volumes, "DJ-USB-B")
    # Add a non-rekordbox drive too; it should be skipped silently.
    (volumes / "Photos" / "DCIM").mkdir(parents=True)
    found = discover_rekordbox_usbs(volumes)
    assert sorted(found) == sorted([m1, m2])


def test_discover_skips_pioneer_without_rekordbox_subfolder(tmp_path: Path) -> None:
    """A backup folder a user happens to have named PIONEER shouldn't
    fool the scanner. Both master.db AND the rekordbox export tree must
    be present."""
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    fake = volumes / "Imposter" / "PIONEER" / "Master"
    fake.mkdir(parents=True)
    (fake / "master.db").write_bytes(b"\x00")
    # No PIONEER/rekordbox/ folder.
    assert discover_rekordbox_usbs(volumes) == []


def test_discover_skips_rekordbox_folder_without_master_db(tmp_path: Path) -> None:
    """Mirror case: rekordbox/ exists but master.db doesn't."""
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    drive = volumes / "Halfbuilt"
    (drive / "PIONEER" / "rekordbox").mkdir(parents=True)
    (drive / "PIONEER" / "Master").mkdir(parents=True)
    # No master.db inside Master/.
    assert discover_rekordbox_usbs(volumes) == []
