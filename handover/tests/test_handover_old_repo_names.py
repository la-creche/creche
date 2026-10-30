"""The old names of three repositories read as nothing.

The release that renamed `Repo` read them for one release, so the handover
that was installed then could release it. Every manifest now says the new
name, and an old name is a stranger like any other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from handover.catalog import Repo
from handover.errors import Refusal
from handover.manifest import Markup, parse_manifest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

CAREGIVER: Final = REPO_ROOT / "caregiver" / "component.yaml"

OLD_NAMES: Final = ("agent-control", "agent-mcp", "agent-registry")


@pytest.mark.parametrize("old", OLD_NAMES)
def test_an_old_name_is_no_repo(old: str) -> None:
    with pytest.raises(ValueError):
        Repo(old)


def test_each_manifest_of_this_tree_says_a_new_name() -> None:
    for path in sorted(REPO_ROOT.glob("*/component.yaml")):
        text = path.read_text(encoding="utf-8")
        assert not any(f"\nrepo: {old}\n" in text for old in OLD_NAMES), path


@pytest.mark.parametrize("old", OLD_NAMES)
def test_a_manifest_with_an_old_name_is_refused(old: str) -> None:
    text = CAREGIVER.read_text(encoding="utf-8").replace("\nrepo: creche\n", f"\nrepo: {old}\n")

    with pytest.raises(Refusal, match="field 'repo' is not one of its values"):
        parse_manifest(text, "caregiver/component.yaml", Markup.YAML)
