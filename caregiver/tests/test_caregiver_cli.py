"""caregiver apply-once | status | delete: every mutating verb
prints its plan and needs --write to act."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caregiver.cli import EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, main
from caregiver.credentials import read_creds
from caregiver.driver import DriverError, FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys, LiteLLMError
from caregiver_helpers import UNREADABLE_JSON, write_no_file_dir, write_registry

from caregiver import paths


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    write_registry(root)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


# --- apply-once ------------------------------------------------------------


def test_apply_once_without_write_makes_no_change(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
        ],
        driver=driver,
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert litellm.minted == 0
    assert driver.calls == []
    assert not paths.creds_path(state_root, "chat").exists()


def test_apply_once_without_write_prints_the_plan(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    out = capsys.readouterr().out
    assert "family: chat" in out
    assert "mint a new LiteLLM key" in out
    assert "create sandbox chat-s1" in out


def test_apply_once_with_write_actually_applies(registry_root: Path, state_root: Path) -> None:
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=driver,
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert litellm.minted == 1
    assert read_creds(paths.creds_path(state_root, "chat")) is not None
    assert driver.ops() == ("create", "set_egress", "assert_egress")


def _write_caregiver_token(state_root: Path) -> None:
    """What `attendance` mints on a host that has run `up` before."""
    token = paths.caregiver_token_path(state_root)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("fixture-caregiver-token-" + "m" * 32, encoding="utf-8")
    token.chmod(0o600)


def test_apply_once_runs_when_the_caregiver_token_exists(
    registry_root: Path, state_root: Path
) -> None:
    """With a token file present the CLI goes on to read `args.attendance_url`,
    which `apply-once` must declare, or it dies of an AttributeError before
    it builds the family. Every other test here has no token file, so
    `read_token` refuses first and would hide it.
    Nothing listens at the address, so the handshake is skipped and the
    family still comes up."""
    _write_caregiver_token(state_root)
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--sessiond-url",
            "http://127.0.0.1:9",
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_OK
    assert read_creds(paths.creds_path(state_root, "chat")) is not None


def test_apply_once_takes_attendance_by_its_unix_socket(
    registry_root: Path, state_root: Path, tmp_path: Path
) -> None:
    """A gate runs `attendance` on a Unix socket and nothing else, so a URL
    alone can never reach it and the sandbox would stay `creating`."""
    _write_caregiver_token(state_root)
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--sessiond-socket",
            str(tmp_path / "no-such.sock"),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_OK


def test_apply_once_plan_on_an_invalid_family_says_so_and_stops(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_PROBLEM
    assert "INVALID" in capsys.readouterr().out


def test_apply_once_of_an_unknown_family_is_a_usage_error(
    registry_root: Path, state_root: Path
) -> None:
    code = main(
        [
            "apply-once",
            str(registry_root),
            "no-such-family",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_USAGE


def test_apply_once_of_a_directory_with_no_family_file_is_a_usage_error(
    registry_root: Path, state_root: Path
) -> None:
    """Contract 01 §5.6 rule 3: `caregiver` ignores the directory."""
    write_no_file_dir(registry_root)
    words = ["apply-once", str(registry_root), "stray", "--image", "sha256:x"]
    litellm = FakeLiteLLMKeys()

    code = main(
        [*words, "--state-root", str(state_root), "--write"], driver=FakeDriver(), litellm=litellm
    )

    assert code == EXIT_USAGE
    assert litellm.minted == 0
    assert not paths.family_dir(state_root, "stray").exists()


def test_apply_once_a_second_time_reports_refresh_not_mint(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "apply-once",
        str(registry_root),
        "chat",
        "--image",
        "sha256:x",
        "--state-root",
        str(state_root),
        "--write",
    ]
    main(args, driver=FakeDriver(), litellm=FakeLiteLLMKeys())
    capsys.readouterr()
    main(args, driver=FakeDriver(), litellm=FakeLiteLLMKeys())
    out = capsys.readouterr().out
    assert "refresh the existing LiteLLM key" in out
    assert "already exists; leave it" in out


# --- status ------------------------------------------------------------


def test_status_of_a_never_applied_registry_says_so(
    state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["status", "--state-root", str(state_root)])
    assert code == EXIT_OK
    assert "no family has a status document" in capsys.readouterr().err


def test_status_of_an_unknown_family_is_a_usage_error(state_root: Path) -> None:
    code = main(["status", "chat", "--state-root", str(state_root)])
    assert code == EXIT_USAGE


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_status_of_a_document_that_does_not_read_is_a_usage_error(
    state_root: Path, capsys: pytest.CaptureFixture[str], raw: bytes
) -> None:
    path = paths.status_path(state_root, "chat")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)

    code = main(["status", "chat", "--state-root", str(state_root)])

    assert code == EXIT_USAGE
    assert "no status document for 'chat'" in capsys.readouterr().err


def test_status_after_apply_prints_the_family_and_state(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    capsys.readouterr()
    code = main(["status", "chat", "--state-root", str(state_root)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "chat: in_sync" in out


def test_status_json_round_trips_the_document(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    capsys.readouterr()
    main(["status", "chat", "--json", "--state-root", str(state_root)])
    body = json.loads(capsys.readouterr().out)
    assert body["family"] == "chat"
    assert body["state"] == "in_sync"


def test_status_with_no_family_lists_every_family(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_registry(
        registry_root, name="code", model={"router": "code-router", "budget_usd_per_day": 30}
    )
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    main(
        [
            "apply-once",
            str(registry_root),
            "code",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    capsys.readouterr()
    main(["status", "--json", "--state-root", str(state_root)])
    body = json.loads(capsys.readouterr().out)
    assert {doc["family"] for doc in body} == {"chat", "code"}


# --- delete ------------------------------------------------------------


def test_delete_without_write_makes_no_change(state_root: Path) -> None:
    grant = paths.grant_path(state_root, "chat")
    grant.parent.mkdir(parents=True)
    grant.write_text("{}", encoding="utf-8")
    litellm = FakeLiteLLMKeys()
    code = main(
        ["delete", "chat", "--state-root", str(state_root)], litellm=litellm, driver=FakeDriver()
    )
    assert code == EXIT_OK
    assert litellm.deleted == []
    assert grant.exists()


def test_delete_without_write_prints_the_plan(
    state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        ["delete", "chat", "--state-root", str(state_root)],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    out = capsys.readouterr().out
    assert "family: chat" in out
    assert "delete the LiteLLM key" in out


def test_delete_with_write_actually_deletes(registry_root: Path, state_root: Path) -> None:
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state_root),
            "--write",
        ],
        driver=driver,
        litellm=litellm,
    )
    code = main(
        ["delete", "chat", "--state-root", str(state_root), "--write"],
        driver=driver,
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert not paths.family_dir(state_root, "chat").exists()
    assert any(call.op == "destroy" for call in driver.calls)


class KeyStays(FakeLiteLLMKeys):
    """A LiteLLM that refuses the delete of a key."""

    def delete_key(self, family: str) -> None:
        raise LiteLLMError(f"key delete failed for {family!r}: HTTP 500")


class NoDestroy(FakeDriver):
    """A driver whose destroy fails."""

    def destroy(self, name: str, allow: tuple[str, ...]) -> None:
        raise DriverError(f"sbx rm {name} failed: the daemon does not answer")


def _applied(registry_root: Path, state_root: Path, driver: FakeDriver) -> None:
    """One family with its credentials and one sandbox."""
    words = ["apply-once", str(registry_root), "chat", "--image", "sha256:x"]
    code = main(
        [*words, "--state-root", str(state_root), "--write"],
        driver=driver,
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_OK


def test_delete_says_a_key_that_stays_in_one_line(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 13: the key is first. The delete stops there, and the verb
    says why in one line. The credential file and the sandbox stay."""
    driver = FakeDriver()
    _applied(registry_root, state_root, driver)
    capsys.readouterr()

    code = main(
        ["delete", "chat", "--state-root", str(state_root), "--write"],
        driver=driver,
        litellm=KeyStays(),
    )

    assert code == EXIT_PROBLEM
    assert capsys.readouterr().err.strip() == "caregiver: key delete failed for 'chat': HTTP 500"
    assert paths.creds_path(state_root, "chat").exists()
    assert "destroy" not in driver.ops()


def test_delete_says_a_destroy_that_fails_in_one_line(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The credentials are gone when the destroy fails, and the state of
    the family stays. A second run of the verb ends the work."""
    _applied(registry_root, state_root, FakeDriver())
    capsys.readouterr()
    delete = ["delete", "chat", "--state-root", str(state_root), "--write"]

    code = main(delete, driver=NoDestroy(), litellm=FakeLiteLLMKeys())

    assert code == EXIT_PROBLEM
    assert capsys.readouterr().err.strip() == (
        "caregiver: sbx rm chat-s1 failed: the daemon does not answer"
    )
    assert not paths.creds_path(state_root, "chat").exists()
    assert paths.family_dir(state_root, "chat").exists()
    assert main(delete, driver=FakeDriver(), litellm=FakeLiteLLMKeys()) == EXIT_OK
    assert not paths.family_dir(state_root, "chat").exists()
