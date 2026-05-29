from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from fetch_runner.guard import PRESERVED_ENVIRONMENT_VARIABLE_NAMES
from fetch_runner.guard import GuardError
from fetch_runner.guard import get_current_real_uid_user_name
from fetch_runner.guard import render_canonical_script_guard
from fetch_runner.guard import render_sudo_argv
from fetch_runner.guard import require_expected_runtime_user
from fetch_runner.guard import validate_canonical_script_guard


def test_render_guard_embeds_user_everywhere():
    rendered_guard = render_canonical_script_guard("deploy")
    assert "DEPLOY_USER=deploy\n" in rendered_guard
    assert '"$(whoami)" != "$DEPLOY_USER"' in rendered_guard
    assert '"$(id -u)" -eq 0' in rendered_guard
    assert rendered_guard.endswith("# <<< fetch-runner-guard:END\n")


@pytest.mark.parametrize(
    "bad",
    ["", "a" * 64, "-root", "foo;bar", "he re", "ro ot", "user$name", "`id`"],
)
def test_render_guard_rejects_unsafe_user(bad):
    with pytest.raises(GuardError):
        render_canonical_script_guard(bad)


def _write_script_file(script_path: Path, script_body: str) -> Path:
    script_path.write_text(script_body)
    return script_path


def test_validate_script_guard_happy(tmp_path: Path):
    script_path = _write_script_file(
        tmp_path / "deploy.sh",
        "#!/bin/bash\n# a comment\n\n" + render_canonical_script_guard("deploy") + "echo hi\n",
    )
    assert validate_canonical_script_guard(script_path, "deploy").is_valid


def test_validate_script_guard_works_without_shebang(tmp_path: Path):
    script_path = _write_script_file(
        tmp_path / "s",
        render_canonical_script_guard("deploy") + "echo hi\n",
    )
    assert validate_canonical_script_guard(script_path, "deploy").is_valid


def test_validate_script_guard_only_comments_has_no_guard(tmp_path: Path):
    script_path = _write_script_file(tmp_path / "s.sh", "#!/bin/bash\n# nothing here\n\n")
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid
    assert "canonical guard block" in guard_validation.error_reason


def test_validate_script_guard_code_without_guard(tmp_path: Path):
    script_path = _write_script_file(tmp_path / "s.sh", "#!/bin/bash\necho hi\n")
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid
    assert "before any executable code" in guard_validation.error_reason


def test_validate_script_guard_wrong_user(tmp_path: Path):
    script_path = _write_script_file(
        tmp_path / "s.sh",
        "#!/bin/bash\n" + render_canonical_script_guard("otheruser") + "echo hi\n",
    )
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid
    assert "different user" in guard_validation.error_reason


def test_validate_script_guard_rejects_code_before_guard(tmp_path: Path):
    script_path = _write_script_file(
        tmp_path / "s.sh",
        "#!/bin/bash\nset -e\n" + render_canonical_script_guard("deploy") + "echo hi\n",
    )
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid
    assert "before any executable code" in guard_validation.error_reason


def test_validate_script_guard_detects_flipped_comparator(tmp_path: Path):
    # The most dangerous form of tampering: a check that always passes.
    canonical_guard = render_canonical_script_guard("deploy")
    tampered_guard = canonical_guard.replace('!= "$DEPLOY_USER"', '= "$DEPLOY_USER"')
    assert tampered_guard != canonical_guard
    script_path = _write_script_file(
        tmp_path / "s.sh",
        "#!/bin/bash\n" + tampered_guard + "echo hi\n",
    )
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid


def test_validate_script_guard_detects_removed_uid_check(tmp_path: Path):
    # Removing the root check: whoami would still match, but a root invocation
    # should be blocked. Tampering must be caught.
    canonical_guard = render_canonical_script_guard("deploy")
    tampered_guard = canonical_guard.replace(' || [ "$(id -u)" -eq 0 ]', "")
    assert tampered_guard != canonical_guard
    script_path = _write_script_file(
        tmp_path / "s.sh",
        "#!/bin/bash\n" + tampered_guard + "echo hi\n",
    )
    assert not validate_canonical_script_guard(script_path, "deploy").is_valid


def test_validate_script_guard_truncated(tmp_path: Path):
    canonical_guard_lines = render_canonical_script_guard("deploy").splitlines()
    truncated_guard = "\n".join(canonical_guard_lines[:-2]) + "\n"
    script_path = _write_script_file(tmp_path / "s.sh", "#!/bin/bash\n" + truncated_guard)
    guard_validation = validate_canonical_script_guard(script_path, "deploy")
    assert not guard_validation.is_valid


def test_validate_script_guard_missing_file(tmp_path: Path):
    guard_validation = validate_canonical_script_guard(tmp_path / "nope.sh", "deploy")
    assert not guard_validation.is_valid
    assert "cannot read" in guard_validation.error_reason


def test_current_user_matches_pwd_entry():
    assert get_current_real_uid_user_name() == pwd.getpwuid(os.getuid()).pw_name


def test_require_runtime_user_rejects_mismatch():
    with pytest.raises(GuardError):
        require_expected_runtime_user("definitely-not-this-user-xyz")


def test_require_runtime_user_accepts_match():
    # Running the test suite as the current user must succeed (uid != 0
    # in a sane test environment). Skip if someone is running tests as root.
    if os.getuid() == 0:
        pytest.skip("test suite is running as root")
    require_expected_runtime_user(get_current_real_uid_user_name())


def test_render_sudo_argv_shape(tmp_path: Path):
    script_path = tmp_path / "deploy.sh"
    argv = render_sudo_argv("app1", script_path)
    # Order matters: -n must come before sudo can prompt, -u must precede the
    # target user, --preserve-env must appear before --, and -- must precede
    # the script path so a path starting with `-` cannot be misread as a flag.
    assert argv[0] == "sudo"
    assert argv[1] == "-n"
    assert argv[2:4] == ["-u", "app1"]
    preserve_env_flag = argv[4]
    assert preserve_env_flag.startswith("--preserve-env=")
    preserved_names = preserve_env_flag[len("--preserve-env=") :].split(",")
    assert tuple(preserved_names) == PRESERVED_ENVIRONMENT_VARIABLE_NAMES
    assert argv[5] == "--"
    assert argv[6] == str(script_path)
    assert len(argv) == 7


@pytest.mark.parametrize("bad", ["", "-evil", "a;b", "user$x"])
def test_render_sudo_argv_rejects_unsafe_user(bad, tmp_path: Path):
    with pytest.raises(GuardError):
        render_sudo_argv(bad, tmp_path / "deploy.sh")
