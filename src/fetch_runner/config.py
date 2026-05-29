"""jobs.toml loader with strict validation.

Loading a config is a security decision: if any check fails, we raise
``ConfigError`` and refuse to start rather than skip the job. The expected
operational pattern is ``fetch-runner --check jobs.toml`` as part of any
deploy, so that config errors surface before a restart.
"""

from __future__ import annotations

import os
import pwd
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .guard import GuardError
from .guard import _require_safe_user_name
from .guard import require_expected_runtime_user
from .guard import validate_canonical_script_guard


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ConfiguredJob:
    name: str
    repo_path: Path
    # None means "follow whatever branch is currently checked out in the
    # working tree" — resolved live on each poll. Useful for dev deployments
    # where the operator switches branches manually.
    branch_name: str | None
    script_path: Path
    script_args: tuple[str, ...]
    script_timeout_seconds: int | None
    # The user this job's git ops and script run as. Defaults to
    # ``RunnerConfig.runtime_user`` when ``run_as`` is omitted in TOML.
    run_as_user: str


@dataclass(frozen=True)
class RunnerConfig:
    runtime_user: str
    poll_interval_seconds: int
    jobs: tuple[ConfiguredJob, ...]


_ALLOWED_TOP_LEVEL_KEYS = {"general", "jobs"}
_ALLOWED_GENERAL_KEYS = {"user", "poll_interval_seconds"}
_ALLOWED_JOB_KEYS = {"name", "path", "branch", "script", "args", "timeout_seconds", "run_as"}

_DISALLOWED_BRANCH_CHARACTERS = frozenset(" \t\n\r\x00'\";|&`$<>()[]{}\\*?")

# Conservative allowlist for script argument characters. Excludes shell
# metacharacters, whitespace, quotes, glob characters, and control bytes so
# args can be embedded in a sudoers rule (with a small set of mandatory
# backslash-escapes; see ``_escape_sudoers_token``) without ambiguity.
_ALLOWED_ARG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./=:+@,%"
)


def load_config(config_path: Path) -> RunnerConfig:
    try:
        config_text = config_path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {config_path}: {e}") from e
    try:
        parsed_toml = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {config_path}: {e}") from e

    _reject_unknown_keys(parsed_toml, _ALLOWED_TOP_LEVEL_KEYS, f"{config_path}: top-level")

    general_section = parsed_toml.get("general")
    if not isinstance(general_section, dict):
        raise ConfigError(f"{config_path}: missing [general] section")
    _reject_unknown_keys(general_section, _ALLOWED_GENERAL_KEYS, f"{config_path}: [general]")

    runtime_user = _require_non_empty_string(
        general_section,
        "user",
        "[general]",
        config_path,
    )
    poll_interval_seconds = _require_integer_at_least(
        general_section,
        "poll_interval_seconds",
        "[general]",
        config_path,
        minimum=1,
    )

    # Enforce user match before doing anything else: an operator who dropped
    # a jobs.toml for the wrong service account should see an immediate
    # error, not have individual jobs quietly skipped.
    try:
        require_expected_runtime_user(runtime_user)
    except GuardError as e:
        raise ConfigError(f"{config_path}: {e}") from e

    raw_job_sections = parsed_toml.get("jobs")
    if not isinstance(raw_job_sections, list) or not raw_job_sections:
        raise ConfigError(f"{config_path}: at least one [[jobs]] entry is required")

    seen_names: set[str] = set()
    seen_repo_paths: set[Path] = set()
    configured_jobs: list[ConfiguredJob] = []
    for job_index, raw_job_section in enumerate(raw_job_sections):
        section_label = f"[[jobs]] #{job_index}"
        if not isinstance(raw_job_section, dict):
            raise ConfigError(f"{config_path}: {section_label} is not a table")
        _reject_unknown_keys(raw_job_section, _ALLOWED_JOB_KEYS, f"{config_path}: {section_label}")

        job_name = _require_non_empty_string(raw_job_section, "name", section_label, config_path)
        if job_name in seen_names:
            raise ConfigError(f"{config_path}: duplicate job name {job_name!r}")
        seen_names.add(job_name)

        # Resolve early so duplicate-path detection is based on the real target
        # path, not on whatever relative spelling happened to appear in TOML.
        repo_path = Path(
            _require_non_empty_string(raw_job_section, "path", section_label, config_path)
        ).resolve()
        # Only one job may own a worktree. Two jobs resetting the same checkout
        # to different commits would create non-deterministic deploy behavior.
        if repo_path in seen_repo_paths:
            raise ConfigError(f"{config_path}: duplicate job path {repo_path}")
        seen_repo_paths.add(repo_path)
        if not (repo_path / ".git").exists():
            raise ConfigError(
                f"{config_path}: {section_label}.path {repo_path} is not a git repository"
            )

        # Omitting `branch` means "follow the current checked-out branch" —
        # resolved live on each poll. Present-but-empty is still rejected.
        raw_branch = raw_job_section.get("branch")
        if raw_branch is None:
            branch_name: str | None = None
        else:
            if not isinstance(raw_branch, str) or not raw_branch:
                raise ConfigError(
                    f"{config_path}: {section_label}.branch must be a non-empty string"
                )
            branch_name = raw_branch
            # Branch names are passed as argv entries, but git still interprets
            # leading dashes and a wide range of refname syntax. A conservative
            # character filter keeps the allowed surface area easy to reason about.
            if branch_name.startswith("-") or any(
                char in _DISALLOWED_BRANCH_CHARACTERS for char in branch_name
            ):
                raise ConfigError(
                    f"{config_path}: {section_label}.branch contains unsafe characters: "
                    f"{branch_name!r}"
                )
            if len(branch_name) > 128:
                raise ConfigError(f"{config_path}: {section_label}.branch too long")

        script_path = Path(
            _require_non_empty_string(raw_job_section, "script", section_label, config_path)
        ).resolve()

        script_args = _parse_script_args(raw_job_section, section_label, config_path)

        # Resolve via passwd at load time so a typo fails fast instead of
        # surfacing as a confusing sudo error during the first poll.
        run_as_user = raw_job_section.get("run_as", runtime_user)
        if not isinstance(run_as_user, str) or not run_as_user:
            raise ConfigError(f"{config_path}: {section_label}.run_as must be a non-empty string")
        try:
            _require_safe_user_name(run_as_user)
        except GuardError as e:
            raise ConfigError(f"{config_path}: {section_label}.run_as: {e}") from e
        try:
            pwd.getpwnam(run_as_user)
        except KeyError as e:
            raise ConfigError(
                f"{config_path}: {section_label}.run_as user {run_as_user!r} "
                f"does not exist on this system"
            ) from e

        _validate_job_script_file(script_path, run_as_user, section_label, config_path)

        script_timeout_seconds = raw_job_section.get("timeout_seconds")
        if script_timeout_seconds is not None:
            if (
                not isinstance(script_timeout_seconds, int)
                or isinstance(script_timeout_seconds, bool)
                or script_timeout_seconds <= 0
            ):
                raise ConfigError(
                    f"{config_path}: {section_label}.timeout_seconds must be a positive integer"
                )

        configured_jobs.append(
            ConfiguredJob(
                name=job_name,
                repo_path=repo_path,
                branch_name=branch_name,
                script_path=script_path,
                script_args=script_args,
                script_timeout_seconds=script_timeout_seconds,
                run_as_user=run_as_user,
            )
        )

    return RunnerConfig(
        runtime_user=runtime_user,
        poll_interval_seconds=poll_interval_seconds,
        jobs=tuple(configured_jobs),
    )


