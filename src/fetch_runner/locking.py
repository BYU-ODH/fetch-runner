"""Per-job advisory locks via ``flock``.

Prevents poll-tick overlap: if a deploy script runs longer than the poll
interval, the next tick must not start a second deploy on top of the first.
The lock is non-blocking — a busy second invocation raises immediately so
the runner can log and skip rather than queue work.

One lock file per job (``<lock_directory>/<job_name>.lock``) keeps
unrelated jobs running in parallel; only the same job serializes.

The lock state lives in the kernel (``fcntl.flock``), not in the file's
contents, so the lock file is left on disk after release. Unlinking it
would race with the next acquisition: ``A`` unlinks → ``B`` creates a
fresh inode → ``C`` opens the original (now unlinked) inode and would see
no contention.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOCK_DIRECTORY = Path("/run/fetch-runner")


class JobBusyError(Exception):
    """Another process already holds the job lock."""


@contextmanager
def acquire_job_lock(
    job_name: str,
    lock_directory: Path = DEFAULT_LOCK_DIRECTORY,
) -> Iterator[None]:
    """Hold an exclusive non-blocking flock on the job's lock file.

    Raises :class:`JobBusyError` if the lock is already held. The lock is
    released on context exit (whether via normal return or exception).
    """
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock_path = lock_directory / f"{job_name}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise JobBusyError(job_name) from None
            raise
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
