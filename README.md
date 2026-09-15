# ghbackup

A modern, fast Python CLI tool to backup entire GitHub organizations and user accounts with full (`.zip`) and partial (`.patch`) backup modes.

## Overview

`ghbackup` automates the process of backing up all repositories in a GitHub organization or user account. It maintains previous states, rotates backup folders safely, and produces binary-compatible patches or complete archives.

### Key Features

- **Backup Modes:**
  - **Full Backup (`--full` / `-f`)**: Clones all repositories and packages them into a compressed `.zip` archive.
  - **Partial Backup (`--partial` / `-p`)**: Rotates existing repositories from `<org>-repo` to `<org>-repo-previous`, clones the latest state into `<org>-repo`, and computes a binary-compatible `.patch` using `git diff --binary --no-index`. Requires an existing `<org>-repo` directory from a prior backup.
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
# Using the wrapper script
./ghbackup --full gpfister
./ghbackup --partial gpfister

# Or using uv run
uv run src.main:app --full gpfister
uv run src.main:app --partial gpfister
```

### Command Options

```text
Usage: ghbackup [OPTIONS] <ORG_OR_USER>

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
  -v, --version               Show the version and exit.
  -h, --help                  Show this message and exit.
```

---

## Examples

### 1. Full Backup
```bash
./ghbackup --full gpfister
```
Output:
- Clones all repositories into `./gpfister-repo/`
- Rotates any previous `./gpfister-repo/` to `./gpfister-repo-previous/`
- Creates `./gpfister-repo-YYYYMMDD_HHMMSS.zip` (and updates `./gpfister-repo.zip`)

### 2. Backup using a Custom SSH Key
```bash
./ghbackup --full --ssh-key ~/.ssh/id_ed25519 gpfister
# Or using short option aliases:
./ghbackup --full -i ~/.ssh/id_ed25519 gpfister
./ghbackup --full -k ~/.ssh/id_ed25519 gpfister
```

### 3. Partial Backup
> Note: Requires an existing `./gpfister-repo/` directory (e.g. from a prior full backup).
```bash
./ghbackup --partial gpfister
```
Output:
- Rotates existing `./gpfister-repo/` to `./gpfister-repo-previous/`
- Clones fresh repositories into `./gpfister-repo/`
- Creates `./gpfister-repo-YYYYMMDD_HHMMSS.patch` (and updates `./gpfister-repo.patch`)

### 4. Applying a Partial Backup Patch
To restore or bring an existing backup directory up-to-date with the patch:

```bash
# Apply patch from the parent directory:
git apply -p2 --unsafe-paths --directory=gpfister-repo-previous gpfister-repo.patch

# Or from inside the directory:
cd gpfister-repo-previous
git apply -p2 --unsafe-paths ../gpfister-repo.patch
```

### 5. Dry Run (Preview Repositories)
```bash
./ghbackup --dry-run gpfister
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
│   ├── main.py             # CLI definition and workflow execution
│   ├── github.py           # GitHub API client with org & user discovery
│   ├── clone.py            # Git cloning and directory rotation logic
│   └── backup.py           # Full (.zip) and Partial (.patch) creation
└── tests/
    └── test_backup.py      # Test suite
```

---

## Authors & Credits

Created and maintained by **Greg PFISTER** in France.

---

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

Copyright (c) 2026 Greg PFISTER, France.

