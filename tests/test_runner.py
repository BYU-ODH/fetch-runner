from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from fetch_runner.config import ConfiguredJob
from fetch_runner.config import RunnerConfig
from fetch_runner.guard import get_current_real_uid_user_name
from fetch_runner.runner import GitPollingRunner


@pytest.fixture(autouse=True)
def _not_running_as_root():
    if os.getuid() == 0:
        pytest.skip("runner tests require a non-root test user")


def _make_runner(
    run_as_user: str,
    script_path: Path,
    runtime_user: str | None = None,
    script_args: tuple[str, ...] = (),
):
    runtime_user_name = runtime_user or get_current_real_uid_user_name()
    runner_config = RunnerConfig(
        runtime_user=runtime_user_name,
        poll_interval_seconds=60,
        jobs=(
            ConfiguredJob(
                name="j",
                repo_path=script_path.parent,
                branch_name="main",
                script_path=script_path,
                script_args=script_args,
                script_timeout_seconds=None,
                run_as_user=run_as_user,
            ),
        ),
    )
    return GitPollingRunner(runner_config), runner_config.jobs[0]


def test_runner_invokes_script_directly_when_run_as_matches_runtime_user(tmp_path: Path):
    # Single-user mode: no sudo, script is exec'd directly so no sudoers
    # configuration is required for an unchanged jobs.toml.
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/sh\n")
    script_path.chmod(0o755)
    current_user_name = get_current_real_uid_user_name()
    runner, job = _make_runner(run_as_user=current_user_name, script_path=script_path)
    with mock.patch("fetch_runner.runner.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0)
        runner._run_job_script_for_commit(job, "main", "deadbeef" * 5)
    invocation_argv = mocked_subprocess_run.call_args.args[0]
    assert invocation_argv == [str(script_path)]


def test_runner_invokes_script_via_sudo_when_run_as_differs(tmp_path: Path):
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/sh\n")
    script_path.chmod(0o755)
    runner, job = _make_runner(run_as_user="someone-else", script_path=script_path)
    with mock.patch("fetch_runner.runner.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0)
        runner._run_job_script_for_commit(job, "main", "cafef00d" * 5)
    invocation_argv = mocked_subprocess_run.call_args.args[0]
    assert invocation_argv[0] == "sudo"
    assert invocation_argv[1] == "-n"
    assert invocation_argv[2:4] == ["-u", "someone-else"]
    assert invocation_argv[-2] == "--"
    assert invocation_argv[-1] == str(script_path)


def test_runner_appends_script_args_in_direct_mode(tmp_path: Path):
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/sh\n")
    script_path.chmod(0o755)
    current_user_name = get_current_real_uid_user_name()
    runner, job = _make_runner(
        run_as_user=current_user_name,
        script_path=script_path,
        script_args=("--env=prod", "frontend"),
    )
    with mock.patch("fetch_runner.runner.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0)
        runner._run_job_script_for_commit(job, "main", "deadbeef" * 5)
    invocation_argv = mocked_subprocess_run.call_args.args[0]
    assert invocation_argv == [str(script_path), "--env=prod", "frontend"]


def test_runner_appends_script_args_after_sudo_dashdash(tmp_path: Path):
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/sh\n")
    script_path.chmod(0o755)
    runner, job = _make_runner(
        run_as_user="someone-else",
        script_path=script_path,
        script_args=("--env=prod", "frontend"),
    )
    with mock.patch("fetch_runner.runner.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0)
        runner._run_job_script_for_commit(job, "main", "cafef00d" * 5)
    invocation_argv = mocked_subprocess_run.call_args.args[0]
    dashdash_index = invocation_argv.index("--")
    assert invocation_argv[dashdash_index + 1 :] == [
        str(script_path),
        "--env=prod",
        "frontend",
    ]


def test_runner_passes_fetch_runner_env_vars_through_subprocess(tmp_path: Path):
    script_path = tmp_path / "deploy.sh"
    script_path.write_text("#!/bin/sh\n")
    script_path.chmod(0o755)
    runner, job = _make_runner(run_as_user="someone-else", script_path=script_path)
    with mock.patch("fetch_runner.runner.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0)
        runner._run_job_script_for_commit(job, "main", "1234567890ab")
    passed_env = mocked_subprocess_run.call_args.kwargs["env"]
    assert passed_env["FETCH_RUNNER_JOB"] == "j"
    assert passed_env["FETCH_RUNNER_BRANCH"] == "main"
    assert passed_env["FETCH_RUNNER_COMMIT"] == "1234567890ab"
    assert passed_env["FETCH_RUNNER_REPO"] == str(script_path.parent)
