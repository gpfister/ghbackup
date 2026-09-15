# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""Unit and integration tests for ghbackup."""

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from src.backup import create_full_backup, create_partial_backup
from src.clone import clone_all_repositories, clone_repository, rotate_backup_directories
from src.github import fetch_org_or_user_repos, resolve_github_token
from src.main import app


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_cli_requires_full_or_partial(runner):
    """Test that CLI errors out when neither --full nor --partial is specified."""
    result = runner.invoke(app, ["backup", "myorg"])
    assert result.exit_code == 1
    assert "You must specify either --full (-f) or --partial (-p)" in result.output


def test_cli_mutual_exclusion(runner):
    """Test that CLI errors out when both --full and --partial are specified."""
    result = runner.invoke(app, ["backup", "--full", "--partial", "myorg"])
    assert result.exit_code == 1
    assert "Cannot specify both --full and --partial" in result.output


def test_directory_rotation_initial(temp_dir):
    """Test initial directory rotation when no directory exists."""
    repo_dir, prev_dir = rotate_backup_directories(temp_dir, "testorg")
    assert repo_dir.exists()
    assert repo_dir.name == "testorg-repo"
    assert prev_dir is None


def test_directory_rotation_with_existing_repo(temp_dir):
    """Test directory rotation when <org>-repo exists."""
    repo_dir = temp_dir / "testorg-repo"
    repo_dir.mkdir()
    (repo_dir / "repo1.txt").write_text("initial version")

    new_repo_dir, prev_dir = rotate_backup_directories(temp_dir, "testorg")

    assert new_repo_dir.exists()
    assert len(list(new_repo_dir.iterdir())) == 0  # fresh directory
    assert prev_dir is not None
    assert prev_dir.exists()
    assert prev_dir.name == "testorg-repo-previous"
    assert (prev_dir / "repo1.txt").read_text() == "initial version"


def test_directory_rotation_with_existing_repo_and_previous(temp_dir):
    """Test rotation when both <org>-repo and <org>-repo-previous exist."""
    prev_dir = temp_dir / "testorg-repo-previous"
    prev_dir.mkdir()
    (prev_dir / "old.txt").write_text("old version")

    repo_dir = temp_dir / "testorg-repo"
    repo_dir.mkdir()
    (repo_dir / "current.txt").write_text("current version")

    new_repo_dir, rotated_prev = rotate_backup_directories(temp_dir, "testorg")

    assert new_repo_dir.exists()
    assert rotated_prev.exists()
    # The previous should now contain "current.txt", and old.txt should be gone
    assert not (rotated_prev / "old.txt").exists()
    assert (rotated_prev / "current.txt").read_text() == "current version"


def test_create_full_backup_zip(temp_dir):
    """Test creating a full .zip backup of repo directory."""
    repo_dir = temp_dir / "testorg-repo"
    repo_dir.mkdir()
    (repo_dir / "file1.txt").write_text("hello world")
    sub_dir = repo_dir / "subdir"
    sub_dir.mkdir()
    (sub_dir / "binary.bin").write_bytes(b"\x00\x01\x02\x03\x04\xff")

    backup_info = create_full_backup(
        repo_dir=repo_dir,
        output_dir=temp_dir,
        org="testorg",
    )

    assert backup_info["mode"] == "full"
    zip_path = Path(backup_info["archive_path"])
    assert zip_path.exists()
    assert zip_path.suffix == ".zip"
    assert backup_info["total_files"] == 2

    # Verify zip content
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "testorg-repo/file1.txt" in namelist
        assert "testorg-repo/subdir/binary.bin" in namelist
        assert zf.read("testorg-repo/subdir/binary.bin") == b"\x00\x01\x02\x03\x04\xff"


