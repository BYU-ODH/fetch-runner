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

from fetch_runner.config import Config, Job
from fetch_runner.git_ops import GitError, checkout, current_commit, fetch
from fetch_runner.guard import validate_script_guard

log = logging.getLogger("fetch_runner")


class Runner:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._last_commit: dict[str, str] = {}
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> int:
        self._initialize_cursors()
        log.info(
            "fetch-runner started: user=%s jobs=%d poll=%ss",
            self.cfg.user,
            len(self.cfg.jobs),
            self.cfg.poll_interval_seconds,
        )
        while not self._stop.is_set():
            for job in self.cfg.jobs:
                if self._stop.is_set():
                    break
                try:
                    self._poll(job)
                except Exception:
                    log.exception("job %s: unexpected error", job.name)
            self._stop.wait(self.cfg.poll_interval_seconds)
        log.info("fetch-runner stopped")
        return 0

    def _initialize_cursors(self) -> None:
        for job in self.cfg.jobs:
            try:
                sha = current_commit(job.path, job.branch)
            except GitError as e:
                log.warning(
                    "job %s: cannot read initial commit for %s: %s",
                    job.name,
                    job.branch,
                    e,
                )
                sha = ""
            self._last_commit[job.name] = sha
            log.info("job %s: initial commit %s", job.name, _short(sha))

    def _poll(self, job: Job) -> None:
        try:
            remote = fetch(job.path, job.branch)
        except GitError as e:
            log.warning("job %s: fetch failed: %s", job.name, e)
            return
        last = self._last_commit.get(job.name, "")
        if remote == last:
            log.debug("job %s: no change (%s)", job.name, _short(remote))
            return
        log.info(
            "job %s: new commit %s -> %s",
            job.name,
            _short(last) or "<init>",
            _short(remote),
        )
        try:
            checkout(job.path, job.branch, remote)
        except GitError as e:
            log.error("job %s: checkout failed: %s", job.name, e)
            return
        # Re-validate the guard after checkout: the incoming commit could
        # have removed or weakened it.
        check = validate_script_guard(job.script, self.cfg.user)
        if not check.ok:
            log.error(
                "job %s: script at %s failed guard check after checkout: %s",
                job.name,
                remote,
                check.reason,
            )
            self._last_commit[job.name] = remote
            return
        self._run_script(job, remote)
        self._last_commit[job.name] = remote

    def _run_script(self, job: Job, commit: str) -> None:
        env = {
            **os.environ,
            "FETCH_RUNNER_JOB": job.name,
            "FETCH_RUNNER_BRANCH": job.branch,
            "FETCH_RUNNER_COMMIT": commit,
            "FETCH_RUNNER_REPO": str(job.path),
        }
        log.info("job %s: running %s", job.name, job.script)
        try:
            proc = subprocess.run(
                [str(job.script)],
                cwd=job.path,
                env=env,
                timeout=job.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.error("job %s: script timed out after %ss", job.name, job.timeout_seconds)
            return
        except OSError as e:
            log.error("job %s: cannot execute script: %s", job.name, e)
            return
        if proc.returncode == 0:
            log.info("job %s: script succeeded", job.name)
        else:
            log.error("job %s: script exited %d", job.name, proc.returncode)


def _short(sha: str) -> str:
    return sha[:12]
