# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Unit and integration tests for backup retention policy."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from src.main import app
from src.retention import (
    apply_retention_policy,
    evaluate_retention_policy,
    find_timestamped_backups,
    parse_retention_policy,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ---------------------------------------------------------------------------
# Test parse_retention_policy
# ---------------------------------------------------------------------------


def test_parse_retention_policy_valid():
    """Test valid retention policy strings."""
    assert parse_retention_policy("7,30,52") == (7, 30, 52)
    assert parse_retention_policy(" 7 , 30 , 52 ") == (7, 30, 52)
    assert parse_retention_policy("1,2,3") == (1, 2, 3)
    assert parse_retention_policy("0,0,0") == (0, 0, 0)
    assert parse_retention_policy("14,60,104") == (14, 60, 104)


def test_parse_retention_policy_invalid_format():
    """Test invalid retention policy formats raise ValueError."""
    with pytest.raises(ValueError, match="Invalid retention policy format"):
        parse_retention_policy("7,30")

    with pytest.raises(ValueError, match="Invalid retention policy format"):
        parse_retention_policy("7,30,52,10")

    with pytest.raises(ValueError, match="Invalid retention policy format"):
        parse_retention_policy("")


def test_parse_retention_policy_non_integer():
    """Test non-integer values raise ValueError."""
    with pytest.raises(ValueError, match="All values must be integers"):
        parse_retention_policy("seven,30,52")

    with pytest.raises(ValueError, match="All values must be integers"):
        parse_retention_policy("7.5,30,52")


def test_parse_retention_policy_negative():
    """Test negative values raise ValueError."""
    with pytest.raises(ValueError, match="All values must be non-negative"):
        parse_retention_policy("-7,30,52")

    with pytest.raises(ValueError, match="All values must be non-negative"):
        parse_retention_policy("7,-30,52")

    with pytest.raises(ValueError, match="All values must be non-negative"):
        parse_retention_policy("7,30,-52")


# ---------------------------------------------------------------------------
# Test evaluate_retention_policy
# ---------------------------------------------------------------------------


def make_backup(name: str, ts: datetime, b_type: str = "full") -> dict:
    ext = ".zip" if b_type == "full" else ".patch"
    filename = f"myorg-repo-{ts.strftime('%Y%m%d_%H%M%S')}{ext}"
    return {
        "path": Path(f"/backups/{filename}"),
        "filename": filename,
        "timestamp": ts,
        "type": b_type,
    }


def test_evaluate_retention_empty():
    """Test empty backups list returns empty kept and removed lists."""
    kept, removed = evaluate_retention_policy([], 7, 30, 52)
    assert kept == []
    assert removed == []


def test_evaluate_retention_hourly_window():
    """Test that within 7 days, all hourly backups (both full and partial) are retained."""
    now = datetime(2026, 9, 15, 12, 0, 0)

    b_now = make_backup("now", now, "full")
    b_1h = make_backup("1h", now - timedelta(hours=1), "patch")
    b_2d_full = make_backup("2d_full", now - timedelta(days=2, hours=3), "full")
    b_2d_patch = make_backup("2d_patch", now - timedelta(days=2, hours=2), "patch")
    b_6d_patch = make_backup("6d_patch", now - timedelta(days=6, hours=23), "patch")

    backups = [b_now, b_1h, b_2d_full, b_2d_patch, b_6d_patch]
    kept, removed = evaluate_retention_policy(backups, 7, 30, 52, now=now)

    assert len(kept) == 5
    assert len(removed) == 0


def test_evaluate_retention_daily_window():
    """Test that between 7 and 30 days, daily retention keeps only full backups, 1 per day."""
    now = datetime(2026, 9, 15, 12, 0, 0)

    # Day 10 (2026-09-05): two full backups and one partial
    day10_early_full = make_backup("d10_early", datetime(2026, 9, 5, 2, 0, 0), "full")
    day10_late_full = make_backup("d10_late", datetime(2026, 9, 5, 14, 0, 0), "full")
    day10_patch = make_backup("d10_patch", datetime(2026, 9, 5, 16, 0, 0), "patch")

    # Day 20 (2026-08-26): one full backup and one patch
    day20_full = make_backup("d20_full", datetime(2026, 8, 26, 3, 0, 0), "full")
    day20_patch = make_backup("d20_patch", datetime(2026, 8, 26, 4, 0, 0), "patch")

    # Day 25 (2026-08-21): only a patch backup (no full backup on that day)
    day25_patch = make_backup("d25_patch", datetime(2026, 8, 21, 10, 0, 0), "patch")

    backups = [
        day10_early_full,
        day10_late_full,
        day10_patch,
        day20_full,
        day20_patch,
        day25_patch,
    ]
    kept, removed = evaluate_retention_policy(backups, 7, 30, 52, now=now)

    kept_paths = {b["path"] for b in kept}
    removed_paths = {b["path"] for b in removed}

    # First full backup of day 10 is kept; second full backup is removed
    assert day10_early_full["path"] in kept_paths
    assert day10_late_full["path"] in removed_paths

    # Day 10 patch is removed (only full backups kept in daily window)
    assert day10_patch["path"] in removed_paths

    # Day 20 full is kept, patch is removed
    assert day20_full["path"] in kept_paths
    assert day20_patch["path"] in removed_paths

    # Day 25 patch is removed
    assert day25_patch["path"] in removed_paths


def test_evaluate_retention_weekly_window():
    """Test that beyond 30 days and within 52 weeks, weekly retention keeps 1 full backup per week."""
    now = datetime(2026, 9, 15, 12, 0, 0)

    # Week 30 of 2026 (Mon July 20 to Sun July 26) - approx 55 days ago
    # Tuesday full backup
    w30_tue_full = make_backup("w30_tue", datetime(2026, 7, 21, 2, 0, 0), "full")
    # Thursday full backup in same week
    w30_thu_full = make_backup("w30_thu", datetime(2026, 7, 23, 2, 0, 0), "full")
    # Friday patch in same week
    w30_fri_patch = make_backup("w30_fri", datetime(2026, 7, 24, 2, 0, 0), "patch")

    # Week 25 of 2026 (Mon June 15 to Sun June 21) - approx 90 days ago
    w25_mon_full = make_backup("w25_mon", datetime(2026, 6, 15, 1, 0, 0), "full")

    backups = [w30_tue_full, w30_thu_full, w30_fri_patch, w25_mon_full]
    kept, removed = evaluate_retention_policy(backups, 7, 30, 52, now=now)

    kept_paths = {b["path"] for b in kept}
    removed_paths = {b["path"] for b in removed}

    # In week 30, the first full backup (Tuesday) is kept; Thursday is removed
    assert w30_tue_full["path"] in kept_paths
    assert w30_thu_full["path"] in removed_paths
    # Friday patch is removed
    assert w30_fri_patch["path"] in removed_paths
    # Week 25 first full backup is kept
    assert w25_mon_full["path"] in kept_paths


def test_evaluate_retention_expired_older_than_52_weeks():
    """Test that backups older than 52 weeks (364 days) are removed."""
    now = datetime(2026, 9, 15, 12, 0, 0)

    # 400 days ago
    old_full = make_backup("old_full", now - timedelta(days=400), "full")
    old_patch = make_backup("old_patch", now - timedelta(days=400), "patch")
    # 53 weeks ago (371 days ago)
    w53_full = make_backup("w53_full", now - timedelta(weeks=53), "full")

    backups = [old_full, old_patch, w53_full]
    kept, removed = evaluate_retention_policy(backups, 7, 30, 52, now=now)

    assert len(kept) == 0
    assert len(removed) == 3


def test_evaluate_retention_overlap_priority():
    """Test priority when 7 days, 30 days, and 52 weeks overlap.

    Hourly has priority over daily over weekly.
    - Within 7 days, partial backups must NOT be pruned even though daily/weekly would discard them.
    - Within 7 days, multiple backups per day must NOT be pruned even though daily would keep only 1.
    - Between 7 and 30 days, daily backups must NOT be pruned to 1 per week even though weekly would keep only 1.
    """
    now = datetime(2026, 9, 15, 12, 0, 0)

    # In the last 7 days (e.g. 3 days ago): 3 backups on the same day (1 full, 2 patches)
    d3_full = make_backup("d3_full", now - timedelta(days=3, hours=10), "full")
    d3_patch1 = make_backup("d3_patch1", now - timedelta(days=3, hours=8), "patch")
    d3_patch2 = make_backup("d3_patch2", now - timedelta(days=3, hours=6), "patch")

    # In days 8 to 30: 3 distinct days within the same calendar week
    # e.g. 10 days ago (Wed), 11 days ago (Tue), 12 days ago (Mon)
    # Each has a daily full backup. Weekly alone would keep only 1 of them!
    # But because daily has priority over weekly, ALL 3 daily full backups must be kept!
    # Mon Sept 1 2026 was in week 36
    b_mon = make_backup("mon", datetime(2026, 8, 31, 2, 0, 0), "full")  # age 15d
    b_tue = make_backup("tue", datetime(2026, 9, 1, 2, 0, 0), "full")   # age 14d
    b_wed = make_backup("wed", datetime(2026, 9, 2, 2, 0, 0), "full")   # age 13d

    backups = [d3_full, d3_patch1, d3_patch2, b_mon, b_tue, b_wed]
    kept, removed = evaluate_retention_policy(backups, 7, 30, 52, now=now)

    kept_paths = {b["path"] for b in kept}

    # All 3 backups on day 3 are kept (hourly priority keeps full + partial)
    assert d3_full["path"] in kept_paths
    assert d3_patch1["path"] in kept_paths
    assert d3_patch2["path"] in kept_paths

    # All 3 daily backups in the same week are kept (daily priority keeps 1 per day)
    assert b_mon["path"] in kept_paths
    assert b_tue["path"] in kept_paths
    assert b_wed["path"] in kept_paths

    assert len(removed) == 0


def test_evaluate_retention_zero_days():
    """Test policy with 0 for some components."""
    now = datetime(2026, 9, 15, 12, 0, 0)

    # 1 day ago (within 7d), 15 days ago (within 30d), 40 days ago (within 52w)
    b1 = make_backup("b1", now - timedelta(days=1), "patch")
    b2 = make_backup("b2", now - timedelta(days=15), "full")
    b3 = make_backup("b3", now - timedelta(days=40), "full")

    # If hourly_days=0, patch at 1 day ago is not kept by hourly; daily only keeps full
    kept, removed = evaluate_retention_policy([b1, b2, b3], 0, 30, 52, now=now)
    kept_paths = {b["path"] for b in kept}
    assert b1["path"] not in kept_paths
    assert b2["path"] in kept_paths
    assert b3["path"] in kept_paths


# ---------------------------------------------------------------------------
# Test find_timestamped_backups and apply_retention_policy
# ---------------------------------------------------------------------------


def test_find_timestamped_backups_excludes_canonical(temp_dir):
    """Test find_timestamped_backups ignores canonical symlinks/files and directories."""
    org = "testorg"

    # Valid timestamped backups
    f1 = temp_dir / f"{org}-repo-20260915_120000.zip"
    f1.write_text("dummy zip")
    f2 = temp_dir / f"{org}-repo-20260915_130000.patch"
    f2.write_text("dummy patch")

    # Canonical files (must be excluded)
    canon_zip = temp_dir / f"{org}-repo.zip"
    canon_zip.write_text("dummy canon zip")
    canon_patch = temp_dir / f"{org}-repo.patch"
    canon_patch.write_text("dummy canon patch")

    # Another organization (must be excluded)
    other = temp_dir / "otherorg-repo-20260915_120000.zip"
    other.write_text("other")

    # Directories (must be excluded)
    repo_dir = temp_dir / f"{org}-repo"
    repo_dir.mkdir()
    prev_dir = temp_dir / f"{org}-repo-previous"
    prev_dir.mkdir()

    backups = find_timestamped_backups(temp_dir, org)
    filenames = [b["filename"] for b in backups]

    assert f"{org}-repo-20260915_120000.zip" in filenames
    assert f"{org}-repo-20260915_130000.patch" in filenames
    assert f"{org}-repo.zip" not in filenames
    assert f"{org}-repo.patch" not in filenames
    assert "otherorg-repo-20260915_120000.zip" not in filenames
    assert len(backups) == 2


def test_apply_retention_policy_deletes_obsolete(temp_dir):
    """Test apply_retention_policy deletes obsolete backups and leaves kept files and canonical intact."""
    org = "testorg"
    now = datetime(2026, 9, 15, 12, 0, 0)

    # 1. Recent backup (1 day ago) - should be kept
    ts_recent = now - timedelta(days=1)
    f_recent = temp_dir / f"{org}-repo-{ts_recent.strftime('%Y%m%d_%H%M%S')}.zip"
    f_recent.write_text("recent")

    # 2. Daily backup in range (15 days ago, full) - should be kept
    ts_daily = now - timedelta(days=15)
    f_daily = temp_dir / f"{org}-repo-{ts_daily.strftime('%Y%m%d_%H%M%S')}.zip"
    f_daily.write_text("daily")

    # 3. Obsolete patch in daily range (15 days ago, patch) - should be deleted
    f_daily_patch = temp_dir / f"{org}-repo-{ts_daily.strftime('%Y%m%d_%H%M%S')}.patch"
    f_daily_patch.write_text("daily patch")

    # 4. Obsolete expired backup (400 days ago) - should be deleted
    ts_old = now - timedelta(days=400)
    f_old = temp_dir / f"{org}-repo-{ts_old.strftime('%Y%m%d_%H%M%S')}.zip"
    f_old.write_text("old")

    # 5. Canonical link - must NOT be touched
    canon = temp_dir / f"{org}-repo.zip"
    canon.write_text("canonical")

    # 6. Other org - must NOT be touched
    f_other = temp_dir / f"other-repo-{ts_old.strftime('%Y%m%d_%H%M%S')}.zip"
    f_other.write_text("other")

    result = apply_retention_policy(
        output_dir=temp_dir,
        org=org,
        hourly_days=7,
        daily_days=30,
        weekly_weeks=52,
        now=now,
    )

    assert result["total_found"] == 4
    assert result["total_kept"] == 2
    assert result["total_removed"] == 2

    # Verify filesystem state
    assert f_recent.exists()
    assert f_daily.exists()
    assert not f_daily_patch.exists()
    assert not f_old.exists()
    assert canon.exists()
    assert f_other.exists()


# ---------------------------------------------------------------------------
# Test CLI Integration
# ---------------------------------------------------------------------------


def test_cli_retention_policy_invalid(runner, temp_dir):
    """Test CLI fails when --retention-policy has invalid format."""
    result = runner.invoke(
        app,
        ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "--retention-policy", "invalid"]
    )
    assert result.exit_code == 1
    assert "Invalid retention policy format" in result.output


