"""Minimal ``git`` wrappers.

The runner intentionally uses a very small, audited subset of ``git``.
Subprocess calls use an argv list (never ``shell=True``); repo paths are
passed with ``-C`` and branch names have already been validated against a
conservative allowlist at config load.

Every operation is performed *as the job's* ``run_as`` user, never as the
polling user. fetch-runner does not need to own the repos: the repos are
owned by their respective ``run_as`` users, and git invocations are
sudo-wrapped (``sudo -n -u <run_as>``) when ``run_as`` differs from the
process's own user.
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
    """Return the absolute path of ``git``.

    Sudo requires absolute paths in Commands rules, so the same value is
    used both when invoking sudo and when emitting the sudoers fragment
    (via :func:`render_sudoers_fragment_git_lines`). Resolved lazily so
    importing this module never fails on systems where git is missing.
    """
    git_path = shutil.which("git")
    if git_path is None:
        raise GitError("git executable not found in PATH")
    return git_path


def _build_git_argv(repo_path: Path, run_as_user_name: str, git_args: tuple[str, ...]) -> list[str]:
    """Construct the argv used to invoke git.

    When ``run_as_user_name`` matches the polling process's own user we run
    git directly. Otherwise we wrap with ``sudo -n -u <run_as> -- /abs/git``
    so the polling user never needs filesystem access to the repo.
    """
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
