# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Retention policy management for ghbackup."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console

from src.restore import parse_backup_timestamp

console = Console()


def parse_retention_policy(policy_str: str) -> Tuple[int, int, int]:
    """Parse retention policy string into (hourly_days, daily_days, weekly_weeks).

    Expected format: 'H,D,W' (e.g. '7,30,52')
    where:
        H: number of days of hourly backups to retain (full + partial)
        D: number of days of daily backups to retain (keep only full backups)
        W: number of weeks of weekly backups to retain (keep one daily backup per week, the first full backup of the week)

    Args:
        policy_str: Comma-separated string with 3 non-negative integers.

    Returns:
        Tuple[int, int, int]: (hourly_days, daily_days, weekly_weeks)

    Raises:
        ValueError: If format is invalid or values are negative / non-integer.
    """
    raw = policy_str.strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ValueError(
            f"Invalid retention policy format: '{policy_str}'. "
            f"Expected format is 'HOURLY_DAYS,DAILY_DAYS,WEEKLY_WEEKS' (e.g. '7,30,52')."
        )

    parsed_values: List[int] = []
    for p in parts:
        try:
            val = int(p)
        except ValueError:
            raise ValueError(
                f"Invalid retention policy value '{p}' in '{policy_str}'. All values must be integers."
            )
        if val < 0:
            raise ValueError(
                f"Invalid retention policy value '{p}' in '{policy_str}'. All values must be non-negative."
            )
        parsed_values.append(val)

    return (parsed_values[0], parsed_values[1], parsed_values[2])


def evaluate_retention_policy(
    backups: List[Dict[str, Any]],
    hourly_days: int,
    daily_days: int,
    weekly_weeks: int,
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Evaluate retention policy on a list of backups.

    Retention policy specification:
    - Retains the last `hourly_days` days of hourly backup (full + partial).
    - Retains the last `daily_days` days of daily backup (keep only the full backups, 1 per day).
    - Retains the last `weekly_weeks` weeks (keep one daily backup per week, the first full backup of the week).
    - Overlap priority: hourly has priority over daily over weekly (considers the policy
      which keeps the most details).

    Args:
        backups: List of backup metadata dicts with keys 'path', 'filename', 'timestamp', 'type'.
        hourly_days: Number of days of hourly backups to retain.
        daily_days: Number of days of daily backups to retain.
        weekly_weeks: Number of weeks of weekly backups to retain.
        now: Optional reference datetime (defaults to datetime.now()).

    Returns:
        Tuple[List[Dict], List[Dict]]: (kept_backups, removed_backups)
    """
    if now is None:
        now = datetime.now()

    # Sort backups chronologically ascending
    sorted_backups = sorted(backups, key=lambda b: (b["timestamp"], b["filename"]))

    keep_paths = set()

    # 1. Hourly policy: retains the last `hourly_days` days of hourly backup (full + partial)
    if hourly_days > 0:
        hourly_cutoff = now - timedelta(days=hourly_days)
        for b in sorted_backups:
            if b["timestamp"] >= hourly_cutoff:
                keep_paths.add(b["path"])

    # 2. Daily policy: retains the last `daily_days` days of daily backup (keep only the full backups)
    # Among full backups on each day within the cutoff, keep the first full backup of the day.
    if daily_days > 0:
        daily_cutoff = now - timedelta(days=daily_days)
        seen_days = set()
        for b in sorted_backups:
            if b["type"] == "full" and b["timestamp"] >= daily_cutoff:
                day_key = b["timestamp"].date()
                if day_key not in seen_days:
                    seen_days.add(day_key)
                    keep_paths.add(b["path"])

    # 3. Weekly policy: retains the last `weekly_weeks` weeks (keep one daily backup per week,
    # the first full backup of the week)
    # Among full backups in each ISO week within the cutoff, keep the first full backup of the week.
    if weekly_weeks > 0:
        weekly_cutoff = now - timedelta(weeks=weekly_weeks)
        seen_weeks = set()
        for b in sorted_backups:
            if b["type"] == "full" and b["timestamp"] >= weekly_cutoff:
                week_key = b["timestamp"].isocalendar()[:2]
                if week_key not in seen_weeks:
                    seen_weeks.add(week_key)
                    keep_paths.add(b["path"])

    kept = [b for b in sorted_backups if b["path"] in keep_paths]
    removed = [b for b in sorted_backups if b["path"] not in keep_paths]

    return kept, removed


def find_timestamped_backups(output_dir: Path, org: str) -> List[Dict[str, Any]]:
    """Scan output_dir for timestamped backups (.zip and .patch) for org.

    Canonical symlinks or non-timestamped files (<org>-repo.zip, <org>-repo.patch)
    and directories are strictly excluded.

    Args:
        output_dir: Directory where backups are stored.
        org: GitHub organization or user name.

    Returns:
        List[Dict[str, Any]]: List of timestamped backup metadata dicts.
    """
    if not output_dir.exists() or not output_dir.is_dir():
        return []

    backups = []
    for item in output_dir.iterdir():
        if item.is_dir():
            continue

        filename = item.name
        is_zip = filename.endswith(".zip")
        is_patch = filename.endswith(".patch")
        if not is_zip and not is_patch:
            continue

        if not (filename.startswith(f"{org}-repo") or filename.startswith(f"{org}-") or filename.startswith(f"{org}.")):
            continue

        # Exclude canonical files and symlinks without timestamp
        if filename in (f"{org}-repo.zip", f"{org}-repo.patch", f"{org}.zip", f"{org}.patch"):
            continue

        ts = parse_backup_timestamp(filename)
        if ts is None:
            continue

        backups.append({
            "path": item,
            "filename": filename,
            "timestamp": ts,
            "type": "full" if is_zip else "patch",
        })

    backups.sort(key=lambda b: (b["timestamp"], b["filename"]))
    return backups


def apply_retention_policy(
    output_dir: Path,
    org: str,
    hourly_days: int,
    daily_days: int,
    weekly_weeks: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Discover timestamped backups in output_dir, evaluate retention policy, and delete obsolete backups.

    Args:
        output_dir: Directory containing backups.
        org: GitHub organization or user name.
        hourly_days: Days of hourly backups to retain.
        daily_days: Days of daily backups to retain.
        weekly_weeks: Weeks of weekly backups to retain.
        now: Reference datetime.

    Returns:
        Dict[str, Any]: Summary containing total_found, total_kept, total_removed, kept_backups,
                        removed_backups, and removed_filenames.
    """
    backups = find_timestamped_backups(output_dir, org)
    kept, removed = evaluate_retention_policy(
        backups=backups,
        hourly_days=hourly_days,
        daily_days=daily_days,
        weekly_weeks=weekly_weeks,
        now=now,
    )

    removed_filenames: List[str] = []
    for b in removed:
        path: Path = b["path"]
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
                removed_filenames.append(b["filename"])
        except OSError as e:
            console.print(f"[yellow]Warning: Could not remove obsolete backup {path}: {e}[/yellow]")

    return {
        "total_found": len(backups),
        "total_kept": len(kept),
        "total_removed": len(removed_filenames),
        "kept_backups": kept,
        "removed_backups": removed,
        "removed_filenames": removed_filenames,
    }
