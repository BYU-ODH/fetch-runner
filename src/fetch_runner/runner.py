"""Polling loop.

Per job, on each tick:
  1. ``git fetch origin <branch>`` and read ``FETCH_HEAD``.
  2. If the SHA differs from the last one we acted on, re-validate the
     script's guard (in case the new commit tampered with it), hard-reset
     the working tree to that SHA, and run the script.
  3. Update the in-memory cursor whether the script succeeded or failed —
     a failing commit should not be re-run in a tight loop.

The initial cursor is the current local branch SHA, so restarting the
service does not replay the last deploy.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import threading

from .config import ConfiguredJob
from .config import RunnerConfig
from .git_ops import GitError
from .git_ops import git_fetch_branch_from_origin
from .git_ops import git_force_checkout_branch_to_commit
from .git_ops import git_get_local_branch_commit_sha
from .guard import render_sudo_argv
from .guard import validate_canonical_script_guard

log = logging.getLogger("fetch_runner")


class GitPollingRunner:
    def __init__(self, runner_config: RunnerConfig) -> None:
        self.runner_config = runner_config
        self._last_processed_commit_by_job_name: dict[str, str] = {}
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def run_forever(self) -> int:
        self._initialize_last_processed_commits()
        log.info(
            "fetch-runner started: user=%s jobs=%d poll=%ss",
            self.runner_config.runtime_user,
            len(self.runner_config.jobs),
            self.runner_config.poll_interval_seconds,
        )
        while not self._stop_requested.is_set():
            for configured_job in self.runner_config.jobs:
                if self._stop_requested.is_set():
                    break
                try:
                    self._poll_job_for_new_commit(configured_job)
                except Exception:
                    log.exception("job %s: unexpected error", configured_job.name)
            self._stop_requested.wait(self.runner_config.poll_interval_seconds)
        log.info("fetch-runner stopped")
        return 0

    def _initialize_last_processed_commits(self) -> None:
        for configured_job in self.runner_config.jobs:
            try:
                # Seed each job from the current local branch tip so a service
                # restart does not replay the last successfully fetched commit.
                initial_commit_sha = git_get_local_branch_commit_sha(
                    configured_job.repo_path,
                    configured_job.branch_name,
                    run_as_user_name=configured_job.run_as_user,
                )
            except GitError as e:
                log.warning(
                    "job %s: cannot read initial commit for %s: %s",
                    configured_job.name,
                    configured_job.branch_name,
                    e,
                )
                initial_commit_sha = ""
            self._last_processed_commit_by_job_name[configured_job.name] = initial_commit_sha
            log.info(
                "job %s: initial commit %s",
                configured_job.name,
                _short_commit_sha(initial_commit_sha),
            )

    def _poll_job_for_new_commit(self, configured_job: ConfiguredJob) -> None:
        try:
            fetched_commit_sha = git_fetch_branch_from_origin(
                configured_job.repo_path,
                configured_job.branch_name,
                run_as_user_name=configured_job.run_as_user,
            )
        except GitError as e:
            log.warning("job %s: fetch failed: %s", configured_job.name, e)
            return
        last_processed_commit_sha = self._last_processed_commit_by_job_name.get(
            configured_job.name,
            "",
        )
        if fetched_commit_sha == last_processed_commit_sha:
            log.debug(
                "job %s: no change (%s)",
                configured_job.name,
                _short_commit_sha(fetched_commit_sha),
            )
            return
        log.info(
            "job %s: new commit %s -> %s",
            configured_job.name,
            _short_commit_sha(last_processed_commit_sha) or "<init>",
            _short_commit_sha(fetched_commit_sha),
        )
        try:
            git_force_checkout_branch_to_commit(
                configured_job.repo_path,
                configured_job.branch_name,
                fetched_commit_sha,
                run_as_user_name=configured_job.run_as_user,
            )
        except GitError as e:
            log.error("job %s: checkout failed: %s", configured_job.name, e)
            return
        # Re-validate after checkout because the fetched commit controls the
        # script bytes (and mode) on disk. A config-time pass only proves the
        # *previous* checkout was safe.
        post_checkout_error = _post_checkout_script_problem(
            configured_job.script_path,
            configured_job.run_as_user,
        )
        if post_checkout_error is not None:
            log.error(
                "job %s: script at %s failed post-checkout validation: %s",
                configured_job.name,
                fetched_commit_sha,
                post_checkout_error,
            )
            # Record the bad commit so the service does not hammer the same
            # broken revision forever. Recovery should be an intentional human
            # action, not an automatic tight loop.
            self._last_processed_commit_by_job_name[configured_job.name] = fetched_commit_sha
            return
        self._run_job_script_for_commit(configured_job, fetched_commit_sha)
        self._last_processed_commit_by_job_name[configured_job.name] = fetched_commit_sha

    def _run_job_script_for_commit(
        self,
        configured_job: ConfiguredJob,
        commit_sha: str,
    ) -> None:
        # Export execution context so scripts can log or branch on it without
        # having to re-run git commands against the working tree.
        script_environment = {
            **os.environ,
            "FETCH_RUNNER_JOB": configured_job.name,
            "FETCH_RUNNER_BRANCH": configured_job.branch_name,
            "FETCH_RUNNER_COMMIT": commit_sha,
            "FETCH_RUNNER_REPO": str(configured_job.repo_path),
        }
        # When the script runs as the polling user, invoke it directly: no sudo
        # rule is required and existing single-user setups keep working with no
        # operator changes. Otherwise dispatch through sudo, which crosses the
        # privilege boundary into the per-job ``run_as`` user.
        if configured_job.run_as_user == self.runner_config.runtime_user:
            script_argv = [str(configured_job.script_path)]
        else:
            script_argv = render_sudo_argv(
                configured_job.run_as_user,
                configured_job.script_path,
            )
        log.info(
            "job %s: running %s as %s",
            configured_job.name,
            configured_job.script_path,
            configured_job.run_as_user,
        )
        try:
            completed_process = subprocess.run(
                script_argv,
                cwd=configured_job.repo_path,
                env=script_environment,
                timeout=configured_job.script_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.error(
                "job %s: script timed out after %ss",
                configured_job.name,
                configured_job.script_timeout_seconds,
            )
            return
        except OSError as e:
            log.error("job %s: cannot execute script: %s", configured_job.name, e)
            return
        if completed_process.returncode == 0:
            log.info("job %s: script succeeded", configured_job.name)
        else:
            log.error("job %s: script exited %d", configured_job.name, completed_process.returncode)


def _short_commit_sha(commit_sha: str) -> str:
    return commit_sha[:12]


def _post_checkout_script_problem(script_path, run_as_user_name: str) -> str | None:
    """Return a human-readable reason the freshly checked-out script is unsafe,
    or ``None`` if it passes every check.

    Re-runs the file-state checks from config load (exists, executable,
    not world-writable) plus the guard check. A malicious or accidental commit
    can change any of these between deploys; config-time validation alone
    cannot catch a tampered post-checkout state.
    """
    if not script_path.is_file():
        return f"{script_path} does not exist"
    if not os.access(script_path, os.X_OK):
        return f"{script_path} is not executable"
    if script_path.stat().st_mode & stat.S_IWOTH:
        return f"{script_path} is world-writable"
    guard_validation = validate_canonical_script_guard(script_path, run_as_user_name)
    if not guard_validation.is_valid:
        return guard_validation.error_reason
    return None
