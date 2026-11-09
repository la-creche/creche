"""delete_family: credentials die before the vmm stops a VM (invariant 13,
contract 05 §4.4, decisions CH21-10, CH33 rule 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from caregiver.delete import delete_family
from caregiver.litellm_keys import FakeLiteLLMKeys, key_alias
from caregiver.vmm import UNREACHABLE, VmmError
from caregiver_helpers import FakeVmm, fake_policy

from caregiver import paths


def seed(state_root: Path, family: str) -> None:
    """A family as a pass leaves it: a fragment of `policy.toml`,
    credentials, the config directory and the two reach files."""
    fragment = paths.policy_fragment_path(state_root, family)
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(f"[apps.{family}]\n", encoding="utf-8")

    creds = paths.creds_path(state_root, family)
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}", encoding="utf-8")

    config = paths.config_dir(state_root, family)
    config.mkdir(parents=True)
    (config / "instructions.md").write_text("Be helpful.\n", encoding="utf-8")
    paths.egress_path(state_root, family).write_text('{"hosts": []}\n', encoding="utf-8")
    paths.repos_path(state_root, family).write_text('{"repos": ["creche"]}\n', encoding="utf-8")


def _delete(state_root: Path, vmm: FakeVmm, litellm: FakeLiteLLMKeys | None = None) -> None:
    delete_family(
        "chat",
        state_root=state_root,
        vmm=vmm,
        litellm=litellm if litellm is not None else FakeLiteLLMKeys(),
        policy=fake_policy(state_root),
    )


def test_credentials_are_deleted_before_the_drop(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    log: list[str] = []

    class RecordingLiteLLM(FakeLiteLLMKeys):
        def delete_key(self, family: str) -> None:
            log.append(f"litellm.delete_key:{family}")
            super().delete_key(family)

    vmm = FakeVmm(on_drop=lambda family: log.append(f"vmm.drop_family:{family}"))
    _delete(state_root, vmm, RecordingLiteLLM())

    assert log == ["litellm.delete_key:chat", "vmm.drop_family:chat"]


def test_the_family_files_are_gone_when_the_vmm_drops(tmp_path: Path) -> None:
    """Contract 05 §4.4, decision CH33 rule 3: the key, the fragment and each
    family file go before the drop, so a create that races the drop finds
    no `config/` and is refused."""
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    litellm = FakeLiteLLMKeys()
    checked: dict[str, bool] = {}

    def look(family: str) -> None:
        checked["key_deleted"] = key_alias(family) in litellm.deleted
        checked["fragment_gone"] = not paths.policy_fragment_path(state_root, family).exists()
        checked["creds_gone"] = not paths.creds_path(state_root, family).exists()
        checked["config_gone"] = not paths.config_dir(state_root, family).exists()
        checked["egress_gone"] = not paths.egress_path(state_root, family).exists()
        checked["repos_gone"] = not paths.repos_path(state_root, family).exists()
        checked["state_kept"] = paths.family_dir(state_root, family).is_dir()

    _delete(state_root, FakeVmm(on_drop=look), litellm)

    assert checked == {
        "key_deleted": True,
        "fragment_gone": True,
        "creds_gone": True,
        "config_gone": True,
        "egress_gone": True,
        "repos_gone": True,
        "state_kept": True,
    }


def test_the_vmm_drops_the_family(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    vmm = FakeVmm()

    _delete(state_root, vmm)

    assert vmm.dropped == ["chat"]


def test_the_litellm_key_is_deleted_by_alias(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    litellm = FakeLiteLLMKeys()

    _delete(state_root, FakeVmm(), litellm)

    assert litellm.deleted == [key_alias("chat")]


def test_the_familys_state_directory_is_removed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")

    _delete(state_root, FakeVmm())

    assert not paths.family_dir(state_root, "chat").exists()


def test_a_drop_that_fails_keeps_the_state_directory(tmp_path: Path) -> None:
    """The loop finds the family by its state directory, so a kept
    directory is what makes the next look run the delete again."""
    state_root = tmp_path / "state"
    seed(state_root, "chat")

    with pytest.raises(VmmError, match=UNREACHABLE):
        _delete(state_root, FakeVmm(refuse=UNREACHABLE))

    assert paths.family_dir(state_root, "chat").is_dir()
    assert not paths.creds_path(state_root, "chat").exists()


def test_deleting_a_family_that_never_existed_does_not_raise(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vmm = FakeVmm()

    delete_family(
        "never-applied",
        state_root=state_root,
        vmm=vmm,
        litellm=FakeLiteLLMKeys(),
        policy=fake_policy(state_root),
    )

    assert vmm.dropped == ["never-applied"]


def test_deleting_twice_does_not_raise(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    vmm = FakeVmm()

    _delete(state_root, vmm)
    _delete(state_root, vmm)

    assert vmm.dropped == ["chat", "chat"]
