"""The component's verify hook (contract 06 §4, `chaperone/component.yaml`)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
import pytest
from chaperone.verify import EXIT_FAILED, EXIT_OK, main

PROBED: list[str] = []

#: Every argv the hook handed `subprocess.run`: its one question to systemd.
ASKED: list[list[str]] = []

#: The root conftest's example site sets `AGENT_LAN_ADDRESS=192.0.2.10`.
SITE_BIND: Final = "192.0.2.10:8300"

SINCE: Final = "2026-09-22T10:00:00Z"
GENERATED: Final = "/var/lib/agent-release/upstreams.yaml"
BASE: Final = "/opt/creche/chaperone/upstreams.yaml"

#: What `/healthz` answers on the host when nothing is wrong (contract 04 §10).
HEALTHY: Final = {
    "ok": True,
    "roster": {"state": "ok", "since": SINCE},
    "upstreams": {"serving": 11, "refused": 0},
}

#: A PEP with no roster source.
NO_ROSTER: Final = {"ok": True, "roster": {"state": "off", "since": None}, "upstreams": None}

#: `creche-chaperone.service`'s environment, as `systemctl show -p Environment
#: --value` prints it: one line, assignments split by spaces.
UNIT_ENV: Final = " ".join(
    [f"PEP_UPSTREAMS={BASE}", f"PEP_UPSTREAMS_GENERATED={GENERATED}", f"PEP_BIND={SITE_BIND}"]
)

#: The same unit with no generated roster to read.
UNIT_ENV_BEFORE_STAGE_7: Final = f"PEP_BIND={SITE_BIND}"


def answer(status: int, body: object) -> httpx.Response:
    return httpx.Response(status_code=status, json=body)


def fake_get(reply: httpx.Response | Exception) -> object:
    def get(url: str, **_: object) -> httpx.Response:
        PROBED.append(url)
        if isinstance(reply, Exception):
            raise reply

        return reply

    return get


def fake_systemctl(
    reply: str | Exception, code: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`subprocess.run` for `systemctl show`: `reply` is its stdout, or
    what it raises."""

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        ASKED.append(argv)
        if isinstance(reply, Exception):
            raise reply

        return subprocess.CompletedProcess(argv, code, stdout=f"{reply}\n", stderr="")

    return run


@pytest.fixture(autouse=True)
def _clean() -> None:
    PROBED.clear()
    ASKED.clear()


def run(
    monkeypatch: pytest.MonkeyPatch,
    reply: httpx.Response | Exception,
    unit: str | Exception = UNIT_ENV,
    unit_code: int = 0,
) -> int:
    monkeypatch.delenv("PEP_BIND", raising=False)
    monkeypatch.setattr("chaperone.verify.httpx.get", fake_get(reply))
    monkeypatch.setattr(subprocess, "run", fake_systemctl(unit, unit_code))

    return main(["--json"])


