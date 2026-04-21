"""User-check enforcement.

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

GUARD_BEGIN_PREFIX = "# >>> fetch-runner-guard:BEGIN"
GUARD_END = "# <<< fetch-runner-guard:END"

_GUARD_TEMPLATE = (
    "# >>> fetch-runner-guard:BEGIN user={user}\n"
    'if [ "$(whoami)" != "{user}" ] || [ "$(id -u)" -eq 0 ]; then\n'
    "    printf 'fetch-runner-guard: refusing to run as %s (uid %s);"
    ' required: {user}, non-root\\n\' "$(whoami)" "$(id -u)" >&2\n'
    "    exit 1\n"
    "fi\n"
    "# <<< fetch-runner-guard:END\n"
)


class GuardError(Exception):
    pass


@dataclass(frozen=True)
class GuardCheck:
    ok: bool
    reason: str = ""


def render_guard(user: str) -> str:
    """Return the canonical guard block for ``user`` (trailing newline included)."""
    _assert_safe_user(user)
    return _GUARD_TEMPLATE.format(user=user)


def current_user() -> str:
    """Return the login name of the real UID.

    Uses ``pwd.getpwuid(os.getuid())`` rather than ``$USER`` or ``whoami`` so
    the answer cannot be spoofed via environment variables or utmp.
    """
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError as e:
        raise GuardError(f"cannot resolve current uid {uid} to a user name") from e


def require_runtime_user(expected: str) -> None:
    """Raise ``GuardError`` unless the process is running as ``expected`` and non-root."""
    _assert_safe_user(expected)
    actual = current_user()
    if actual != expected:
        raise GuardError(f"runtime user {actual!r} does not match configured user {expected!r}")
    if os.getuid() == 0:
        raise GuardError("refusing to run as root (uid 0)")


def validate_script_guard(script: Path, user: str) -> GuardCheck:
    """Check that ``script`` begins with the canonical guard for ``user``.

    Allowed before the guard: an optional shebang on line 1, blank lines, and
    ``#``-comment lines. Anything else (including ``set -e``) is rejected:
    the guard must be the first code that runs.
    """
    _assert_safe_user(user)
    try:
        text = script.read_text()
    except OSError as e:
        return GuardCheck(False, f"cannot read script {script}: {e}")

    lines = text.splitlines()
    i = 0
    if lines and lines[0].startswith("#!"):
        i = 1

    expected_begin = f"# >>> fetch-runner-guard:BEGIN user={user}"
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == expected_begin:
            break
        if stripped.startswith(GUARD_BEGIN_PREFIX):
            return GuardCheck(
                False,
                f"{script}:{i + 1}: guard BEGIN marker targets a different user "
                f"(expected {user!r}); found {stripped!r}",
            )
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        return GuardCheck(
            False,
            f"{script}:{i + 1}: guard must come before any executable code; found {lines[i]!r}",
        )
    else:
        return GuardCheck(False, f"{script}: canonical guard block for user {user!r} not found")

    expected = render_guard(user).splitlines()
    for k, want in enumerate(expected):
        line_no = i + k + 1
        if i + k >= len(lines):
            return GuardCheck(
                False,
                f"{script}:{line_no}: guard block truncated; expected {want!r}",
            )
        got = lines[i + k]
        if got != want:
            return GuardCheck(
                False,
                f"{script}:{line_no}: guard block mismatch; expected {want!r}, got {got!r}",
            )
    return GuardCheck(True)


# Conservative allowlist; keeps us safe if a user name is ever interpolated
# into shell text. POSIX permits a broader set but this covers real-world
# Linux account names.
_ALLOWED_USER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _assert_safe_user(user: str) -> None:
    if not isinstance(user, str) or not user:
        raise GuardError("user name must be a non-empty string")
    if len(user) > 32:
        raise GuardError(f"user name too long: {user!r}")
    if user.startswith("-"):
        raise GuardError(f"user name may not start with '-': {user!r}")
    bad = [c for c in user if c not in _ALLOWED_USER_CHARS]
    if bad:
        raise GuardError(f"user name contains disallowed characters {sorted(set(bad))!r}: {user!r}")