def test_cli_retention_policy_negative(runner, temp_dir):
    """Test CLI fails when --retention-policy has negative numbers."""
    result = runner.invoke(
        app,
        ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "--retention-policy", "7,-30,52"]
    )
    assert result.exit_code == 1
    assert "non-negative" in result.output


def test_cli_retention_policy_e2e(runner, temp_dir):
    """Test CLI runs retention policy after backup and prints summary."""
    mock_repos = [
        {
            "name": "sample-repo",
            "full_name": "testorg/sample-repo",
            "clone_url": "https://github.com/testorg/sample-repo.git",
            "ssh_url": "git@github.com:testorg/sample-repo.git",
            "is_fork": False,
            "is_archived": False,
            "is_private": False,
            "default_branch": "main",
            "size_kb": 10,
        }
    ]
    mock_summary = {"total": 1, "successful": 1, "failed": 0, "failed_repos": []}

    # Create an old expired backup in temp_dir that should be pruned
    old_backup = temp_dir / "testorg-repo-20200101_000000.zip"
    old_backup.write_text("old")

    with patch("src.main.fetch_org_or_user_repos", return_value=("organization", mock_repos)), \
         patch("src.main.clone_all_repositories", return_value=mock_summary), \
         patch("src.main.create_full_backup", return_value={"archive_path": str(temp_dir / "testorg-repo-20260915_120000.zip"), "total_files": 1, "uncompressed_bytes": 10, "compressed_bytes": 5}):
        result = runner.invoke(
            app,
            [
                "backup",
                "--full",
                "testorg",
                "--output-dir",
                str(temp_dir),
                "--retention-policy",
                "7,30,52",
            ],
        )

        assert result.exit_code == 0
        assert "Retention Policy:" in result.output
        assert "7 days hourly, 30 days daily, 52 weeks weekly" in result.output
        assert "Retention Policy Applied Successfully!" in result.output
        assert "Backups Evaluated:" in result.output
        assert not old_backup.exists()