def body(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def failed_checks(report: dict[str, object]) -> dict[str, str]:
    """Each failed check's name, and the line it printed."""
    checks = report["checks"]
    assert isinstance(checks, list)

    return {one["name"]: one["detail"] for one in checks if not one["ok"]}  # type: ignore[index]


def test_a_healthy_pep_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(monkeypatch, answer(200, HEALTHY))

    assert code == EXIT_OK
    assert body(capsys)["ok"] is True


def test_the_report_carries_the_roster_and_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger's `verify chaperone:` line is this report, so a release that
    succeeded still says what the PEP served."""
    run(monkeypatch, answer(200, HEALTHY))

    report = body(capsys)
    assert report["roster"] == {"state": "ok", "since": SINCE}
    assert report["upstreams"] == {"serving": 11, "refused": 0}


def test_a_healthy_pep_never_asks_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit's environment decides only an `off` roster and names the
    file of an `unreadable` one. A PEP whose roster reads passes on
    `/healthz` alone, so the first release that carries this hook cannot
    fail on a question it did not need to ask."""
    run(monkeypatch, answer(200, HEALTHY), unit=OSError("no bus"))

    assert ASKED == []


def test_the_probe_uses_the_site_s_lan_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback answers nothing on the host."""
    run(monkeypatch, answer(200, HEALTHY))

    assert [f"http://{SITE_BIND}/healthz"] == PROBED


def test_a_refused_connection_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(monkeypatch, httpx.ConnectError("refused"))

    assert code == EXIT_FAILED
    assert body(capsys)["ok"] is False


def test_another_service_on_the_port_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 200 alone is not the PEP: the body has to say so."""
    code = run(monkeypatch, answer(200, {"service": "something else"}))

    assert code == EXIT_FAILED
    assert list(failed_checks(body(capsys))) == ["healthz"]


# -- the roster (contract 04 §10) --------------------------------------------


def test_an_unreadable_roster_fails_and_names_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A roster that will not parse starts the PEP with no MCP server, and
    `/healthz` still answers `ok: true`. The hook must not verify that
    release as a success."""
    unreadable = {**HEALTHY, "roster": {"state": "unreadable", "since": SINCE}}

    code = run(monkeypatch, answer(200, unreadable))

    assert code == EXIT_FAILED
    failed = failed_checks(body(capsys))
    assert list(failed) == ["roster"]
    assert SINCE in failed["roster"]
    assert f"PEP_UPSTREAMS_GENERATED={GENERATED}" in failed["roster"]


def test_an_unreadable_roster_fails_when_systemd_will_not_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file goes unnamed, and the verdict does not move."""
    unreadable = {**HEALTHY, "roster": {"state": "unreadable", "since": SINCE}}

    code = run(monkeypatch, answer(200, unreadable), unit=FileNotFoundError("systemctl"))

    assert code == EXIT_FAILED
    assert list(failed_checks(body(capsys))) == ["roster"]


def test_an_off_roster_fails_when_the_unit_names_a_generated_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A released PEP on the host always has a roster to read. `off` there
    means no read runs, and no server a release installed is served."""
    code = run(monkeypatch, answer(200, NO_ROSTER), unit=UNIT_ENV)

    assert code == EXIT_FAILED
    failed = failed_checks(body(capsys))
    assert list(failed) == ["roster"]
    assert f"PEP_UPSTREAMS_GENERATED={GENERATED}" in failed["roster"]


def test_an_off_roster_passes_when_the_unit_names_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No roster, and nothing to read."""
    code = run(monkeypatch, answer(200, NO_ROSTER), unit=UNIT_ENV_BEFORE_STAGE_7)

    assert code == EXIT_OK
    assert body(capsys)["upstreams"] is None


@pytest.mark.parametrize(
    ("unit", "unit_code"),
    [(FileNotFoundError("systemctl"), 0), (subprocess.TimeoutExpired("systemctl", 10), 0), ("", 1)],
    ids=["no-systemctl", "timeout", "non-zero-exit"],
)
def test_an_off_roster_fails_when_the_unit_will_not_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unit: str | Exception,
    unit_code: int,
) -> None:
    """Fail closed: a hook that cannot tell whether the unit names a roster
    cannot say the PEP serves it."""
    code = run(monkeypatch, answer(200, NO_ROSTER), unit=unit, unit_code=unit_code)

    assert code == EXIT_FAILED
    assert list(failed_checks(body(capsys))) == ["roster"]


def test_the_unit_is_asked_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`chaperone/component.yaml`'s `unit`, read off systemd and not off a file:
    a drop-in is part of what the unit sets."""
    run(monkeypatch, answer(200, NO_ROSTER))

    assert ASKED == [
        [
            "/usr/bin/systemctl",
            "show",
            "creche-chaperone.service",
            "--property=Environment",
            "--value",
        ]
    ]


def test_a_quoted_assignment_is_read_whole(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """systemd quotes an assignment that holds a space."""
    spaced = "/srv/agents/state/rework/up streams.yaml"
    unreadable = {**HEALTHY, "roster": {"state": "unreadable", "since": SINCE}}

    run(monkeypatch, answer(200, unreadable), unit=f'"PEP_UPSTREAMS_GENERATED={spaced}"')

    assert spaced in failed_checks(body(capsys))["roster"]


# -- the upstreams (contract 04 §10) -----------------------------------------


def test_a_refused_upstream_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A launcher that cannot switch users refuses its upstream, and the PEP
    still answers 200. The count spares the operator reading the journal by hand
    after a release."""
    refusing = {**HEALTHY, "upstreams": {"serving": 9, "refused": 2}}

    code = run(monkeypatch, answer(200, refusing))

    assert code == EXIT_FAILED
    failed = failed_checks(body(capsys))
    assert list(failed) == ["upstreams"]
    assert "2 refused" in failed["upstreams"]


