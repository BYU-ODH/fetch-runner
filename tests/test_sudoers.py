from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fetch_runner.cli import render_sudoers_fragment
from fetch_runner.config import ConfiguredJob
from fetch_runner.config import RunnerConfig


def _make_runner_config(*jobs: ConfiguredJob) -> RunnerConfig:
    return RunnerConfig(
        runtime_user="fetch-runner",
        poll_interval_seconds=60,
        jobs=tuple(jobs),
    )


def _make_job(
    name: str,
    run_as_user: str,
    script_path: str = "/srv/app1/repo/deploy.sh",
    script_args: tuple[str, ...] = (),
) -> ConfiguredJob:
    return ConfiguredJob(
        name=name,
        repo_path=Path(script_path).parent,
        branch_name="main",
        script_path=Path(script_path),
        script_args=script_args,
        script_timeout_seconds=None,
        run_as_user=run_as_user,
    )


def test_render_sudoers_fragment_empty_when_all_jobs_match_runtime_user():
    # No cross-user jobs means no sudoers rules are required at all.
    runner_config = _make_runner_config(_make_job("j", run_as_user="fetch-runner"))
    rendered_fragment = render_sudoers_fragment(runner_config)
    assert "Defaults!" not in rendered_fragment
    assert "NOPASSWD" not in rendered_fragment
    assert "no sudoers rules needed" in rendered_fragment


def test_render_sudoers_fragment_emits_lines_for_cross_user_jobs():
    if shutil.which("git") is None:
        pytest.skip("git not on PATH; cannot render git sudoers lines")
    git_absolute_path = shutil.which("git")
    runner_config = _make_runner_config(
        _make_job("api", run_as_user="app1", script_path="/srv/app1/api/deploy.sh"),
        _make_job("web", run_as_user="app2", script_path="/srv/app2/web/deploy.sh"),
    )
    rendered_fragment = render_sudoers_fragment(runner_config)
    assert (
        'Defaults!/srv/app1/api/deploy.sh env_keep += "FETCH_RUNNER_JOB FETCH_RUNNER_BRANCH '
        'FETCH_RUNNER_COMMIT FETCH_RUNNER_REPO"'
    ) in rendered_fragment
    # One git rule per unique run_as user; pinned absolutely to the resolved
    # git binary so the rule cannot be sidestepped by a different git on PATH.
    assert f"fetch-runner ALL=(app1) NOPASSWD: {git_absolute_path}" in rendered_fragment
    assert f"fetch-runner ALL=(app2) NOPASSWD: {git_absolute_path}" in rendered_fragment
    assert "fetch-runner ALL=(app1) NOPASSWD: /srv/app1/api/deploy.sh" in rendered_fragment
    assert "fetch-runner ALL=(app2) NOPASSWD: /srv/app2/web/deploy.sh" in rendered_fragment


def test_render_sudoers_fragment_deduplicates_shared_script_and_runas():
    # Two jobs that share the same (run_as, script) should not produce two
    # identical NOPASSWD lines — sudoers would still parse but a diff against
    # the running file would be noisy.
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    git_absolute_path = shutil.which("git")
    runner_config = _make_runner_config(
        _make_job("a", run_as_user="app1", script_path="/srv/app1/shared/deploy.sh"),
        _make_job("b", run_as_user="app1", script_path="/srv/app1/shared/deploy.sh"),
    )
    rendered_fragment = render_sudoers_fragment(runner_config)
    assert rendered_fragment.count("Defaults!/srv/app1/shared/deploy.sh") == 1
    assert rendered_fragment.count("NOPASSWD: /srv/app1/shared/deploy.sh") == 1
    # Two jobs share the same run_as, so only one git rule should be emitted.
    assert rendered_fragment.count(f"NOPASSWD: {git_absolute_path}") == 1


def test_render_sudoers_fragment_includes_script_args(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    runner_config = _make_runner_config(
        _make_job(
            "api",
            run_as_user="app1",
            script_path="/srv/app1/api/deploy.sh",
            script_args=("--env=prod", "frontend"),
        ),
    )
    rendered_fragment = render_sudoers_fragment(runner_config)
    # `=` is a sudoers special and must be backslash-escaped to appear
    # literally in the matched command spec.
    expected_command_spec = "/srv/app1/api/deploy.sh --env\\=prod frontend"
    assert f"fetch-runner ALL=(app1) NOPASSWD: {expected_command_spec}" in rendered_fragment
    assert f"Defaults!{expected_command_spec} env_keep" in rendered_fragment


def test_render_sudoers_fragment_separates_same_script_with_different_args(tmp_path: Path):
    # Two jobs invoking the same script with different args must produce two
    # distinct NOPASSWD rules — sudo matches args literally.
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    runner_config = _make_runner_config(
        _make_job(
            "a",
            run_as_user="app1",
            script_path="/srv/app1/api/deploy.sh",
            script_args=("frontend",),
        ),
        _make_job(
            "b",
            run_as_user="app1",
            script_path="/srv/app1/api/deploy.sh",
            script_args=("backend",),
        ),
    )
    rendered_fragment = render_sudoers_fragment(runner_config)
    assert "NOPASSWD: /srv/app1/api/deploy.sh frontend" in rendered_fragment
    assert "NOPASSWD: /srv/app1/api/deploy.sh backend" in rendered_fragment


def test_render_sudoers_fragment_skips_matching_runas_but_keeps_diverging(tmp_path: Path):
    runner_config = _make_runner_config(
        _make_job(
            "same",
            run_as_user="fetch-runner",
            script_path="/srv/fetch-runner/x/deploy.sh",
        ),
        _make_job("diff", run_as_user="app1", script_path="/srv/app1/y/deploy.sh"),
    )
    rendered_fragment = render_sudoers_fragment(runner_config)
    assert "/srv/fetch-runner/x/deploy.sh" not in rendered_fragment
    assert "fetch-runner ALL=(app1) NOPASSWD: /srv/app1/y/deploy.sh" in rendered_fragment