def test_create_partial_backup_and_apply(temp_dir):
    """Test creating a binary-compatible partial .patch and verifying application."""
    prev_dir = temp_dir / "testorg-repo-previous"
    prev_dir.mkdir()
    (prev_dir / "text.txt").write_text("version 1")
    (prev_dir / "deleted.txt").write_text("to be deleted")
    (prev_dir / "binary.dat").write_bytes(b"\x01\x02\x03\x04")

    curr_dir = temp_dir / "testorg-repo"
    curr_dir.mkdir()
    (curr_dir / "text.txt").write_text("version 2 modified")
    (curr_dir / "added.txt").write_text("brand new file")
    (curr_dir / "binary.dat").write_bytes(b"\x01\x02\xaa\xbb\xcc")

    patch_info = create_partial_backup(
        current_repo_dir=curr_dir,
        previous_repo_dir=prev_dir,
        output_dir=temp_dir,
        org="testorg",
    )

    assert patch_info["mode"] == "partial"
    patch_path = Path(patch_info["patch_path"])
    assert patch_path.exists()
    assert patch_path.stat().st_size > 0

    # Test applying the patch onto a copy of prev_dir
    restore_dir = temp_dir / "restore"
    shutil.copytree(prev_dir, restore_dir)

    # Use git apply
    apply_cmd = [
        "git",
        "apply",
        "-p2",
        "--unsafe-paths",
        f"--directory={restore_dir}",
        str(patch_path),
    ]
    proc = subprocess.run(apply_cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git apply failed: {proc.stderr}"

    # Verify restore matches curr_dir
    assert (restore_dir / "text.txt").read_text() == "version 2 modified"
    assert (restore_dir / "added.txt").read_text() == "brand new file"
    assert not (restore_dir / "deleted.txt").exists()
    assert (restore_dir / "binary.dat").read_bytes() == b"\x01\x02\xaa\xbb\xcc"


def test_create_partial_backup_without_baseline(temp_dir):
    """Test partial backup raises error when no previous backup exists."""
    curr_dir = temp_dir / "testorg-repo"
    curr_dir.mkdir()
    (curr_dir / "init.txt").write_text("initial commit")
    (curr_dir / "bin.dat").write_bytes(b"\xde\xad\xbe\xef")

    with pytest.raises(FileNotFoundError, match="previous repository directory"):
        create_partial_backup(
            current_repo_dir=curr_dir,
            previous_repo_dir=None,
            output_dir=temp_dir,
            org="testorg",
        )


def test_cli_partial_backup_fails_if_no_repo(runner, temp_dir):
    """Test that CLI errors out when --partial is used but no <org>-repo exists."""
    result = runner.invoke(app, ["backup", "--partial", "testorg", "--output-dir", str(temp_dir)])
    assert result.exit_code == 1
    assert "Cannot perform partial backup" in result.output
    assert "testorg-repo" in result.output
    assert "does not exist" in result.output


def test_cli_partial_backup_fails_if_repo_is_file(runner, temp_dir):
    """Test that CLI errors out when <org>-repo exists as a file instead of a directory."""
    repo_file = temp_dir / "testorg-repo"
    repo_file.write_text("not a directory")

    result = runner.invoke(app, ["backup", "--partial", "testorg", "--output-dir", str(temp_dir)])
    assert result.exit_code == 1
    assert "Cannot perform partial backup" in result.output
    assert "is not a directory" in result.output


def test_cli_full_backup_e2e(runner, temp_dir):
    """End-to-end CLI test for full backup with mocked GitHub and git clone."""
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

    def mock_clone(repo_info, target_dir, **kwargs):
        dest = target_dir / repo_info["name"]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("# Sample Repo")
        return True, "Cloned successfully"

    with patch("src.main.fetch_org_or_user_repos", return_value=("organization", mock_repos)), \
         patch("src.clone.clone_repository", side_effect=mock_clone):
        result = runner.invoke(
            app,
            ["backup", "--full", "testorg", "--output-dir", str(temp_dir)]
        )
        assert result.exit_code == 0
        assert "Full Backup Created Successfully!" in result.output
        assert (temp_dir / "testorg-repo" / "sample-repo" / "README.md").exists()
        assert (temp_dir / "testorg-repo.zip").exists()


def test_cli_partial_backup_e2e(runner, temp_dir):
    """End-to-end CLI test for partial backup with mocked GitHub and git clone."""
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

    # Pre-populate previous repo directory
    prev_dir = temp_dir / "testorg-repo"
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / "old_file.txt").write_text("old content")

    def mock_clone(repo_info, target_dir, **kwargs):
        dest = target_dir / repo_info["name"]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "new_file.txt").write_text("new content")
        return True, "Cloned successfully"

    with patch("src.main.fetch_org_or_user_repos", return_value=("organization", mock_repos)), \
         patch("src.clone.clone_repository", side_effect=mock_clone):
        result = runner.invoke(
            app,
            ["backup", "--partial", "testorg", "--output-dir", str(temp_dir)]
        )
        assert result.exit_code == 0
        assert "Partial Backup Created Successfully!" in result.output
        assert (temp_dir / "testorg-repo-previous" / "old_file.txt").exists()
        assert (temp_dir / "testorg-repo" / "sample-repo" / "new_file.txt").exists()
        assert (temp_dir / "testorg-repo.patch").exists()



