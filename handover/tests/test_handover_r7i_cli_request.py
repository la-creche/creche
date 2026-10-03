"""`handover request`, end to end on a fixture repository.

The command the operator types. It resolves with the real resolver, prints §2.5's
seven fields, and writes ONE file into the spool. `--dry-run` writes nothing.

`handover_fixtures.py` builds the manifests, so every refusal here is the one
the test names and not a fixture that was already broken.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from handover.catalog import CATALOG
from handover.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from handover.executor.request import parse_request
from handover_fixtures import manifest_text, write_manifest

SPOOL = "spool"


def _tree(tmp_path: Path) -> list[str]:
    """One root with every component, and the live state `latest` needs.

    `caregiver` publishes that document on the host and the executor reads the
    same path at step 2 (contract 06 §11), so a request naming `latest` needs
    one on this side too or it cannot say what `latest` is.
    """
    root = tmp_path / "repo"
    for row in CATALOG:
        write_manifest(root, row.name, manifest_text(row.name))

    return ["--root", str(root), "--partial", "--state", _state_file(tmp_path)]


def _state_file(tmp_path: Path) -> str:
    body = {
        "live": {"chaperone": "2.0.3", "attendance": "1.4.7"},
        "provided": {},
        "latest": {"chaperone": "2.1.0", "attendance": "1.5.0"},
        "facts": {},
    }
    path = tmp_path / "live-state.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return str(path)


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / SPOOL
    directory.mkdir()

    return directory


def _filed(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def test_one_request_lands_and_the_executor_can_parse_it(tmp_path: Path) -> None:
    spool = _spool(tmp_path)

    code = main(["request", "chaperone", *_tree(tmp_path), "--spool", str(spool)])

    assert code == EXIT_OK
    written = _filed(spool)
    assert len(written) == 1
    request = parse_request(written[0].read_bytes(), written[0].stem)
    assert request.wanted() == {"chaperone": "latest"}
    assert request.requested_by == "human"


def test_a_version_after_the_at_sign_reaches_the_file(tmp_path: Path) -> None:
    """The argument's shape: `<component>[@<version>]`."""
    spool = _spool(tmp_path)

    main(["request", "chaperone@2.1.0", *_tree(tmp_path), "--spool", str(spool)])

    request = parse_request(_filed(spool)[0].read_bytes(), _filed(spool)[0].stem)
    assert request.wanted() == {"chaperone": "2.1.0"}


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    spool = _spool(tmp_path)

    code = main(["request", "chaperone", *_tree(tmp_path), "--spool", str(spool), "--dry-run"])

    assert code == EXIT_OK
    assert _filed(spool) == []


def test_the_phone_fields_are_printed_before_the_file_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.5's seven fields, so the operator reads what the phone will ask them to tap."""
    main(["request", "chaperone", *_tree(tmp_path), "--spool", str(_spool(tmp_path)), "--dry-run"])
    printed = capsys.readouterr().out

    for field in ("review", "components", "contracts", "restarts", "restore", "requested_by"):
        assert field in printed


def test_no_gate_id_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """§2.5: root is the only actor that computes `manifest_sha256`, and the
    gate derives from it. Printing one here would claim the tap binds to this
    side's arithmetic."""
    main(["request", "chaperone", *_tree(tmp_path), "--spool", str(_spool(tmp_path)), "--dry-run"])
    printed = capsys.readouterr().out

    assert "gate" not in printed.lower()
    assert "root computes it at step 2" in printed


def test_the_review_row_never_claims_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root says `safe:` only after the provenance predicate has run at the
    tag (§2.4 step 3). This side has run none of it."""
    main(["request", "chaperone", *_tree(tmp_path), "--spool", str(_spool(tmp_path)), "--dry-run"])
    printed = capsys.readouterr().out

    assert "safe:" not in printed
    assert "local:" in printed


def test_a_component_the_catalog_does_not_list_is_refused(tmp_path: Path) -> None:
    spool = _spool(tmp_path)

    code = main(["request", "nosuch", *_tree(tmp_path), "--spool", str(spool)])

    assert code == EXIT_REFUSED
    assert _filed(spool) == []


def test_a_malformed_version_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """The executor's own reason, before the tap instead of after it."""
    spool = _spool(tmp_path)

    code = main(["request", "chaperone@2.1", *_tree(tmp_path), "--spool", str(spool)])

    assert code == EXIT_REFUSED
    assert _filed(spool) == []


def test_a_spool_this_host_does_not_have_is_a_usage_error(tmp_path: Path) -> None:
    """A refusal is what root would say about the request. A missing spool is
    this host's fault and gets a different exit code."""
    code = main(["request", "chaperone", *_tree(tmp_path), "--spool", str(tmp_path / "absent")])

    assert code == EXIT_USAGE


def test_json_prints_one_object_with_the_id_and_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spool = _spool(tmp_path)

    main(["request", "chaperone", *_tree(tmp_path), "--spool", str(spool), "--json"])
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["filed"] == str(_filed(spool)[0])
    assert set(report["phone"]) == {
        "review",
        "components",
        "contracts",
        "restarts",
        "restore",
        "requested_by",
        "manifest",
    }


def test_a_rollback_carries_its_target(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    target = "01K5J8M2Q7V3X9R4T6N0B8C2DE"

    code = main(
        ["request", "chaperone", *_tree(tmp_path), "--spool", str(spool), "--rollback-of", target]
    )

    assert code == EXIT_OK
    request = parse_request(_filed(spool)[0].read_bytes(), _filed(spool)[0].stem)
    assert request.rollback_of == target
    assert str(request.kind) == "rollback"


def test_two_requests_land_as_two_files(tmp_path: Path) -> None:
    """Each mint is a fresh ULID, so a second request never replaces the
    first one's name."""
    spool = _spool(tmp_path)
    roots = _tree(tmp_path)

    main(["request", "chaperone", *roots, "--spool", str(spool)])
    main(["request", "attendance", *roots, "--spool", str(spool)])

    assert len(_filed(spool)) == 2
