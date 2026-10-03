"""The site file: the one place a deployment's own values come from.

`site.py` reads `/etc/agent-control/site.env` and nothing else knows a
host, an account or an owner. What is held here: a value is read from the
file the environment names, a missing or malformed one refuses with the
`site` code and never falls back to a default, and the provenance predicate
asks GitHub about the site's owner.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import provenance

from handover import site


@pytest.fixture
def site_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A site file of this test's own, named the way a test names one."""
    path = tmp_path / "site.env"
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))

    return path


def test_the_owner_is_read_from_the_site_file(site_file: Path) -> None:
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")

    assert site.github_owner() == "some-org"


def test_comments_and_blank_lines_are_skipped_and_the_last_line_wins(site_file: Path) -> None:
    lines = ["# who owns the repositories", "", "AGENT_GITHUB_OWNER=first"]
    lines.append("  AGENT_GITHUB_OWNER = second  ")
    site_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert site.github_owner() == "second"


def test_no_site_file_refuses_and_names_no_default(site_file: Path) -> None:
    """A default would be somebody's host. Root's executor must stop."""
    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE
    assert str(site_file) in caught.value.detail


def test_a_file_that_sets_no_owner_refuses(site_file: Path) -> None:
    site_file.write_text("AGENT_SOMETHING_ELSE=1\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE
    assert "AGENT_GITHUB_OWNER" in caught.value.detail


@pytest.mark.parametrize(
    "value",
    ["", "-leading", "trailing-", "two--hyphens", "has/slash", "has space", "a" * 40, "../etc"],
)
def test_an_owner_no_github_login_could_be_refuses(site_file: Path, value: str) -> None:
    """The owner is spliced into an API path. A value that is not a login
    must never reach it."""
    site_file.write_text(f"AGENT_GITHUB_OWNER={value}\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE


def test_a_line_that_is_not_an_assignment_refuses(site_file: Path) -> None:
    site_file.write_text("AGENT_GITHUB_OWNER some-org\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE


def test_a_changed_file_is_read_again_after_forget(site_file: Path) -> None:
    site_file.write_text("AGENT_GITHUB_OWNER=before\n", encoding="utf-8")
    assert site.github_owner() == "before"

    site_file.write_text("AGENT_GITHUB_OWNER=after\n", encoding="utf-8")
    assert site.github_owner() == "before"

    site.forget()
    assert site.github_owner() == "after"


def test_with_no_variable_set_the_host_path_is_the_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(site.SITE_FILE_ENV)

    assert site.site_file() == Path("/etc/agent-control/site.env")


def test_provenance_asks_github_about_the_sites_owner(site_file: Path) -> None:
    """Every P1 to P5 path starts `/repos/<owner>/<repo>`, and the owner is
    the site's. This code names nobody's account."""
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")
    asked: list[str] = []

    def api(path: str, accept: provenance.Accept) -> provenance.ApiReply:
        asked.append(path)

        return provenance.ApiReply(status=404, body=None)

    with pytest.raises(Refusal):
        provenance.check_release(api, "chaperone", "agent-control", "chaperone-v0.1.0")

    assert asked == ["/repos/some-org/agent-control/releases/tags/chaperone-v0.1.0"]


def test_the_operator_is_read_from_the_site_file(site_file: Path) -> None:
    site_file.write_text("AGENT_OPERATOR_USER=someone\n", encoding="utf-8")

    assert site.operator_user() == "someone"


@pytest.mark.parametrize("value", ["root", "", "Upper", "1starts-with-a-digit", "a b", "x" * 33])
def test_an_operator_that_is_root_or_no_account_name_refuses(site_file: Path, value: str) -> None:
    """The executor drops to this account to install the user trees. Root,
    or a string `useradd` would not take, must stop it."""
    site_file.write_text(f"AGENT_OPERATOR_USER={value}\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.operator_user()

    assert caught.value.code is RefusalCode.SITE


def test_the_operators_home_is_read_from_the_site_file(site_file: Path) -> None:
    site_file.write_text("AGENT_OPERATOR_HOME=/home/someone\n", encoding="utf-8")

    assert site.operator_home() == "/home/someone"


@pytest.mark.parametrize(
    "value", ["", "home/someone", "/home/someone/", "/home/../etc", "/a b", "~"]
)
def test_a_home_that_is_no_plain_absolute_path_refuses(site_file: Path, value: str) -> None:
    """Every `~/` in a manifest and every `%h/` in a user unit becomes a
    path under this value, and root installs there."""
    site_file.write_text(f"AGENT_OPERATOR_HOME={value}\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.operator_home()

    assert caught.value.code is RefusalCode.SITE


def test_the_lan_address_is_read_from_the_site_file(site_file: Path) -> None:
    site_file.write_text("AGENT_LAN_ADDRESS=192.0.2.10\n", encoding="utf-8")

    assert site.lan_address() == "192.0.2.10"


def test_a_host_name_of_plain_labels_is_a_lan_address(site_file: Path) -> None:
    site_file.write_text("AGENT_LAN_ADDRESS=host.example.org\n", encoding="utf-8")

    assert site.lan_address() == "host.example.org"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "192.0.2.10:8380",
        "https://192.0.2.10",
        "192.0.2.10/24",
        "-leading.example.org",
        "two..dots",
        "a b",
        "0.0.0.0/0",
    ],
)
def test_a_lan_address_with_a_port_a_scheme_or_a_slash_refuses(site_file: Path, value: str) -> None:
    """Root binds a listener on this value and builds a link from it. A
    port, a scheme or a path would bind or link somewhere else."""
    site_file.write_text(f"AGENT_LAN_ADDRESS={value}\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.lan_address()

    assert caught.value.code is RefusalCode.SITE


def test_a_repository_keeps_its_catalog_name_when_the_site_names_no_other(
    site_file: Path,
) -> None:
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")

    assert site.github_repo("agent-control") == "agent-control"


def test_the_site_may_give_a_repository_another_name_on_github(site_file: Path) -> None:
    """The catalog's name is this code's own. What the repository is called
    where it is hosted is the site's to say."""
    lines = ["AGENT_GITHUB_REPO_AGENT_CONTROL=some-product", "AGENT_GITHUB_REPO_AGENT_MCP=servers"]
    site_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert site.github_repo("agent-control") == "some-product"
    assert site.github_repo("agent-mcp") == "servers"
    assert site.github_repo("agent-registry") == "agent-registry"


@pytest.mark.parametrize("value", ["", "has/slash", "..", ".", "a b", "-leading", "x" * 101])
def test_a_repository_name_github_could_not_hold_refuses(site_file: Path, value: str) -> None:
    """The name is spliced into an API path, as the owner is."""
    site_file.write_text(f"AGENT_GITHUB_REPO_AGENT_CONTROL={value}\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.github_repo("agent-control")

    assert caught.value.code is RefusalCode.SITE


def test_provenance_asks_github_about_the_sites_name_for_a_repository(site_file: Path) -> None:
    lines = ["AGENT_GITHUB_OWNER=some-org", "AGENT_GITHUB_REPO_AGENT_CONTROL=some-product"]
    site_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    asked: list[str] = []

    def api(path: str, accept: provenance.Accept) -> provenance.ApiReply:
        asked.append(path)

        return provenance.ApiReply(status=404, body=None)

    with pytest.raises(Refusal):
        provenance.check_release(api, "chaperone", "agent-control", "chaperone-v0.1.0")

    assert asked == ["/repos/some-org/some-product/releases/tags/chaperone-v0.1.0"]


@pytest.mark.parametrize("quoted", ['"some-org"', "'some-org'"])
def test_one_pair_of_quotes_is_dropped_as_systemd_drops_it(site_file: Path, quoted: str) -> None:
    """The same file is every unit's `EnvironmentFile`, and systemd unquotes
    a value. A quoted line must not start the services and stop every
    release."""
    site_file.write_text(f"AGENT_GITHUB_OWNER={quoted}\n", encoding="utf-8")

    assert site.github_owner() == "some-org"


def test_a_quote_on_one_side_only_is_not_dropped(site_file: Path) -> None:
    site_file.write_text('AGENT_GITHUB_OWNER="some-org\n', encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE


def test_a_site_file_group_or_other_can_write_refuses(site_file: Path) -> None:
    """The file says which account root drops to and where root installs.
    Whoever can write it chooses both."""
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")
    site_file.chmod(0o666)

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE
    assert "writable" in caught.value.detail


def test_a_site_file_another_account_owns_refuses(
    site_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root reads a file root owns. Anybody else reads root's, or their own."""
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")
    owner = site_file.stat().st_uid
    monkeypatch.setattr(site.os, "getuid", lambda: owner + 1)

    with pytest.raises(Refusal) as caught:
        site.github_owner()

    assert caught.value.code is RefusalCode.SITE
    assert "owned" in caught.value.detail


def test_every_value_a_release_needs_is_asked_for_at_once(site_file: Path) -> None:
    """`check --site` is the release executor's own verify hook. A site file
    that lacks one value must fail it, so the release is put back, and not
    the next run of an executor that cannot start."""
    lines = ["AGENT_GITHUB_OWNER=some-org", "AGENT_OPERATOR_USER=someone"]
    lines.append("AGENT_LAN_ADDRESS=192.0.2.10")
    site_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        site.require_complete()

    assert "AGENT_OPERATOR_HOME" in caught.value.detail


COMPLETE = [
    "AGENT_GITHUB_OWNER=some-org",
    "AGENT_OPERATOR_USER=someone",
    "AGENT_OPERATOR_HOME=/home/someone",
    "AGENT_LAN_ADDRESS=192.0.2.10",
]


@pytest.mark.usefixtures("operator_is_an_account")
def test_a_complete_site_file_passes(site_file: Path) -> None:
    site_file.write_text("\n".join(COMPLETE) + "\n", encoding="utf-8")

    site.require_complete()


def test_a_complete_file_for_an_account_this_host_lacks_refuses(
    site_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor stops before its spool when the operator is no account
    of the host. The verify hook must stop for the same reason, or it keeps
    an executor that can never start."""
    site_file.write_text("\n".join(COMPLETE) + "\n", encoding="utf-8")

    def lookup(name: str) -> pwd.struct_passwd:
        raise KeyError(name)

    monkeypatch.setattr(site.pwd, "getpwnam", lookup)

    with pytest.raises(Refusal) as caught:
        site.require_complete()

    assert "someone" in caught.value.detail


def test_a_home_the_password_database_spells_with_a_slash_is_the_same_home(
    site_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/home/someone/` and `/home/someone` are one directory. The site's
    shape cannot carry the slash, so a textual comparison could never match."""
    site_file.write_text("\n".join(COMPLETE) + "\n", encoding="utf-8")
    entry = pwd.struct_passwd(("someone", "x", 1234, 1234, "", "/home/someone/", "/bin/sh"))
    monkeypatch.setattr(site.pwd, "getpwnam", lambda name: entry)

    assert site.operator_account().pw_uid == 1234


def test_the_file_that_is_judged_is_the_file_that_is_read(
    site_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One open, then both on its descriptor. Judging a path and then
    reading the path again lets the file change in between."""
    site_file.write_text("AGENT_GITHUB_OWNER=some-org\n", encoding="utf-8")
    judged: list[int] = []
    real_fstat = os.fstat

    def fstat(descriptor: int) -> os.stat_result:
        judged.append(descriptor)

        return real_fstat(descriptor)

    def no_second_look(self: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"{self} was opened a second time, by name")

    monkeypatch.setattr(site.os, "fstat", fstat)
    monkeypatch.setattr(Path, "read_text", no_second_look)

    assert site.github_owner() == "some-org"
    assert len(judged) == 1
