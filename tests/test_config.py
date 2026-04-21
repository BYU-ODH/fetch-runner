from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fetch_runner.config import ConfigError
from fetch_runner.config import load_config
from fetch_runner.guard import current_user
from fetch_runner.guard import render_guard


@pytest.fixture(autouse=True)
def _not_running_as_root():
    if os.getuid() == 0:
        pytest.skip("config tests require a non-root test user")


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _make_script(path: Path, user: str) -> Path:
    path.write_text("#!/bin/bash\n" + render_guard(user) + "echo hi\n")
    path.chmod(0o755)
    return path


def _write_cfg(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def _base_cfg(tmp_path: Path, *, user: str, extra_job_lines: str = "") -> Path:
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    return _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 30

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
{extra_job_lines}
""",
    )


def test_load_happy_path(tmp_path: Path):
    user = current_user()
    cfg_path = _base_cfg(tmp_path, user=user)
    cfg = load_config(cfg_path)
    assert cfg.user == user
    assert cfg.poll_interval_seconds == 30
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "j1"
    assert cfg.jobs[0].branch == "main"
    assert cfg.jobs[0].timeout_seconds is None


def test_load_rejects_wrong_user(tmp_path: Path):
    # A jobs.toml whose user does not match the running user must be refused.
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", "someone-else-xyz")
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "someone-else-xyz"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="runtime user"):
        load_config(cfg_path)


def test_load_rejects_unknown_top_level_key(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[extras]
something = 1

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(cfg_path)


def test_load_rejects_unknown_job_key(tmp_path: Path):
    cfg_path = _base_cfg(tmp_path, user=current_user(), extra_job_lines='command = "rm -rf /"')
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(cfg_path)


def test_load_rejects_missing_script(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{tmp_path}/nope.sh"
""",
    )
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(cfg_path)


def test_load_rejects_non_git_path(tmp_path: Path):
    user = current_user()
    script = _make_script(tmp_path / "deploy.sh", user)
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{not_a_repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="not a git repository"):
        load_config(cfg_path)


def test_load_rejects_world_writable_script(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    script.chmod(script.stat().st_mode | stat.S_IWOTH)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="world-writable"):
        load_config(cfg_path)


def test_load_rejects_non_executable_script(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    script.chmod(0o644)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="not executable"):
        load_config(cfg_path)


def test_load_rejects_script_without_guard(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = tmp_path / "deploy.sh"
    script.write_text("#!/bin/bash\necho hi\n")
    script.chmod(0o755)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="guard"):
        load_config(cfg_path)


def test_load_rejects_duplicate_path(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "a"
path = "{repo}"
branch = "main"
script = "{script}"

[[jobs]]
name = "b"
path = "{repo}"
branch = "other"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="duplicate job path"):
        load_config(cfg_path)


def test_load_rejects_unsafe_branch(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main; rm -rf /"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="unsafe characters"):
        load_config(cfg_path)


def test_load_rejects_leading_dash_branch(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 5

[[jobs]]
name = "j1"
path = "{repo}"
branch = "--upload-pack=evil"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="unsafe characters"):
        load_config(cfg_path)


def test_load_rejects_zero_poll_interval(tmp_path: Path):
    user = current_user()
    repo = _make_repo(tmp_path / "repo")
    script = _make_script(tmp_path / "deploy.sh", user)
    cfg_path = _write_cfg(
        tmp_path / "jobs.toml",
        f"""
[general]
user = "{user}"
poll_interval_seconds = 0

[[jobs]]
name = "j1"
path = "{repo}"
branch = "main"
script = "{script}"
""",
    )
    with pytest.raises(ConfigError, match="poll_interval_seconds"):
        load_config(cfg_path)
