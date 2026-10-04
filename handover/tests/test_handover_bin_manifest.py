"""`kind: binary`: a component whose artifact is a compiled program.

The catalog holds no such component. So the manifest here is a fixture file
beside this module, under a name that discovery never reads. What is proved
is the reader: the kind parses, every other field keeps its rule, and the
kinds the reader knew before still read the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import CATALOG, Kind, RunsAs
from handover.discovery import MANIFEST_FILENAME, discover
from handover.errors import Refusal, RefusalCode
from handover.manifest import ComponentManifest, parse_manifest
from handover.silent import silent_manifest

#: The fixture manifest. Its name is not `component.yaml` on purpose.
FIXTURE = Path(__file__).resolve().parent / "handover_bin_component.yaml"

REPO_ROOT = Path(__file__).resolve().parents[2]

OPERATOR_TREE = "/home/operator/.local/components/noticeboard"


def _fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _parse(text: str) -> ComponentManifest:
    return parse_manifest(text, "fixture/component.yaml")


def test_a_binary_manifest_parses() -> None:
    parsed = _parse(_fixture())

    assert parsed.kind is Kind.BINARY
    assert parsed.name == "noticeboard"
    assert parsed.runs_as is RunsAs.OPERATOR
    assert parsed.unit == "creche-noticeboard.service"


def test_a_binary_build_is_a_list_of_argv_lists() -> None:
    """The build argv comes from the manifest, as a venv build does."""
    parsed = _parse(_fixture())

    assert parsed.build == (
        (
            "/usr/local/bin/cargo",
            "install",
            "--locked",
            "--no-track",
            "--path",
            "rust/crates/noticeboard",
        ),
    )


def test_a_binary_program_sits_under_the_install_tree() -> None:
    """The install layout is the venv layout: `<install.to>/bin/<name>`. So
    a verify command path keeps its shape when a component changes kind."""
    parsed = _parse(_fixture())

    assert parsed.install.to == OPERATOR_TREE
    assert parsed.install.prev == f"{OPERATOR_TREE}.prev"
    assert parsed.verify.command[0] == f"{OPERATOR_TREE}/bin/noticeboard-verify"


def test_a_binary_build_refuses_a_command_string() -> None:
    """The kind opens no second shape for `build`. A string reaches a shell."""
    start = _fixture().index("build:")
    end = _fixture().index("install:")
    text = _fixture()[:start] + 'build: "cargo install"\n' + _fixture()[end:]

    with pytest.raises(Refusal) as caught:
        _parse(text)

    assert caught.value.code is RefusalCode.MANIFEST


def test_a_kind_the_contract_does_not_name_is_refused() -> None:
    """The key set stays closed: a fifth kind adds one word, not a pattern."""
    with pytest.raises(Refusal) as caught:
        _parse(_fixture().replace("kind: binary", "kind: static-binary"))

    assert caught.value.code is RefusalCode.MANIFEST
    assert "kind" in caught.value.detail


def test_the_output_field_is_still_refused() -> None:
    """Contract 06 §8 names `output`. The reader refuses it for every kind:
    the executor picks the variable from the kind, so no manifest names a
    variable of a root child's environment."""
    with pytest.raises(Refusal) as caught:
        _parse(_fixture() + "\noutput: CARGO_INSTALL_ROOT\n")

    assert "unknown field: output" in caught.value.detail


def test_the_kinds_are_the_four_old_ones_and_binary() -> None:
    assert {str(one) for one in Kind} == {"venv", "binary", "oci-image", "compose", "data"}


def test_no_component_of_the_catalog_is_binary() -> None:
    """This change adds the kind and no component."""
    assert all(row.kind is not Kind.BINARY for row in CATALOG)


def test_an_uninstalled_component_never_reads_as_binary() -> None:
    """A silent manifest declares nothing, so its kind decides nothing. It
    stays a venv or a data component, as before."""
    for row in CATALOG:
        assert silent_manifest(row.name).kind in (Kind.VENV, Kind.DATA), row.name


def test_the_fixture_is_not_a_declaration() -> None:
    """`handover check` walks this repository for `component.yaml`. A
    fixture under that name is a second declaration of its component."""
    assert FIXTURE.name != MANIFEST_FILENAME
    found = discover([REPO_ROOT])

    assert found.manifests()["noticeboard"].kind is Kind.VENV


#: The flag that makes cargo build what the lock file pins, and refuse when
#: the lock file is not up to date.
LOCKED = "--locked"


def _unlocked(manifest: ComponentManifest) -> list[str]:
    """Each build argv of a binary component that does not carry `LOCKED`."""
    if manifest.kind is not Kind.BINARY:
        return []

    return [" ".join(argv) for argv in manifest.build if LOCKED not in argv]


def test_every_binary_component_builds_what_the_lock_file_pins() -> None:
    """A venv build carries `--frozen`, so root installs what `uv.lock`
    pins and resolves nothing. `--locked` is the same rule for cargo.
    Without it a build may resolve a newer crate than the one a reviewer
    read, and root runs that crate's build script.

    No fence after the build can see this fault, so the manifests of this
    repository are held to the flag here. None is binary today.
    """
    for name, manifest in discover([REPO_ROOT]).manifests().items():
        if manifest.kind is Kind.BINARY:
            assert manifest.build, f"{name}: a binary component with no build"

        assert _unlocked(manifest) == [], name


def test_the_fixture_build_is_locked_and_an_unlocked_one_is_seen() -> None:
    """The check above passes over an empty set today. This shows that it
    reads a binary manifest, and that it finds the missing flag."""
    locked = _parse(_fixture())
    unlocked = _parse(_fixture().replace(f'"{LOCKED}", ', ""))

    assert _unlocked(locked) == []
    assert len(_unlocked(unlocked)) == 1
