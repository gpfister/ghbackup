# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Backup creation module for Full (.zip) and Partial (.patch) modes."""

import os
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from rich.console import Console

from src.retention import (
    apply_retention_policy,
    evaluate_retention_policy,
    parse_retention_policy,
)

console = Console()


def generate_backup_timestamp() -> str:
    """Generate a standard timestamp string for backup files."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def create_full_backup(
    repo_dir: Path,
    output_dir: Path,
    org: str,
    custom_output_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create a full backup (.zip archive) of the repo_dir.

    Args:
        repo_dir: Directory containing the cloned repositories (<org>-repo).
        output_dir: Base directory where backup files are saved.
        org: Organization or user name.
        custom_output_file: Optional explicit path for the zip file.

    Returns:
        Dict[str, Any] with backup information.
    """
    if custom_output_file:
        zip_path = custom_output_file
    else:
        timestamp = generate_backup_timestamp()
        zip_path = output_dir / f"{org}-repo-{timestamp}.zip"

    # Also maintain a canonical latest symlink or copy <org>-repo.zip
    canonical_path = output_dir / f"{org}-repo.zip"

    console.print(f"[bold cyan]Creating full backup archive:[/bold cyan] {zip_path}")

    total_files = 0
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo_dir):
            for file in files:
                file_path = Path(root) / file
                # Store relative to repo_dir's parent so archive unpacks cleanly
                arcname = file_path.relative_to(repo_dir.parent)
                try:
                    zf.write(file_path, arcname=str(arcname))
                    total_files += 1
                    total_uncompressed_bytes += file_path.stat().st_size
                except (OSError, PermissionError) as e:
                    console.print(f"[yellow]Warning: Could not archive {file_path}: {e}[/yellow]")

    compressed_size = zip_path.stat().st_size

    # Maintain canonical <org>-repo.zip
    if not custom_output_file and zip_path != canonical_path:
        try:
            if canonical_path.exists() or canonical_path.is_symlink():
                canonical_path.unlink()
            # Try symlink first, fallback to copy if symlink fails
            try:
                canonical_path.symlink_to(zip_path.name)
            except OSError:
                shutil.copy2(zip_path, canonical_path)
        except OSError:
            pass

    return {
        "mode": "full",
        "archive_path": zip_path,
        "canonical_path": canonical_path if not custom_output_file else None,
        "total_files": total_files,
        "uncompressed_bytes": total_uncompressed_bytes,
        "compressed_bytes": compressed_size,
    }


def create_partial_backup(
    current_repo_dir: Path,
    previous_repo_dir: Optional[Path],
    output_dir: Path,
    org: str,
    custom_output_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create a partial backup (.patch file) using git binary diff.

    Uses `git diff --binary --no-index` between previous_repo_dir and current_repo_dir.

    Args:
        current_repo_dir: Path to newly cloned <org>-repo.
        previous_repo_dir: Path to previous backup directory <org>-repo-previous.
        output_dir: Base directory where backup files are saved.
        org: Organization or user name.
        custom_output_file: Optional explicit path for the patch file.

    Returns:
        Dict[str, Any] with patch details and apply instructions.

    Raises:
        FileNotFoundError: If previous_repo_dir is None or does not exist.
        RuntimeError: If git diff fails.
    """
    if previous_repo_dir is None or not previous_repo_dir.exists():
        raise FileNotFoundError(
            f"Cannot create partial backup: previous repository directory '{previous_repo_dir}' does not exist."
        )

    if custom_output_file:
        patch_path = custom_output_file
    else:
        timestamp = generate_backup_timestamp()
        patch_path = output_dir / f"{org}-repo-{timestamp}.patch"

    canonical_path = output_dir / f"{org}-repo.patch"

    console.print(f"[bold cyan]Creating binary-compatible partial patch:[/bold cyan] {patch_path}")

    baseline_dir = previous_repo_dir

    # Compute paths relative to output_dir so git diff headers are predictable:
    # e.g. a/<org>-repo-previous/path and b/<org>-repo/path
    rel_baseline = os.path.relpath(baseline_dir, output_dir)
    rel_current = os.path.relpath(current_repo_dir, output_dir)

    cmd = [
        "git",
        "diff",
        "--binary",
        "--no-index",
        rel_baseline,
        rel_current,
    ]

    # Use temporary or direct file writing
    with open(patch_path, "wb") as f_out:
        proc = subprocess.run(
            cmd,
            stdout=f_out,
            stderr=subprocess.PIPE,
            cwd=output_dir,
            check=False,
        )

    # In git diff:
    # returncode 0 = no diff
    # returncode 1 = diff generated successfully
    # returncode >= 2 = git error
    if proc.returncode >= 2:
        err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git diff failed with exit code {proc.returncode}: {err_msg}")

    patch_size = patch_path.stat().st_size

    # Maintain canonical <org>-repo.patch
    if not custom_output_file and patch_path != canonical_path:
        try:
            if canonical_path.exists() or canonical_path.is_symlink():
                canonical_path.unlink()
            try:
                canonical_path.symlink_to(patch_path.name)
            except OSError:
                shutil.copy2(patch_path, canonical_path)
        except OSError:
            pass

    apply_command = f"git apply -p2 --unsafe-paths --directory=<destination_dir> {patch_path}"

    return {
        "mode": "partial",
        "patch_path": patch_path,
        "canonical_path": canonical_path if not custom_output_file else None,
        "patch_bytes": patch_size,
        "baseline_existed": True,
        "apply_command": apply_command,
    }