def _validate_job_script_file(
    script_path: Path,
    runtime_user: str,
    section_label: str,
    config_path: Path,
) -> None:
    """Run the startup-time script checks.

    This is intentionally duplicated later in the runner after checkout. The
    load-time check catches bad deployments before the service starts; the
    post-checkout check catches a newly fetched commit that changed the script.
    """
    if not script_path.is_file():
        raise ConfigError(f"{config_path}: {section_label}.script {script_path} does not exist")
    if not os.access(script_path, os.X_OK):
        raise ConfigError(f"{config_path}: {section_label}.script {script_path} is not executable")
    script_stat = script_path.stat()
    if script_stat.st_mode & stat.S_IWOTH:
        raise ConfigError(
            f"{config_path}: {section_label}.script {script_path} is world-writable; refusing"
        )
    guard_validation = validate_canonical_script_guard(script_path, runtime_user)
    if not guard_validation.is_valid:
        raise ConfigError(
            f"{config_path}: {section_label}.script failed guard validation: "
            f"{guard_validation.error_reason}"
        )


def _parse_script_args(
    raw_job_section: dict,
    section_label: str,
    config_path: Path,
) -> tuple[str, ...]:
    """Validate and return ``[[jobs]].args``. Each element must be a non-empty
    string drawn from a conservative character set so it can be embedded in a
    sudoers rule without ambiguity. A leading dash is allowed (common in CLI
    flags); the sudoers escape handler covers the few remaining specials.
    """
    raw_args = raw_job_section.get("args")
    if raw_args is None:
        return ()
    if not isinstance(raw_args, list):
        raise ConfigError(f"{config_path}: {section_label}.args must be an array of strings")
    validated_args: list[str] = []
    for arg_index, raw_arg in enumerate(raw_args):
        arg_label = f"{section_label}.args[{arg_index}]"
        if not isinstance(raw_arg, str) or not raw_arg:
            raise ConfigError(f"{config_path}: {arg_label} must be a non-empty string")
        if len(raw_arg) > 256:
            raise ConfigError(f"{config_path}: {arg_label} too long")
        disallowed = sorted({c for c in raw_arg if c not in _ALLOWED_ARG_CHARS})
        if disallowed:
            raise ConfigError(
                f"{config_path}: {arg_label} contains disallowed characters {disallowed!r}: "
                f"{raw_arg!r}"
            )
        validated_args.append(raw_arg)
    return tuple(validated_args)


def _reject_unknown_keys(raw_section: dict, allowed_keys: set[str], section_label: str) -> None:
    unknown_keys = set(raw_section) - allowed_keys
    if unknown_keys:
        raise ConfigError(f"{section_label}: unknown keys {sorted(unknown_keys)!r}")


def _require_non_empty_string(
    raw_section: dict,
    key: str,
    section_label: str,
    config_path: Path,
) -> str:
    value = raw_section.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{config_path}: {section_label}.{key} must be a non-empty string")
    return value


def _require_integer_at_least(
    raw_section: dict,
    key: str,
    section_label: str,
    config_path: Path,
    *,
    minimum: int,
) -> int:
    value = raw_section.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{config_path}: {section_label}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"{config_path}: {section_label}.{key} must be >= {minimum}")
    return value
