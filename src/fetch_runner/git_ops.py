"""Minimal ``git`` wrappers.

Subprocess calls use an argv list (never ``shell=True``); repo paths are
passed with ``-C`` and branch names have already been validated against an
unsafe-character set at config load.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _run(repo: Path, *args: str, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitError("git executable not found in PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} in {repo} timed out after {timeout}s") from e
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} in {repo} failed: {msg}")
    return result.stdout.strip()


def current_commit(repo: Path, branch: str) -> str:
    """Return the commit the local ``branch`` ref points at, or '' if it does not exist."""
    try:
        return _run(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    except GitError:
        return ""


def fetch(repo: Path, branch: str, timeout: float = 120) -> str:
    """Fetch ``origin/<branch>`` and return the fetched commit SHA."""
    _run(repo, "fetch", "--quiet", "--no-tags", "origin", branch, timeout=timeout)
    return _run(repo, "rev-parse", "FETCH_HEAD")


def checkout(repo: Path, branch: str, commit: str) -> None:
    """Force the local ``branch`` to ``commit`` and reset the working tree."""
    _run(repo, "checkout", "--quiet", "--force", "-B", branch, commit)
