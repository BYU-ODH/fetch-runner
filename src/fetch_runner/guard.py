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

GUARD_BEGIN_MARKER_PREFIX = "# >>> fetch-runner-guard:BEGIN"
GUARD_END_MARKER = "# <<< fetch-runner-guard:END"

# Environment variables fetch-runner sets for every job script. Listed here so
# both the sudo invocation (--preserve-env=) and the sudoers env_keep policy
# render from the same source — a drift would silently drop variables.
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
    "# >>> fetch-runner-guard:BEGIN user={user}\n"
    'if [ "$(whoami)" != "{user}" ] || [ "$(id -u)" -eq 0 ]; then\n'
    "    printf 'fetch-runner-guard: refusing to run as %s (uid %s);"
    ' required: {user}, non-root\\n\' "$(whoami)" "$(id -u)" >&2\n'
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

    ``-n`` makes sudo fail immediately if a password is required; fetch-runner
    has no tty and must surface a missing sudoers rule as an error rather than
    hang. ``--`` terminates option parsing so a future absolute path that
    happens to start with ``-`` cannot be misread as a sudo flag.
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

    expected_guard_begin_marker = f"# >>> fetch-runner-guard:BEGIN user={expected_user_name}"
    while current_line_index < len(script_lines):
        stripped_line = script_lines[current_line_index].strip()
        if stripped_line == expected_guard_begin_marker:
            break
        if stripped_line.startswith(GUARD_BEGIN_MARKER_PREFIX):
            return ScriptGuardValidation(
                False,
                f"{script_path}:{current_line_index + 1}: guard BEGIN marker targets a different "
                f"user (expected {expected_user_name!r}); found {stripped_line!r}",
            )
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
