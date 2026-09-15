# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Unit and integration tests for ghbackup restore command and logic."""

import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
import pytest
from click.testing import CliRunner

from src.main import app
from src.restore import (
    RestoreError,
    apply_patch_to_repo,
    extract_repo_from_zip,
    filter_patch_for_repo,
    find_backups,
    parse_backup_timestamp,
    parse_target_date,
    restore_repository,
    select_backup_chain,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def create_mock_full_backup(zip_path: Path, org: str, repo_files: dict):
    """Helper to create a full backup zip file.

    repo_files: { "repo1": { "file.txt": "content", ... }, ... }
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for repo_name, files in repo_files.items():
            for rel_file, content in files.items():
                arcname = f"{org}-repo/{repo_name}/{rel_file}"
                if isinstance(content, str):
                    zf.writestr(arcname, content.encode("utf-8"))
                else:
                    zf.writestr(arcname, content)


def create_mock_patch(
    patch_path: Path,
    prev_root: Path,
    curr_root: Path,
    base_dir: Path,
):
    """Helper to create a binary patch between prev_root and curr_root."""
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    rel_prev = os.path.relpath(prev_root, base_dir)
    rel_curr = os.path.relpath(curr_root, base_dir)
    with open(patch_path, "wb") as f:
        subprocess.run(
            ["git", "diff", "--binary", "--no-index", rel_prev, rel_curr],
            cwd=base_dir,
            stdout=f,
            check=False,
        )


# ==============================================================================
# Timestamp & Date Parsing Tests
# ==============================================================================

def test_parse_backup_timestamp():
    """Test extracting timestamp from various filename formats."""
    # Standard format
    dt = parse_backup_timestamp("myorg-repo-20260915_123045.zip")
    assert dt == datetime(2026, 9, 15, 12, 30, 45)

    # Patch file
    dt_patch = parse_backup_timestamp("myorg-repo-20260915_130000.patch")
    assert dt_patch == datetime(2026, 9, 15, 13, 0, 0)

    # Hyphen format
    dt_hyphen = parse_backup_timestamp("myorg-repo-20260915-140000.zip")
    assert dt_hyphen == datetime(2026, 9, 15, 14, 0, 0)

    # ISO format
    dt_iso = parse_backup_timestamp("myorg-repo-2026-09-15_15-00-00.zip")
    assert dt_iso == datetime(2026, 9, 15, 15, 0, 0)

    # No timestamp
    assert parse_backup_timestamp("myorg-repo.zip") is None
    assert parse_backup_timestamp("random-file.txt") is None


def test_parse_target_date_valid():
    """Test parsing user date strings in various valid formats."""
    # Date only (should default time to 23:59:59)
    d1 = parse_target_date("2026-09-15")
    assert d1 == datetime(2026, 9, 15, 23, 59, 59)

    d2 = parse_target_date("20260915")
    assert d2 == datetime(2026, 9, 15, 23, 59, 59)

    # Date and time
    d3 = parse_target_date("2026-09-15 14:30:00")
    assert d3 == datetime(2026, 9, 15, 14, 30, 0)

    d4 = parse_target_date("2026-09-15T14:30:00")
    assert d4 == datetime(2026, 9, 15, 14, 30, 0)

    d5 = parse_target_date("20260915_143000")
    assert d5 == datetime(2026, 9, 15, 14, 30, 0)

    d6 = parse_target_date("2026-09-15 14:30")
    assert d6 == datetime(2026, 9, 15, 14, 30, 0)


def test_parse_target_date_invalid():
    """Test invalid date strings raise RestoreError."""
    with pytest.raises(RestoreError, match="Invalid date format"):
        parse_target_date("not-a-date")

    with pytest.raises(RestoreError, match="Invalid date format"):
        parse_target_date("2026-13-45")


# ==============================================================================
# Backup Discovery & Chain Selection Tests
# ==============================================================================

def test_find_backups(temp_dir):
    """Test finding full backups and patches for an organization."""
    # Create files for testorg and otherorg
    (temp_dir / "testorg-repo-20260915_100000.zip").write_text("dummy")
    (temp_dir / "testorg-repo-20260915_110000.patch").write_text("dummy")
    (temp_dir / "testorg-repo-20260915_120000.patch").write_text("dummy")
    (temp_dir / "testorg-repo-20260915_130000.zip").write_text("dummy")
    (temp_dir / "otherorg-repo-20260915_100000.zip").write_text("dummy")
    (temp_dir / "unrelated.txt").write_text("dummy")

    full, patches = find_backups(temp_dir, "testorg")

    assert len(full) == 2
    assert len(patches) == 2

    # Check ascending sort order
    assert full[0]["filename"] == "testorg-repo-20260915_100000.zip"
    assert full[1]["filename"] == "testorg-repo-20260915_130000.zip"
    assert patches[0]["filename"] == "testorg-repo-20260915_110000.patch"
    assert patches[1]["filename"] == "testorg-repo-20260915_120000.patch"


def test_find_backups_canonical_symlink(temp_dir):
    """Test symlinks to existing timestamped files are not duplicated."""
    real_file = temp_dir / "testorg-repo-20260915_100000.zip"
    real_file.write_text("zip content")
    symlink_file = temp_dir / "testorg-repo.zip"
    symlink_file.symlink_to(real_file.name)

    full, patches = find_backups(temp_dir, "testorg")
    assert len(full) == 1
    assert full[0]["filename"] == "testorg-repo-20260915_100000.zip"


def test_select_backup_chain_latest(temp_dir):
    """Test chain selection without target date selects latest full backup + subsequent patches."""
    full = [
        {"filename": "f1.zip", "timestamp": datetime(2026, 9, 1, 10, 0), "type": "full", "path": Path("f1.zip")},
        {"filename": "f2.zip", "timestamp": datetime(2026, 9, 10, 10, 0), "type": "full", "path": Path("f2.zip")},
    ]
    patches = [
        {"filename": "p1.patch", "timestamp": datetime(2026, 9, 5, 10, 0), "type": "patch", "path": Path("p1.patch")},
        {"filename": "p2.patch", "timestamp": datetime(2026, 9, 12, 10, 0), "type": "patch", "path": Path("p2.patch")},
        {"filename": "p3.patch", "timestamp": datetime(2026, 9, 15, 10, 0), "type": "patch", "path": Path("p3.patch")},
    ]

    selected_full, selected_patches = select_backup_chain(full, patches, target_date=None)

    assert selected_full["filename"] == "f2.zip"
    assert len(selected_patches) == 2
    assert [p["filename"] for p in selected_patches] == ["p2.patch", "p3.patch"]


def test_select_backup_chain_with_date(temp_dir):
    """Test chain selection with target date chooses the nearest full backup and intermediate patches."""
    full = [
        {"filename": "f1.zip", "timestamp": datetime(2026, 9, 1, 10, 0), "type": "full", "path": Path("f1.zip")},
        {"filename": "f2.zip", "timestamp": datetime(2026, 9, 10, 10, 0), "type": "full", "path": Path("f2.zip")},
    ]
    patches = [
        {"filename": "p1.patch", "timestamp": datetime(2026, 9, 5, 10, 0), "type": "patch", "path": Path("p1.patch")},
        {"filename": "p2.patch", "timestamp": datetime(2026, 9, 12, 10, 0), "type": "patch", "path": Path("p2.patch")},
        {"filename": "p3.patch", "timestamp": datetime(2026, 9, 15, 10, 0), "type": "patch", "path": Path("p3.patch")},
    ]

    # Target date between p1 and f2 -> nearest full is f1, intermediate is p1
    target = datetime(2026, 9, 7, 0, 0)
    selected_full, selected_patches = select_backup_chain(full, patches, target_date=target)
    assert selected_full["filename"] == "f1.zip"
    assert [p["filename"] for p in selected_patches] == ["p1.patch"]

    # Target date between p2 and p3 -> nearest full is f2, intermediate is p2
    target2 = datetime(2026, 9, 13, 0, 0)
    selected_full2, selected_patches2 = select_backup_chain(full, patches, target_date=target2)
    assert selected_full2["filename"] == "f2.zip"
    assert [p["filename"] for p in selected_patches2] == ["p2.patch"]


def test_select_backup_chain_date_before_any_full():
    """Test error when target date is before earliest full backup."""
    full = [
        {"filename": "f1.zip", "timestamp": datetime(2026, 9, 10, 10, 0), "type": "full", "path": Path("f1.zip")}
    ]
    patches = []

    target = datetime(2026, 9, 1, 0, 0)
    with pytest.raises(RestoreError, match="No full backup found on or before"):
        select_backup_chain(full, patches, target_date=target)


def test_select_backup_chain_no_full_backups():
    """Test error when no full backup exists."""
    with pytest.raises(RestoreError, match="No full backup"):
        select_backup_chain([], [], target_date=None)


# ==============================================================================
# Extract from Zip & Patch Filtering Tests
# ==============================================================================

def test_extract_repo_from_zip(temp_dir):
    """Test extracting a single repository from a full backup zip."""
    zip_path = temp_dir / "myorg-repo-20260915_100000.zip"
    repo_data = {
        "repoA": {
            "README.md": "# Repo A",
            "src/code.py": "print('hello')",
        },
        "repoB": {
            "README.md": "# Repo B",
        },
    }
    create_mock_full_backup(zip_path, "myorg", repo_data)

    target_dir = temp_dir / "restored_repoA"
    res = extract_repo_from_zip(zip_path, "repoA", target_dir, "myorg")

    assert res["files_extracted"] == 2
    assert (target_dir / "README.md").read_text() == "# Repo A"
    assert (target_dir / "src" / "code.py").read_text() == "print('hello')"
    # Ensure repoB files were NOT extracted into target_dir
    assert not (target_dir / "repoB").exists()


def test_extract_repo_not_found_raises(temp_dir):
    """Test error when repository is missing from zip archive."""
    zip_path = temp_dir / "myorg-repo-20260915_100000.zip"
    create_mock_full_backup(zip_path, "myorg", {"repoA": {"file.txt": "abc"}})

    target_dir = temp_dir / "restored"
    with pytest.raises(RestoreError, match="was not found in backup archive"):
        extract_repo_from_zip(zip_path, "nonexistent-repo", target_dir, "myorg")


def test_filter_patch_for_repo():
    """Test isolating diff chunks belonging to a specific repository."""
    patch_text = (
        "diff --git a/myorg-repo-previous/repoA/f.txt b/myorg-repo/repoA/f.txt\n"
        "--- a/myorg-repo-previous/repoA/f.txt\n"
        "+++ b/myorg-repo/repoA/f.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/myorg-repo-previous/repoB/g.txt b/myorg-repo/repoB/g.txt\n"
        "--- a/myorg-repo-previous/repoB/g.txt\n"
        "+++ b/myorg-repo/repoB/g.txt\n"
        "@@ -1 +1 @@\n"
        "-b_old\n"
        "+b_new\n"
        "diff --git a/myorg-repo-previous/repoA-extra/h.txt b/myorg-repo-previous/repoA-extra/h.txt\n"
        "--- a/myorg-repo-previous/repoA-extra/h.txt\n"
        "+++ b/myorg-repo-previous/repoA-extra/h.txt\n"
    ).encode("utf-8")

    filtered = filter_patch_for_repo(patch_text, "repoA")
    filtered_str = filtered.decode("utf-8")

    assert "repoA/f.txt" in filtered_str
    assert "repoB/g.txt" not in filtered_str
    assert "repoA-extra/h.txt" not in filtered_str


# ==============================================================================
# End-to-End Restore Integration Tests
# ==============================================================================

def test_restore_full_backup_only(temp_dir):
    """End-to-end restore from full backup when no intermediate patches exist."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()

    zip_file = source_dir / "testorg-repo-20260915_100000.zip"
    create_mock_full_backup(
        zip_file,
        "testorg",
        {
            "myrepo": {
                "README.md": "# Original",
                "binary.dat": b"\x00\x01\x02",
            }
        },
    )

    dest_dir = temp_dir / "restored_myrepo"
    res = restore_repository(
        source_dir=source_dir,
        target_dir=dest_dir,
        repository="testorg/myrepo",
    )

    repo_dir = dest_dir / "testorg" / "myrepo"
    assert res["org"] == "testorg"
    assert res["repo"] == "myrepo"
    assert res["target_dir"] == repo_dir
    assert res["files_extracted"] == 2
    assert len(res["applied_patches"]) == 0
    assert (repo_dir / "README.md").read_text() == "# Original"
    assert (repo_dir / "binary.dat").read_bytes() == b"\x00\x01\x02"


def test_restore_full_plus_patches(temp_dir):
    """End-to-end restore from full backup + multiple intermediate patches."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()

    # 1. Full backup at 10:00:00
    zip_file = source_dir / "testorg-repo-20260915_100000.zip"
    create_mock_full_backup(
        zip_file,
        "testorg",
        {
            "targetrepo": {
                "file.txt": "v1 initial",
                "del.txt": "to be deleted",
                "bin.dat": b"\x01\x02\x03",
            },
            "otherrepo": {
                "other.txt": "other v1",
            },
        },
    )

    # 2. State at 11:00:00 (Patch 1: modify file.txt, delete del.txt, add add1.txt)
    state_10 = temp_dir / "state_10"
    state_11 = temp_dir / "state_11"
    for s in (state_10, state_11):
        (s / "testorg-repo-previous" / "targetrepo").mkdir(parents=True)
        (s / "testorg-repo" / "targetrepo").mkdir(parents=True)

    # Patch 1 prev vs curr
    p1_prev = temp_dir / "p1_prev"
    p1_curr = temp_dir / "p1_curr"
    (p1_prev / "targetrepo").mkdir(parents=True)
    (p1_curr / "targetrepo").mkdir(parents=True)

    (p1_prev / "targetrepo" / "file.txt").write_text("v1 initial")
    (p1_prev / "targetrepo" / "del.txt").write_text("to be deleted")
    (p1_prev / "targetrepo" / "bin.dat").write_bytes(b"\x01\x02\x03")

    (p1_curr / "targetrepo" / "file.txt").write_text("v2 modified")
    (p1_curr / "targetrepo" / "bin.dat").write_bytes(b"\x01\x02\x99")
    (p1_curr / "targetrepo" / "add1.txt").write_text("added in patch 1")

    # Include otherrepo diff in patch 1
    (p1_prev / "otherrepo").mkdir(parents=True)
    (p1_curr / "otherrepo").mkdir(parents=True)
    (p1_prev / "otherrepo" / "other.txt").write_text("other v1")
    (p1_curr / "otherrepo" / "other.txt").write_text("other v2")

    p1_file = source_dir / "testorg-repo-20260915_110000.patch"
    # Create git diff
    with open(p1_file, "wb") as f:
        subprocess.run(
            ["git", "diff", "--binary", "--no-index", "p1_prev", "p1_curr"],
            cwd=temp_dir,
            stdout=f,
            check=False,
        )

    # 3. State at 12:00:00 (Patch 2: modify file.txt to v3, add add2.txt)
    p2_prev = temp_dir / "p2_prev"
    p2_curr = temp_dir / "p2_curr"
    (p2_prev / "targetrepo").mkdir(parents=True)
    (p2_curr / "targetrepo").mkdir(parents=True)

    (p2_prev / "targetrepo" / "file.txt").write_text("v2 modified")
    (p2_prev / "targetrepo" / "bin.dat").write_bytes(b"\x01\x02\x99")
    (p2_prev / "targetrepo" / "add1.txt").write_text("added in patch 1")

    (p2_curr / "targetrepo" / "file.txt").write_text("v3 final")
    (p2_curr / "targetrepo" / "bin.dat").write_bytes(b"\x01\x02\x99")
    (p2_curr / "targetrepo" / "add1.txt").write_text("added in patch 1")
    (p2_curr / "targetrepo" / "add2.txt").write_text("added in patch 2")

    p2_file = source_dir / "testorg-repo-20260915_120000.patch"
    with open(p2_file, "wb") as f:
        subprocess.run(
            ["git", "diff", "--binary", "--no-index", "p2_prev", "p2_curr"],
            cwd=temp_dir,
            stdout=f,
            check=False,
        )

    # Restore to latest
    dest_dir = temp_dir / "restored_latest"
    res = restore_repository(
        source_dir=source_dir,
        target_dir=dest_dir,
        repository="testorg/targetrepo",
    )

    repo_dir = dest_dir / "testorg" / "targetrepo"
    assert len(res["applied_patches"]) == 2
    assert (repo_dir / "file.txt").read_text() == "v3 final"
    assert (repo_dir / "add1.txt").read_text() == "added in patch 1"
    assert (repo_dir / "add2.txt").read_text() == "added in patch 2"
    assert not (repo_dir / "del.txt").exists()
    assert (repo_dir / "bin.dat").read_bytes() == b"\x01\x02\x99"

    # Restore with date cutoff at 11:30:00 (should only apply Patch 1)
    dest_cutoff = temp_dir / "restored_cutoff"
    res_cutoff = restore_repository(
        source_dir=source_dir,
        target_dir=dest_cutoff,
        repository="testorg/targetrepo",
        target_date="2026-09-15 11:30:00",
    )

    cutoff_repo_dir = dest_cutoff / "testorg" / "targetrepo"
    assert len(res_cutoff["applied_patches"]) == 1
    assert res_cutoff["applied_patches"][0]["filename"] == "testorg-repo-20260915_110000.patch"
    assert (cutoff_repo_dir / "file.txt").read_text() == "v2 modified"
    assert (cutoff_repo_dir / "add1.txt").read_text() == "added in patch 1"
    assert not (cutoff_repo_dir / "add2.txt").exists()


def test_restore_force_overwrite(temp_dir):
    """Test overwrite protection and --force behavior."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()
    zip_file = source_dir / "testorg-repo-20260915_100000.zip"
    create_mock_full_backup(zip_file, "testorg", {"repo1": {"a.txt": "1"}})

    target = temp_dir / "target_base"
    occupied_dir = target / "testorg" / "repo1"
    occupied_dir.mkdir(parents=True)
    (occupied_dir / "conflict.txt").write_text("existing")

    # Without force: must fail
    with pytest.raises(RestoreError, match="already exists and is not empty"):
        restore_repository(
            source_dir=source_dir,
            target_dir=target,
            repository="testorg/repo1",
            force=False,
        )

    # With force: must succeed
    res = restore_repository(
        source_dir=source_dir,
        target_dir=target,
        repository="testorg/repo1",
        force=True,
    )
    assert res["files_extracted"] == 1
    assert (occupied_dir / "a.txt").read_text() == "1"
    assert not (occupied_dir / "conflict.txt").exists()


# ==============================================================================
# CLI Invocation Tests
# ==============================================================================

def test_cli_restore_invalid_repo_format(runner):
    """Test CLI errors when repository argument does not contain slash."""
    result = runner.invoke(app, ["restore", "onlyrepo"])
    assert result.exit_code == 1
    assert "Invalid repository identifier" in result.output
    assert "Expected format: <org>/<repo>" in result.output


def test_cli_restore_e2e(runner, temp_dir):
    """Test CLI restore command end-to-end and verifies <org>/<repo> folder created."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()
    zip_file = source_dir / "myorg-repo-20260915_100000.zip"
    create_mock_full_backup(
        zip_file,
        "myorg",
        {"sample-repo": {"README.md": "# Sample", "main.py": "print('ok')"}},
    )

    dest_dir = temp_dir / "restored_base"

    result = runner.invoke(
        app,
        [
            "restore",
            "myorg/sample-repo",
            "--source",
            str(source_dir),
            "--target",
            str(dest_dir),
        ],
    )

    assert result.exit_code == 0
    assert "Repository Restored Successfully!" in result.output
    repo_dir = dest_dir / "myorg" / "sample-repo"
    assert (repo_dir / "README.md").read_text() == "# Sample"
    assert (repo_dir / "main.py").read_text() == "print('ok')"


def test_cli_restore_with_date_and_force(runner, temp_dir):
    """Test CLI restore with date filtering and force flag."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()
    zip_file = source_dir / "myorg-repo-20260915_100000.zip"
    create_mock_full_backup(
        zip_file,
        "myorg",
        {"my-tool": {"config.json": '{"v": 1}'}},
    )

    dest_dir = temp_dir / "restore_base"
    repo_dir = dest_dir / "myorg" / "my-tool"
    repo_dir.mkdir(parents=True)
    (repo_dir / "dirty.txt").write_text("dirty")

    result = runner.invoke(
        app,
        [
            "restore",
            "myorg/my-tool",
            "-s",
            str(source_dir),
            "-t",
            str(dest_dir),
            "-d",
            "2026-09-15 12:00:00",
            "-f",
        ],
    )

    assert result.exit_code == 0
    assert "Repository Restored Successfully!" in result.output
    assert (repo_dir / "config.json").read_text() == '{"v": 1}'
    assert not (repo_dir / "dirty.txt").exists()


def test_cli_restore_fails_when_dest_not_empty_without_force(runner, temp_dir):
    """Test CLI errors when destination is dirty and --force is not passed."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()
    zip_file = source_dir / "myorg-repo-20260915_100000.zip"
    create_mock_full_backup(zip_file, "myorg", {"repo1": {"a.txt": "1"}})

    dest_dir = temp_dir / "restore_base"
    repo_dir = dest_dir / "myorg" / "repo1"
    repo_dir.mkdir(parents=True)
    (repo_dir / "somefile.txt").write_text("content")

    result = runner.invoke(
        app,
        [
            "restore",
            "myorg/repo1",
            "-s",
            str(source_dir),
            "-t",
            str(dest_dir),
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "not empty" in result.output
    assert "--force" in result.output


def test_restore_creates_org_repo_folder_structure(temp_dir):
    """Test that restoring explicitly creates <org>/<repo> inside the chosen target folder."""
    source_dir = temp_dir / "backups"
    source_dir.mkdir()
    zip_file = source_dir / "acme-repo-20260915_100000.zip"
    create_mock_full_backup(
        zip_file,
        "acme",
        {"widget": {"main.py": "print('widget')"}},
    )

    target_base = temp_dir / "my_custom_target"
    res = restore_repository(
        source_dir=source_dir,
        target_dir=target_base,
        repository="acme/widget",
    )

    expected_repo_dir = target_base / "acme" / "widget"
    assert res["target_base"] == target_base
    assert res["target_dir"] == expected_repo_dir
    assert expected_repo_dir.exists()
    assert (expected_repo_dir / "main.py").read_text() == "print('widget')"
