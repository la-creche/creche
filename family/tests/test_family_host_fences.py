"""The two fences of the host (contract 01 §5.4, §5.5 rule 1): the reader of
the site keys, and what the validator does with and without them."""

from __future__ import annotations

import pytest
from agent_family.fences import (
    MOUNT_ROOTS_KEY,
    PLATFORM_FAMILIES_KEY,
    FenceError,
    Fences,
    read_fences,
)
from agent_family.grammar import PLATFORM_SERVER
from agent_family.report import Issues
from agent_family.validate import check_family
from family_helpers import edited, full_index, host

ROOTS = "/srv/agents/vault,/srv/agents/work/runner"


def test_the_site_keys_read_as_fences() -> None:
    fences = read_fences({MOUNT_ROOTS_KEY: ROOTS, PLATFORM_FAMILIES_KEY: "code,ops"})

    assert fences == Fences(
        mount_roots=("/srv/agents/vault", "/srv/agents/work/runner"),
        platform_families=frozenset({"code", "ops"}),
    )


@pytest.mark.parametrize("value", [None, ""])
def test_no_platform_family_is_no_platform_family(value: str | None) -> None:
    environ = {MOUNT_ROOTS_KEY: ROOTS}
    if value is not None:
        environ[PLATFORM_FAMILIES_KEY] = value

    assert read_fences(environ).platform_families == frozenset()


@pytest.mark.parametrize("value", [None, ""])
def test_no_mount_roots_is_a_fault(value: str | None) -> None:
    environ = {} if value is None else {MOUNT_ROOTS_KEY: value}
    with pytest.raises(FenceError, match=MOUNT_ROOTS_KEY):
        read_fences(environ)


@pytest.mark.parametrize(
    "roots",
    [
        "srv/agents/vault",
        "/",
        "/srv/agents/vault/",
        "/srv//agents",
        "/srv/agents/../etc",
        "/srv/agents/./vault",
        "/srv/agents/va ult",
        "/srv/agents/vault,",
        "/srv/agents",
        "/srv/agents/sessions/chat",
        "/srv/agents/vault,/srv/agents/vault",
    ],
)
def test_a_root_of_another_shape_is_a_fault(roots: str) -> None:
    """Each fault names the key, and never the value."""
    with pytest.raises(FenceError, match=MOUNT_ROOTS_KEY) as fault:
        read_fences({MOUNT_ROOTS_KEY: roots})

    assert roots not in str(fault.value)


@pytest.mark.parametrize("families", ["Code", "c", "code,", "code,code", "co_de"])
def test_a_family_of_another_shape_is_a_fault(families: str) -> None:
    with pytest.raises(FenceError, match=PLATFORM_FAMILIES_KEY):
        read_fences({MOUNT_ROOTS_KEY: ROOTS, PLATFORM_FAMILIES_KEY: families})


def _issues(fences: Fences | None, **changes: object) -> Issues:
    family = edited("chat", **changes)
    issues = Issues()
    check_family(family, "chat", full_index(), issues, host(), fences)
    return issues


def test_a_mount_outside_the_host_roots_is_an_error() -> None:
    fences = Fences(mount_roots=("/srv/agents/code",), platform_families=frozenset())
    issues = _issues(fences, files=[{"path": "/srv/agents/vault", "mode": "ro"}])

    assert any("under no allowed root" in issue.msg for issue in issues.frozen())


def test_no_platform_family_on_the_host_refuses_the_platform_server() -> None:
    fences = Fences(mount_roots=("/srv/agents",), platform_families=frozenset())
    issues = _issues(fences, files=[], tools={PLATFORM_SERVER: "all"})

    assert any("only no family may hold" in issue.msg for issue in issues.frozen())


def test_with_no_fences_each_fence_check_is_a_downgraded_warning() -> None:
    """Registry CI has no host. The caregiver of the host refuses the family."""
    issues = _issues(
        None,
        files=[{"path": "/etc", "mode": "ro"}],
        tools={PLATFORM_SERVER: "all"},
    )
    found = issues.frozen()

    assert not any("allowed root" in issue.msg for issue in found)
    assert not any("platform fence" in issue.msg for issue in found)
    downgraded = {issue.msg for issue in found if issue.downgraded}
    assert "no host fences: no mount root was checked" in downgraded
    assert f"no host fences: who may hold '{PLATFORM_SERVER}' was not checked" in downgraded
