from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from fetch_runner.git_ops import _build_git_argv
from fetch_runner.git_ops import git_fetch_branch_from_origin
from fetch_runner.guard import get_current_real_uid_user_name


@pytest.fixture(autouse=True)
def _not_running_as_root():
    if os.getuid() == 0:
        pytest.skip("git_ops tests require a non-root test user")


def test_build_git_argv_direct_when_run_as_matches_process_user(tmp_path: Path):
    current_user_name = get_current_real_uid_user_name()
    argv = _build_git_argv(tmp_path, current_user_name, ("rev-parse", "HEAD"))
    # No sudo wrapper at all — fetch-runner runs git directly when the
    # process user already matches run_as.
    assert argv[0] != "sudo"
    assert argv[0].endswith("git")
    assert argv[1:] == ["-C", str(tmp_path), "rev-parse", "HEAD"]


def test_build_git_argv_sudo_wraps_when_run_as_differs(tmp_path: Path):
    argv = _build_git_argv(tmp_path, "someone-else", ("fetch", "origin", "main"))
    assert argv[0] == "sudo"
    assert argv[1] == "-n"
    assert argv[2:4] == ["-u", "someone-else"]
    assert argv[4] == "--"
    assert argv[5].endswith("git")
    assert argv[6:] == ["-C", str(tmp_path), "fetch", "origin", "main"]


def test_build_git_argv_uses_absolute_git_path(tmp_path: Path):
    # Sudoers Commands rules require absolute paths, so the argv must use the
    # resolved git binary path, not the bare name. Skip if git isn't on PATH
    # (e.g. minimal CI image) — the test has nothing to verify there.
    git_absolute_path = shutil.which("git")
    if git_absolute_path is None:
        pytest.skip("git not on PATH")
    argv_same_user = _build_git_argv(tmp_path, get_current_real_uid_user_name(), ("status",))
    assert argv_same_user[0] == git_absolute_path
    argv_cross_user = _build_git_argv(tmp_path, "someone-else", ("status",))
    assert argv_cross_user[5] == git_absolute_path


def test_git_fetch_passes_through_to_subprocess_with_sudo_when_run_as_differs(tmp_path: Path):
    # Drive a real git_ops function to make sure run_as_user_name is plumbed
    # all the way through, not just the helper.
    with mock.patch("fetch_runner.git_ops.subprocess.run") as mocked_subprocess_run:
        mocked_subprocess_run.return_value = mock.Mock(returncode=0, stdout="cafef00d\n", stderr="")
        git_fetch_branch_from_origin(
            tmp_path,
            "main",
            run_as_user_name="someone-else",
        )
    # Two calls: `git fetch ...` then `git rev-parse FETCH_HEAD`. Both must
    # be sudo-wrapped to the same target user.
    assert mocked_subprocess_run.call_count == 2
    for invocation_call in mocked_subprocess_run.call_args_list:
        invocation_argv = invocation_call.args[0]
        assert invocation_argv[0] == "sudo"
        assert invocation_argv[2:4] == ["-u", "someone-else"]
