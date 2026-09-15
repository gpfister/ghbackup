# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Restore module for restoring a repository from full backups and intermediate patches."""

import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from rich.console import Console

console = Console()


class RestoreError(Exception):
    """Custom exception raised when restore fails."""
    pass


def parse_backup_timestamp(filename_or_str: str) -> Optional[datetime]:
    """Extract and parse timestamp from a backup filename.

    Expected formats in filename:
        - YYYYMMDD_HHMMSS (standard ghbackup format)
        - YYYYMMDD-HHMMSS
        - YYYY-MM-DD_HH-MM-SS
        - YYYYMMDDHHMMSS
    """
    patterns = [
        (r"(\d{8}_\d{6})", "%Y%m%d_%H%M%S"),
        (r"(\d{8}-\d{6})", "%Y%m%d-%H%M%S"),
        (r"(\d{4}-\d{2}-\d{2}[_T]\d{2}-\d{2}-\d{2})", "%Y-%m-%d_%H-%M-%S"),
        (r"(\d{4}-\d{2}-\d{2}[_T]\d{2}:\d{2}:\d{2})", "%Y-%m-%d_%H:%M:%S"),
        (r"(\d{14})", "%Y%m%d%H%M%S"),
    ]
    for pattern, dt_fmt in patterns:
        match = re.search(pattern, filename_or_str)
        if match:
            raw_ts = match.group(1).replace("T", "_")
            try:
                return datetime.strptime(raw_ts, dt_fmt)
            except ValueError:
                continue
    return None


def parse_target_date(date_str: str) -> datetime:
    """Parse a user-specified date/time string.

    Supported formats:
        - 'YYYY-MM-DD HH:MM:SS'
        - 'YYYY-MM-DDTHH:MM:SS'
        - 'YYYY-MM-DD HH:MM'
        - 'YYYY-MM-DDTHH:MM'
        - 'YYYYMMDD_HHMMSS'
        - 'YYYYMMDDHHMMSS'
        - 'YYYY-MM-DD' (interpreted as 23:59:59 on that date)
        - 'YYYYMMDD' (interpreted as 23:59:59 on that date)

    Raises:
        RestoreError: If the date cannot be parsed.
    """
    date_str = date_str.strip()
    datetime_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y%m%d_%H%M%S",
        "%Y%m%d%H%M%S",
    ]
    for fmt in datetime_formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    date_only_formats = [
        "%Y-%m-%d",
        "%Y%m%d",
    ]
    for fmt in date_only_formats:
        try:
            d = datetime.strptime(date_str, fmt).date()
            return datetime.combine(d, time(23, 59, 59))
        except ValueError:
            continue

    raise RestoreError(
        f"Invalid date format: '{date_str}'.\n"
        f"Supported formats include: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', 'YYYYMMDD_HHMMSS'."
    )