def test_token_resolution():
    """Test token resolution order."""
    # Explicit argument
    assert resolve_github_token("explicit_token") == "explicit_token"

    # Environment variables
    with patch.dict(os.environ, {"GITHUB_TOKEN": "env_token"}, clear=True):
        assert resolve_github_token() == "env_token"

    with patch.dict(os.environ, {"GH_TOKEN": "gh_token"}, clear=True):
        assert resolve_github_token() == "gh_token"


def test_github_api_fallback_to_user():
    """Test GitHub API fallback from org 404 to user repos."""
    mock_user_repos = [
        {
            "name": "test-repo",
            "full_name": "gpfister/test-repo",
            "clone_url": "https://github.com/gpfister/test-repo.git",
            "ssh_url": "git@github.com:gpfister/test-repo.git",
            "fork": False,
            "archived": False,
            "private": False,
            "default_branch": "main",
            "size": 120,
        }
    ]

    with patch("requests.Session.get") as mock_get:
        # First call (org): 404
        # Second call (user): 200
        mock_org_resp = MagicMock()
        mock_org_resp.status_code = 404

        mock_user_resp = MagicMock()
        mock_user_resp.status_code = 200
        mock_user_resp.json.return_value = mock_user_repos
        mock_user_resp.headers = {}

        mock_get.side_effect = [mock_org_resp, mock_user_resp]

        target_type, repos = fetch_org_or_user_repos("gpfister")

        assert target_type == "user"
        assert len(repos) == 1
        assert repos[0]["name"] == "test-repo"


def test_cli_ssh_key_nonexistent(runner, temp_dir):
    """Test that CLI errors out when --ssh-key specifies a nonexistent file."""
    fake_key = temp_dir / "nonexistent_key"
    result = runner.invoke(app, ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "--ssh-key", str(fake_key)])
    assert result.exit_code == 1
    assert "SSH key file does not exist" in result.output


def test_cli_ssh_key_is_directory(runner, temp_dir):
    """Test that CLI errors out when --ssh-key specifies a directory."""
    key_dir = temp_dir / "key_dir"
    key_dir.mkdir()
    result = runner.invoke(app, ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "--ssh-key", str(key_dir)])
    assert result.exit_code != 0


def test_cli_ssh_key_e2e(runner, temp_dir):
    """Test CLI runs with --ssh-key and passes ssh_key to clone_all_repositories."""
    key_file = temp_dir / "id_ed25519"
    key_file.write_text("dummy-private-key")

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

    mock_summary = {
        "total": 1,
        "successful": 1,
        "failed": 0,
        "failed_repos": [],
    }

    with patch("src.main.fetch_org_or_user_repos", return_value=("organization", mock_repos)), \
         patch("src.main.clone_all_repositories", return_value=mock_summary) as mock_clone, \
         patch("src.main.create_full_backup", return_value={"archive_path": "a.zip", "total_files": 1, "uncompressed_bytes": 10, "compressed_bytes": 5}):
        result = runner.invoke(
            app,
            ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "--ssh-key", str(key_file)]
        )
        assert result.exit_code == 0
        assert "SSH Key:" in result.output
        assert str(key_file.resolve()) in result.output
        mock_clone.assert_called_once()
        call_kwargs = mock_clone.call_args[1]
        assert call_kwargs["use_ssh"] is True
        assert call_kwargs["ssh_key"] == key_file.resolve()


