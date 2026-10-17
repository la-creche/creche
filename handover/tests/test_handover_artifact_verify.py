"""Check P6: the verifier, its runner and the value that a pass gives.

Root verifies its copy of the archive of a binary component with `cosign`,
as two children in a transient unit of systemd with a dynamic user. Root
reads the exit status of each child and no text. Three groups of tests:

1. The two optional keys of the site file that name the verifier and
   `systemd-run`.
2. The runner (`host.py`): the exact words of a verifier child, the
   fail-closed arm, root's time limit, and the check of each program file.
3. `check_artifact` with a recorded runner: each class of exit status, the
   exact words of each child, and the member list after a pass.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from handover.artifacts import ArtifactRefusal, SignerPolicy, signer_policy, verify_words
from handover.catalog import Repo
from handover.errors import Refusal, RefusalCode
from handover.executor import host, provenance
from handover.executor.artifact import Copied
from handover.executor.host import (
    NO_VERIFIER_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    UNIT_END,
    VERIFIER_TIMEOUT_S,
    VERIFIER_TUF_ROOT,
    VERIFIER_UNIT_SECONDS,
    As,
    Command,
    Distrust,
    Network,
    Result,
    distrusted,
    make_runner,
    verifier_argv,
)
from handover.executor.provenance import (
    TRUSTED_ROOT,
    VerifiedArtifact,
    check_artifact,
)
from handover_bin_fixtures import (
    BINARY_NAME,
    OWNER,
    PROGRAMS,
    ROOT_OWNED_STARTER,
    ROOT_OWNED_VERIFIER,
    archive_bytes,
    signed_bundle,
    site_text,
)
from handover_executor_fixtures import (
    DIGEST,
    SHA_OF,
)

from handover import site

COMPONENT = BINARY_NAME
SHA = SHA_OF[COMPONENT]
LIVE_VERSION = "0.4.0"
NEW_VERSION = "0.5.0"

#: The text that a child writes. No refusal holds it.
CHILD_TEXT = "the text of the verifier\n"


@pytest.fixture
def home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A site file that names the two root-owned programs and a home."""
    made = tmp_path_factory.mktemp("home")
    _site(tmp_path_factory, monkeypatch, site_text(made))

    return made


def _site(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, text: str
) -> Path:
    path = tmp_path_factory.mktemp("site") / "site.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))

    return path


# ---- 1. the two keys of the site file ------------------------------------------


def test_the_verifier_and_systemd_run_have_their_defaults() -> None:
    """The example site file of the suite sets neither key."""
    assert site.cosign_program() == "/usr/local/bin/cosign"
    assert site.systemd_run_program() == "/usr/bin/systemd-run"


def test_the_site_file_names_the_verifier_and_systemd_run(home: Path) -> None:
    del home

    assert site.cosign_program() == ROOT_OWNED_VERIFIER
    assert site.systemd_run_program() == ROOT_OWNED_STARTER


@pytest.mark.parametrize(
    "value", ["", "cosign", "bin/cosign", "/usr/bin/", "/usr/../bin/cosign", "/a b", "~/cosign"]
)
@pytest.mark.parametrize("key", [site.SiteKey.COSIGN_PROGRAM, site.SiteKey.SYSTEMD_RUN_PROGRAM])
def test_a_program_that_is_no_plain_absolute_path_refuses(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    key: site.SiteKey,
    value: str,
) -> None:
    _site(tmp_path_factory, monkeypatch, f"{key.value}={value}\n")
    read = site.cosign_program if key is site.SiteKey.COSIGN_PROGRAM else site.systemd_run_program

    with pytest.raises(Refusal) as caught:
        read()

    assert caught.value.code is RefusalCode.SITE
    assert key.value in caught.value.detail