def find_backups(source_dir: Path, org: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Scan source_dir for full and partial backups belonging to org.

    Returns:
        Tuple[List[Dict], List[Dict]]: (full_backups, patch_backups)
        Each sorted chronologically in ascending order.
    """
    if not source_dir.exists():
        raise RestoreError(f"Source directory does not exist: '{source_dir}'")
    if not source_dir.is_dir():
        raise RestoreError(f"Source path is not a directory: '{source_dir}'")

    full_by_target: Dict[Path, Dict[str, Any]] = {}
    patch_by_target: Dict[Path, Dict[str, Any]] = {}

    for item in source_dir.iterdir():
        if not item.is_file() and not item.is_symlink():
            continue

        filename = item.name
        # Match files for org: e.g. <org>-repo-*.zip, <org>-repo.zip, <org>-repo-*.patch, <org>-repo.patch
        is_zip = filename.endswith(".zip")
        is_patch = filename.endswith(".patch")

        if not is_zip and not is_patch:
            continue

        # Check if file belongs to this org
        # e.g. starts with <org>-repo or <org>- or is <org>.zip
        if not (filename.startswith(f"{org}-repo") or filename.startswith(f"{org}-") or filename.startswith(f"{org}.")):
            continue

        try:
            resolved = item.resolve()
        except OSError:
            continue

        has_explicit_ts = parse_backup_timestamp(filename) is not None
        ts = parse_backup_timestamp(filename)
        is_canonical = (not has_explicit_ts) or item.is_symlink()

        if ts is None:
            # Check target if symlink
            if item.is_symlink():
                ts = parse_backup_timestamp(resolved.name)
            if ts is None:
                # Fallback to mtime
                try:
                    ts = datetime.fromtimestamp(item.stat().st_mtime)
                except OSError:
                    continue

        record = {
            "path": item,
            "resolved_path": resolved,
            "filename": filename,
            "timestamp": ts,
            "is_canonical": is_canonical,
            "type": "full" if is_zip else "patch",
        }

        bucket = full_by_target if is_zip else patch_by_target
        if resolved not in bucket:
            bucket[resolved] = record
        else:
            # Prefer non-canonical (timestamped) file over canonical name/symlink
            if not is_canonical and bucket[resolved]["is_canonical"]:
                bucket[resolved] = record

    full_backups = list(full_by_target.values())
    patch_backups = list(patch_by_target.values())

    # Sort ascending by timestamp
    full_backups.sort(key=lambda x: (x["timestamp"], x["filename"]))
    patch_backups.sort(key=lambda x: (x["timestamp"], x["filename"]))

    return full_backups, patch_backups


def select_backup_chain(
    full_backups: List[Dict[str, Any]],
    patch_backups: List[Dict[str, Any]],
    target_date: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Select the nearest full backup and intermediate patches to reach target_date.

    Args:
        full_backups: List of full backup metadata dicts, sorted ascending by timestamp.
        patch_backups: List of patch backup metadata dicts, sorted ascending by timestamp.
        target_date: Optional target date/time cutoff. If None, restores to latest available state.

    Returns:
        Tuple[Dict, List[Dict]]: (selected_full_backup, selected_intermediate_patches)

    Raises:
        RestoreError: If no suitable full backup is found.
    """
    if not full_backups:
        raise RestoreError("No full backup (.zip archive) found in the source directory.")

    if target_date is not None:
        eligible_full = [b for b in full_backups if b["timestamp"] <= target_date]
        if not eligible_full:
            earliest = full_backups[0]["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            requested = target_date.strftime("%Y-%m-%d %H:%M:%S")
            raise RestoreError(
                f"No full backup found on or before {requested}.\n"
                f"The earliest available full backup is from {earliest}."
            )
        # The nearest full backup is the latest eligible one (closest to target_date)
        selected_full = eligible_full[-1]

        # Intermediate patches: occurring strictly after selected_full and on or before target_date
        selected_patches = [
            p for p in patch_backups
            if selected_full["timestamp"] < p["timestamp"] <= target_date
        ]
    else:
        # Latest available state: use latest full backup
        selected_full = full_backups[-1]
        # Intermediate patches: occurring strictly after selected_full
        selected_patches = [
            p for p in patch_backups
            if selected_full["timestamp"] < p["timestamp"]
        ]

    return selected_full, selected_patches


def extract_repo_from_zip(
    zip_path: Path,
    repo_name: str,
    target_dir: Path,
    org: str,
) -> Dict[str, Any]:
    """Extract only the specified repo from a full backup zip archive into target_dir.

    Args:
        zip_path: Path to .zip archive.
        repo_name: Name of the repository to extract.
        target_dir: Target directory where the repository files should be placed.
        org: Organization name.

    Returns:
        Dict[str, Any] with extraction summary.

    Raises:
        RestoreError: If zip file is corrupted or repository is not found inside.
    """
    if not zip_path.exists():
        raise RestoreError(f"Backup archive does not exist: '{zip_path}'")

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as e:
        raise RestoreError(f"Corrupt backup archive '{zip_path.name}': {e}")

    with zf:
        all_names = zf.namelist()

        # Identify the prefix used in the archive for this repository
        candidate_prefixes = [
            f"{org}-repo/{repo_name}/",
            f"{org}-repo/{repo_name}.git/",
            f"{repo_name}/",
            f"{repo_name}.git/",
        ]

        # Also support stripped repo name if repo_name has .git suffix
        bare_repo_name = repo_name.removesuffix(".git")
        if bare_repo_name != repo_name:
            candidate_prefixes.extend([
                f"{org}-repo/{bare_repo_name}/",
                f"{org}-repo/{bare_repo_name}.git/",
                f"{bare_repo_name}/",
                f"{bare_repo_name}.git/",
            ])

        matched_prefix = None
        for prefix in candidate_prefixes:
            if any(name.startswith(prefix) for name in all_names):
                matched_prefix = prefix
                break

        if matched_prefix is None:
            # Discover available repos in archive to give helpful error message
            available_repos = set()
            for name in all_names:
                parts = name.strip("/").split("/")
                if len(parts) >= 2 and parts[0].endswith("-repo"):
                    available_repos.add(parts[1])
                elif len(parts) >= 1:
                    available_repos.add(parts[0])

            repos_hint = ", ".join(sorted(available_repos)) if available_repos else "None found"
            raise RestoreError(
                f"Repository '{repo_name}' was not found in backup archive '{zip_path.name}'.\n"
                f"Available repositories in archive: {repos_hint}"
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_target = target_dir.resolve()

        files_extracted = 0
        total_uncompressed_bytes = 0

        for member in zf.infolist():
            if not member.filename.startswith(matched_prefix):
                continue

            rel_path_str = member.filename[len(matched_prefix):]
            if not rel_path_str:
                # Root folder of the repo
                continue

            dest_path = target_dir / rel_path_str
            # Guard against Zip Slip path traversal
            resolved_dest = dest_path.resolve()
            try:
                resolved_dest.relative_to(resolved_target)
            except ValueError:
                raise RestoreError(f"Security error: Archive contains invalid path: '{member.filename}'")

            if member.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Check if entry is a symlink (Unix attribute 0o120000)
            is_symlink = ((member.external_attr >> 16) & 0o120000) == 0o120000
            if is_symlink:
                link_target = zf.read(member).decode("utf-8")
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                os.symlink(link_target, dest_path)
            else:
                with zf.open(member) as src_f, open(dest_path, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                # Restore executable permissions if preserved
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    try:
                        dest_path.chmod(mode)
                    except OSError:
                        pass

            files_extracted += 1
            total_uncompressed_bytes += member.file_size

    return {
        "repo_name": repo_name,
        "matched_prefix": matched_prefix,
        "files_extracted": files_extracted,
        "uncompressed_bytes": total_uncompressed_bytes,
    }


def filter_patch_for_repo(patch_content: bytes, repo_name: str) -> bytes:
    """Extract only diff chunks corresponding to repo_name from a patch file.

    In ghbackup patches, chunks start with 'diff --git '.
    The diff headers reference '<baseline>/<repo_name>/...' and '<current>/<repo_name>/...'.
    """
    pattern = re.compile(rb"(?=^diff --git )", re.MULTILINE)
    chunks = pattern.split(patch_content)
    filtered: List[bytes] = []

    bare_name = repo_name.removesuffix(".git")
    repo_pat = re.compile(
        rf"/(?:{re.escape(repo_name)}|{re.escape(bare_name)})(?:\.git)?/".encode("utf-8")
    )

    for chunk in chunks:
        if not chunk.strip():
            continue
        first_line = chunk.split(b"\n", 1)[0]
        if repo_pat.search(first_line):
            filtered.append(chunk)

    return b"".join(filtered)


def apply_patch_to_repo(
    patch_bytes: bytes,
    target_dir: Path,
    patch_name: str = "patch",
) -> bool:
    """Apply a filtered patch to the repository in target_dir using git apply.

    Args:
        patch_bytes: Bytes of the filtered patch.
        target_dir: Directory where repository is restored.
        patch_name: Name of the patch file for logging.

    Returns:
        bool: True if patch had changes and was applied, False if empty/no-op.

    Raises:
        RestoreError: If git apply fails.
    """
    if not patch_bytes.strip():
        return False

    # Stripping 3 leading components (-p3):
    # 'a/<org>-repo-previous/<repo_name>/<file>' -> '<file>'
    # 'b/<org>-repo/<repo_name>/<file>' -> '<file>'
    cmd = [
        "git",
        "apply",
        "-p3",
        "--unsafe-paths",
        f"--directory={target_dir}",
        "-",
    ]

    proc = subprocess.run(
        cmd,
        input=patch_bytes,
        capture_output=True,
        check=False,
    )

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        raise RestoreError(
            f"Failed to apply patch '{patch_name}': {err or out or f'exit code {proc.returncode}'}"
        )

    return True


def restore_repository(
    source_dir: Union[Path, str],
    target_dir: Union[Path, str],
    repository: str,
    target_date: Optional[Union[datetime, str]] = None,
    force: bool = False,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """Restore a given repository (<org>/<repo>) from the nearest full backup + intermediate patches.

    Args:
        source_dir: Source folder containing backup files.
        target_dir: Target folder to restore the repository to.
        repository: Repository identifier formatted as '<org>/<repo>'.
        target_date: Optional cutoff date/time (string or datetime).
        force: If True, overwrite target_dir if it already exists and is not empty.
        progress_callback: Optional callback(stage, message) for status updates.

    Returns:
        Dict[str, Any] with full details of the restore operation.

    Raises:
        RestoreError: If repository identifier is invalid, backups are missing,
                     target directory cannot be overwritten, or restore fails.
    """
    def notify(stage: str, message: str) -> None:
        if progress_callback:
            progress_callback(stage, message)

    # 1. Parse repository identifier
    if "/" not in repository:
        raise RestoreError(
            f"Invalid repository identifier: '{repository}'.\n"
            f"Expected format: <org>/<repo> (for example: 'gpfister/ghbackup')."
        )

    parts = repository.strip().split("/", 1)
    org, repo = parts[0].strip(), parts[1].strip()
    if not org or not repo:
        raise RestoreError(
            f"Invalid repository identifier: '{repository}'. Both organization and repository name must be non-empty."
        )

    source_path = Path(source_dir).resolve()
    target_base = Path(target_dir).resolve()
    target_path = target_base / org / repo

    # 2. Parse target date if provided as string
    parsed_date: Optional[datetime] = None
    if isinstance(target_date, str):
        parsed_date = parse_target_date(target_date)
    elif isinstance(target_date, datetime):
        parsed_date = target_date

    # 3. Check target directory
    if target_path.exists() and any(target_path.iterdir()):
        if not force:
            raise RestoreError(
                f"Target directory '{target_path}' already exists and is not empty.\n"
                f"Use --force (-f) to overwrite existing contents."
            )
        notify("clean", f"Cleaning existing destination directory '{target_path}'")
        shutil.rmtree(target_path)

    # 4. Discover backups
    notify("scan", f"Scanning for backups of '{org}' in '{source_path}'")
    full_backups, patch_backups = find_backups(source_path, org)

    # 5. Select nearest full backup and intermediate patches
    selected_full, intermediate_patches = select_backup_chain(
        full_backups=full_backups,
        patch_backups=patch_backups,
        target_date=parsed_date,
    )

    notify(
        "select",
        f"Selected base full backup: {selected_full['filename']} "
        f"({selected_full['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}) "
        f"with {len(intermediate_patches)} intermediate patch(es)"
    )

    # 6. Extract repository from full backup
    notify("extract", f"Extracting '{repo}' from {selected_full['filename']} into '{target_path}'")
    extract_info = extract_repo_from_zip(
        zip_path=selected_full["path"],
        repo_name=repo,
        target_dir=target_path,
        org=org,
    )

    # 7. Apply intermediate patches in chronological order
    applied_patches: List[Dict[str, Any]] = []
    skipped_patches: List[Dict[str, Any]] = []

    for patch in intermediate_patches:
        p_path: Path = patch["path"]
        notify("patch", f"Processing patch: {patch['filename']} ({patch['timestamp'].strftime('%Y-%m-%d %H:%M:%S')})")

        with open(p_path, "rb") as f:
            raw_content = f.read()

        filtered = filter_patch_for_repo(raw_content, repo)
        if not filtered.strip():
            skipped_patches.append({
                "filename": patch["filename"],
                "timestamp": patch["timestamp"],
                "reason": "No changes for this repository",
            })
            notify("patch_skip", f"No changes for '{repo}' in {patch['filename']}")
            continue

        apply_patch_to_repo(
            patch_bytes=filtered,
            target_dir=target_path,
            patch_name=patch["filename"],
        )
        applied_patches.append({
            "filename": patch["filename"],
            "timestamp": patch["timestamp"],
            "patch_size": len(filtered),
        })
        notify("patch_apply", f"Applied patch {patch['filename']} successfully")

    return {
        "org": org,
        "repo": repo,
        "source_dir": source_path,
        "target_base": target_base,
        "target_dir": target_path,
        "target_date": parsed_date,
        "base_backup": selected_full,
        "intermediate_patches_total": len(intermediate_patches),
        "applied_patches": applied_patches,
        "skipped_patches": skipped_patches,
        "files_extracted": extract_info["files_extracted"],
        "uncompressed_bytes": extract_info["uncompressed_bytes"],
    }
