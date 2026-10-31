"""The fleet `policy.toml`: one fragment per family, one checked assembly
(chaperone design section 6)."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import threading
import tomllib
from pathlib import Path
from typing import Final

import pytest
from caregiver.policy_file import App, Limits, PolicyFile, render_app, render_policy
from caregiver.policy_fleet import (
    INSTALL_FILE,
    POLICY_FILE_MAX_BYTES,
    ChaperoneChecker,
    CheckerError,
    FakeChecker,
    FleetOutcome,
    Passed,
    PolicyTarget,
    Refused,
    TargetFile,
    assemble_policy,
    remove_fragment,
    write_fragment,
)

from caregiver import paths

INSTALL_TEXT: Final = "version = 1\n\n[plugins.kagi]\n"
LIMITS: Final = Limits(calls_per_minute=60, max_pending_gates=10)
STUB_OK: Final = '#!/bin/sh\necho "$@" > "$(dirname "$0")/args"\n'


def app_of(family: str, note: str | None = None) -> App:
    digest = hashlib.sha256(family.encode("ascii")).hexdigest()
    return App(token_sha256=(digest,), limits=LIMITS, note=note)


@pytest.fixture
def target(tmp_path: Path) -> PolicyTarget:
    config = tmp_path / "etc-chaperone"
    config.mkdir()
    (config / INSTALL_FILE).write_text(INSTALL_TEXT, encoding="utf-8")
    return PolicyTarget(config)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def policy_text(target: PolicyTarget) -> str:
    return target.path(TargetFile.POLICY).read_text(encoding="utf-8")


def test_the_cap_is_the_rust_policy_file_cap() -> None:
    """`POLICY_FILE_CAP` of `crates/core/src/policy.rs`: 1 MiB."""
    assert POLICY_FILE_MAX_BYTES == 1024 * 1024


def test_fragments_join_into_one_policy_file(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "code", app_of("code"))
    write_fragment(root, "chat", app_of("chat"))
    result = assemble_policy(root, target, FakeChecker())
    assert result.outcome is FleetOutcome.WRITTEN
    assert result.left_out == {}
    expected = render_policy(PolicyFile({"chat": app_of("chat"), "code": app_of("code")}))
    assert policy_text(target) == expected


def test_a_fragment_holds_one_app_table(root: Path) -> None:
    write_fragment(root, "chat", app_of("chat"))
    text = paths.policy_fragment_path(root, "chat").read_text(encoding="utf-8")
    assert text == render_app("chat", app_of("chat"))


def test_a_fragment_that_fails_alone_is_left_out(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    write_fragment(root, "code", app_of("code"))
    result = assemble_policy(root, target, FakeChecker(refused_apps=frozenset({"code"})))
    assert result.outcome is FleetOutcome.WRITTEN
    assert result.left_out == {"code": "the app code is refused"}
    assert list(tomllib.loads(policy_text(target))["apps"]) == ["chat"]


def test_fragments_that_clash_write_nothing(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    assert assemble_policy(root, target, FakeChecker()).outcome is FleetOutcome.WRITTEN
    before = policy_text(target)
    write_fragment(root, "code", app_of("code"))
    checker = FakeChecker(clashes=(frozenset({"chat", "code"}),))
    result = assemble_policy(root, target, checker)
    assert result.outcome is FleetOutcome.REFUSED
    assert result.error == "the apps chat and code clash"
    assert result.left_out == {}
    assert policy_text(target) == before
    assert not target.path(TargetFile.PREV).exists()


def test_named_clashing_apps_are_left_out_and_the_rest_written(
    root: Path, target: PolicyTarget
) -> None:
    """`chaperone check` 0.4.0 names each app it refuses. Those apps are
    left out, and one clash no longer stops the write of the others."""
    for family in ("chat", "code", "ops"):
        write_fragment(root, family, app_of(family))
    checker = FakeChecker(clashes=(frozenset({"chat", "code"}),), names_clashes=True)

    result = assemble_policy(root, target, checker)

    assert result.outcome is FleetOutcome.WRITTEN
    assert set(result.left_out) == {"chat", "code"}
    assert result.left_out["chat"] == "the app chat is refused: the apps chat and code clash"
    assert set(tomllib.loads(policy_text(target))["apps"]) == {"ops"}


def test_a_refused_base_marks_no_family(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    result = assemble_policy(root, target, FakeChecker(broken="install.toml: bad"))
    assert result.outcome is FleetOutcome.REFUSED
    assert result.error == "install.toml: bad"
    assert result.left_out == {}
    assert not target.path(TargetFile.POLICY).exists()


def test_an_unchecked_file_is_never_renamed(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    result = assemble_policy(root, target, FakeChecker(unavailable=True))
    assert result.outcome is FleetOutcome.REFUSED
    assert result.error is not None
    assert sorted(path.name for path in target.config_dir.iterdir()) == [INSTALL_FILE]


def test_each_written_text_was_checked(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    write_fragment(root, "code", app_of("code"))
    checker = FakeChecker(refused_apps=frozenset({"code"}))
    assemble_policy(root, target, checker)
    assert checker.checked[-1] == policy_text(target)


def test_a_fragment_over_the_cap_is_left_out(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    write_fragment(root, "code", app_of("code", note="x" * POLICY_FILE_MAX_BYTES))
    checker = FakeChecker()
    result = assemble_policy(root, target, checker)
    assert result.outcome is FleetOutcome.WRITTEN
    assert "bytes" in result.left_out["code"]
    assert all(len(text.encode()) <= POLICY_FILE_MAX_BYTES for text in checker.checked)


def test_a_fleet_over_the_cap_writes_nothing(root: Path, target: PolicyTarget) -> None:
    half = "x" * (POLICY_FILE_MAX_BYTES // 2)
    write_fragment(root, "chat", app_of("chat", note=half))
    write_fragment(root, "code", app_of("code", note=half))
    result = assemble_policy(root, target, FakeChecker())
    assert result.outcome is FleetOutcome.REFUSED
    assert result.error is not None
    assert "bytes" in result.error
    assert not target.path(TargetFile.POLICY).exists()


def test_the_last_policy_is_kept_as_prev(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    assemble_policy(root, target, FakeChecker())
    first = policy_text(target)
    write_fragment(root, "code", app_of("code"))
    assert assemble_policy(root, target, FakeChecker()).outcome is FleetOutcome.WRITTEN
    assert target.path(TargetFile.PREV).read_text(encoding="utf-8") == first
    assert policy_text(target) != first


def test_an_unchanged_policy_is_not_written(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    assemble_policy(root, target, FakeChecker())
    result = assemble_policy(root, target, FakeChecker())
    assert result.outcome is FleetOutcome.UNCHANGED
    assert not target.path(TargetFile.PREV).exists()


def test_a_removed_fragment_drops_its_app(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    write_fragment(root, "code", app_of("code"))
    remove_fragment(root, "code")
    remove_fragment(root, "code")
    assemble_policy(root, target, FakeChecker())
    assert list(tomllib.loads(policy_text(target))["apps"]) == ["chat"]


def test_no_fragment_writes_a_policy_with_no_app(root: Path, target: PolicyTarget) -> None:
    assert assemble_policy(root, target, FakeChecker()).outcome is FleetOutcome.WRITTEN
    assert policy_text(target) == "version = 1\n"


def test_a_fragment_with_another_app_is_left_out(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    write_fragment(root, "code", app_of("code"))
    paths.policy_fragment_path(root, "code").write_text(
        render_app("other", app_of("other")), encoding="utf-8"
    )
    result = assemble_policy(root, target, FakeChecker())
    assert result.left_out == {"code": "the fragment of code holds more than [apps.code]"}
    assert list(tomllib.loads(policy_text(target))["apps"]) == ["chat"]


def test_a_fragment_that_is_not_toml_is_left_out(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    paths.policy_fragment_path(root, "code").write_bytes(b"\xff[apps")
    result = assemble_policy(root, target, FakeChecker())
    assert set(result.left_out) == {"code"}
    assert list(tomllib.loads(policy_text(target))["apps"]) == ["chat"]


def test_a_file_with_no_family_name_is_not_a_fragment(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    (paths.policy_fragment_path(root, "chat").parent / "Not A Family.toml").write_text("x")
    result = assemble_policy(root, target, FakeChecker())
    assert result.left_out == {}
    assert list(tomllib.loads(policy_text(target))["apps"]) == ["chat"]


def test_the_writer_writes_only_policy_files(root: Path, target: PolicyTarget) -> None:
    install = target.config_dir / INSTALL_FILE
    before = install.stat()
    write_fragment(root, "chat", app_of("chat"))
    assemble_policy(root, target, FakeChecker())
    write_fragment(root, "code", app_of("code"))
    assemble_policy(root, target, FakeChecker())
    names = sorted(path.name for path in target.config_dir.iterdir())
    assert names == [INSTALL_FILE, "policy.toml", "policy.toml.prev"]
    assert install.read_text(encoding="utf-8") == INSTALL_TEXT
    assert install.stat().st_ino == before.st_ino
    assert install.stat().st_mtime_ns == before.st_mtime_ns


def test_a_target_file_is_the_policy_or_its_prev() -> None:
    assert {str(name) for name in TargetFile} == {"policy.toml", "policy.toml.prev"}


def test_the_policy_file_mode_is_0644(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    assemble_policy(root, target, FakeChecker())
    mode = stat.S_IMODE(target.path(TargetFile.POLICY).stat().st_mode)
    assert mode == 0o644


def test_an_assembly_waits_for_the_lock(root: Path, target: PolicyTarget) -> None:
    write_fragment(root, "chat", app_of("chat"))
    lock = paths.policy_lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    done = threading.Event()

    def run() -> None:
        assemble_policy(root, target, FakeChecker())
        done.set()

    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        thread = threading.Thread(target=run)
        thread.start()
        assert not done.wait(0.3)
        assert not target.path(TargetFile.POLICY).exists()
        fcntl.flock(handle, fcntl.LOCK_UN)

    thread.join(5)
    assert done.is_set()


def write_stub(directory: Path, body: str) -> Path:
    stub = directory / "chaperone"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_the_real_checker_runs_check_beside_a_copy_of_install(
    tmp_path: Path, target: PolicyTarget
) -> None:
    body = (
        "#!/bin/sh\n"
        'here=$(dirname "$0")\n'
        'echo "$@" > "$here/args"\n'
        'cp "$(dirname "$2")/install.toml" "$here/install.seen"\n'
        'cp "$2" "$here/policy.seen"\n'
    )
    stub = write_stub(tmp_path, body)
    plugins = tmp_path / "plugins"
    checker = ChaperoneChecker(binary=stub, config_dir=target.config_dir, plugins_dir=plugins)
    assert checker.check("version = 1\n") == Passed()
    args = (tmp_path / "args").read_text(encoding="utf-8").split()
    assert args[0] == "check"
    assert Path(args[1]).name == "policy.toml"
    assert Path(args[1]).parent != target.config_dir
    assert args[2:] == ["--plugins-dir", str(plugins)]
    assert (tmp_path / "install.seen").read_text(encoding="utf-8") == INSTALL_TEXT
    assert (tmp_path / "policy.seen").read_text(encoding="utf-8") == "version = 1\n"


def test_the_real_checker_reads_the_first_error_line(tmp_path: Path, target: PolicyTarget) -> None:
    body = "#!/bin/sh\necho 'policy.toml: the app chat is bad' >&2\necho more >&2\nexit 1\n"
    stub = write_stub(tmp_path, body)
    checker = ChaperoneChecker(binary=stub, config_dir=target.config_dir, plugins_dir=tmp_path)
    assert checker.check("version = 1\n") == Refused("policy.toml: the app chat is bad")


def test_the_real_checker_reads_each_refused_app(tmp_path: Path, target: PolicyTarget) -> None:
    """The output of `chaperone check` 0.4.0 for a policy that refuses two
    apps: one line for each, the first with the file name."""
    body = (
        "#!/bin/sh\n"
        "echo 'policy.toml: the app chat is refused: the app chat grants tools of x' >&2\n"
        "echo 'the app code is refused: the apps code and chat share a token digest' >&2\n"
        "exit 1\n"
    )
    stub = write_stub(tmp_path, body)
    checker = ChaperoneChecker(binary=stub, config_dir=target.config_dir, plugins_dir=tmp_path)

    verdict = checker.check("version = 1\n")

    assert isinstance(verdict, Refused)
    assert dict(verdict.apps) == {
        "chat": "the app chat is refused: the app chat grants tools of x",
        "code": "the app code is refused: the apps code and chat share a token digest",
    }


def test_a_checker_that_cannot_run_raises(tmp_path: Path, target: PolicyTarget) -> None:
    checker = ChaperoneChecker(
        binary=tmp_path / "absent", config_dir=target.config_dir, plugins_dir=tmp_path
    )
    with pytest.raises(CheckerError):
        checker.check("version = 1\n")


def test_a_checker_that_hangs_raises(tmp_path: Path, target: PolicyTarget) -> None:
    stub = write_stub(tmp_path, "#!/bin/sh\nexec sleep 5\n")
    checker = ChaperoneChecker(
        binary=stub, config_dir=target.config_dir, plugins_dir=tmp_path, timeout_s=0.2
    )
    with pytest.raises(CheckerError, match="did not finish"):
        checker.check("version = 1\n")


def test_a_checker_with_no_install_file_raises(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, STUB_OK)
    empty = tmp_path / "empty"
    empty.mkdir()
    checker = ChaperoneChecker(binary=stub, config_dir=empty, plugins_dir=tmp_path)
    with pytest.raises(CheckerError):
        checker.check("version = 1\n")
    assert not os.path.exists(tmp_path / "args")
