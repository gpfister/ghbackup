# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Git cloning and directory rotation management."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from rich.console import Console

console = Console()


def rotate_backup_directories(output_dir: Path, org: str) -> Tuple[Path, Optional[Path]]:
    """Rotate repository backup directories according to specification:

    1. Main clone folder: `<org>-repo`
    2. Previous folder: `<org>-repo-previous`
    3. If `<org>-repo` already exists:
       - If `<org>-repo-previous` also exists, remove it.
       - Rename `<org>-repo` to `<org>-repo-previous`.
    4. Create fresh `<org>-repo` folder.

    Returns:
        Tuple[Path, Optional[Path]]: (current_repo_dir, previous_repo_dir if it exists)
    """
    repo_dir = output_dir / f"{org}-repo"
    prev_dir = output_dir / f"{org}-repo-previous"

    if repo_dir.exists():
        if prev_dir.exists():
            console.print(
                f"[yellow]Found existing previous directory. Removing:[/yellow] [bold]{prev_dir}[/bold]"
            )
            shutil.rmtree(prev_dir)

        console.print(
            f"[cyan]Renaming current directory to previous:[/cyan] [bold]{repo_dir}[/bold] -> [bold]{prev_dir}[/bold]"
        )
        shutil.move(str(repo_dir), str(prev_dir))

    # Ensure fresh repo_dir exists
    repo_dir.mkdir(parents=True, exist_ok=True)

    actual_prev_dir = prev_dir if prev_dir.exists() else None
    return repo_dir, actual_prev_dir


def clone_repository(
    repo_info: Dict[str, Any],
    target_dir: Path,
    token: Optional[str] = None,
    use_ssh: bool = False,
    ssh_key: Optional[Union[Path, str]] = None,
    mirror: bool = False,
    timeout: int = 300,
) -> Tuple[bool, str]:
    """Clone a single repository.

    Args:
        repo_info: Repository metadata dictionary.
        target_dir: Destination path for the repository.
        token: Optional GitHub Personal Access Token for HTTPS auth.
        use_ssh: Whether to use SSH clone URL.
        ssh_key: Optional path to SSH private key to use for authentication.
        mirror: Whether to clone as a bare mirror.
        timeout: Maximum seconds to wait for git clone.

    Returns:
        Tuple[bool, str]: (Success status, stdout/stderr message).
    """
    if ssh_key:
        use_ssh = True

    repo_name = repo_info["name"]
    dest_path = target_dir / (f"{repo_name}.git" if mirror else repo_name)

    if dest_path.exists():
        # Target already exists (e.g. re-clone attempt)
        shutil.rmtree(dest_path)

    clone_url = (repo_info.get("ssh_url") or repo_info.get("clone_url")) if use_ssh else repo_info["clone_url"]

    cmd: List[str] = ["git"]

    # If token is provided and using HTTPS, pass authorization header securely
    if token and not use_ssh:
        cmd.extend(["-c", f"http.extraheader=AUTHORIZATION: bearer {token}"])

    ssh_key_resolved = None
    if ssh_key:
        ssh_key_resolved = str(Path(os.path.expanduser(str(ssh_key))).resolve())
        ssh_cmd = f"ssh -i {shlex.quote(ssh_key_resolved)} -o IdentitiesOnly=yes"
        cmd.extend(["-c", f"core.sshCommand={ssh_cmd}"])

    cmd.append("clone")
    if mirror:
        cmd.append("--mirror")
    else:
        cmd.extend(["--no-single-branch", "--tags"])

    cmd.extend([clone_url, str(dest_path)])

    env = os.environ.copy()
    # Prevent git from hanging waiting for interactive username/password prompts
    env["GIT_TERMINAL_PROMPT"] = "0"
    if ssh_key_resolved:
        ssh_cmd = f"ssh -i {shlex.quote(ssh_key_resolved)} -o IdentitiesOnly=yes"
        env["GIT_SSH_COMMAND"] = ssh_cmd

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
        if proc.returncode == 0:
            if not mirror and dest_path.exists():
                setup_local_branches(dest_path, env=env)
            return True, proc.stderr.strip()
        else:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            return False, f"Git clone exited with code {proc.returncode}: {err_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Git clone timed out after {timeout} seconds"
    except Exception as e:
        return False, f"Git clone failed: {e}"


def setup_local_branches(repo_dir: Path, env: Optional[Dict[str, str]] = None) -> None:
    """Create local tracking branches for all remote branches.

    When git clone is executed without --mirror, only the default branch
    has a local tracking branch created. This function inspects all remote
    branches and sets up local tracking branches for every branch that
    does not already exist locally, ensuring all branches and their
    complete history are available locally.
    """
    if not (repo_dir / ".git").exists():
        return

    try:
        # Get existing local branches
        res_local = subprocess.run(
            ["git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname)", "refs/heads/"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if res_local.returncode != 0:
            return

        local_heads = {
            line.strip()[len("refs/heads/"):]
            for line in res_local.stdout.splitlines()
            if line.strip().startswith("refs/heads/")
        }

        # Get remote branches from all remotes (typically origin)
        res_remotes = subprocess.run(
            ["git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname) %(symref)", "refs/remotes/"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if res_remotes.returncode != 0:
            return

        for line in res_remotes.stdout.splitlines():
            parts = line.strip().split()
            if not parts or len(parts) > 1:
                # Skip empty lines or symrefs (e.g. refs/remotes/origin/HEAD)
                continue

            refname = parts[0]
            if not refname.startswith("refs/remotes/"):
                continue

            rel = refname[len("refs/remotes/"):]
            remote_name, _, branch_name = rel.partition("/")
            if not remote_name or not branch_name:
                continue

            if branch_name == "HEAD" or branch_name.endswith("/HEAD"):
                continue

            if branch_name not in local_heads:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "branch", "--track", branch_name, f"{remote_name}/{branch_name}"],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
    except Exception as e:
        console.print(f"[yellow]Warning: Could not setup local tracking branches for {repo_dir.name}: {e}[/yellow]")


def clone_all_repositories(
    repos: List[Dict[str, Any]],
    target_dir: Path,
    token: Optional[str] = None,
    use_ssh: bool = False,
    ssh_key: Optional[Union[Path, str]] = None,
    mirror: bool = False,
    progress_callback: Optional[Callable[[int, int, str, bool], None]] = None,
) -> Dict[str, Any]:
    """Clone all repositories in the given list into target_dir.

    Returns:
        Dict[str, Any] with summary: total, successful, failed, failed_repos list.
    """
    total = len(repos)
    successful = 0
    failed = 0
    failed_repos: List[Dict[str, str]] = []

    for idx, repo in enumerate(repos, start=1):
        repo_name = repo["name"]
        success, message = clone_repository(
            repo_info=repo,
            target_dir=target_dir,
            token=token,
            use_ssh=use_ssh,
            ssh_key=ssh_key,
            mirror=mirror,
        )

        if success:
            successful += 1
        else:
            failed += 1
            failed_repos.append({"name": repo_name, "error": message})

        if progress_callback:
            progress_callback(idx, total, repo_name, success)

    return {
        "total": total,
        "successful": successful,
        "failed": failed,
        "failed_repos": failed_repos,
    }
