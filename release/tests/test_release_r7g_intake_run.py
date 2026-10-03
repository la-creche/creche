"""The root half wired together: drain, mint, push, bind, close.

Without this wiring nothing drains a gap, mints a token, starts the listener
or builds a sealer, and there is no unit and no CLI route, so "one file,
one secret, one tap" stops at the gap file.

No socket is bound here and no child is started. `Listener` is faked, the
push is a recorder and the sealer is a stub, so nothing runs as root on
any machine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest
from agent_release.errors import Refusal, RefusalCode
from agent_release.intake import run
from agent_release.intake.gaps import GapDirectory, OpenGap
from agent_release.intake.notify import PUSH_KIND, link_for, make_push, say_nothing, summary_of
from agent_release.intake.run import (
    KEPT_ENV,
    REMINT_COOLDOWN_S,
    Secrets,
    Watch,
    Wiring,
    build_wiring,
    keep_only_the_push_secrets,
    one_pass,
    proved_root_file,
)
from agent_release.intake.store import SecretStore
from agent_release.intake.token import TOKEN_TTL_S, Tokens

SERVER: Final = "weather"
NAME: Final = "weather_token"
SEALED: Final = b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed\n"
BASE: Final = "https://192.0.2.10:8380"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeListener:
    """`run.Binding`, with no socket anywhere. Every bind and unbind is
    recorded, which is what a test about the LAN surface has to read."""

    def __init__(self, *, will_open: bool = True) -> None:
        self.bound = False
        self.opens = 0
        self.closes = 0
        self._will_open = will_open

    def open(self) -> bool:
        self.opens += 1
        if not self._will_open:
            return False

        self.bound = True

        return True

    def close(self) -> None:
        self.closes += 1
        self.bound = False


class _Pushed:
    """Every push, in full. A test reads the link out of it, which is the
    only place in this system a token is ever legible."""

    def __init__(self, *, lands: bool = True) -> None:
        self.sent: list[tuple[OpenGap, str]] = []
        self._lands = lands

    def __call__(self, gap: OpenGap, token: str) -> bool:
        self.sent.append((gap, token))

        return self._lands


def _seal(plaintext: bytes) -> bytes | None:
    del plaintext

    return SEALED


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    gaps = tmp_path / "secret-gaps"
    gaps.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)

    return gaps, secrets


def _gap_file(gaps: Path, server: str, secret: str) -> None:
    (gaps / f"{secret}.json").write_text(
        f'{{"server": "{server}", "secret": "{secret}", "at": 1.0}}', encoding="utf-8"
    )


def _wiring(
    roots: tuple[Path, Path],
    clock: _Clock,
    push: _Pushed,
    listener: _FakeListener,
) -> Wiring:
    gaps, secrets = roots
    directory = GapDirectory(gaps, os.getuid())

    return Wiring(
        gaps=directory,
        store=SecretStore(secrets, _seal, owner_uid=os.getuid()),
        tokens=Tokens(clock),
        push=push,
        watch=Watch(clock),
        listener=listener,
        say=lambda line: None,
    )


# -- the pass ------------------------------------------------------------


def test_one_gap_mints_one_token_and_one_push(roots: tuple[Path, Path]) -> None:
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    push, listener = _Pushed(), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (1, 0)
    assert [(gap.server, gap.secret) for gap, _ in push.sent] == [(SERVER, NAME)]
    assert listener.bound is True


def test_a_second_pass_does_not_push_again(roots: tuple[Path, Path]) -> None:
    """`caregiver` re-opens the gap file every reconcile pass, so the file
    cannot be what paces the phone. Root's own cooldown is."""
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    push, listener = _Pushed(), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)
    one_pass(wiring)

    assert one_pass(wiring) == (0, 0)
    assert len(push.sent) == 1


def test_a_gap_is_pushed_again_after_the_cooldown(roots: tuple[Path, Path]) -> None:
    """A notification the operator never answered expires with its token. The
    gap is still open, so the operator is asked once more."""
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    clock, push, listener = _Clock(), _Pushed(), _FakeListener()
    wiring = _wiring(roots, clock, push, listener)
    one_pass(wiring)
    clock.now += REMINT_COOLDOWN_S

    assert one_pass(wiring) == (1, 0)
    assert len(push.sent) == 2


def test_the_cooldown_outlives_the_token(roots: tuple[Path, Path]) -> None:
    """Two live tokens for one name would be two capabilities for one
    gap, and the second would burn the first's slot."""
    assert REMINT_COOLDOWN_S > TOKEN_TTL_S


