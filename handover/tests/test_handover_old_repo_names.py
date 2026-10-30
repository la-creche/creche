"""The old names of three repositories read as their new names, for one release.

The handover that is installed releases the one that renamed `Repo`, and
every manifest still says the old name. So the reader maps each old name to
its new value. The release that moves every manifest to the new names
removes `OLD_REPO_NAMES`, and this file with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from handover.catalog import OLD_REPO_NAMES, Repo, repo_named
from handover.errors import Refusal
from handover.manifest import Markup, parse_manifest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: A manifest of this tree that still says `repo: agent-control`.
CAREGIVER: Final = REPO_ROOT / "caregiver" / "component.yaml"


@pytest.mark.parametrize(
    ("old", "new"),
    [("agent-control", Repo.CRECHE), ("agent-mcp", Repo.MCP), ("agent-registry", Repo.REGISTRY)],
)
def test_an_old_name_reads_as_its_new_value(old: str, new: Repo) -> None:
    assert repo_named(old) is new


@pytest.mark.parametrize("name", ["creche", "mcp", "registry", "chaperone"])
def test_a_new_name_reads_as_itself(name: str) -> None:
    assert repo_named(name) is Repo(name)


def test_no_other_name_reads() -> None:
    with pytest.raises(ValueError):
        repo_named("agent-nope")


def test_the_map_holds_three_names_and_no_new_one() -> None:
    assert sorted(OLD_REPO_NAMES) == ["agent-control", "agent-mcp", "agent-registry"]
    assert not set(OLD_REPO_NAMES) & {str(one) for one in Repo}


def test_a_manifest_with_the_old_name_reads() -> None:
    text = CAREGIVER.read_text(encoding="utf-8")
    assert "\nrepo: agent-control\n" in text

    manifest = parse_manifest(text, "caregiver/component.yaml", Markup.YAML)

    assert manifest.repo is Repo.CRECHE


def test_a_manifest_with_the_new_name_reads() -> None:
    text = CAREGIVER.read_text(encoding="utf-8").replace(
        "\nrepo: agent-control\n", "\nrepo: creche\n"
    )

    manifest = parse_manifest(text, "caregiver/component.yaml", Markup.YAML)

    assert manifest.repo is Repo.CRECHE


def test_a_manifest_with_an_unknown_name_is_refused() -> None:
    text = CAREGIVER.read_text(encoding="utf-8").replace(
        "\nrepo: agent-control\n", "\nrepo: agent-nope\n"
    )

    with pytest.raises(Refusal, match="field 'repo' is not one of its values"):
        parse_manifest(text, "caregiver/component.yaml", Markup.YAML)
