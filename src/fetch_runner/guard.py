"""Runtime-user and script-guard enforcement.

Two jobs:

1. At runtime, verify fetch-runner itself is executing as the configured user
   and not as root. The check is based on the real UID (``os.getuid``) and the
   passwd database, so ``$USER`` / ``$LOGNAME`` / utmp cannot influence it.

2. Before a job script runs, verify the script contains a canonical guard
   block near the top. The block is matched by exact bytes, so a weakened
   or removed check is detected.

The guard is written in portable POSIX shell, so it works under both
``#!/bin/sh`` and ``#!/bin/bash`` (and anything else that speaks POSIX
``test``/``printf``).
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path

GUARD_BEGIN_MARKER = "# >>> fetch-runner-guard:BEGIN"
GUARD_END_MARKER = "# <<< fetch-runner-guard:END"
GUARD_USER_ASSIGNMENT_PREFIX = "DEPLOY_USER="

# Env vars fetch-runner sets per script. Sudo's --preserve-env= and sudoers
# env_keep both render from this tuple to prevent drift.
PRESERVED_ENVIRONMENT_VARIABLE_NAMES: tuple[str, ...] = (
    "FETCH_RUNNER_JOB",
    "FETCH_RUNNER_BRANCH",
    "FETCH_RUNNER_COMMIT",
    "FETCH_RUNNER_REPO",
)

# Keep the guard as a literal byte template. The validator compares a script's
# bytes against the rendered output exactly, so "helpful" rewrites do not
# silently weaken the check.
_GUARD_TEMPLATE = (
    f"{GUARD_BEGIN_MARKER}\n"
    f"{GUARD_USER_ASSIGNMENT_PREFIX}{{user}}\n"
    'if [ "$(whoami)" != "$DEPLOY_USER" ] || [ "$(id -u)" -eq 0 ]; then\n'
    "    printf 'fetch-runner-guard: refusing to run as %s (uid %s);"
    ' required: %s, non-root\\n\' "$(whoami)" "$(id -u)" "$DEPLOY_USER" >&2\n'
    "    exit 1\n"
    "fi\n"
    f"{GUARD_END_MARKER}\n"
)


class GuardError(Exception):
    pass


@dataclass(frozen=True)
class ScriptGuardValidation:
    is_valid: bool
    error_reason: str = ""


def render_canonical_script_guard(user_name: str) -> str:
    """Return the canonical guard block for ``user`` (trailing newline included)."""
    _require_safe_user_name(user_name)
    return _GUARD_TEMPLATE.format(user=user_name)


def render_sudo_argv(run_as_user_name: str, script_path: Path) -> list[str]:
    """Build the argv used to execute ``script_path`` as ``run_as_user_name``.

    ``-n`` makes sudo fail immediately if a password is required (no tty).
    ``--`` terminates option parsing so a script path starting with ``-``
    cannot be misread as a sudo flag.
    """
    _require_safe_user_name(run_as_user_name)
    preserve_env_flag = "--preserve-env=" + ",".join(PRESERVED_ENVIRONMENT_VARIABLE_NAMES)
    return [
        "sudo",
        "-n",
        "-u",
        run_as_user_name,
        preserve_env_flag,
        "--",
        str(script_path),
    ]


def get_current_real_uid_user_name() -> str:
    """Return the login name of the real UID.

    Uses ``pwd.getpwuid(os.getuid())`` rather than ``$USER`` or ``whoami`` so
    the answer cannot be spoofed via environment variables or utmp.
    """
    real_uid = os.getuid()
    try:
        return pwd.getpwuid(real_uid).pw_name
    except KeyError as e:
        raise GuardError(f"cannot resolve current uid {real_uid} to a user name") from e


def require_expected_runtime_user(expected_user_name: str) -> None:
    """Raise ``GuardError`` unless the process is running as ``expected`` and non-root."""
    _require_safe_user_name(expected_user_name)
    actual_user_name = get_current_real_uid_user_name()
    if actual_user_name != expected_user_name:
        raise GuardError(
            f"runtime user {actual_user_name!r} does not match configured user "
            f"{expected_user_name!r}"
        )
    if os.getuid() == 0:
        raise GuardError("refusing to run as root (uid 0)")


def validate_canonical_script_guard(
    script_path: Path,
    expected_user_name: str,
) -> ScriptGuardValidation:
    """Check that ``script`` begins with the canonical guard for ``user``.

    Allowed before the guard: an optional shebang on line 1, blank lines, and
    ``#``-comment lines. Anything else (including ``set -e``) is rejected:
    the guard must be the first code that runs.
    """
    _require_safe_user_name(expected_user_name)
    try:
        script_text = script_path.read_text()
    except OSError as e:
        return ScriptGuardValidation(False, f"cannot read script {script_path}: {e}")

    script_lines = script_text.splitlines()
    current_line_index = 0
    if script_lines and script_lines[0].startswith("#!"):
        current_line_index = 1

    while current_line_index < len(script_lines):
        stripped_line = script_lines[current_line_index].strip()
        if stripped_line == GUARD_BEGIN_MARKER:
            break
        # Allow only comments before the guard. Even benign shell code like
        # `set -e` can change behavior before the identity check runs.
        if stripped_line == "" or stripped_line.startswith("#"):
            current_line_index += 1
            continue
        return ScriptGuardValidation(
            False,
            f"{script_path}:{current_line_index + 1}: guard must come before any executable "
            f"code; found {script_lines[current_line_index]!r}",
        )
    else:
        return ScriptGuardValidation(
            False,
            f"{script_path}: canonical guard block for user {expected_user_name!r} not found",
        )

    user_assignment_line_index = current_line_index + 1
    if user_assignment_line_index < len(script_lines):
        user_assignment_line = script_lines[user_assignment_line_index]
        expected_user_assignment = f"{GUARD_USER_ASSIGNMENT_PREFIX}{expected_user_name}"
        if (
            user_assignment_line.startswith(GUARD_USER_ASSIGNMENT_PREFIX)
            and user_assignment_line != expected_user_assignment
        ):
            return ScriptGuardValidation(
                False,
                f"{script_path}:{user_assignment_line_index + 1}: guard targets a different "
                f"user (expected {expected_user_name!r}); found {user_assignment_line!r}",
            )

    expected_guard_lines = render_canonical_script_guard(expected_user_name).splitlines()
    for guard_line_offset, expected_line in enumerate(expected_guard_lines):
        line_number = current_line_index + guard_line_offset + 1
        if current_line_index + guard_line_offset >= len(script_lines):
            return ScriptGuardValidation(
                False,
                f"{script_path}:{line_number}: guard block truncated; expected {expected_line!r}",
            )
        actual_line = script_lines[current_line_index + guard_line_offset]
        if actual_line != expected_line:
            return ScriptGuardValidation(
                False,
                f"{script_path}:{line_number}: guard block mismatch; "
                f"expected {expected_line!r}, got {actual_line!r}",
            )
    return ScriptGuardValidation(True)


# Conservative allowlist; keeps us safe if a user name is ever interpolated
# into shell text. POSIX permits a broader set but this covers real-world
# Linux account names.
_ALLOWED_USER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _require_safe_user_name(user_name: str) -> None:
    if not isinstance(user_name, str) or not user_name:
        raise GuardError("user name must be a non-empty string")
    if len(user_name) > 32:
        raise GuardError(f"user name too long: {user_name!r}")
    if user_name.startswith("-"):
        raise GuardError(f"user name may not start with '-': {user_name!r}")
    disallowed_characters = [char for char in user_name if char not in _ALLOWED_USER_CHARS]
    if disallowed_characters:
        raise GuardError(
            f"user name contains disallowed characters {sorted(set(disallowed_characters))!r}: "
            f"{user_name!r}"
        )