def test_a_gap_whose_name_has_a_value_is_closed_and_never_pushed(
    roots: tuple[Path, Path],
) -> None:
    """§4.3 keeps the sops path as break glass, so a name can be
    filled without this service. The gap is then stale."""
    gaps, secrets = roots
    _gap_file(gaps, SERVER, NAME)
    (secrets / f"{NAME}.enc").write_bytes(SEALED)
    push, listener = _Pushed(), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (0, 1)
    assert push.sent == []
    assert not (gaps / f"{NAME}.json").exists()


def test_a_push_that_does_not_land_drops_its_token(roots: tuple[Path, Path]) -> None:
    """A capability nobody was told about holds a slot and keeps the
    listener bound for fifteen minutes, for nothing."""
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    push, listener = _Pushed(lands=False), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (0, 0)
    assert wiring.tokens.pending_count() == 0
    assert listener.bound is False


def test_a_listener_that_will_not_bind_mints_nothing(roots: tuple[Path, Path]) -> None:
    """A token for a page nobody can open is a wasted push."""
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    push, listener = _Pushed(), _FakeListener(will_open=False)
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (0, 0)
    assert push.sent == []
    assert wiring.tokens.pending_count() == 0


def test_no_gap_means_no_listener(roots: tuple[Path, Path]) -> None:
    """The point of binding on demand: a host with nothing to ask has no
    root listener on the LAN at all."""
    push, listener = _Pushed(), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (0, 0)
    assert listener.bound is False
    assert listener.opens == 0


def test_the_listener_closes_when_the_last_token_expires(roots: tuple[Path, Path]) -> None:
    gaps, _ = roots
    _gap_file(gaps, SERVER, NAME)
    clock, push, listener = _Clock(), _Pushed(), _FakeListener()
    wiring = _wiring(roots, clock, push, listener)
    one_pass(wiring)
    assert listener.bound is True

    clock.now += TOKEN_TTL_S + 1.0
    one_pass(wiring)

    assert listener.bound is False
    assert listener.closes == 1


def test_a_planted_gap_does_not_stop_a_real_one(roots: tuple[Path, Path]) -> None:
    """One write into an operator-writable directory must not stop every
    future MCP server."""
    gaps, _ = roots
    (gaps / "broken_one.json").write_bytes(b"not json")
    (gaps / "notaname.txt").write_text("{}", encoding="utf-8")
    _gap_file(gaps, SERVER, NAME)
    push, listener = _Pushed(), _FakeListener()

    assert one_pass(_wiring(roots, _Clock(), push, listener)) == (1, 0)


def test_a_pass_with_many_gaps_mints_one_token_each(roots: tuple[Path, Path]) -> None:
    gaps, _ = roots
    for index in range(5):
        _gap_file(gaps, SERVER, f"secret_{index}")
    push, listener = _Pushed(), _FakeListener()
    wiring = _wiring(roots, _Clock(), push, listener)

    assert one_pass(wiring) == (5, 0)
    assert len({token for _, token in push.sent}) == 5


# -- the push ------------------------------------------------------------


def test_the_link_carries_the_token_in_its_path() -> None:
    assert link_for(BASE, "a-token") == f"{BASE}/secret/a-token/"
    assert link_for(f"{BASE}/", "a-token") == f"{BASE}/secret/a-token/"


def test_the_push_names_the_server_the_secret_and_the_link() -> None:
    """§4.3 step 2, in full, and nothing else."""
    sent: list[dict[str, object]] = []

    def record(body: dict[str, object]) -> bool:
        sent.append(body)

        return True

    push = make_push("https://hook.invalid", "bearer", BASE, record)
    gap = OpenGap(SERVER, NAME, 1.0)

    assert push(gap, "a-token") is True
    (body,) = sent
    fields = body["fields"]
    assert body["kind"] == PUSH_KIND
    assert isinstance(fields, dict)
    assert fields["server"] == SERVER
    assert fields["secret"] == NAME
    assert fields["link"] == f"{BASE}/secret/a-token/"


def test_the_summary_holds_no_token() -> None:
    line = summary_of(OpenGap(SERVER, NAME, 1.0))

    assert SERVER in line
    assert NAME in line


def test_a_host_with_no_hook_pushes_nothing() -> None:
    """Fail closed: an intake that cannot tell the operator about a token must
    not leave one lying about."""
    assert say_nothing(OpenGap(SERVER, NAME, 1.0), "a-token") is False


