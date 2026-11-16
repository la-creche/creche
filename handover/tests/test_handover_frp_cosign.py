"""The cosign check of P1 with a real cosign (`docs/design/forgejo-release-path.md`,
FR1, FR12, section 3 M7). The words and the statuses of cosign v3.1.4 were
measured on 2026-11-16.

The runner gives each verifier child to the real check of
`host.verifier_argv`, as `KeyStandIn` does. Then it starts the real cosign
with the words of that check, in place of the root-owned program of the
site file. The key of FR10 is a root-owned file of the host, so the runner
gives cosign the key of the test in place of that one path. Every other
word is the word of the check.

Each test skips on a machine that has no `cosign`. Each child has an empty
`HOME` and `TUF_ROOT` and a dead proxy, so a child that reaches for the
network fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.host import NO_VERIFIER_EXIT_CODE, UNIT_END, Command, Result, verifier_argv
from handover.executor.release_statement import (
    RELEASE_SIGNING_KEY,
    VERIFY_SKIP_TLOG,
    StatementReason,
    UnverifiedMessage,
    check_signature,
    decode_message,
    read_statement,
)
from handover_frp_fixtures import SIGNER, tag_message, use_verifier_site

COSIGN = shutil.which("cosign")
pytestmark = pytest.mark.skipif(COSIGN is None, reason="cosign is not on this machine")

#: The public key, the statement and the bundle of the measurement of
#: 2026-11-16. They verify together. The private key is deleted.
RECORDED = SIGNER / "vectors" / "cosign-3.1.4"

COMPONENT = "vmm"

#: The bytes of a bundle of cosign v3.1.4 with a P-256 key (measured).
MEASURED_BUNDLE_BYTES = 387

#: A port that no listener holds, for each proxy of a child.
DEAD_PROXY = "http://127.0.0.1:9"


def _cosign() -> str:
    assert COSIGN is not None

    return COSIGN


def _offline_env(where: Path) -> dict[str, str]:
    """An environment with an empty `HOME`, an empty `TUF_ROOT` and dead
    proxies. cosign needs none of them for these words (measured)."""
    home, tuf = where / "home", where / "tuf"
    home.mkdir(exist_ok=True)
    tuf.mkdir(exist_ok=True)

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TUF_ROOT": str(tuf),
        "HTTP_PROXY": DEAD_PROXY,
        "HTTPS_PROXY": DEAD_PROXY,
        "ALL_PROXY": DEAD_PROXY,
        "COSIGN_PASSWORD": "",
    }


@dataclass
class RealCosign:
    """The runner of each verifier child: the real check of
    `host.verifier_argv`, then the real cosign. `key` takes the place of the
    key of FR10. `words` records each child argv that cosign got."""

    key: Path
    env: dict[str, str]
    words: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def __call__(self, command: Command) -> Result:
        argv = verifier_argv(command)
        if argv is None:
            return Result(NO_VERIFIER_EXIT_CODE)

        # The words after `--` and after the program of the site file.
        child = tuple(
            str(self.key) if word == RELEASE_SIGNING_KEY else word
            for word in argv[argv.index(UNIT_END) + 2 :]
        )
        self.words.append(child)
        done = subprocess.run(
            [_cosign(), *child],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=self.env,
            timeout=60.0,
            check=False,
        )

        return Result(done.returncode)


def _cosign_in(where: Path, *words: str) -> None:
    """One cosign command of the test itself, in `where`."""
    subprocess.run(
        [_cosign(), *words],
        cwd=where,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_offline_env(where),
        timeout=60.0,
        check=True,
    )


@dataclass(frozen=True)
class KeyPair:
    """A throwaway key pair in the temp folder of one test."""

    where: Path

    @property
    def public(self) -> Path:
        return self.where / "cosign.pub"

    def sign(self, statement: bytes) -> bytes:
        """The bundle of `statement`, with the sign words of the
        measurement: an empty signing config, so no log and no network."""
        (self.where / "statement.json").write_bytes(statement)
        _cosign_in(
            self.where,
            "sign-blob",
            "--key",
            "cosign.key",
            "--signing-config",
            "empty-signing-config.json",
            "--bundle",
            "statement.sigstore.json",
            "statement.json",
        )

        return (self.where / "statement.sigstore.json").read_bytes()


@pytest.fixture
def key_pair(tmp_path: Path) -> KeyPair:
    where = tmp_path / "signer"
    where.mkdir()
    _cosign_in(where, "generate-key-pair")
    _cosign_in(where, "signing-config", "create", "--out", "empty-signing-config.json")

    return KeyPair(where)


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()

    return work


def _child_env(tmp_path: Path) -> dict[str, str]:
    where = tmp_path / "child"
    where.mkdir()

    return _offline_env(where)


def _message(statement: bytes, bundle: bytes) -> UnverifiedMessage:
    return decode_message(COMPONENT, tag_message(statement, bundle))


def _recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def _refusal(run: RealCosign, message: UnverifiedMessage, work: Path) -> str:
    with pytest.raises(Refusal) as refused:
        check_signature(run, COMPONENT, message, work)

    assert refused.value.code is RefusalCode.P1

    return refused.value.detail


def test_a_signed_statement_passes_the_real_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_pair: KeyPair
) -> None:
    use_verifier_site(tmp_path, monkeypatch)
    statement = _recorded("statement.json")
    bundle = key_pair.sign(statement)
    assert len(bundle) == MEASURED_BUNDLE_BYTES

    env = _child_env(tmp_path)
    run = RealCosign(key_pair.public, env)
    signed = check_signature(run, COMPONENT, _message(statement, bundle), _work(tmp_path))

    assert read_statement(signed).commit == "0123456789abcdef0123456789abcdef01234567"
    [words] = run.words
    assert words[-2] == VERIFY_SKIP_TLOG[-1]

    # cosign read no trust root: the empty folders stay empty.
    assert list(Path(env["HOME"]).iterdir()) == []
    assert list(Path(env["TUF_ROOT"]).iterdir()) == []


def test_each_measured_failure_is_a_refused_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_pair: KeyPair
) -> None:
    use_verifier_site(tmp_path, monkeypatch)
    statement = _recorded("statement.json")
    bundle = key_pair.sign(statement)
    env = _child_env(tmp_path)
    work = _work(tmp_path)
    refused = StatementReason.SIGNATURE_REFUSED.value

    changed = statement.replace(b'"run":1234', b'"run":1235')
    assert _refusal(RealCosign(key_pair.public, env), _message(changed, bundle), work) == refused

    other_key = RECORDED / "cosign.pub"
    assert _refusal(RealCosign(other_key, env), _message(statement, bundle), work) == refused

    no_key = tmp_path / "missing.pub"
    assert _refusal(RealCosign(no_key, env), _message(statement, bundle), work) == refused

    other_bundle = _recorded("statement.sigstore.json")
    message = _message(statement, other_bundle)
    assert _refusal(RealCosign(key_pair.public, env), message, work) == refused


def test_the_recorded_files_pass_the_real_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_verifier_site(tmp_path, monkeypatch)
    statement, bundle = _recorded("statement.json"), _recorded("statement.sigstore.json")
    run = RealCosign(RECORDED / "cosign.pub", _child_env(tmp_path))
    signed = check_signature(run, COMPONENT, _message(statement, bundle), _work(tmp_path))

    assert signed.data == statement
    assert [tag.tag for tag in read_statement(signed).tags] == ["handover-v0.1.96", "vmm-v0.1.24"]
