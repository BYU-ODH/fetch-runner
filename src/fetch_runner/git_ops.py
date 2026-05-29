"""Minimal ``git`` wrappers.

Subprocess calls use an argv list (never ``shell=True``); repo paths are
passed with ``-C`` and branch names have already been validated against a
conservative allowlist at config load.

Each operation runs as the job's ``run_as`` user — directly when that is
the current process user, otherwise wrapped in ``sudo -n -u <run_as>``.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

from .guard import _require_safe_user_name


class GitError(Exception):
    pass


def _git_absolute_path() -> str:
    """Resolve git lazily. Sudoers Commands rules need the absolute path, so
    this is shared between the sudo argv and ``render_sudoers_fragment``.
    """
    git_path = shutil.which("git")
    if git_path is None:
        raise GitError("git executable not found in PATH")
    return git_path


def _build_git_argv(repo_path: Path, run_as_user_name: str, git_args: tuple[str, ...]) -> list[str]:
    _require_safe_user_name(run_as_user_name)
    git_path = _git_absolute_path()
    direct_git_argv = [git_path, "-C", str(repo_path), *git_args]
    current_process_user_name = pwd.getpwuid(os.getuid()).pw_name
    if run_as_user_name == current_process_user_name:
        return direct_git_argv
    return ["sudo", "-n", "-u", run_as_user_name, "--", *direct_git_argv]


def _run_git_command(
    repo_path: Path,
    *git_args: str,
    run_as_user_name: str,
    timeout: float = 120,
) -> str:
    argv = _build_git_argv(repo_path, run_as_user_name, git_args)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitError(f"executable not found: {argv[0]!r}") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(git_args)} in {repo_path} timed out after {timeout}s") from e
    if result.returncode != 0:
        error_output = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(git_args)} in {repo_path} failed: {error_output}")
    return result.stdout.strip()


def git_get_current_branch(
    repo_path: Path,
    *,
    run_as_user_name: str,
) -> str | None:
    """Return the branch HEAD points at, or ``None`` for detached HEAD."""
    try:
        return _run_git_command(
            repo_path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            run_as_user_name=run_as_user_name,
        )
    except GitError:
        return None


def git_get_local_branch_commit_sha(
    repo_path: Path,
    branch_name: str,
    *,
    run_as_user_name: str,
) -> str:
    """Return the commit the local ``branch_name`` ref points at, or ``""`` if missing."""
    try:
        return _run_git_command(
            repo_path,
            "rev-parse",
            "--verify",
            f"refs/heads/{branch_name}",
            run_as_user_name=run_as_user_name,
        )
    except GitError:
        return ""


def git_fetch_branch_from_origin(
    repo_path: Path,
    branch_name: str,
    *,
    run_as_user_name: str,
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
        run_as_user_name=run_as_user_name,
        timeout=timeout,
    )
    return _run_git_command(
        repo_path,
        "rev-parse",
        "FETCH_HEAD",
        run_as_user_name=run_as_user_name,
    )


def git_force_checkout_branch_to_commit(
    repo_path: Path,
    branch_name: str,
    commit_sha: str,
    *,
    run_as_user_name: str,
) -> None:
    """Force the local ``branch_name`` ref and working tree to ``commit_sha``."""
    _run_git_command(
        repo_path,
        "checkout",
        "--quiet",
        "--force",
        "-B",
        branch_name,
        commit_sha,
        run_as_user_name=run_as_user_name,
    )
