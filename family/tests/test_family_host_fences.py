"""The fence of the host (contract 01 §5.5 rule 1): the reader of the site
key, and what the validator does with and without it. Minor 18 drops the
mount roots and `AGENT_MOUNT_ROOTS` (decision CH10-10)."""

from __future__ import annotations

import pytest
from agent_family.fences import PLATFORM_FAMILIES_KEY, FenceError, Fences, read_fences
from agent_family.grammar import PLATFORM_SERVER
from agent_family.report import Issues
from agent_family.validate import check_family
from family_helpers import edited, full_index, host


def test_the_site_key_reads_as_fences() -> None:
    fences = read_fences({PLATFORM_FAMILIES_KEY: "code,ops"})

    assert fences == Fences(platform_families=frozenset({"code", "ops"}))


@pytest.mark.parametrize("value", [None, ""])
def test_no_platform_family_is_no_platform_family(value: str | None) -> None:
    environ = {} if value is None else {PLATFORM_FAMILIES_KEY: value}

    assert read_fences(environ).platform_families == frozenset()


def test_the_mount_roots_key_is_not_read() -> None:
    """A host that still sets the dropped key gets the same fences."""
    environ = {"AGENT_MOUNT_ROOTS": "/srv/agents/vault", PLATFORM_FAMILIES_KEY: "code"}

    assert read_fences(environ) == Fences(platform_families=frozenset({"code"}))


@pytest.mark.parametrize("families", ["Code", "c", "code,", "code,code", "co_de"])
def test_a_family_of_another_shape_is_a_fault(families: str) -> None:
    with pytest.raises(FenceError, match=PLATFORM_FAMILIES_KEY):
        read_fences({PLATFORM_FAMILIES_KEY: families})


def _issues(fences: Fences | None, **changes: object) -> Issues:
    family = edited("chat", **changes)
    issues = Issues()
    check_family(family, "chat", full_index(), issues, host(), fences)
    return issues


def test_no_platform_family_on_the_host_refuses_the_platform_server() -> None:
    fences = Fences(platform_families=frozenset())
    issues = _issues(fences, tools={PLATFORM_SERVER: "all"})

    assert any("only no family may hold" in issue.msg for issue in issues.frozen())


def test_with_no_fences_each_fence_check_is_a_downgraded_warning() -> None:
    """Registry CI has no host. The caregiver of the host refuses the family."""
    issues = _issues(None, tools={PLATFORM_SERVER: "all"})
    found = issues.frozen()

    assert not any("platform fence" in issue.msg for issue in found)
    downgraded = {issue.msg for issue in found if issue.downgraded}
    assert f"no host fences: who may hold '{PLATFORM_SERVER}' was not checked" in downgraded
