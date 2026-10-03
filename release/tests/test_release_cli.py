"""`agent-releasectl check` and `… resolve`, end to end on a fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME, RETIRING, Repo
from agent_release.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from agent_release.state import MAX_STATE_BYTES
from release_fixtures import manifest_text, provides_entry, requires_entry, write_manifest

RELEASE_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DE"

#: `pep` provides pep-grant 2.1 and `attendance` calls it at 2.0.
EDGES: dict[str, tuple[str, str]] = {
    "pep": (provides_entry("pep-grant", 2, 1), ""),
    "attendance": ("", requires_entry("pep-grant", 2, 0)),
}


def _repo_roots(tmp_path: Path, *, floor: int = 0) -> list[str]:
    """One root per repo, holding all nine of contract 06 §1's components."""
    roots = {repo: tmp_path / str(repo) for repo in Repo}
    for row in CATALOG:
        provides, requires = EDGES.get(row.name, ("", ""))
        if row.name == "attendance" and floor:
            requires = requires_entry("pep-grant", 2, floor)

        write_manifest(
            roots[row.repo],
            row.name,
            manifest_text(row.name, provides=provides, requires=requires),
        )

    return [str(path) for path in roots.values()]


def _roots_argv(tmp_path: Path, *, floor: int = 0) -> list[str]:
    argv: list[str] = []
    for root in _repo_roots(tmp_path, floor=floor):
        argv.extend(["--root", root])

    return argv


def _state_file(tmp_path: Path) -> str:
    facts = {
        row.name: {
            "sha": f"{index:040x}",
            "input_digest": "sha256:" + f"{index:064x}",
            "artifact_digest": None,
        }
        for index, row in enumerate(CATALOG)
    }
    body = {
        "live": {"pep": "2.0.3", "attendance": "1.4.7"},
        "provided": {"pep-grant": "2.0"},
        "latest": {"pep": "2.1.0"},
        "facts": facts,
    }
    path = tmp_path / "live-state.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return str(path)


def test_check_passes_on_a_complete_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", *_roots_argv(tmp_path)])

    assert code == EXIT_OK
    assert "check: PASS" in capsys.readouterr().out


def test_check_passes_when_a_retiring_components_directory_is_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tree after the commit that deletes a retiring component. The
    requester a host already runs must still read it (`catalog.RETIRING`)."""
    argv = _roots_argv(tmp_path)
    for name in RETIRING:
        row = CATALOG_BY_NAME[name]
        (tmp_path / str(row.repo) / row.path / "component.yaml").unlink()

    code = main(["check", *argv])

    assert code == EXIT_OK
    assert "check: PASS" in capsys.readouterr().out


@pytest.mark.usefixtures("operator_is_an_account")
def test_check_with_site_passes_on_a_complete_site(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["check", "--site", *_roots_argv(tmp_path)])

    assert code == EXIT_OK
    assert "check: PASS" in capsys.readouterr().out


def test_check_with_site_refuses_an_operator_this_host_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The example site is complete, and its operator is no account of this
    machine. The executor could not start here, so the hook must not pass."""
    code = main(["check", "--site", *_roots_argv(tmp_path)])

    assert code == EXIT_REFUSED
    assert "no account of this host" in capsys.readouterr().err


def test_check_with_site_refuses_a_site_that_lacks_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The release executor's own verify hook runs `check --site`. A new
    executor that could not start on this host's site file must fail the
    hook while the old one is still there to put back."""
    lacking = tmp_path / "site.env"
    lacking.write_text("AGENT_GITHUB_OWNER=example-owner\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_SITE_FILE", str(lacking))

    code = main(["check", "--site", *_roots_argv(tmp_path)])

    assert code == EXIT_REFUSED
    assert "AGENT_OPERATOR_USER" in capsys.readouterr().err


def test_check_refuses_an_incomplete_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_manifest(tmp_path, "pep", manifest_text("pep"))

    code = main(["check", "--root", str(tmp_path)])

    assert code == EXIT_REFUSED
    assert "no component.yaml under any root" in capsys.readouterr().err


def test_partial_accepts_one_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_manifest(tmp_path, "pep", manifest_text("pep"))

    code = main(["check", "--root", str(tmp_path), "--partial"])

    assert code == EXIT_OK
    assert "missing: attendance" in capsys.readouterr().out


def test_check_json_reports_every_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["check", *_roots_argv(tmp_path), "--json"])
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert report["ok"] is True
    assert len(report["components"]) == len(CATALOG)
    assert report["missing"] == []


def test_resolve_prints_the_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["resolve", "pep=2.1.0", *_roots_argv(tmp_path), "--state", _state_file(tmp_path)]

    code = main(argv)
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "pep             deploy     2.0.3     2.1.0     pep-v2.1.0" in out
    assert "order: pep" in out
    assert "manifest_sha256" not in out


def test_resolve_prints_the_hash_with_an_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "resolve",
        "pep=latest",
        *_roots_argv(tmp_path),
        "--state",
        _state_file(tmp_path),
        "--id",
        RELEASE_ID,
    ]

    code = main(argv)
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "manifest_sha256: sha256:" in out


def test_resolve_json_carries_the_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "resolve",
        "pep=2.1.0",
        *_roots_argv(tmp_path),
        "--state",
        _state_file(tmp_path),
        "--id",
        RELEASE_ID,
        "--requested-by",
        "agent-control",
        "--json",
    ]

    code = main(argv)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert report["manifest"]["id"] == RELEASE_ID
    assert report["manifest"]["requested_by"] == "agent-control"
    assert report["manifest"]["order"] == ["pep"]


def test_a_broken_floor_names_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`stage7-releases.md` §7.1's acceptance run: a fixture with a floor pep cannot meet."""
    argv = ["resolve", "attendance=1.5.0", *_roots_argv(tmp_path, floor=4)]

    code = main(argv)
    error = capsys.readouterr().err

    assert code == EXIT_REFUSED
    assert "refused [C1] pep-grant" in error
    assert "attendance requires pep-grant 2.4" in error
    assert "pep provides 2.1" in error


def test_a_refusal_in_json_is_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["resolve", "attendance=1.5.0", *_roots_argv(tmp_path, floor=4), "--json"]

    code = main(argv)
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert report["ok"] is False
    assert report["check"] == "C1"


def test_an_oversized_state_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fat = tmp_path / "fat.json"
    fat.write_text("{}" + (" " * MAX_STATE_BYTES), encoding="utf-8")
    argv = ["resolve", *_roots_argv(tmp_path), "--state", str(fat)]

    code = main(argv)

    assert code == EXIT_REFUSED
    assert "larger than" in capsys.readouterr().err


def test_a_missing_state_file_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["resolve", *_roots_argv(tmp_path), "--state", str(tmp_path / "absent.json")]

    code = main(argv)

    assert code == EXIT_USAGE
    assert "agent-releasectl:" in capsys.readouterr().err
