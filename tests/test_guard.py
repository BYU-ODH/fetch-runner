from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from fetch_runner.guard import (
    GuardError,
    current_user,
    render_guard,
    require_runtime_user,
    validate_script_guard,
)


def test_render_guard_embeds_user_everywhere():
    g = render_guard("deploy")
    assert "user=deploy" in g
    assert '"$(whoami)" != "deploy"' in g
    assert '"$(id -u)" -eq 0' in g
    assert g.endswith("# <<< fetch-runner-guard:END\n")


@pytest.mark.parametrize(
    "bad",
    ["", "a" * 64, "-root", "foo;bar", "he re", "ro ot", "user$name", "`id`"],
)
def test_render_guard_rejects_unsafe_user(bad):
    with pytest.raises(GuardError):
        render_guard(bad)


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_validate_script_guard_happy(tmp_path: Path):
    script = _write(
        tmp_path / "deploy.sh",
        "#!/bin/bash\n# a comment\n\n" + render_guard("deploy") + "echo hi\n",
    )
    assert validate_script_guard(script, "deploy").ok


def test_validate_script_guard_works_without_shebang(tmp_path: Path):
    script = _write(tmp_path / "s", render_guard("deploy") + "echo hi\n")
    assert validate_script_guard(script, "deploy").ok


def test_validate_script_guard_only_comments_has_no_guard(tmp_path: Path):
    script = _write(tmp_path / "s.sh", "#!/bin/bash\n# nothing here\n\n")
    result = validate_script_guard(script, "deploy")
    assert not result.ok
    assert "canonical guard block" in result.reason


def test_validate_script_guard_code_without_guard(tmp_path: Path):
    script = _write(tmp_path / "s.sh", "#!/bin/bash\necho hi\n")
    result = validate_script_guard(script, "deploy")
    assert not result.ok
    assert "before any executable code" in result.reason


def test_validate_script_guard_wrong_user(tmp_path: Path):
    script = _write(tmp_path / "s.sh", "#!/bin/bash\n" + render_guard("otheruser") + "echo hi\n")
    result = validate_script_guard(script, "deploy")
    assert not result.ok
    assert "different user" in result.reason


def test_validate_script_guard_rejects_code_before_guard(tmp_path: Path):
    script = _write(
        tmp_path / "s.sh",
        "#!/bin/bash\nset -e\n" + render_guard("deploy") + "echo hi\n",
    )
    result = validate_script_guard(script, "deploy")
    assert not result.ok
    assert "before any executable code" in result.reason


def test_validate_script_guard_detects_flipped_comparator(tmp_path: Path):
    # The most dangerous form of tampering: a check that always passes.
    good = render_guard("deploy")
    tampered = good.replace('!= "deploy"', '== "deploy"')
    assert tampered != good
    script = _write(tmp_path / "s.sh", "#!/bin/bash\n" + tampered + "echo hi\n")
    result = validate_script_guard(script, "deploy")
    assert not result.ok


def test_validate_script_guard_detects_removed_uid_check(tmp_path: Path):
    # Removing the root check: whoami would still match, but a root invocation
    # should be blocked. Tampering must be caught.
    good = render_guard("deploy")
    tampered = good.replace(' || [ "$(id -u)" -eq 0 ]', "")
    assert tampered != good
    script = _write(tmp_path / "s.sh", "#!/bin/bash\n" + tampered + "echo hi\n")
    assert not validate_script_guard(script, "deploy").ok


def test_validate_script_guard_truncated(tmp_path: Path):
    good = render_guard("deploy").splitlines()
    truncated = "\n".join(good[:-2]) + "\n"
    script = _write(tmp_path / "s.sh", "#!/bin/bash\n" + truncated)
    result = validate_script_guard(script, "deploy")
    assert not result.ok


def test_validate_script_guard_missing_file(tmp_path: Path):
    result = validate_script_guard(tmp_path / "nope.sh", "deploy")
    assert not result.ok
    assert "cannot read" in result.reason


def test_current_user_matches_pwd_entry():
    assert current_user() == pwd.getpwuid(os.getuid()).pw_name


def test_require_runtime_user_rejects_mismatch():
    with pytest.raises(GuardError):
        require_runtime_user("definitely-not-this-user-xyz")


def test_require_runtime_user_accepts_match():
    # Running the test suite as the current user must succeed (uid != 0
    # in a sane test environment). Skip if someone is running tests as root.
    if os.getuid() == 0:
        pytest.skip("test suite is running as root")
    require_runtime_user(current_user())
