from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from fetch_runner.locking import JobBusyError
from fetch_runner.locking import acquire_job_lock


def test_acquire_job_lock_creates_lock_file(tmp_path: Path):
    with acquire_job_lock("j", lock_directory=tmp_path):
        assert (tmp_path / "j.lock").exists()


def test_acquire_job_lock_creates_missing_directory(tmp_path: Path):
    nested_lock_directory = tmp_path / "does" / "not" / "exist"
    with acquire_job_lock("j", lock_directory=nested_lock_directory):
        assert (nested_lock_directory / "j.lock").exists()


def test_acquire_job_lock_can_be_reacquired_after_release(tmp_path: Path):
    with acquire_job_lock("j", lock_directory=tmp_path):
        pass
    # Second acquisition succeeds because the first released on exit.
    with acquire_job_lock("j", lock_directory=tmp_path):
        pass


def test_acquire_job_lock_releases_on_exception(tmp_path: Path):
    with pytest.raises(RuntimeError):
        with acquire_job_lock("j", lock_directory=tmp_path):
            raise RuntimeError("boom")
    # Lock was released despite the exception, so a fresh acquisition works.
    with acquire_job_lock("j", lock_directory=tmp_path):
        pass


def test_acquire_job_lock_isolates_different_job_names(tmp_path: Path):
    with acquire_job_lock("job-a", lock_directory=tmp_path):
        # Different job name -> different lock file -> no contention.
        with acquire_job_lock("job-b", lock_directory=tmp_path):
            pass


def _hold_lock_in_child(
    lock_directory_str: str,
    acquired_event,
    release_event,
) -> None:
    with acquire_job_lock("j", lock_directory=Path(lock_directory_str)):
        acquired_event.set()
        release_event.wait(timeout=10)


def test_acquire_job_lock_raises_when_already_held(tmp_path: Path):
    # flock is per-fd, so same-process double-acquire would succeed. Use a
    # subprocess to model a real second tick of the runner.
    ctx = multiprocessing.get_context("fork")
    acquired_event = ctx.Event()
    release_event = ctx.Event()
    holder_process = ctx.Process(
        target=_hold_lock_in_child,
        args=(str(tmp_path), acquired_event, release_event),
    )
    holder_process.start()
    try:
        assert acquired_event.wait(timeout=5), "child never signaled lock acquisition"
        with pytest.raises(JobBusyError):
            with acquire_job_lock("j", lock_directory=tmp_path):
                pass
    finally:
        release_event.set()
        holder_process.join(timeout=5)
    # After the holder releases, the lock can be taken again.
    with acquire_job_lock("j", lock_directory=tmp_path):
        pass
