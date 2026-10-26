"""The chaperone is a binary component of its own repository.

The Rust chaperone lives in the chaperone repository. That repository's
`release.yml` builds two programs, packs them into one archive, signs it
and makes the tag `chaperone-v<version>`. The catalog row says where the
executor finds the manifest, which workflow signed the archive and which
paths move the component.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.allocate import touched
from handover.artifacts import archive_components, asset_names, rows, signer_policy
from handover.catalog import (
    BINARY_BUILD_FILES,
    CATALOG_BY_NAME,
    RELEASE_WORKFLOW,
    CatalogRow,
    Kind,
    Releases,
    Repo,
    RunsAs,
    VerifyUser,
)
from handover.cli import EXIT_OK, main
from handover.discovery import read_one
from handover.executor.source import digest_paths
from handover.manifest import MANIFEST_FILENAME

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

#: The manifest that the chaperone repository must hold at its root.
MANIFEST = HERE / "handover_chaperone_component.toml"

OWNER = "example-owner"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

#: The two build commands of `release.yml` in the chaperone repository.
BUILD = (
    (
        "cargo",
        "install",
        "--locked",
        "--no-track",
        "--path",
        "crates/chaperone",
        "--bin",
        "chaperone",
    ),
    (
        "cargo",
        "install",
        "--locked",
        "--no-track",
        "--path",
        "crates/run-as",
        "--bin",
        "chaperone-as",
    ),
)


def _body(text: str) -> str:
    """The manifest without its leading comment lines."""
    lines = text.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if not line.startswith("#"))

    return "".join(lines[start:])


def test_the_chaperone_row_is_a_binary_of_its_own_repository() -> None:
    assert CATALOG_BY_NAME["chaperone"] == CatalogRow(
        "chaperone", Repo.CHAPERONE, ".", Kind.BINARY, Releases.YES
    )
    assert str(Repo.CHAPERONE) == "chaperone"


def test_the_chaperone_repository_signs_with_its_release_workflow() -> None:
    policy = signer_policy(Repo.CHAPERONE, OWNER, "chaperone", COMMIT)

    assert RELEASE_WORKFLOW[Repo.CHAPERONE] == "release.yml"
    assert policy.identity() == (
        f"https://github.com/{OWNER}/chaperone/.github/workflows/release.yml@refs/heads/main"
    )


def test_the_build_files_are_those_of_each_repository() -> None:
    """The Cargo workspace of the chaperone repository is at its root."""
    assert BINARY_BUILD_FILES[Repo.CHAPERONE] == ("Cargo.lock", "Cargo.toml", "rust-toolchain.toml")
    assert Repo.AGENT_MCP not in BINARY_BUILD_FILES
    assert Repo.AGENT_REGISTRY not in BINARY_BUILD_FILES


def test_each_path_of_the_chaperone_repository_moves_chaperone() -> None:
    changed = ("crates/core/src/lib.rs", "plugins/default/bearer/src/lib.rs", "Cargo.lock")

    assert touched(changed, Repo.CHAPERONE) == ("chaperone",)


def test_a_path_of_this_repository_no_longer_moves_chaperone() -> None:
    assert touched(("chaperone/src/chaperone/call.py",), Repo.AGENT_CONTROL) == ()


def test_the_digest_of_chaperone_is_its_whole_repository() -> None:
    assert digest_paths("chaperone") == (".",)


def test_the_assets_have_the_names_that_its_workflow_publishes() -> None:
    names = asset_names("chaperone")

    assert archive_components(Repo.CHAPERONE) == ("chaperone",)
    assert names.archive == "chaperone-x86_64-unknown-linux-musl.tar.gz"
    assert names.bundle == "chaperone-x86_64-unknown-linux-musl.tar.gz.sigstore.json"


def test_the_tag_script_names_the_two_assets_of_chaperone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["allocate-tags", "--repo", "chaperone", "--assets", "chaperone"]

    assert main(argv) == EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "chaperone-x86_64-unknown-linux-musl.tar.gz 268435456",
        "chaperone-x86_64-unknown-linux-musl.tar.gz.sigstore.json 1048576",
    ]


def test_the_manifest_reads_at_the_root_of_the_chaperone_repository(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text(MANIFEST.read_text(encoding="utf-8"))

    manifest = read_one(tmp_path, "chaperone").manifest

    assert (manifest.repo, manifest.path, manifest.kind) == (Repo.CHAPERONE, ".", Kind.BINARY)
    assert manifest.runs_as is RunsAs.ROOT
    assert manifest.verify.user is VerifyUser.ROOT
    assert manifest.build == BUILD
    assert rows({"chaperone": manifest}, Repo.CHAPERONE)[0].commands == BUILD


def test_the_verify_command_names_the_listen_key() -> None:
    """`chaperone verify` reads the front door address from the site file,
    under the key `CHAPERONE_LISTEN`."""
    tail = '"--env-file", "/etc/creche/site.env", "--listen-key", "CHAPERONE_LISTEN"]'

    assert tail in MANIFEST.read_text(encoding="utf-8")


def test_the_handover_rules_hold_the_same_manifest_text() -> None:
    rules = (REPO_ROOT / "handover" / "AGENTS.md").read_text(encoding="utf-8")

    assert _body(MANIFEST.read_text(encoding="utf-8")) in rules


def test_this_repository_holds_no_manifest_of_chaperone() -> None:
    directory = REPO_ROOT / "chaperone"

    assert not (directory / "component.yaml").exists()
    assert not (directory / MANIFEST_FILENAME).exists()
