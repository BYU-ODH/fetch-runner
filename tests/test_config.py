from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fetch_runner.config import ConfigError
from fetch_runner.config import load_config
from fetch_runner.guard import get_current_real_uid_user_name
from fetch_runner.guard import render_canonical_script_guard


@pytest.fixture(autouse=True)
def _not_running_as_root():
    if os.getuid() == 0:
        pytest.skip("config tests require a non-root test user")


def _create_repo_directory(repo_path: Path) -> Path:
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / ".git").mkdir()
    return repo_path


def _create_guarded_script(script_path: Path, user_name: str) -> Path:
    script_path.write_text("#!/bin/bash\n" + render_canonical_script_guard(user_name) + "echo hi\n")
    script_path.chmod(0o755)
    return script_path


def _write_jobs_toml(config_path: Path, config_body: str) -> Path:
    config_path.write_text(config_body)
    return config_path


def _write_minimal_valid_jobs_toml(
    tmp_path: Path,
    *,
    user_name: str,
    extra_job_lines: str = "",
) -> Path:
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    return _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
{extra_job_lines}
""",
    )


def test_load_happy_path(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(tmp_path, user_name=user_name)
    runner_config = load_config(config_path)
    assert runner_config.runtime_user == user_name
    assert runner_config.poll_interval_seconds == 30
    assert len(runner_config.jobs) == 1
    assert runner_config.jobs[0].name == "j1"
    assert runner_config.jobs[0].branch_name == "main"
    assert runner_config.jobs[0].script_timeout_seconds is None


def test_load_rejects_wrong_user(tmp_path: Path):
    # A jobs.toml whose user does not match the running user must be refused.
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", "someone-else-xyz")
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "someone-else-xyz"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="runtime user"):
        load_config(config_path)


def test_load_rejects_unknown_top_level_key(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[extras]
something = 1

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(config_path)


def test_load_rejects_unknown_job_key(tmp_path: Path):
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=get_current_real_uid_user_name(),
        extra_job_lines='command = "rm -rf /"',
    )
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(config_path)


def test_load_rejects_missing_script(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{tmp_path}/nope.sh"
""",
    )
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(config_path)


def test_load_rejects_non_git_path(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{not_a_repo}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="not a git repository"):
        load_config(config_path)


def test_load_rejects_world_writable_script(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    script_path.chmod(script_path.stat().st_mode | stat.S_IWOTH)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="world-writable"):
        load_config(config_path)


def test_load_rejects_non_executable_script(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    script_path.chmod(0o644)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="not executable"):
        load_config(config_path)


def test_load_rejects_script_without_guard(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/bash\necho hi\n")
    script_path.chmod(0o755)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="guard"):
        load_config(config_path)


def test_load_rejects_duplicate_path(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "a"
path = "{repo_path}"
branch = "main"
script = "{script_path}"

[[jobs]]
name = "b"
path = "{repo_path}"
branch = "other"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="duplicate job path"):
        load_config(config_path)


def test_load_rejects_unsafe_branch(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main; rm -rf /"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="unsafe characters"):
        load_config(config_path)


def test_load_rejects_leading_dash_branch(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "--upload-pack=evil"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="unsafe characters"):
        load_config(config_path)


def test_load_rejects_zero_poll_interval(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 0

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
""",
    )
    with pytest.raises(ConfigError, match="poll_interval_seconds"):
        load_config(config_path)
