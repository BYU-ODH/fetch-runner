"""Minimal ``git`` wrappers.

The runner intentionally uses a very small, audited subset of ``git``.
Subprocess calls use an argv list (never ``shell=True``); repo paths are
passed with ``-C`` and branch names have already been validated against a
conservative allowlist at config load.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _run_git_command(repo_path: Path, *git_args: str, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *git_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found in PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(git_args)} in {repo_path} timed out after {timeout}s") from e
    if result.returncode != 0:
        error_output = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(git_args)} in {repo_path} failed: {error_output}")
    return result.stdout.strip()


def git_get_local_branch_commit_sha(repo_path: Path, branch_name: str) -> str:
    """Return the commit the local ``branch_name`` ref points at, or ``""`` if missing."""
    try:
        return _run_git_command(repo_path, "rev-parse", "--verify", f"refs/heads/{branch_name}")
    except GitError:
        return ""


def git_fetch_branch_from_origin(
    repo_path: Path,
    branch_name: str,
    timeout: float = 120,
) -> str:
    """Fetch ``origin/<branch_name>`` and return the resulting ``FETCH_HEAD`` SHA."""
    _run_git_command(
        repo_path,
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        branch_name,
        timeout=timeout,
    )
    return _run_git_command(repo_path, "rev-parse", "FETCH_HEAD")


def git_force_checkout_branch_to_commit(
    repo_path: Path,
    branch_name: str,
    commit_sha: str,
) -> None:
    """Force the local ``branch_name`` ref and working tree to ``commit_sha``."""
    _run_git_command(repo_path, "checkout", "--quiet", "--force", "-B", branch_name, commit_sha)
