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
import subprocess
import threading

from .config import ConfiguredJob
from .config import RunnerConfig
from .git_ops import GitError
from .git_ops import git_fetch_branch_from_origin
from .git_ops import git_force_checkout_branch_to_commit
from .git_ops import git_get_local_branch_commit_sha
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
            )
        except GitError as e:
            log.error("job %s: checkout failed: %s", configured_job.name, e)
            return
        # Re-validate after checkout because the fetched commit controls the
        # script bytes on disk. A config-time pass only proves the *previous*
        # checkout was safe.
        guard_validation = validate_canonical_script_guard(
            configured_job.script_path,
            self.runner_config.runtime_user,
        )
        if not guard_validation.is_valid:
            log.error(
                "job %s: script at %s failed guard check after checkout: %s",
                configured_job.name,
                fetched_commit_sha,
                guard_validation.error_reason,
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
        log.info("job %s: running %s", configured_job.name, configured_job.script_path)
        try:
            completed_process = subprocess.run(
                [str(configured_job.script_path)],
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