@pytest.mark.usefixtures("operator_is_an_account")
def test_the_verify_hook_of_the_executor_reads_both_keys(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check --site` refuses a site file with a value that P6 refuses."""
    lines = (
        "AGENT_GITHUB_OWNER=some-org\nAGENT_OPERATOR_USER=someone\n"
        "AGENT_OPERATOR_HOME=/home/someone\nAGENT_LAN_ADDRESS=192.0.2.10\n"
    )
    _site(tmp_path_factory, monkeypatch, f"{lines}AGENT_SYSTEMD_RUN_PROGRAM=systemd-run\n")

    with pytest.raises(Refusal) as caught:
        site.require_complete()

    assert "AGENT_SYSTEMD_RUN_PROGRAM" in caught.value.detail


# ---- 2. the runner ---------------------------------------------------------------

#: The words of the unit of a verifier child, as probe run 38943722125 ran
#: them, after `systemd-run`.
UNIT_WORDS = (
    "--quiet",
    "--wait",
    "--collect",
    "-p",
    "DynamicUser=yes",
    "-p",
    "StateDirectory=creche-verify",
    "-p",
    "NoNewPrivileges=yes",
    "-p",
    "ProtectSystem=strict",
    "-p",
    "ProtectHome=yes",
    "-p",
    "PrivateTmp=yes",
    "-p",
    "PrivateDevices=yes",
    "-p",
    "CapabilityBoundingSet=",
    "-p",
    "MemoryMax=512M",
    "-p",
    "RuntimeMaxSec=120",
    "--setenv=HOME=/var/lib/creche-verify",
    "--setenv=TUF_ROOT=/var/lib/creche-verify/tuf",
)


def _command(*words: str, network: Network = Network.HOST) -> Command:
    return Command(
        argv=(ROOT_OWNED_VERIFIER, *words),
        identity=As.VERIFIER,
        timeout_s=VERIFIER_TIMEOUT_S,
        network=network,
    )


@pytest.mark.usefixtures("home")
def test_a_verifier_child_runs_in_its_unit() -> None:
    assert verifier_argv(_command("initialize")) == (
        ROOT_OWNED_STARTER,
        *UNIT_WORDS,
        UNIT_END,
        ROOT_OWNED_VERIFIER,
        "initialize",
    )


@pytest.mark.usefixtures("home")
def test_a_child_with_no_network_gets_a_private_network() -> None:
    assert verifier_argv(_command("verify-blob", network=Network.NONE)) == (
        ROOT_OWNED_STARTER,
        *UNIT_WORDS,
        "-p",
        "PrivateNetwork=yes",
        UNIT_END,
        ROOT_OWNED_VERIFIER,
        "verify-blob",
    )


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize("first", ["/usr/bin/true", "/usr/local/bin/cosign", "env", ""])
def test_a_command_that_does_not_start_the_verifier_gets_no_unit(first: str) -> None:
    command = Command(argv=(first, "initialize"), identity=As.VERIFIER, timeout_s=1.0)

    assert verifier_argv(command) is None
    assert verifier_argv(replace(command, argv=())) is None


@dataclass
class Spawned:
    """What the real runner gave `subprocess.run`."""

    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    code: int = 0
    timeout: bool = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, float(kwargs["timeout"]))

        return subprocess.CompletedProcess(argv, self.code, None, None)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Spawned:
    made = Spawned()
    monkeypatch.setattr(host.subprocess, "run", made)

    return made


@pytest.mark.usefixtures("home")
def test_the_runner_starts_the_unit_and_reads_no_text(spawned: Spawned) -> None:
    spawned.code = 1

    result = make_runner(1234)(_command("initialize"))

    (call,) = spawned.calls
    assert call["argv"] == [
        ROOT_OWNED_STARTER,
        *UNIT_WORDS,
        UNIT_END,
        ROOT_OWNED_VERIFIER,
        "initialize",
    ]
    assert call["stdout"] is subprocess.DEVNULL
    assert call["stderr"] is subprocess.DEVNULL
    assert call["timeout"] == VERIFIER_TIMEOUT_S
    assert "SUDO_UID" not in call["env"]
    assert result == Result(1, "", "")


@pytest.mark.usefixtures("home")
def test_the_runner_starts_no_child_for_another_program(spawned: Spawned) -> None:
    command = Command(argv=("/usr/bin/true",), identity=As.VERIFIER, timeout_s=1.0)

    assert make_runner(1234)(command).code == NO_VERIFIER_EXIT_CODE
    assert spawned.calls == []


def test_the_runner_starts_no_child_for_a_verifier_that_is_not_there(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    spawned: Spawned,
) -> None:
    absent = tmp_path / "cosign"
    _site(tmp_path_factory, monkeypatch, f"AGENT_COSIGN_PROGRAM={absent}\n")
    command = Command(argv=(str(absent), "initialize"), identity=As.VERIFIER, timeout_s=1.0)

    assert make_runner(1234)(command).code == NO_VERIFIER_EXIT_CODE
    assert spawned.calls == []


def test_the_runner_starts_no_child_for_a_starter_that_is_not_there(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    spawned: Spawned,
) -> None:
    lines = f"AGENT_COSIGN_PROGRAM={ROOT_OWNED_VERIFIER}\n"
    _site(tmp_path_factory, monkeypatch, f"{lines}AGENT_SYSTEMD_RUN_PROGRAM={tmp_path / 'run'}\n")

    assert make_runner(1234)(_command("initialize")).code == NO_VERIFIER_EXIT_CODE
    assert spawned.calls == []


@pytest.mark.usefixtures("home")
def test_a_child_past_the_time_of_root_answers_the_timeout_code(spawned: Spawned) -> None:
    spawned.timeout = True

    assert make_runner(1234)(_command("initialize")).code == TIMEOUT_EXIT_CODE


def test_the_time_of_root_ends_before_the_time_of_the_unit() -> None:
    """Through `systemd-run --wait`, the time limit of the unit ends with
    the status of a refused signature. Root's own limit ends first and
    answers `TIMEOUT_EXIT_CODE`."""
    assert VERIFIER_TIMEOUT_S < VERIFIER_UNIT_SECONDS


def test_the_trust_root_is_in_the_state_directory_of_the_unit() -> None:
    assert TRUSTED_ROOT.startswith(f"{VERIFIER_TUF_ROOT}/")


# -- the check of a program file


def _faked_lstat(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Callable[[os.stat_result], os.stat_result]]
) -> None:
    """`os.lstat` with a change for each path of `changes`. A test cannot
    give a file the owner root, and it cannot give a file of root a write
    bit."""
    real = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        found = real(path, *args, **kwargs)
        change = changes.get(os.fspath(path))

        return found if change is None else change(found)

    monkeypatch.setattr(os, "lstat", lstat)


def _with(found: os.stat_result, **fields: int) -> os.stat_result:
    names = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size")
    values = list(found[:10])
    for name, value in fields.items():
        values[names.index(name)] = value

    return os.stat_result(values)


def _more_mode(bits: int) -> Callable[[os.stat_result], os.stat_result]:
    return lambda found: _with(found, st_mode=found.st_mode | bits)


def _uid(uid: int) -> Callable[[os.stat_result], os.stat_result]:
    return lambda found: _with(found, st_uid=uid)


def test_root_trusts_a_program_of_root_below_directories_of_root() -> None:
    assert distrusted(ROOT_OWNED_VERIFIER) is None
    assert distrusted(ROOT_OWNED_STARTER) is None


def test_an_absent_program_is_not_trusted(tmp_path: Path) -> None:
    found = distrusted(str(tmp_path / "cosign"))

    assert found is not None
    assert (found.path, found.why) == (tmp_path / "cosign", Distrust.ABSENT)


def test_a_link_is_not_trusted(tmp_path: Path) -> None:
    """The link names a program of root. Root judges the link."""
    (tmp_path / "cosign").symlink_to(ROOT_OWNED_VERIFIER)
    found = distrusted(str(tmp_path / "cosign"))

    assert found is not None
    assert (found.path, found.why) == (tmp_path / "cosign", Distrust.NOT_A_FILE)


def test_a_directory_is_not_a_program(tmp_path: Path) -> None:
    found = distrusted(str(tmp_path))

    assert found is not None
    assert found.why is Distrust.NOT_A_FILE


def test_a_program_of_another_uid_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _faked_lstat(monkeypatch, {ROOT_OWNED_VERIFIER: _uid(1000)})
    found = distrusted(ROOT_OWNED_VERIFIER)

    assert found is not None
    assert (str(found.path), found.why) == (ROOT_OWNED_VERIFIER, Distrust.NOT_ROOTS)


@pytest.mark.parametrize("bits", [stat.S_IWGRP, stat.S_IWOTH], ids=["group", "other"])
def test_a_program_with_a_write_bit_for_others_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, bits: int
) -> None:
    _faked_lstat(monkeypatch, {ROOT_OWNED_VERIFIER: _more_mode(bits)})
    found = distrusted(ROOT_OWNED_VERIFIER)

    assert found is not None
    assert (str(found.path), found.why) == (ROOT_OWNED_VERIFIER, Distrust.OPEN)


@pytest.mark.parametrize("above", ["/usr/bin", "/usr", "/"])
def test_a_directory_above_that_another_account_can_write_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, above: str
) -> None:
    _faked_lstat(monkeypatch, {above: _more_mode(stat.S_IWOTH)})
    found = distrusted(ROOT_OWNED_VERIFIER)

    assert found is not None
    assert (str(found.path), found.why) == (above, Distrust.OPEN)


def test_a_directory_above_of_another_uid_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _faked_lstat(monkeypatch, {"/usr": _uid(1000)})
    found = distrusted(ROOT_OWNED_VERIFIER)

    assert found is not None
    assert (str(found.path), found.why) == ("/usr", Distrust.NOT_ROOTS)


def test_a_link_in_the_place_of_a_directory_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    link = lambda found: _with(found, st_mode=stat.S_IFLNK | 0o777)  # noqa: E731
    _faked_lstat(monkeypatch, {"/usr/bin": link})
    found = distrusted(ROOT_OWNED_VERIFIER)

    assert found is not None
    assert (str(found.path), found.why) == ("/usr/bin", Distrust.NOT_A_DIRECTORY)


# ---- 3. check P6 with a recorded runner -------------------------------------------


@dataclass
class Recorded:
    """A runner that answers each verifier child with the next status of
    `codes`, and records each command. Each answer holds a text."""

    codes: list[int]
    seen: list[Command] = field(default_factory=list[Command])
    raises: bool = False

    def __call__(self, command: Command) -> Result:
        self.seen.append(command)
        if self.raises:
            raise PermissionError(command.argv[0])

        return Result(self.codes.pop(0), CHILD_TEXT, CHILD_TEXT)


@pytest.fixture
def copy(tmp_path: Path) -> Copied:
    """Root's copy of a good archive and its bundle."""
    return _copy(tmp_path, archive_bytes())


def _copy(tmp_path: Path, raw: bytes) -> Copied:
    where = tmp_path / "copy"
    where.mkdir(exist_ok=True)
    (where / "archive.tar.gz").write_bytes(raw)
    (where / "archive.tar.gz.sigstore.json").write_bytes(signed_bundle(raw))

    return Copied(
        archive=where / "archive.tar.gz",
        bundle=where / "archive.tar.gz.sigstore.json",
        digest=DIGEST,
    )


def _policy() -> SignerPolicy:
    return signer_policy(Repo.AGENT_CONTROL, OWNER, "agent-control", SHA)


def _refusal(run: Recorded, copy: Copied) -> Refusal:
    with pytest.raises(Refusal) as caught:
        check_artifact(run, COMPONENT, _policy(), copy)

    assert caught.value.code is RefusalCode.P6
    assert caught.value.subject == COMPONENT

    return caught.value


def test_a_pass_gives_a_verified_artifact(copy: Copied) -> None:
    verified = check_artifact(Recorded([0, 0]), COMPONENT, _policy(), copy)

    assert verified.component == COMPONENT
    assert verified.archive == copy.archive
    assert verified.digest == copy.digest
    assert verified.programs == PROGRAMS


def test_the_exact_words_of_each_child(copy: Copied) -> None:
    run = Recorded([0, 0])

    check_artifact(run, COMPONENT, _policy(), copy)

    refresh, check = run.seen
    verifier = site.cosign_program()
    assert refresh == Command(
        argv=(verifier, "initialize"), identity=As.VERIFIER, timeout_s=VERIFIER_TIMEOUT_S
    )
    words = verify_words(_policy(), str(copy.archive), str(copy.bundle), TRUSTED_ROOT)
    assert check == Command(
        argv=(verifier, *words),
        identity=As.VERIFIER,
        timeout_s=VERIFIER_TIMEOUT_S,
        network=Network.NONE,
    )
    assert check.argv[1:5] == ("verify-blob", "--bundle", str(copy.bundle), "--trusted-root")
    assert check.argv[-1] == str(copy.archive)


@pytest.mark.parametrize("code", [1, 2, NO_VERIFIER_EXIT_CODE, TIMEOUT_EXIT_CODE])
def test_a_refresh_that_does_not_pass_is_no_verifier(copy: Copied, code: int) -> None:
    """Child 1 needs the network. The signature has no answer, so the
    follower asks again."""
    run = Recorded([code])

    assert _refusal(run, copy).detail == str(ArtifactRefusal.VERIFIER_UNAVAILABLE)
    assert len(run.seen) == 1


@pytest.mark.parametrize("code", [NO_VERIFIER_EXIT_CODE, TIMEOUT_EXIT_CODE])
def test_a_check_that_did_not_start_or_end_is_no_verifier(copy: Copied, code: int) -> None:
    run = Recorded([0, code])

    assert _refusal(run, copy).detail == str(ArtifactRefusal.VERIFIER_UNAVAILABLE)


def test_a_runner_that_cannot_start_the_child_is_no_verifier(copy: Copied) -> None:
    run = Recorded([], raises=True)

    assert _refusal(run, copy).detail == str(ArtifactRefusal.VERIFIER_UNAVAILABLE)


@pytest.mark.parametrize("code", [1, 2, 10, 255])
def test_each_other_status_of_the_check_is_a_refused_signature(copy: Copied, code: int) -> None:
    """Status 1 is the status of each refused signature of the probe runs."""
    assert _refusal(Recorded([0, code]), copy).detail == str(ArtifactRefusal.SIGNATURE_REFUSED)


def test_no_text_of_a_child_reaches_the_refusal(copy: Copied) -> None:
    refusal = _refusal(Recorded([0, 1]), copy)

    assert CHILD_TEXT.strip() not in refusal.as_line()


def test_no_file_of_the_copy_is_opened_before_the_signature_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy names no file. A refused signature is still a refused
    signature, and the member list is never read."""
    listed: list[Path] = []
    monkeypatch.setattr(provenance, "list_component_archive", lambda path, _: listed.append(path))
    copy = Copied(archive=tmp_path / "absent", bundle=tmp_path / "absent.json", digest=DIGEST)

    assert _refusal(Recorded([0, 1]), copy).detail == str(ArtifactRefusal.SIGNATURE_REFUSED)
    assert listed == []


def _link(tar: tarfile.TarFile) -> None:
    info = tarfile.TarInfo(f"{COMPONENT}/bin/linked")
    info.type = tarfile.SYMTYPE
    info.linkname = "/usr/bin/env"
    tar.addfile(info)


def _directory(tar: tarfile.TarFile) -> None:
    info = tarfile.TarInfo(f"{COMPONENT}/bin")
    info.type = tarfile.DIRTYPE
    tar.addfile(info)


@pytest.mark.parametrize(
    "raw",
    [
        archive_bytes(extra=_link),
        archive_bytes(extra=_directory),
        archive_bytes(component="caregiver"),
        b"not a gzip of a tar",
    ],
    ids=["link", "directory", "another-component", "not-an-archive"],
)
def test_a_shape_that_the_unpack_refuses_is_refused_after_a_pass(
    tmp_path: Path, raw: bytes
) -> None:
    """The signature passed. The member list decides before the tap."""
    run = Recorded([0, 0])

    refusal = _refusal(run, _copy(tmp_path, raw))

    assert refusal.detail == str(ArtifactRefusal.SHAPE_REFUSED)
    assert len(run.seen) == 2


def test_a_copy_that_is_gone_after_the_pass_is_refused(copy: Copied) -> None:
    copy.archive.unlink()

    assert _refusal(Recorded([0, 0]), copy).detail == str(ArtifactRefusal.SHAPE_REFUSED)


def test_the_list_writes_no_file(copy: Copied) -> None:
    before = sorted(copy.archive.parent.iterdir())

    check_artifact(Recorded([0, 0]), COMPONENT, _policy(), copy)

    assert sorted(copy.archive.parent.iterdir()) == before


def test_no_other_value_makes_a_verified_artifact(copy: Copied) -> None:
    with pytest.raises(TypeError):
        VerifiedArtifact(object(), COMPONENT, copy.archive, DIGEST, PROGRAMS)  # pyright: ignore[reportArgumentType]


def test_only_check_p6_builds_a_verified_artifact() -> None:
    """The stage takes only this type, so only the check makes it."""
    source = Path(__file__).resolve().parents[1] / "src" / "handover"
    builders = sorted(
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if "VerifiedArtifact(" in path.read_text(encoding="utf-8")
    )

    assert builders == ["executor/provenance.py"]
