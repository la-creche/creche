"""The executor's start: the site's operator must be an account of this host.

`site.py` names the operator and the operator's home. systemd expands `%h`
in a user unit to the account's home in the password database, and the
executor judges such a unit against the site's value. The two must be one
directory, and the account must exist, or the executor stops with one line
before it touches the spool.
"""

from __future__ import annotations

import pwd
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import drain

from handover import site


def _site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str) -> None:
    path = tmp_path / "site.env"
    lines = ["AGENT_GITHUB_OWNER=example-owner", "AGENT_OPERATOR_USER=someone"]
    lines += [f"AGENT_OPERATOR_HOME={home}", "AGENT_LAN_ADDRESS=192.0.2.10"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))


def _entry(home: str) -> pwd.struct_passwd:
    return pwd.struct_passwd(("someone", "x", 1234, 1234, "", home, "/bin/sh"))


def test_the_account_of_the_site_is_looked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site(tmp_path, monkeypatch, "/home/someone")
    asked: list[str] = []

    def lookup(name: str) -> pwd.struct_passwd:
        asked.append(name)

        return _entry("/home/someone")

    monkeypatch.setattr(site.pwd, "getpwnam", lookup)

    assert drain.operator_account().pw_uid == 1234
    assert asked == ["someone"]


def test_an_account_this_host_does_not_have_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site(tmp_path, monkeypatch, "/home/someone")

    def lookup(name: str) -> pwd.struct_passwd:
        raise KeyError(name)

    monkeypatch.setattr(site.pwd, "getpwnam", lookup)

    with pytest.raises(Refusal) as caught:
        drain.operator_account()

    assert caught.value.code is RefusalCode.SITE
    assert "someone" in caught.value.detail


def test_a_site_home_that_is_not_the_accounts_home_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`%h` in a user unit is the password database's home. A site file that
    names another directory would have the unit check approve a path systemd
    never runs."""
    _site(tmp_path, monkeypatch, "/srv/someone")
    monkeypatch.setattr(site.pwd, "getpwnam", lambda name: _entry("/home/someone"))

    with pytest.raises(Refusal) as caught:
        drain.operator_account()

    assert caught.value.code is RefusalCode.SITE
    assert "AGENT_OPERATOR_HOME" in caught.value.detail


def test_main_reports_a_site_that_cannot_be_read_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A traceback here re-fires the path unit until its start limit, and
    says nothing a person can act on."""
    monkeypatch.setenv(site.SITE_FILE_ENV, str(tmp_path / "absent.env"))
    monkeypatch.setattr(drain.os, "geteuid", lambda: 0)

    code = drain.main()

    err = capsys.readouterr().err
    assert code == 1
    assert err.count("\n") == 1, err
    assert "absent.env" in err
