"""caregiver apply-once | status | delete: every mutating verb
prints its plan and needs --write to act."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caregiver.cli import EXIT_CONFIG, EXIT_OK, EXIT_PROBLEM, EXIT_USAGE
from caregiver.credentials import read_creds
from caregiver.litellm_keys import FakeLiteLLMKeys, LiteLLMError
from caregiver.vmm import UNREACHABLE
from caregiver_helpers import UNREADABLE_JSON, FakeVmm, main, write_no_file_dir, write_registry

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
    litellm = FakeLiteLLMKeys()
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--state-root",
            str(state_root),
        ],
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert litellm.minted == 0
    assert not paths.creds_path(state_root, "chat").exists()


def test_apply_once_without_write_prints_the_plan(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--state-root",
            str(state_root),
        ],
        litellm=FakeLiteLLMKeys(),
    )
    out = capsys.readouterr().out
    assert "family: chat" in out
    assert "mint a new LiteLLM key" in out
    assert "the config directory and the reach files" in out


def test_apply_once_with_write_actually_applies(registry_root: Path, state_root: Path) -> None:
    litellm = FakeLiteLLMKeys()
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--state-root",
            str(state_root),
            "--write",
        ],
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert litellm.minted == 1
    assert read_creds(paths.creds_path(state_root, "chat")) is not None
    assert paths.egress_path(state_root, "chat").exists()


def test_apply_once_plan_on_an_invalid_family_says_so_and_stops(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    code = main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--state-root",
            str(state_root),
            "--write",
        ],
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
            "--state-root",
            str(state_root),
        ],
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_USAGE


def test_apply_once_of_a_directory_with_no_family_file_is_a_usage_error(
    registry_root: Path, state_root: Path
) -> None:
    """Contract 01 §5.6 rule 3: `caregiver` ignores the directory."""
    write_no_file_dir(registry_root)
    words = ["apply-once", str(registry_root), "stray"]
    litellm = FakeLiteLLMKeys()

    code = main([*words, "--state-root", str(state_root), "--write"], litellm=litellm)

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
        "--state-root",
        str(state_root),
        "--write",
    ]
    main(args, litellm=FakeLiteLLMKeys())
    capsys.readouterr()
    main(args, litellm=FakeLiteLLMKeys())
    out = capsys.readouterr().out
    assert "refresh the existing LiteLLM key" in out


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
            "--state-root",
            str(state_root),
            "--write",
        ],
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
            "--state-root",
            str(state_root),
            "--write",
        ],
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
            "--state-root",
            str(state_root),
            "--write",
        ],
        litellm=FakeLiteLLMKeys(),
    )
    main(
        [
            "apply-once",
            str(registry_root),
            "code",
            "--state-root",
            str(state_root),
            "--write",
        ],
        litellm=FakeLiteLLMKeys(),
    )
    capsys.readouterr()
    main(["status", "--json", "--state-root", str(state_root)])
    body = json.loads(capsys.readouterr().out)
    assert {doc["family"] for doc in body} == {"chat", "code"}


# --- delete ------------------------------------------------------------


def test_delete_without_write_makes_no_change(state_root: Path) -> None:
    fragment = paths.policy_fragment_path(state_root, "chat")
    fragment.parent.mkdir(parents=True)
    fragment.write_text("[apps.chat]\n", encoding="utf-8")
    litellm = FakeLiteLLMKeys()
    code = main(["delete", "chat", "--state-root", str(state_root)], litellm=litellm, vmm=FakeVmm())
    assert code == EXIT_OK
    assert litellm.deleted == []
    assert fragment.exists()


def test_delete_without_write_prints_the_plan(
    state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        ["delete", "chat", "--state-root", str(state_root)],
        vmm=FakeVmm(),
        litellm=FakeLiteLLMKeys(),
    )
    out = capsys.readouterr().out
    assert "family: chat" in out
    assert "delete the LiteLLM key" in out
    assert "drop every session of the family on the vmm" in out


def test_delete_with_write_actually_deletes(registry_root: Path, state_root: Path) -> None:
    vmm = FakeVmm()
    litellm = FakeLiteLLMKeys()
    main(
        [
            "apply-once",
            str(registry_root),
            "chat",
            "--state-root",
            str(state_root),
            "--write",
        ],
        litellm=litellm,
    )
    code = main(
        ["delete", "chat", "--state-root", str(state_root), "--write"],
        vmm=vmm,
        litellm=litellm,
    )
    assert code == EXIT_OK
    assert not paths.family_dir(state_root, "chat").exists()
    assert vmm.dropped == ["chat"]


def test_delete_with_write_and_no_vmm_socket_names_the_key(
    state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Decision CH20: the unit names `vmm.sock` in `CAREGIVER_VMM_SOCKET`.
    With no value the verb would leave the VMs of the family running, so it
    stops before the first step."""
    litellm = FakeLiteLLMKeys()
    code = main(
        ["delete", "chat", "--state-root", str(state_root), "--write"],
        litellm=litellm,
        environ={},
    )
    assert code == EXIT_CONFIG
    assert "CAREGIVER_VMM_SOCKET" in capsys.readouterr().err
    assert litellm.deleted == []


class KeyStays(FakeLiteLLMKeys):
    """A LiteLLM that refuses the delete of a key."""

    def delete_key(self, family: str) -> None:
        raise LiteLLMError(f"key delete failed for {family!r}: HTTP 500")


def _applied(registry_root: Path, state_root: Path) -> None:
    """One family with its credentials."""
    words = ["apply-once", str(registry_root), "chat"]
    code = main(
        [*words, "--state-root", str(state_root), "--write"],
        litellm=FakeLiteLLMKeys(),
    )
    assert code == EXIT_OK


def test_delete_says_a_key_that_stays_in_one_line(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 13: the key is first. The delete stops there, and the verb
    says why in one line. The credential file and the sessions stay."""
    vmm = FakeVmm()
    _applied(registry_root, state_root)
    capsys.readouterr()

    code = main(
        ["delete", "chat", "--state-root", str(state_root), "--write"],
        vmm=vmm,
        litellm=KeyStays(),
    )

    assert code == EXIT_PROBLEM
    assert capsys.readouterr().err.strip() == "caregiver: key delete failed for 'chat': HTTP 500"
    assert paths.creds_path(state_root, "chat").exists()
    assert vmm.dropped == []


def test_delete_says_a_drop_that_fails_in_one_line(
    registry_root: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The credentials are gone when the drop fails, and the state of the
    family stays. A second run of the verb ends the work."""
    _applied(registry_root, state_root)
    capsys.readouterr()
    delete = ["delete", "chat", "--state-root", str(state_root), "--write"]

    code = main(delete, vmm=FakeVmm(refuse=UNREACHABLE), litellm=FakeLiteLLMKeys())

    assert code == EXIT_PROBLEM
    assert capsys.readouterr().err.strip() == (
        "caregiver: unreachable: the fake vmm refused the drop of chat"
    )
    assert not paths.creds_path(state_root, "chat").exists()
    assert paths.family_dir(state_root, "chat").exists()
    assert main(delete, vmm=FakeVmm(), litellm=FakeLiteLLMKeys()) == EXIT_OK
    assert not paths.family_dir(state_root, "chat").exists()