def test_cli_retention_policy_short_flag(runner, temp_dir):
    """Test CLI -R short flag for --retention-policy."""
    mock_repos = [
        {
            "name": "sample-repo",
            "full_name": "testorg/sample-repo",
            "clone_url": "https://github.com/testorg/sample-repo.git",
            "ssh_url": "git@github.com:testorg/sample-repo.git",
            "is_fork": False,
            "is_archived": False,
            "is_private": False,
            "default_branch": "main",
            "size_kb": 10,
        }
    ]
    mock_summary = {"total": 1, "successful": 1, "failed": 0, "failed_repos": []}

    with patch("src.main.fetch_org_or_user_repos", return_value=("organization", mock_repos)), \
         patch("src.main.clone_all_repositories", return_value=mock_summary), \
         patch("src.main.create_full_backup", return_value={"archive_path": str(temp_dir / "testorg-repo-20260915_120000.zip"), "total_files": 1, "uncompressed_bytes": 10, "compressed_bytes": 5}):
        result = runner.invoke(
            app,
            [
                "backup",
                "--full",
                "testorg",
                "--output-dir",
                str(temp_dir),
                "-R",
                "7,30,52",
            ],
        )

        assert result.exit_code == 0
        assert "Retention Policy:" in result.output
        assert "Retention Policy Applied Successfully!" in result.output
