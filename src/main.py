# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Main entry point for ghbackup CLI tool."""

import os
import sys
from pathlib import Path
from typing import Optional
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src import __version__
from src.backup import create_full_backup, create_partial_backup
from src.clone import clone_all_repositories, rotate_backup_directories
from src.github import (
    GitHubAPIError,
    fetch_org_or_user_repos,
    resolve_github_token,
)
from src.restore import RestoreError, restore_repository

console = Console()
error_console = Console(stderr=True)


def format_bytes(size: int) -> str:
    """Format bytes to human-readable string."""
    power = 2**10
    n = 0
    power_labels = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
    size_float = float(size)
    while size_float > power and n < 4:
        size_float /= power
        n += 1
    return f"{size_float:.2f} {power_labels[n]}"


@click.group(
    name="ghbackup",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="GitHub Backup & Restore CLI: backup organizations and restore individual repositories.",
)
@click.version_option(version=__version__, prog_name="ghbackup")
def app() -> None:
    """GitHub backup and restore management tool."""
    pass


@app.command(
    name="backup",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Backup an entire GitHub organization or user repositories.",
)
@click.argument("org", required=True, metavar="<ORG_OR_USER>")
@click.option(
    "--full",
    "-f",
    "full_mode",
    is_flag=True,
    default=False,
    help="Perform a full backup: clones repositories and generates a .zip archive.",
)
@click.option(
    "--partial",
    "-p",
    "partial_mode",
    is_flag=True,
    default=False,
    help="Perform a partial backup: clones repositories and generates a binary-compatible .patch.",
)
@click.option(
    "--token",
    "-t",
    "token",
    default=None,
    envvar=["GITHUB_TOKEN", "GH_TOKEN"],
    help="GitHub Personal Access Token. Can also be set via GITHUB_TOKEN or GH_TOKEN.",
)
@click.option(
    "--output-dir",
    "-d",
    "output_dir_str",
    default=".",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Directory where <org>-repo and backup artifacts are placed. Defaults to current directory.",
)
@click.option(
    "--output-file",
    "-o",
    "output_file_str",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Custom path for the resulting .zip or .patch file.",
)
@click.option(
    "--ssh",
    is_flag=True,
    default=False,
    help="Use SSH clone URLs (git@github.com:...) instead of HTTPS.",
)
@click.option(
    "--ssh-key",
    "-k",
    "-i",
    "ssh_key_str",
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    envvar=["GH_SSH_KEY", "GITHUB_SSH_KEY"],
    help="Path to the SSH private key to use for git authentication.",
)
@click.option(
    "--include-forks/--no-forks",
    default=True,
    help="Include or exclude forked repositories (default: include).",
)
@click.option(
    "--include-archived/--no-archived",
    default=True,
    help="Include or exclude archived repositories (default: include).",
)
@click.option(
    "--mirror",
    is_flag=True,
    default=False,
    help="Clone repositories as bare mirrors (--mirror).",
)
@click.option(
    "--repo",
    "-r",
    "selected_repos",
    multiple=True,
    help="Filter specific repository name(s) to backup. If omitted, all repositories are backed up.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Fetch and list repositories without cloning or creating backups.",
)
def backup_cmd(
    org: str,
    full_mode: bool,
    partial_mode: bool,
    token: Optional[str],
    output_dir_str: str,
    output_file_str: Optional[str],
    ssh: bool,
    ssh_key_str: Optional[str],
    include_forks: bool,
    include_archived: bool,
    mirror: bool,
    selected_repos: tuple,
    dry_run: bool,
) -> None:
    """Backup an entire GitHub organization or user repositories."""
    # Validate backup mode
    if not full_mode and not partial_mode and not dry_run:
        error_console.print(
            "[bold red]Error:[/bold red] You must specify either [bold cyan]--full[/bold cyan] (-f) "
            "or [bold cyan]--partial[/bold cyan] (-p) backup mode."
        )
        error_console.print("Run [bold]ghbackup backup --help[/bold] for usage information.")
        sys.exit(1)

    if full_mode and partial_mode:
        error_console.print(
            "[bold red]Error:[/bold red] Cannot specify both [bold cyan]--full[/bold cyan] "
            "and [bold cyan]--partial[/bold cyan]. Please choose one mode."
        )
        sys.exit(1)

    # Validate SSH key if provided
    ssh_key_path: Optional[Path] = None
    if ssh_key_str:
        candidate_key = Path(os.path.expanduser(ssh_key_str)).resolve()
        if not candidate_key.exists():
            error_console.print(
                f"[bold red]Error:[/bold red] SSH key file does not exist: '[bold cyan]{candidate_key}[/bold cyan]'"
            )
            sys.exit(1)
        if not candidate_key.is_file():
            error_console.print(
                f"[bold red]Error:[/bold red] SSH key path is not a file: '[bold cyan]{candidate_key}[/bold cyan]'"
            )
            sys.exit(1)
        ssh_key_path = candidate_key

    use_ssh = ssh or (ssh_key_path is not None)

    output_dir = Path(output_dir_str).resolve()
    existing_repo_dir = output_dir / f"{org}-repo"

    if partial_mode:
        if not existing_repo_dir.exists():
            error_console.print(
                f"[bold red]Error:[/bold red] Cannot perform partial backup: "
                f"repository directory '[bold cyan]{existing_repo_dir}[/bold cyan]' does not exist.\n"
                f"A full backup (--full) must be performed first."
            )
            sys.exit(1)
        if not existing_repo_dir.is_dir():
            error_console.print(
                f"[bold red]Error:[/bold red] Cannot perform partial backup: "
                f"'[bold cyan]{existing_repo_dir}[/bold cyan]' exists but is not a directory."
            )
            sys.exit(1)

    # Resolve token
    resolved_token = resolve_github_token(token)

    output_dir.mkdir(parents=True, exist_ok=True)
    custom_output_file = Path(output_file_str).resolve() if output_file_str else None

    mode_label = "DRY RUN" if dry_run else ("FULL (.zip)" if full_mode else "PARTIAL (.patch)")
    protocol_label = "SSH" if use_ssh else "HTTPS"

    config_lines = [
        f"[bold green]GitHub Backup CLI[/bold green] (ghbackup v{__version__})",
        f"[bold]Target:[/bold] [cyan]{org}[/cyan]",
        f"[bold]Mode:[/bold] [magenta]{mode_label}[/magenta]",
        f"[bold]Destination:[/bold] [blue]{output_dir}[/blue]",
        f"[bold]Protocol:[/bold] [yellow]{protocol_label}[/yellow]",
        f"[bold]Auth:[/bold] {'[green]Token present[/green]' if resolved_token else '[yellow]No token (public repos only)[/yellow]'}",
    ]
    if ssh_key_path:
        config_lines.append(f"[bold]SSH Key:[/bold] [cyan]{ssh_key_path}[/cyan]")

    console.print(
        Panel.fit(
            "\n".join(config_lines),
            title="[bold]Configuration[/bold]",
            border_style="green",
        )
    )

    # Step 1: Discover repositories
    console.print(f"\n[bold]Fetching repositories for '[cyan]{org}[/cyan]'...[/bold]")
    try:
        target_type, repos = fetch_org_or_user_repos(
            target=org,
            token=resolved_token,
            include_forks=include_forks,
            include_archived=include_archived,
        )
    except GitHubAPIError as e:
        error_console.print(f"[bold red]API Error:[/bold red] {e}")
        sys.exit(2)
    except Exception as e:
        error_console.print(f"[bold red]Unexpected Error:[/bold red] {e}")
        sys.exit(2)

    console.print(
        f"Discovered [bold green]{len(repos)}[/bold green] repository(ies) "
        f"for {target_type} '[cyan]{org}[/cyan]'."
    )

    if selected_repos:
        selected_set = set(selected_repos)
        repos = [r for r in repos if r["name"] in selected_set]
        console.print(f"Filtered to [bold green]{len(repos)}[/bold green] selected repository(ies): {', '.join(selected_repos)}")

    if not repos:
        console.print("[yellow]No repositories found matching the given filters. Exiting.[/yellow]")
        sys.exit(0)

    # Display repos table
    table = Table(title=f"Repositories to Backup ({len(repos)})", show_lines=False)
    table.add_column("No.", style="dim", width=4)
    table.add_column("Repository Name", style="bold")
    table.add_column("Default Branch", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Size", justify="right", style="green")

    for i, r in enumerate(repos, start=1):
        r_type = []
        if r["is_private"]:
            r_type.append("private")
        if r["is_fork"]:
            r_type.append("fork")
        if r["is_archived"]:
            r_type.append("archived")
        if not r_type:
            r_type.append("public")

        table.add_row(
            str(i),
            r["name"],
            r["default_branch"],
            ", ".join(r_type),
            format_bytes(r["size_kb"] * 1024),
        )

    console.print(table)

    if dry_run:
        console.print("\n[bold yellow]Dry-run requested. No files were cloned or modified.[/bold yellow]")
        sys.exit(0)

    # Step 2: Directory rotation
    console.print("\n[bold]Preparing backup directories...[/bold]")
    current_repo_dir, previous_repo_dir = rotate_backup_directories(output_dir, org)
    console.print(f"Target clone directory: [bold green]{current_repo_dir}[/bold green]")
    if previous_repo_dir:
        console.print(f"Previous directory preserved at: [bold yellow]{previous_repo_dir}[/bold yellow]")

    # Step 3: Clone repositories
    console.print(f"\n[bold]Cloning {len(repos)} repositories...[/bold]")

    def progress_callback(current: int, total: int, repo_name: str, success: bool) -> None:
        status = "[green]✓ OK[/green]" if success else "[red]✗ FAILED[/red]"
        console.print(f"[{current}/{total}] {status} {repo_name}")

    clone_summary = clone_all_repositories(
        repos=repos,
        target_dir=current_repo_dir,
        token=resolved_token,
        use_ssh=use_ssh,
        ssh_key=ssh_key_path,
        mirror=mirror,
        progress_callback=progress_callback,
    )

    console.print(
        f"\n[bold]Clone complete:[/bold] [green]{clone_summary['successful']} successful[/green], "
        f"[red]{clone_summary['failed']} failed[/red] out of {clone_summary['total']} total."
    )

    if clone_summary["failed"] > 0:
        console.print("[yellow]Warnings occurred during cloning:[/yellow]")
        for f in clone_summary["failed_repos"]:
            console.print(f"  - [bold]{f['name']}:[/bold] {f['error']}")

    # Step 4: Full or Partial Backup Artifact Creation
    if full_mode:
        console.print("\n[bold]Generating full backup (.zip)...[/bold]")
        backup_info = create_full_backup(
            repo_dir=current_repo_dir,
            output_dir=output_dir,
            org=org,
            custom_output_file=custom_output_file,
        )
        console.print(
            Panel.fit(
                f"[bold green]Full Backup Created Successfully![/bold green]\n"
                f"[bold]Archive File:[/bold] {backup_info['archive_path']}\n"
                + (f"[bold]Canonical Link:[/bold] {backup_info['canonical_path']}\n" if backup_info.get('canonical_path') else "")
                + f"[bold]Files Archived:[/bold] {backup_info['total_files']}\n"
                f"[bold]Uncompressed Size:[/bold] {format_bytes(backup_info['uncompressed_bytes'])}\n"
                f"[bold]Archive Size:[/bold] {format_bytes(backup_info['compressed_bytes'])}",
                title="[bold]Backup Result[/bold]",
                border_style="green",
            )
        )
    elif partial_mode:
        console.print("\n[bold]Generating partial backup (.patch)...[/bold]")
        patch_info = create_partial_backup(
            current_repo_dir=current_repo_dir,
            previous_repo_dir=previous_repo_dir,
            output_dir=output_dir,
            org=org,
            custom_output_file=custom_output_file,
        )
        console.print(
            Panel.fit(
                f"[bold green]Partial Backup Created Successfully![/bold green]\n"
                f"[bold]Patch File:[/bold] {patch_info['patch_path']}\n"
                + (f"[bold]Canonical Link:[/bold] {patch_info['canonical_path']}\n" if patch_info.get('canonical_path') else "")
                + f"[bold]Patch Size:[/bold] {format_bytes(patch_info['patch_bytes'])}\n"
                f"[bold]Baseline:[/bold] {previous_repo_dir.name if previous_repo_dir else 'None'}\n\n"
                f"[bold cyan]To apply this patch to a previous backup directory:[/bold cyan]\n"
                f"  git apply -p2 --unsafe-paths --directory=<destination_folder> {patch_info['patch_path']}",
                title="[bold]Backup Result[/bold]",
                border_style="green",
            )
        )


@app.command(
    name="restore",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Restore a repository (<org>/<repo>) from the nearest full backup and intermediate patches.",
)
@click.argument("repository", required=True, metavar="<ORG/REPO>")
@click.option(
    "--source",
    "-s",
    "--source-dir",
    "source_dir_str",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=str),
    help="Directory containing backup files (.zip and .patch). Defaults to current directory.",
)
@click.option(
    "--target",
    "-t",
    "--target-dir",
    "target_dir_str",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Destination directory to restore the repository to. Defaults to ./<repo>.",
)
@click.option(
    "--date",
    "-d",
    "-D",
    "date_str",
    default=None,
    type=str,
    help="Restore to the state at this date/time (e.g. 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', or 'YYYYMMDD_HHMMSS'). Defaults to latest available state.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Overwrite destination directory if it already exists and is not empty.",
)
def restore_cmd(
    repository: str,
    source_dir_str: str,
    target_dir_str: Optional[str],
    date_str: Optional[str],
    force: bool,
) -> None:
    """Restore a repository (<org>/<repo>) from backup archives and patches."""
    if "/" not in repository:
        error_console.print(
            f"[bold red]Error:[/bold red] Invalid repository identifier '[bold cyan]{repository}[/bold cyan]'.\n"
            f"Expected format: <org>/<repo> (for example: 'gpfister/ghbackup')."
        )
        sys.exit(1)

    parts = repository.strip().split("/", 1)
    org, repo = parts[0].strip(), parts[1].strip()

    source_dir = Path(source_dir_str).resolve()
    target_dir = Path(target_dir_str if target_dir_str else repo).resolve()

    date_label = date_str if date_str else "Latest available state"

    config_lines = [
        f"[bold green]GitHub Restore CLI[/bold green] (ghbackup v{__version__})",
        f"[bold]Target Repository:[/bold] [cyan]{org}/{repo}[/cyan]",
        f"[bold]Source Directory:[/bold] [blue]{source_dir}[/blue]",
        f"[bold]Target Directory:[/bold] [magenta]{target_dir}[/magenta]",
        f"[bold]Target Date/Time:[/bold] [yellow]{date_label}[/yellow]",
        f"[bold]Overwrite Force:[/bold] {'[green]Enabled[/green]' if force else '[dim]Disabled[/dim]'}",
    ]

    console.print(
        Panel.fit(
            "\n".join(config_lines),
            title="[bold]Restore Configuration[/bold]",
            border_style="cyan",
        )
    )

    def progress_callback(stage: str, message: str) -> None:
        if stage == "scan":
            console.print(f"[dim]•[/dim] {message}")
        elif stage == "select":
            console.print(f"[bold cyan]✓[/bold cyan] {message}")
        elif stage == "extract":
            console.print(f"[bold green]✓[/bold green] {message}")
        elif stage == "patch_apply":
            console.print(f"  [green]✓ Applied:[/green] {message}")
        elif stage == "patch_skip":
            console.print(f"  [dim]- Skipped (no changes): {message}[/dim]")
        elif stage == "clean":
            console.print(f"[yellow]! Cleaned destination:[/yellow] {message}")

    try:
        result = restore_repository(
            source_dir=source_dir,
            target_dir=target_dir,
            repository=repository,
            target_date=date_str,
            force=force,
            progress_callback=progress_callback,
        )
    except RestoreError as e:
        error_console.print(f"[bold red]Restore Error:[/bold red] {e}")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[bold red]Unexpected Error:[/bold red] {e}")
        sys.exit(2)

    # Success display
    applied = result["applied_patches"]
    skipped = result["skipped_patches"]
    base = result["base_backup"]

    summary_lines = [
        f"[bold green]Repository Restored Successfully![/bold green]\n",
        f"[bold]Repository:[/bold] [cyan]{result['org']}/{result['repo']}[/cyan]",
        f"[bold]Destination:[/bold] [blue]{result['target_dir']}[/blue]",
        f"[bold]Base Backup:[/bold] {base['filename']} ({base['timestamp'].strftime('%Y-%m-%d %H:%M:%S')})",
        f"[bold]Files Extracted:[/bold] {result['files_extracted']}",
        f"[bold]Uncompressed Size:[/bold] {format_bytes(result['uncompressed_bytes'])}",
        f"[bold]Intermediate Patches:[/bold] {len(applied)} applied, {len(skipped)} skipped (total {result['intermediate_patches_total']})",
    ]

    if applied:
        summary_lines.append("\n[bold]Applied Patches:[/bold]")
        for p in applied:
            summary_lines.append(f"  - {p['filename']} ({p['timestamp'].strftime('%Y-%m-%d %H:%M:%S')})")

    console.print(
        Panel.fit(
            "\n".join(summary_lines),
            title="[bold]Restore Summary[/bold]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    app()
