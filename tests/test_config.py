from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

import pytest

from fetch_runner.config import ConfigError
from fetch_runner.config import load_config
from fetch_runner.guard import get_current_real_uid_user_name
from fetch_runner.guard import render_canonical_script_guard


def _pick_other_real_user_or_skip() -> str:
    """Return a real user name that is *not* the current user, for run_as tests.

    ``pwd.getpwnam`` must succeed for ``run_as`` validation, so we can't fake
    one. ``nobody`` is conventional on macOS and Linux but not strictly
    required; skip the test if nothing suitable exists.
    """
    current_user_name = get_current_real_uid_user_name()
    for candidate in ("nobody", "daemon"):
        try:
            pwd.getpwnam(candidate)
        except KeyError:
            continue
        if candidate != current_user_name:
            return candidate
    pytest.skip("no second real user available for run_as test")


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


def test_load_allows_branch_to_be_omitted(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
script = "{script_path}"
""",
    )
    runner_config = load_config(config_path)
    assert runner_config.jobs[0].branch_name is None


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


def test_load_run_as_defaults_to_general_user(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(tmp_path, user_name=user_name)
    runner_config = load_config(config_path)
    assert runner_config.jobs[0].run_as_user == user_name


def test_load_accepts_per_job_run_as(tmp_path: Path):
    runtime_user_name = get_current_real_uid_user_name()
    run_as_user_name = _pick_other_real_user_or_skip()
    repo_path = _create_repo_directory(tmp_path / "repo")
    # The guard text must match the *run-as* user, not the runtime user.
    script_path = _create_guarded_script(tmp_path / "deploy.sh", run_as_user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{runtime_user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
run_as = "{run_as_user_name}"
""",
    )
    runner_config = load_config(config_path)
    assert runner_config.runtime_user == runtime_user_name
    assert runner_config.jobs[0].run_as_user == run_as_user_name


def test_load_rejects_run_as_user_not_in_passwd(tmp_path: Path):
    runtime_user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", "ghost-user-xyz-9999")
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{runtime_user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
run_as = "ghost-user-xyz-9999"
""",
    )
    with pytest.raises(ConfigError, match="does not exist on this system"):
        load_config(config_path)


def test_load_rejects_unsafe_run_as_value(tmp_path: Path):
    runtime_user_name = get_current_real_uid_user_name()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", runtime_user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{runtime_user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
run_as = "evil; rm -rf /"
""",
    )
    with pytest.raises(ConfigError, match="run_as"):
        load_config(config_path)


def test_load_rejects_guard_naming_runtime_user_when_run_as_differs(tmp_path: Path):
    # The script's guard must match the run-as user. A guard that still names
    # the runtime user should be refused once run_as diverges.
    runtime_user_name = get_current_real_uid_user_name()
    run_as_user_name = _pick_other_real_user_or_skip()
    repo_path = _create_repo_directory(tmp_path / "repo")
    script_path = _create_guarded_script(tmp_path / "deploy.sh", runtime_user_name)
    config_path = _write_jobs_toml(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{runtime_user_name}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo_path}"
branch = "main"
script = "{script_path}"
run_as = "{run_as_user_name}"
""",
    )
    with pytest.raises(ConfigError, match="guard"):
        load_config(config_path)


def test_load_args_default_to_empty(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(tmp_path, user_name=user_name)
    runner_config = load_config(config_path)
    assert runner_config.jobs[0].script_args == ()


def test_load_accepts_valid_args(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=user_name,
        extra_job_lines='args = ["--env=prod", "frontend"]',
    )
    runner_config = load_config(config_path)
    assert runner_config.jobs[0].script_args == ("--env=prod", "frontend")


def test_load_rejects_args_with_shell_metacharacters(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=user_name,
        extra_job_lines='args = ["frontend; rm -rf /"]',
    )
    with pytest.raises(ConfigError, match="disallowed characters"):
        load_config(config_path)


def test_load_rejects_args_with_whitespace(tmp_path: Path):
    # Whitespace would split tokens in a sudoers rule, so embedded spaces in
    # a single arg are refused — operators should pass two separate strings.
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=user_name,
        extra_job_lines='args = ["hello world"]',
    )
    with pytest.raises(ConfigError, match="disallowed characters"):
        load_config(config_path)


def test_load_rejects_args_not_an_array(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=user_name,
        extra_job_lines='args = "frontend"',
    )
    with pytest.raises(ConfigError, match="array of strings"):
        load_config(config_path)


def test_load_rejects_empty_arg(tmp_path: Path):
    user_name = get_current_real_uid_user_name()
    config_path = _write_minimal_valid_jobs_toml(
        tmp_path,
        user_name=user_name,
        extra_job_lines='args = ["ok", ""]',
    )
    with pytest.raises(ConfigError, match="non-empty string"):
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