def test_a_push_that_the_hook_refuses_answers_false() -> None:
    def refuse(body: dict[str, object]) -> bool:
        del body

        return False

    push = make_push("https://hook.invalid", "bearer", BASE, refuse)

    assert push(OpenGap(SERVER, NAME, 1.0), "a-token") is False


# -- where the listener binds ------------------------------------------


class _Recorded:
    """What `build_wiring` handed the listener and the push."""

    def __init__(self) -> None:
        self.bind = ""
        self.base = ""


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    seen = _Recorded()

    def listener(intake: object, certfile: str, keyfile: str, bind: str, port: int) -> object:
        del intake, certfile, keyfile, port
        seen.bind = bind

        return _FakeListener()

    def push(url: str, token: str, base: str) -> object:
        del url, token
        seen.base = base

        return say_nothing

    monkeypatch.setattr(run, "Listener", listener)
    monkeypatch.setattr(run, "make_push", push)

    return seen


def _site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "site.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AGENT_SITE_FILE", str(path))


def test_the_listener_binds_the_sites_lan_address(recorded: _Recorded) -> None:
    """The root conftest's example site names `192.0.2.10`."""
    build_wiring(Secrets("https://hook.invalid", "bearer"), os.getuid(), _Clock())

    assert recorded.bind == "192.0.2.10"
    assert recorded.base == BASE


def test_a_site_with_no_lan_address_is_the_site_refusal(
    recorded: _Recorded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No default: a default would be somebody's host. The environment
    is no fallback either, because the intake clears it."""
    _site(tmp_path, monkeypatch, "AGENT_OPERATOR_USER=operator\n")

    with pytest.raises(Refusal) as caught:
        build_wiring(Secrets("", ""), os.getuid(), _Clock())

    assert caught.value.code is RefusalCode.SITE
    assert "AGENT_LAN_ADDRESS" in caught.value.detail
    assert recorded.bind == ""


def test_main_reports_a_missing_lan_address_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same shape as every other start failure: one stderr line, 1."""
    _site(tmp_path, monkeypatch, "AGENT_OPERATOR_USER=operator\n")
    monkeypatch.setattr(run.os, "geteuid", lambda: 0)
    monkeypatch.setattr(run, "keep_only_the_push_secrets", lambda environ: Secrets("", ""))
    monkeypatch.setattr(run, "_certificate_is_roots", lambda: True)
    monkeypatch.setattr(run.pwd, "getpwnam", lambda name: run.pwd.getpwuid(os.getuid()))

    code = run.main()

    err = capsys.readouterr().err
    assert code == 1
    assert err.count("\n") == 1, err
    assert "AGENT_LAN_ADDRESS" in err


# -- what root proves before it serves -----------------------------------


def test_a_key_file_another_uid_owns_is_refused(tmp_path: Path) -> None:
    """A key the operator can write is a certificate the operator chose."""
    key = tmp_path / "intake.key"
    key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    key.chmod(0o600)

    assert proved_root_file(key, owner_uid=os.getuid()) is True
    assert proved_root_file(key, owner_uid=os.getuid() + 1) is False


def test_a_key_file_others_can_read_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "intake.key"
    key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    key.chmod(0o644)

    assert proved_root_file(key, owner_uid=os.getuid()) is False


def test_a_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.key"
    real.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "intake.key"
    link.symlink_to(real)

    assert proved_root_file(link, owner_uid=os.getuid()) is False


def test_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    assert proved_root_file(tmp_path / "never-made", owner_uid=os.getuid()) is False


# -- the environment -----------------------------------------------------


def test_the_process_keeps_two_secrets_and_drops_the_rest() -> None:
    """The wrapper runs this under `sops exec-env`, and unlike the
    executor it then runs for days."""
    environ = {
        "RELEASE_APPROVAL_URL": "https://hook.invalid",
        "RELEASE_APPROVAL_TOKEN": "bearer",
        "RELEASE_GITHUB_TOKEN": "ghp_should_not_survive",
        "POSTGRES_PASSWORD": "should_not_survive",
        "PATH": "/usr/bin",
    }

    found = keep_only_the_push_secrets(environ)

    assert found.approval_url == "https://hook.invalid"
    assert found.approval_token == "bearer"
    assert set(environ) <= KEPT_ENV
    assert "ghp_should_not_survive" not in "".join(environ.values())