def test_cli_ssh_key_aliases(runner, temp_dir):
    """Test CLI -k and -i aliases for --ssh-key."""
    key_file = temp_dir / "id_rsa"
    key_file.write_text("dummy-rsa-key")

    mock_repos = [{"name": "repo1", "clone_url": "https://...", "ssh_url": "git@...", "is_fork": False, "is_archived": False, "is_private": False, "default_branch": "main", "size_kb": 5}]
    mock_summary = {"total": 1, "successful": 1, "failed": 0, "failed_repos": []}

    with patch("src.main.fetch_org_or_user_repos", return_value=("user", mock_repos)), \
         patch("src.main.clone_all_repositories", return_value=mock_summary) as mock_clone, \
         patch("src.main.create_full_backup", return_value={"archive_path": "a.zip", "total_files": 1, "uncompressed_bytes": 5, "compressed_bytes": 2}):
        # Test -k
        res_k = runner.invoke(app, ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "-k", str(key_file)])
        assert res_k.exit_code == 0
        assert mock_clone.call_args[1]["ssh_key"] == key_file.resolve()
        assert mock_clone.call_args[1]["use_ssh"] is True

        # Test -i
        mock_clone.reset_mock()
        res_i = runner.invoke(app, ["backup", "--full", "testorg", "--output-dir", str(temp_dir), "-i", str(key_file)])
        assert res_i.exit_code == 0
        assert mock_clone.call_args[1]["ssh_key"] == key_file.resolve()
        assert mock_clone.call_args[1]["use_ssh"] is True


def test_cli_ssh_key_envvar(runner, temp_dir):
    """Test GH_SSH_KEY environment variable."""
    key_file = temp_dir / "id_env"
    key_file.write_text("env-key")

    mock_repos = [{"name": "repo1", "clone_url": "https://...", "ssh_url": "git@...", "is_fork": False, "is_archived": False, "is_private": False, "default_branch": "main", "size_kb": 5}]
    mock_summary = {"total": 1, "successful": 1, "failed": 0, "failed_repos": []}

    with patch("src.main.fetch_org_or_user_repos", return_value=("user", mock_repos)), \
         patch("src.main.clone_all_repositories", return_value=mock_summary) as mock_clone, \
         patch("src.main.create_full_backup", return_value={"archive_path": "a.zip", "total_files": 1, "uncompressed_bytes": 5, "compressed_bytes": 2}), \
         patch.dict(os.environ, {"GH_SSH_KEY": str(key_file)}):
        res = runner.invoke(app, ["backup", "--full", "testorg", "--output-dir", str(temp_dir)])
        assert res.exit_code == 0
        assert mock_clone.call_args[1]["ssh_key"] == key_file.resolve()
        assert mock_clone.call_args[1]["use_ssh"] is True


def test_clone_repository_with_ssh_key(temp_dir):
    """Test clone_repository passes sshCommand and GIT_SSH_COMMAND properly."""
    key_file = temp_dir / "id_ed25519"
    key_file.write_text("fake-key")

    repo_info = {
        "name": "my-repo",
        "clone_url": "https://github.com/myorg/my-repo.git",
        "ssh_url": "git@github.com:myorg/my-repo.git",
    }

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        success, msg = clone_repository(
            repo_info=repo_info,
            target_dir=temp_dir,
            ssh_key=key_file,
        )

        assert success is True
        mock_run.assert_called_once()
        call_args, call_kwargs = mock_run.call_args
        cmd = call_args[0]
        env = call_kwargs["env"]

        # Verify SSH URL used
        assert "git@github.com:myorg/my-repo.git" in cmd
        # Verify core.sshCommand configured
        assert "-c" in cmd
        expected_ssh_cmd = f"ssh -i {key_file.resolve()} -o IdentitiesOnly=yes"
        assert f"core.sshCommand={expected_ssh_cmd}" in cmd
        # Verify GIT_SSH_COMMAND in env
        assert env["GIT_SSH_COMMAND"] == expected_ssh_cmd


def test_clone_repository_with_ssh_key_spaces(temp_dir):
    """Test clone_repository handles SSH key paths containing spaces."""
    key_dir = temp_dir / "my keys"
    key_dir.mkdir()
    key_file = key_dir / "id_rsa"
    key_file.write_text("key-with-spaces")

    repo_info = {
        "name": "my-repo",
        "clone_url": "https://github.com/myorg/my-repo.git",
        "ssh_url": "git@github.com:myorg/my-repo.git",
    }

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        success, msg = clone_repository(
            repo_info=repo_info,
            target_dir=temp_dir,
            ssh_key=key_file,
        )

        assert success is True
        mock_run.assert_called_once()
        call_args, call_kwargs = mock_run.call_args
        cmd = call_args[0]
        env = call_kwargs["env"]

        expected_quoted = f"ssh -i '{key_file.resolve()}' -o IdentitiesOnly=yes"
        assert f"core.sshCommand={expected_quoted}" in cmd
        assert env["GIT_SSH_COMMAND"] == expected_quoted
