"""delete_family: credentials die before the sandbox (invariant 13)."""

from __future__ import annotations

from pathlib import Path

from caregiver.delete import delete_family
from caregiver.driver import FakeDriver
from caregiver.egress import litellm_endpoint, pep_endpoint
from caregiver.litellm_keys import FakeLiteLLMKeys, key_alias

from caregiver import paths


def seed(state_root: Path, family: str, *, sandboxes: int = 1) -> None:
    """A family with a grant file, credentials and a sandbox count, as
    `apply_once` would have left it."""
    grant = paths.grant_path(state_root, family)
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text("{}", encoding="utf-8")

    creds = paths.creds_path(state_root, family)
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}", encoding="utf-8")

    seq = paths.sandbox_seq_path(state_root, family)
    seq.parent.mkdir(parents=True, exist_ok=True)
    seq.write_text(f"{sandboxes}\n", encoding="utf-8")


def test_credentials_are_deleted_before_the_sandbox(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    log: list[str] = []

    class RecordingLiteLLM(FakeLiteLLMKeys):
        def delete_key(self, family: str) -> None:
            log.append(f"litellm.delete_key:{family}")
            super().delete_key(family)

    class RecordingDriver(FakeDriver):
        def destroy(self, name: str, allow: tuple[str, ...]) -> None:
            log.append(f"driver.destroy:{name}")
            super().destroy(name, allow)

    delete_family(
        "chat", state_root=state_root, driver=RecordingDriver(), litellm=RecordingLiteLLM()
    )
    assert log == ["litellm.delete_key:chat", "driver.destroy:chat-s1"]


def test_the_grant_file_and_creds_are_already_gone_when_the_sandbox_is_destroyed(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    litellm = FakeLiteLLMKeys()
    grant_path = paths.grant_path(state_root, "chat")
    creds_path = paths.creds_path(state_root, "chat")
    checked: dict[str, bool] = {}

    class CheckingDriver(FakeDriver):
        def destroy(self, name: str, allow: tuple[str, ...]) -> None:
            checked["key_deleted"] = key_alias("chat") in litellm.deleted
            checked["grant_gone"] = not grant_path.exists()
            checked["creds_gone"] = not creds_path.exists()
            super().destroy(name, allow)

    delete_family("chat", state_root=state_root, driver=CheckingDriver(), litellm=litellm)
    assert checked == {"key_deleted": True, "grant_gone": True, "creds_gone": True}


def test_the_litellm_key_is_deleted_by_alias(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    litellm = FakeLiteLLMKeys()
    delete_family("chat", state_root=state_root, driver=FakeDriver(), litellm=litellm)
    assert litellm.deleted == [key_alias("chat")]


def test_every_sandbox_the_family_ever_had_is_destroyed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat", sandboxes=3)
    driver = FakeDriver()
    delete_family("chat", state_root=state_root, driver=driver, litellm=FakeLiteLLMKeys())
    destroyed = [call.args[0] for call in driver.calls if call.op == "destroy"]
    assert destroyed == ["chat-s1", "chat-s2", "chat-s3"]


def test_a_family_with_no_sandbox_yet_destroys_nothing(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat", sandboxes=0)
    driver = FakeDriver()
    delete_family("chat", state_root=state_root, driver=driver, litellm=FakeLiteLLMKeys())
    assert driver.calls == []


def test_egress_is_passed_through_to_every_destroy_call(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat", sandboxes=1)
    driver = FakeDriver()
    delete_family(
        "chat",
        state_root=state_root,
        driver=driver,
        litellm=FakeLiteLLMKeys(),
        egress=("github.com",),
    )
    assert driver.calls[0].args[1] == (litellm_endpoint(), pep_endpoint(), "github.com")


def test_the_two_plane_rows_are_removed_even_with_no_family_egress(tmp_path: Path) -> None:
    """Contract 05 §4.4 step 3: a caller that passes only the family's own
    list (or nothing at all) still leaks two rows per sandbox unless this
    function adds them itself."""
    state_root = tmp_path / "state"
    seed(state_root, "chat", sandboxes=1)
    driver = FakeDriver()
    delete_family("chat", state_root=state_root, driver=driver, litellm=FakeLiteLLMKeys())
    assert driver.calls[0].args[1] == (litellm_endpoint(), pep_endpoint())


def test_the_familys_state_directory_is_removed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    delete_family("chat", state_root=state_root, driver=FakeDriver(), litellm=FakeLiteLLMKeys())
    assert not paths.family_dir(state_root, "chat").exists()


def test_deleting_a_family_that_never_existed_does_not_raise(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    delete_family(
        "never-applied", state_root=state_root, driver=FakeDriver(), litellm=FakeLiteLLMKeys()
    )


def test_deleting_twice_does_not_raise(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    seed(state_root, "chat")
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    delete_family("chat", state_root=state_root, driver=driver, litellm=litellm)
    delete_family("chat", state_root=state_root, driver=driver, litellm=litellm)