@pytest.mark.parametrize(
    "shape",
    [
        {"ok": True},
        {"ok": True, "roster": {"state": "ok", "since": SINCE}},
        {**HEALTHY, "upstreams": None},
        {**HEALTHY, "roster": {"state": "maybe", "since": SINCE}},
        {**HEALTHY, "roster": {"state": "ok", "since": "yesterday"}},
        {**HEALTHY, "roster": "ok"},
        {**HEALTHY, "upstreams": {"serving": 11}},
        {**HEALTHY, "upstreams": {"serving": 11, "refused": -1}},
        {**HEALTHY, "upstreams": {"serving": True, "refused": 0}},
    ],
    ids=[
        "before-prl",
        "before-pvr",
        "no-counts-with-a-roster",
        "unknown-state",
        "since-not-a-time",
        "roster-not-an-object",
        "one-count",
        "negative-count",
        "bool-count",
    ],
)
def test_a_body_the_contract_does_not_describe_fails(
    monkeypatch: pytest.MonkeyPatch, shape: dict[str, object]
) -> None:
    """Fail closed. A PEP older than this hook is the tree a release did not
    start, and a shape nobody wrote cannot say the roster reads."""
    assert run(monkeypatch, answer(200, shape)) == EXIT_FAILED


# -- the environment ---------------------------------------------------------


def test_a_bind_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chaperone.verify.httpx.get", fake_get(answer(200, HEALTHY)))
    monkeypatch.setenv("PEP_BIND", "127.0.0.1:8300")

    main(["--json"])

    assert PROBED == ["http://127.0.0.1:8300/healthz"]


def test_an_env_file_supplies_the_bind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--env-file` works the same way here as it does for `noticeboard-verify`,
    `attendance-verify` and `caregiver-verify`: one mechanism for all four."""
    monkeypatch.delenv("PEP_BIND", raising=False)
    monkeypatch.setattr("chaperone.verify.httpx.get", fake_get(answer(200, HEALTHY)))
    env_file = tmp_path / "chaperone.env"
    env_file.write_text("PEP_BIND=127.0.0.1:9999\n", encoding="utf-8")

    main(["--json", "--env-file", str(env_file)])

    assert PROBED == ["http://127.0.0.1:9999/healthz"]


def test_a_missing_env_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A file the manifest promised and the host does not have is a hook
    failure, never a silent fall-back to the inherited environment."""
    monkeypatch.delenv("PEP_BIND", raising=False)

    code = main(["--json", "--env-file", str(tmp_path / "absent.env")])

    assert code == EXIT_FAILED
    assert list(failed_checks(body(capsys))) == ["env-file"]


def test_the_site_file_supplies_the_lan_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`chaperone/component.yaml` passes the site file, which is the unit's
    `EnvironmentFile`. Root's executor hands the hook no such variable."""
    monkeypatch.delenv("PEP_BIND", raising=False)
    monkeypatch.delenv("AGENT_LAN_ADDRESS", raising=False)
    monkeypatch.setattr("chaperone.verify.httpx.get", fake_get(answer(200, HEALTHY)))
    env_file = tmp_path / "site.env"
    env_file.write_text("AGENT_LAN_ADDRESS=192.0.2.30\n", encoding="utf-8")

    main(["--json", "--env-file", str(env_file)])

    assert PROBED == ["http://192.0.2.30:8300/healthz"]


def test_no_lan_address_and_no_bind_is_one_failed_check_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No default: a default would be somebody's host."""
    monkeypatch.delenv("PEP_BIND", raising=False)
    monkeypatch.delenv("AGENT_LAN_ADDRESS", raising=False)
    monkeypatch.setattr("chaperone.verify.httpx.get", fake_get(answer(200, HEALTHY)))
    env_file = tmp_path / "site.env"
    env_file.write_text("AGENT_GITHUB_OWNER=example-owner\n", encoding="utf-8")

    code = main(["--json", "--env-file", str(env_file)])

    assert code == EXIT_FAILED
    failed = failed_checks(body(capsys))
    assert list(failed) == ["bind"]
    assert "AGENT_LAN_ADDRESS" in failed["bind"]
    assert PROBED == []
