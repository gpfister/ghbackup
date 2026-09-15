# ghbackup

A modern, fast Python CLI tool to backup entire GitHub organizations and user accounts with full (`.zip`) and partial (`.patch`) backup modes, and restore individual repositories to any point in time.

## Overview

`ghbackup` provides commands to:
1. **`backup`**: Automatically backup all repositories in a GitHub organization or user account. It maintains previous states, rotates backup folders safely, and produces binary-compatible patches or complete archives.
2. **`restore`**: Restore any individual repository (`<org>/<repo>`) from the nearest full backup archive (`.zip`) plus any intermediate binary patches (`.patch`) up to an optional target date/time.

### Key Features

- **Backup Modes (`backup`):**
  - **Full Backup (`--full` / `-f`)**: Clones all repositories and packages them into a compressed `.zip` archive.
  - **Partial Backup (`--partial` / `-p`)**: Rotates existing repositories from `<org>-repo` to `<org>-repo-previous`, clones the latest state into `<org>-repo`, and computes a binary-compatible `.patch` using `git diff --binary --no-index`. Requires an existing `<org>-repo` directory from a prior backup.
- **Repository Restore (`restore`):**
  - **Point-in-Time Restore**: Accepts an optional `--date` / `-d` and restores the repository from the nearest full backup on or before that date, followed by sequentially applying all intermediate patches up to that date.
  - **Custom Source & Target Directories**: Select the source directory containing backup archives/patches (`--source` / `-s`) and the target destination folder (`--target` / `-t`).
  - **Selective Patching**: Accurately filters multi-repo patches to extract and apply only changes relevant to the target repository.
- **Automatic Directory Rotation:**
  - Clones into `<org>-repo`.
  - If `<org>-repo` already exists, any existing `<org>-repo-previous` is removed and `<org>-repo` is renamed to `<org>-repo-previous`.
- **Dual Support for Organizations & Users:**
  - Seamlessly checks `/orgs/{org}/repos`, falling back to `/users/{user}/repos` if the target is a user account (e.g. `gpfister`).
- **Binary Compatibility:**
  - Generates binary-compatible git patches including binary assets, added files, and deletions.
- **Authentication & Rate Limits:**
  - Automatically checks `--token`, `GITHUB_TOKEN`, `GH_TOKEN`, or local `gh auth token` from GitHub CLI.
  - Avoids interactive password hangs with non-interactive git configuration.
- **Managed via `uv` + `pyproject.toml`**.

---

## Installation & Setup

