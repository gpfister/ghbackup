# Copyright (c) 2026 Greg PFISTER, France
# SPDX-License-Identifier: MIT

"""GitHub API client for repository discovery."""

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import requests


def resolve_github_token(provided_token: Optional[str] = None) -> Optional[str]:
    """Resolve GitHub token from provided argument, environment, or gh CLI.

    Checks:
    1. Explicitly provided token argument
    2. GITHUB_TOKEN environment variable
    3. GH_TOKEN environment variable
    4. gh CLI (`gh auth token`) if available
    """
    if provided_token:
        return provided_token.strip()

    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token.strip()

    # Try gh auth token if gh CLI exists
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass

    return None


class GitHubAPIError(Exception):
    """Exception raised for GitHub API request errors."""
    pass


def get_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Generate headers for GitHub API requests."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ghbackup-tool/0.1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_org_or_user_repos(
    target: str,
    token: Optional[str] = None,
    include_forks: bool = True,
    include_archived: bool = True,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Fetch all repositories for an organization or user.

    Attempts to query /orgs/{target}/repos first; if it returns 404,
    falls back to /users/{target}/repos.

    Returns:
        Tuple[str, List[Dict[str, Any]]]: Tuple of (target_type, repos_list)
        where target_type is "organization" or "user".
    """
    headers = get_headers(token)
    session = requests.Session()
    session.headers.update(headers)

    # First attempt: check if target is an organization
    org_url = f"https://api.github.com/orgs/{target}/repos"
    params = {"per_page": 100, "page": 1, "type": "all"}

    resp = session.get(org_url, params=params, timeout=30)

    target_type = "organization"
    base_url = org_url

    if resp.status_code == 404:
        # Fall back to user repos
        user_url = f"https://api.github.com/users/{target}/repos"
        resp = session.get(user_url, params=params, timeout=30)
        if resp.status_code == 404:
            raise GitHubAPIError(
                f"Neither GitHub organization nor user '{target}' was found (HTTP 404)."
            )
        target_type = "user"
        base_url = user_url

    if resp.status_code == 401:
        raise GitHubAPIError(
            "Authentication failed (HTTP 401). Please check your GitHub token."
        )
    if resp.status_code == 403:
        rate_limit_remaining = resp.headers.get("x-ratelimit-remaining", "")
        if rate_limit_remaining == "0":
            reset_time = resp.headers.get("x-ratelimit-reset", "soon")
            raise GitHubAPIError(
                f"GitHub API rate limit exceeded (HTTP 403). Limit resets at unix timestamp {reset_time}. "
                "Consider providing a GitHub token via --token or GITHUB_TOKEN."
            )
        raise GitHubAPIError(
            f"Access forbidden (HTTP 403): {resp.json().get('message', resp.text)}"
        )

    if resp.status_code != 200:
        raise GitHubAPIError(
            f"GitHub API returned error {resp.status_code}: {resp.text}"
        )

    raw_repos: List[Dict[str, Any]] = []
    page = 1

    while True:
        if page > 1:
            params["page"] = page
            resp = session.get(base_url, params=params, timeout=30)
            if resp.status_code != 200:
                raise GitHubAPIError(
                    f"Failed to fetch page {page} for '{target}': HTTP {resp.status_code}"
                )

        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            break

        raw_repos.extend(data)

        # Check pagination Link header
        link_header = resp.headers.get("Link", "")
        if 'rel="next"' not in link_header:
            break

        page += 1

    # Filter repos
    filtered_repos: List[Dict[str, Any]] = []
    for r in raw_repos:
        if not include_forks and r.get("fork", False):
            continue
        if not include_archived and r.get("archived", False):
            continue

        filtered_repos.append(
            {
                "name": r["name"],
                "full_name": r["full_name"],
                "clone_url": r["clone_url"],
                "ssh_url": r["ssh_url"],
                "is_fork": r.get("fork", False),
                "is_archived": r.get("archived", False),
                "is_private": r.get("private", False),
                "default_branch": r.get("default_branch", "main"),
                "size_kb": r.get("size", 0),
            }
        )

    return target_type, filtered_repos
