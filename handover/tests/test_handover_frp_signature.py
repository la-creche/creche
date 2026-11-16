"""The cosign boundary of P1 (`docs/design/forgejo-release-path.md`, FR1,
FR10, FR12 and section 5): the words of the verifier, the files that root
writes for it, the one child, and the reading of its exit status.

The real check of the two programs (`host.verifier_argv`) runs, and a
stand-in of `cosign verify-blob --key` runs in the place of cosign. No test
here needs cosign. `test_handover_frp_cosign.py` runs the real cosign
v3.1.4 with these words (section 3 M7).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.host import (
    NO_VERIFIER_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    VERIFIER_TIMEOUT_S,
    As,
    Command,
    Network,
    Result,
)
from handover.executor.release_statement import (
    RELEASE_SIGNING_KEY,
    VERIFY_SKIP_TLOG,
    SignedStatement,
    StatementReason,
    UnverifiedMessage,
    check_signature,
    statement_words,
)
from handover_frp_fixtures import KeyStandIn, signed_bundle, use_verifier_site

from handover import site

COMPONENT = "vmm"
STATEMENT = b'{"statement":1}'


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()

    return work


def test_the_words_name_the_key_the_bundle_and_no_log() -> None:
    bundle, statement = Path("/w/b.sigstore.json"), Path("/w/s.json")
    assert statement_words(bundle, statement) == (
        "verify-blob",
        "--key",
        RELEASE_SIGNING_KEY,
        "--bundle",
        str(bundle),
        *VERIFY_SKIP_TLOG,
        str(statement),
    )
    assert RELEASE_SIGNING_KEY == "/etc/creche/release-signing.pub"

    with pytest.raises(ValueError):
        statement_words(Path("b.json"), statement)

    with pytest.raises(ValueError):
        statement_words(bundle, Path("s.json"))


class Answers:
    """A runner that records each command and answers one exit status."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.seen: list[Command] = []

    def __call__(self, command: Command) -> Result:
        self.seen.append(command)

        return Result(self.code)


def test_one_child_reads_the_files_that_root_wrote(tmp_path: Path) -> None:
    work = _work(tmp_path)
    run = Answers(0)
    message = UnverifiedMessage(statement=STATEMENT, bundle=b"bundle bytes")
    signed = check_signature(run, COMPONENT, message, work)

    where = work / "statement" / COMPONENT
    statement, bundle = where / "statement.json", where / "statement.sigstore.json"
    assert (statement.read_bytes(), bundle.read_bytes()) == (STATEMENT, b"bundle bytes")
    for directory in (work / "statement", where):
        assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o755
    for file in (statement, bundle):
        assert stat.S_IMODE(os.lstat(file).st_mode) == 0o644

    [child] = run.seen
    assert child.argv == (site.cosign_program(), *statement_words(bundle, statement))
    assert (child.identity, child.network, child.timeout_s) == (
        As.VERIFIER,
        Network.NONE,
        VERIFIER_TIMEOUT_S,
    )
    assert (signed.component, signed.data) == (COMPONENT, STATEMENT)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (1, StatementReason.SIGNATURE_REFUSED),
        (2, StatementReason.SIGNATURE_REFUSED),
        (-9, StatementReason.SIGNATURE_REFUSED),
        (NO_VERIFIER_EXIT_CODE, StatementReason.VERIFIER_UNAVAILABLE),
        (TIMEOUT_EXIT_CODE, StatementReason.VERIFIER_UNAVAILABLE),
    ],
)
def test_each_status_has_its_reason(tmp_path: Path, code: int, reason: StatementReason) -> None:
    message = UnverifiedMessage(statement=STATEMENT, bundle=b"x")
    with pytest.raises(Refusal) as refused:
        check_signature(Answers(code), COMPONENT, message, _work(tmp_path))

    assert (refused.value.code, refused.value.detail) == (RefusalCode.P1, reason.value)