Ensure you have [uv](https://github.com/astral-sh/uv) installed (Python >= 3.10):

```bash
# Clone or navigate to the repository
cd /path/to/repository

# Install dependencies and sync virtual environment
uv sync
```

---

## Usage

You can run `ghbackup` via the provided `./ghbackup` script or via `uv run src.main:app`:

```bash
# General help
./ghbackup --help

# Subcommand help
./ghbackup backup --help
./ghbackup restore --help
```

---

### Backup Command (`ghbackup backup`)

```text
Usage: ghbackup backup [OPTIONS] <ORG_OR_USER>

  Backup an entire GitHub organization or user repositories.

Arguments:
  <ORG_OR_USER>  GitHub organization or user to backup  [required]

Options:
  -f, --full                  Perform a full backup: clones repositories and
                              generates a .zip archive.
  -p, --partial               Perform a partial backup: clones repositories
                              and generates a binary-compatible .patch.
  -t, --token TEXT            GitHub Personal Access Token. Can also be set
                              via GITHUB_TOKEN or GH_TOKEN.
  -d, --output-dir DIRECTORY  Directory where <org>-repo and backup artifacts
                              are placed. Defaults to current directory.
  -o, --output-file FILE      Custom path for the resulting .zip or .patch
                              file.
  --ssh                       Use SSH clone URLs (git@github.com:...) instead
                              of HTTPS.
  -k, -i, --ssh-key FILE      Path to the SSH private key to use for git
                              authentication.
  --include-forks / --no-forks
                              Include or exclude forked repositories (default:
                              include).
  --include-archived / --no-archived
                              Include or exclude archived repositories
                              (default: include).
  --mirror                    Clone repositories as bare mirrors (--mirror).
  -r, --repo TEXT             Filter specific repository name(s) to backup. If
                              omitted, all repositories are backed up.
  --dry-run                   Fetch and list repositories without cloning or
                              creating backups.
  -h, --help                  Show this message and exit.
```

---

### Restore Command (`ghbackup restore`)

```text
Usage: ghbackup restore [OPTIONS] <ORG/REPO>

  Restore a repository (<org>/<repo>) from the nearest full backup and
  intermediate patches.

Arguments:
  <ORG/REPO>  Target repository formatted as <org>/<repo> (e.g. gpfister/myrepo)  [required]

Options:
  -s, --source, --source-dir DIRECTORY
                              Directory containing backup files (.zip and
                              .patch). Defaults to current directory.
  -t, --target, --target-dir DIRECTORY
                              Destination base directory where <org>/<repo> will
                              be created and restored. Defaults to current
                              directory.
  -d, -D, --date TEXT         Restore to the state at this date/time (e.g.
                              'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', or
                              'YYYYMMDD_HHMMSS'). Defaults to latest
                              available state.
  -f, --force                 Overwrite destination directory if it already
                              exists and is not empty.
  -h, --help                  Show this message and exit.
```

---

## Examples

### 1. Full Backup
```bash
./ghbackup backup --full gpfister
```
Output:
- Clones all repositories into `./gpfister-repo/`
- Rotates any previous `./gpfister-repo/` to `./gpfister-repo-previous/`
- Creates `./gpfister-repo-YYYYMMDD_HHMMSS.zip` (and updates `./gpfister-repo.zip`)

### 2. Backup using a Custom SSH Key
```bash
./ghbackup backup --full --ssh-key ~/.ssh/id_ed25519 gpfister
# Or using short option aliases:
./ghbackup backup --full -i ~/.ssh/id_ed25519 gpfister
./ghbackup backup --full -k ~/.ssh/id_ed25519 gpfister
```

### 3. Partial Backup
> Note: Requires an existing `./gpfister-repo/` directory (e.g. from a prior full backup).
```bash
./ghbackup backup --partial gpfister
```
Output:
- Rotates existing `./gpfister-repo/` to `./gpfister-repo-previous/`
- Clones fresh repositories into `./gpfister-repo/`
- Creates `./gpfister-repo-YYYYMMDD_HHMMSS.patch` (and updates `./gpfister-repo.patch`)

### 4. Restore Repository to Latest State
```bash
# Restore repository gpfister/ghbackup from backups in current folder into ./gpfister/ghbackup
./ghbackup restore gpfister/ghbackup

# Specify source folder where backups are stored and a custom destination folder (restores into /tmp/restores/gpfister/ghbackup)
./ghbackup restore gpfister/ghbackup --source /mnt/backups --target /tmp/restores
```

### 5. Restore Repository to a Specific Date/Time
```bash
# Restore repository as it existed on September 15, 2026 at 14:30 into /tmp/restores/gpfister/ghbackup
./ghbackup restore gpfister/ghbackup -s /mnt/backups -t /tmp/restores -d "2026-09-15 14:30:00"

# Restore by date (finds nearest full backup on/before date + patches in between)
./ghbackup restore gpfister/ghbackup -s /mnt/backups -t /tmp/restores -d 2026-09-15
```

### 6. Overwrite Existing Destination
```bash
./ghbackup restore gpfister/ghbackup --target /tmp/restores --force
```

### 7. Dry Run (Preview Repositories to Backup)
```bash
./ghbackup backup --dry-run gpfister
```

---

## Project Structure

```
├── README.md               # Project documentation
├── LICENSE.md              # MIT License
├── pyproject.toml          # uv / PEP 621 project configuration
├── ghbackup                # Executable shell wrapper calling "uv run src.main:app"
├── ghbackup.sh             # Alternative shell wrapper script
├── src/
│   ├── __init__.py         # Package metadata
│   ├── main.py             # CLI definition with 'backup' and 'restore' commands
│   ├── github.py           # GitHub API client with org & user discovery
│   ├── clone.py            # Git cloning and directory rotation logic
│   ├── backup.py           # Full (.zip) and Partial (.patch) creation
│   └── restore.py          # Point-in-time restore from full backup + patches
└── tests/
    ├── test_backup.py      # Backup command and rotation test suite
    └── test_restore.py     # Restore command and chain resolution test suite
```

---

## Authors & Credits

Created and maintained by **Greg PFISTER** in France.

---

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

Copyright (c) 2026 Greg PFISTER, France.
