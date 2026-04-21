"""jobs.toml loader with strict validation.

Loading a config is a security decision: if any check fails, we raise
``ConfigError`` and refuse to start rather than skip the job. The expected
operational pattern is ``fetch-runner --check jobs.toml`` as part of any
deploy, so that config errors surface before a restart.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from fetch_runner.guard import (
    GuardError,
    require_runtime_user,
    validate_script_guard,
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    path: Path
    branch: str
    script: Path
    timeout_seconds: int | None


@dataclass(frozen=True)
class Config:
    user: str
    poll_interval_seconds: int
    jobs: tuple[Job, ...]


_ALLOWED_TOP = {"general", "jobs"}
_ALLOWED_GENERAL = {"user", "poll_interval_seconds"}
_ALLOWED_JOB = {"name", "path", "branch", "script", "timeout_seconds"}

_UNSAFE_BRANCH_CHARS = frozenset(" \t\n\r\x00'\";|&`$<>()[]{}\\*?")


def load_config(path: Path) -> Config:
    try:
        raw_text = path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e

    _reject_unknown(raw, _ALLOWED_TOP, f"{path}: top-level")

    general = raw.get("general")
    if not isinstance(general, dict):
        raise ConfigError(f"{path}: missing [general] section")
    _reject_unknown(general, _ALLOWED_GENERAL, f"{path}: [general]")

    user = _require_str(general, "user", "[general]", path)
    poll_interval = _require_int(general, "poll_interval_seconds", "[general]", path, minimum=1)

    # Enforce user match before doing anything else: an operator who dropped
    # a jobs.toml for the wrong service account should see an immediate
    # error, not have individual jobs quietly skipped.
    try:
        require_runtime_user(user)
    except GuardError as e:
        raise ConfigError(f"{path}: {e}") from e

    raw_jobs = raw.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ConfigError(f"{path}: at least one [[jobs]] entry is required")

    seen_names: set[str] = set()
    seen_paths: set[Path] = set()
    jobs: list[Job] = []
    for i, entry in enumerate(raw_jobs):
        section = f"[[jobs]] #{i}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: {section} is not a table")
        _reject_unknown(entry, _ALLOWED_JOB, f"{path}: {section}")

        name = _require_str(entry, "name", section, path)
        if name in seen_names:
            raise ConfigError(f"{path}: duplicate job name {name!r}")
        seen_names.add(name)

        repo_path = Path(_require_str(entry, "path", section, path)).resolve()
        if repo_path in seen_paths:
            raise ConfigError(f"{path}: duplicate job path {repo_path}")
        seen_paths.add(repo_path)
        if not (repo_path / ".git").exists():
            raise ConfigError(f"{path}: {section}.path {repo_path} is not a git repository")

        branch = _require_str(entry, "branch", section, path)
        if branch.startswith("-") or any(c in _UNSAFE_BRANCH_CHARS for c in branch):
            raise ConfigError(f"{path}: {section}.branch contains unsafe characters: {branch!r}")
        if len(branch) > 128:
            raise ConfigError(f"{path}: {section}.branch too long")

        script_path = Path(_require_str(entry, "script", section, path)).resolve()
        _validate_script_file(script_path, user, section, path)

        timeout = entry.get("timeout_seconds")
        if timeout is not None:
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                raise ConfigError(f"{path}: {section}.timeout_seconds must be a positive integer")

        jobs.append(
            Job(
                name=name,
                path=repo_path,
                branch=branch,
                script=script_path,
                timeout_seconds=timeout,
            )
        )

    return Config(user=user, poll_interval_seconds=poll_interval, jobs=tuple(jobs))


def _validate_script_file(script: Path, user: str, section: str, cfg_path: Path) -> None:
    if not script.is_file():
        raise ConfigError(f"{cfg_path}: {section}.script {script} does not exist")
    if not os.access(script, os.X_OK):
        raise ConfigError(f"{cfg_path}: {section}.script {script} is not executable")
    st = script.stat()
    if st.st_mode & stat.S_IWOTH:
        raise ConfigError(f"{cfg_path}: {section}.script {script} is world-writable; refusing")
    check = validate_script_guard(script, user)
    if not check.ok:
        raise ConfigError(f"{cfg_path}: {section}.script failed guard validation: {check.reason}")


def _reject_unknown(d: dict, allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{where}: unknown keys {sorted(extra)!r}")


def _require_str(d: dict, key: str, section: str, cfg_path: Path) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ConfigError(f"{cfg_path}: {section}.{key} must be a non-empty string")
    return v


def _require_int(d: dict, key: str, section: str, cfg_path: Path, *, minimum: int) -> int:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ConfigError(f"{cfg_path}: {section}.{key} must be an integer")
    if v < minimum:
        raise ConfigError(f"{cfg_path}: {section}.{key} must be >= {minimum}")
    return v