def test_a_child_that_does_not_start_is_no_answer(tmp_path: Path) -> None:
    def run(command: Command) -> Result:
        raise FileNotFoundError(command.argv[0])

    message = UnverifiedMessage(statement=STATEMENT, bundle=b"x")
    with pytest.raises(Refusal) as refused:
        check_signature(run, COMPONENT, message, _work(tmp_path))

    assert refused.value.detail == StatementReason.VERIFIER_UNAVAILABLE.value


def test_the_reasons_of_the_verifier_are_those_of_p6() -> None:
    """The follower asks again for the text of `VERIFIER_UNAVAILABLE`, so
    the two texts are the texts of `ArtifactRefusal`."""
    from handover.artifacts import ArtifactRefusal

    assert StatementReason.VERIFIER_UNAVAILABLE.value == ArtifactRefusal.VERIFIER_UNAVAILABLE.value
    assert StatementReason.SIGNATURE_REFUSED.value == ArtifactRefusal.SIGNATURE_REFUSED.value


def test_only_the_check_makes_a_signed_statement() -> None:
    with pytest.raises(TypeError):
        SignedStatement(object(), COMPONENT, STATEMENT)


def test_a_name_outside_the_catalog_builds_no_path(tmp_path: Path) -> None:
    message = UnverifiedMessage(statement=STATEMENT, bundle=b"x")
    with pytest.raises(Refusal) as refused:
        check_signature(Answers(0), "../escape", message, _work(tmp_path))

    assert refused.value.code is RefusalCode.CATALOG
    assert not (tmp_path / "escape").exists()


def test_a_link_in_the_work_tree_fails_the_step(tmp_path: Path) -> None:
    work = _work(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (work / "statement").symlink_to(elsewhere)

    message = UnverifiedMessage(statement=STATEMENT, bundle=b"x")
    with pytest.raises(NotADirectoryError):
        check_signature(Answers(0), COMPONENT, message, work)

    assert list(elsewhere.iterdir()) == []


def test_a_link_in_place_of_a_file_is_not_followed(tmp_path: Path) -> None:
    work = _work(tmp_path)
    where = work / "statement" / COMPONENT
    where.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_bytes(b"kept")
    (where / "statement.json").symlink_to(target)

    message = UnverifiedMessage(statement=STATEMENT, bundle=b"x")
    with pytest.raises(OSError):
        check_signature(Answers(0), COMPONENT, message, work)

    assert target.read_bytes() == b"kept"


# -- the real check of the programs, and the stand-in of cosign ---------------


def test_the_stand_in_passes_a_bundle_of_the_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_verifier_site(tmp_path, monkeypatch)
    run = KeyStandIn()
    message = UnverifiedMessage(statement=STATEMENT, bundle=signed_bundle(STATEMENT))
    signed = check_signature(run, COMPONENT, message, _work(tmp_path))

    assert signed.data == STATEMENT
    [argv] = run.words
    assert "PrivateNetwork=yes" in argv


def test_the_stand_in_refuses_a_bundle_of_other_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_verifier_site(tmp_path, monkeypatch)
    message = UnverifiedMessage(statement=STATEMENT, bundle=signed_bundle(b"other bytes"))
    with pytest.raises(Refusal) as refused:
        check_signature(KeyStandIn(), COMPONENT, message, _work(tmp_path))

    assert refused.value.detail == StatementReason.SIGNATURE_REFUSED.value


def test_a_verifier_that_root_does_not_trust_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site names a verifier that does not exist. `host.verifier_argv`
    refuses it, so no child starts."""
    use_verifier_site(tmp_path, monkeypatch, f"{tmp_path}/no-cosign")

    run = KeyStandIn()
    message = UnverifiedMessage(statement=STATEMENT, bundle=signed_bundle(STATEMENT))
    with pytest.raises(Refusal) as refused:
        check_signature(run, COMPONENT, message, _work(tmp_path))

    assert refused.value.detail == StatementReason.VERIFIER_UNAVAILABLE.value
    assert run.words == []
