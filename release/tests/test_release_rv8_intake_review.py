"""Attacks on the secret intake.

Every test here is one attack. The attacker is `stage7-releases.md` §3.2
and §6, and more: it is on the LAN, it writes every operator-writable
directory root reads, and it wants to read a secret, pre-empt one, or
wedge the intake for ever.

Nothing here runs as root. A test that needs root's ownership check passes
`owner_uid=os.getuid()` instead, which is the same code path with a
different number.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import pytest
from agent_release.intake.store import SecretStore

NAME: Final = "weather_token"
SERVER: Final = "weather"
VALUE: Final = "a-real-looking-upstream-credential"

SEALED: Final = b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed\n"


class _Sealer:
    """Records every plaintext it is handed, so a test can prove the store
    refused BEFORE it sealed."""

    def __init__(self) -> None:
        self.handed: list[bytes] = []

    def __call__(self, plaintext: bytes) -> bytes | None:
        self.handed.append(plaintext)

        return SEALED


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)

    return directory


def _store(directory: Path, sealer: _Sealer) -> SecretStore:
    return SecretStore(directory, sealer, owner_uid=os.getuid())


def _form_body(token: str, name: str, value: str) -> bytes:
    """What a scriptless form posts: the one encoding `store_value` reads."""
    return urlencode({"token": token, "name": name, "value": value}).encode("utf-8")


# -- the secrets directory is trusted without a check --------------------


def test_a_directory_another_user_may_write_is_refused(
    secrets_dir: Path,
    tmp_path: Path,
) -> None:
    """The attacker's real lever. Sealing needs PUBLIC keys only, so any
    writer of this directory can author a valid `<name>.enc` of its own.
    Ownership and the write bits are the whole control."""
    del tmp_path
    secrets_dir.chmod(0o770)
    sealer = _Sealer()

    assert _store(secrets_dir, sealer).fill(NAME, VALUE) is False
    assert sealer.handed == [], "the value was sealed before the directory was checked"


def test_a_directory_owned_by_another_user_is_refused(secrets_dir: Path) -> None:
    sealer = _Sealer()
    store = SecretStore(secrets_dir, sealer, owner_uid=os.getuid() + 1)

    assert store.fill(NAME, VALUE) is False
    assert sealer.handed == []


def test_a_symlinked_secrets_directory_is_refused(tmp_path: Path) -> None:
    """`mv secrets secrets.real; ln -s /tmp/mine secrets`. `O_NOFOLLOW` on
    the directory open is what refuses it. Without it root writes the
    sealed file into a directory the attacker owns."""
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    link = tmp_path / "secrets"
    link.symlink_to(real, target_is_directory=True)
    sealer = _Sealer()

    assert _store(link, sealer).fill(NAME, VALUE) is False
    assert sealer.handed == []


def test_a_planted_symlink_is_a_name_that_already_has_a_value(secrets_dir: Path) -> None:
    """A DANGLING symlink at `<name>.enc`. `Path.exists()` follows it and
    answers False, so a store that asked it would seal and then fail the
    write, after the service had already spent the operator's token."""
    (secrets_dir / f"{NAME}.enc").symlink_to(secrets_dir / "nowhere")
    sealer = _Sealer()

    assert _store(secrets_dir, sealer).has(NAME) is True
    assert _store(secrets_dir, sealer).fill(NAME, VALUE) is False
    assert sealer.handed == []


def test_a_gap_is_still_fillable_when_the_directory_is_root_only(secrets_dir: Path) -> None:
    """The symlink check must not refuse the good case."""
    sealer = _Sealer()

    assert _store(secrets_dir, sealer).fill(NAME, VALUE) is True
    assert (secrets_dir / f"{NAME}.enc").read_bytes() == SEALED
    assert _store(secrets_dir, sealer).filled() == (NAME,)


def test_a_second_fill_of_one_name_is_refused(secrets_dir: Path) -> None:
    """§4.3 rule 2: a token fills a GAP. Overwriting is `rotate`."""
    sealer = _Sealer()

    assert _store(secrets_dir, sealer).fill(NAME, VALUE) is True
    assert _store(secrets_dir, sealer).fill(NAME, "a second value") is False
    assert sealer.handed == [VALUE.encode("utf-8")]


def test_the_gap_directory_is_not_inside_the_secrets_directory() -> None:
    """`managerd` writes the gap as the operator. If the gap directory sits inside
    the secrets directory, that directory cannot be root's alone, and the
    test above has nothing left to protect."""
    from agent_managerd.mcp_release import GAPS_DIR, SECRETS_DIR

    assert SECRETS_DIR not in GAPS_DIR.parents


# -- one TCP connection wedges the listener for ever ---------------------


class _FakeSocket:
    """One accepted connection. It records the order of what root does to
    it, which is the whole point: a timeout set AFTER the handshake is a
    timeout the handshake never had."""

    def __init__(self) -> None:
        self.done: list[str] = []

    def settimeout(self, seconds: float | None) -> None:
        self.done.append(f"settimeout {seconds}")

    def close(self) -> None:
        self.done.append("close")


class _FakeListener:
    def __init__(self, accepted: _FakeSocket) -> None:
        self._accepted = accepted

    def accept(self) -> tuple[_FakeSocket, tuple[str, int]]:
        return self._accepted, ("192.0.2.30", 51234)


class _FakeContext:
    """A TLS context that records when the handshake ran, or refuses."""

    def __init__(self, *, works: bool = True) -> None:
        self.works = works
        self.wrapped_at: list[str] = []

    def wrap_socket(self, sock: _FakeSocket, *, server_side: bool) -> _FakeSocket:
        assert server_side
        self.wrapped_at = list(sock.done)
        if not self.works:
            raise OSError("handshake failed")

        return sock


def _get_request(context: _FakeContext, sock: _FakeSocket) -> object:
    from agent_release.intake.service import accept_one

    return accept_one(_FakeListener(sock), context)  # type: ignore[arg-type]


def test_the_accepted_socket_gets_a_timeout_first(tmp_path: Path) -> None:
    """The wedge. `wrap_socket` on the LISTENING socket runs the handshake
    inside `accept()`, in the single accept loop, with no timeout anywhere:
    one LAN host that connects and then says nothing stops every other
    connection for ever.

    The handshake is not in `accept_one` at all, because `socketserver`
    calls `get_request` in the accept loop, so a bounded handshake there
    would still be a wedge.
    What this function must still do is put every accepted socket under a
    clock before anybody reads a byte from it.
    """
    del tmp_path
    sock = _FakeSocket()
    context = _FakeContext()
    _get_request(context, sock)

    assert not context.wrapped_at, "the handshake is the worker thread's, not the accept loop's"
    assert sock.done, "the accepted socket was handed on with no timeout"
    assert sock.done[0].startswith("settimeout "), sock.done
    assert sock.done[0] != "settimeout None"


def test_a_slow_request_line_does_not_hold_a_thread_for_ever() -> None:
    """Past the handshake the same hole repeats: `BaseHTTPRequestHandler`
    inherits `timeout = None`, so a request line that never ends holds one
    thread of the root process for ever."""
    from agent_release.intake.service import CONNECTION_TIMEOUT_S, _handler_for

    handler = _handler_for(_nothing_intake())

    assert handler.timeout == CONNECTION_TIMEOUT_S
    assert CONNECTION_TIMEOUT_S > 0


# -- a non-ASCII token crashes instead of being refused ------------------


def test_a_non_ascii_token_is_one_more_refusal() -> None:
    """`hmac.compare_digest` on two `str` raises `TypeError` when either
    holds a non-ASCII character, and the loop only runs when a gap is
    pending. `http.server` decodes the request line as iso-8859-1 and JSON
    carries any Unicode, so the attacker picks the character."""
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)
    tokens.mint(SERVER, NAME)

    assert tokens.claim("é" * 43, NAME) is None
    assert tokens.describe("héllo") is None


def test_the_service_answers_a_non_ascii_token_from_the_closed_list(secrets_dir: Path) -> None:
    """Rule 5: every answer is a fixed string. An exception out of the
    handler is no answer at all, and it prints a traceback into root's
    journal on every probe."""
    from agent_release.intake.service import Intake
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)
    tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _Sealer()))

    assert intake.form("Ã©").status == 404
    assert intake.store_value(_form_body("é", NAME, VALUE)).status == 403


# -- a spent token is destroyed before the value lands -------------------


class _BrokenSealer:
    """An encryptor that fails once and then works, which is what a
    missing `sops`, a full disk or a planted symlink look like."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, plaintext: bytes) -> bytes | None:
        del plaintext
        self.calls += 1
        if self.calls == 1:
            return None

        return SEALED


def test_a_failed_seal_does_not_cost_the_operator_the_token(secrets_dir: Path) -> None:
    """A `claim` that destroys the token, THEN the fill, THEN the 500: one
    transient failure would cost a whole new phone push."""
    from agent_release.intake.service import Intake
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    sealer = _BrokenSealer()
    intake = Intake(
        tokens=tokens,
        store=SecretStore(secrets_dir, sealer, owner_uid=os.getuid()),
    )
    body = _form_body(minted, NAME, VALUE)

    assert intake.store_value(body).status == 500
    assert intake.store_value(body).status == 200
    assert (secrets_dir / f"{NAME}.enc").read_bytes() == SEALED


def test_a_stored_value_still_spends_the_token(secrets_dir: Path) -> None:
    """Single use still holds on the path that worked."""
    from agent_release.intake.service import Intake
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(
        tokens=tokens,
        store=SecretStore(secrets_dir, _Sealer(), owner_uid=os.getuid()),
    )
    body = _form_body(minted, NAME, VALUE)

    assert intake.store_value(body).status == 200
    assert intake.store_value(body).status == 403


def test_a_reserved_token_cannot_be_claimed_twice() -> None:
    """Two requests racing on one token. The first reservation takes it out
    of the pending set, so the second is a miss like any other."""
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)

    assert tokens.claim(minted, NAME) is not None
    assert tokens.claim(minted, NAME) is None


# -- the form page trusts a name the attacker writes ---------------------


def test_a_server_name_outside_the_pattern_is_refused_at_the_mint() -> None:
    """`mint` checked nothing, and it cannot rely on its caller. The
    name's source is `mcp/<name>/server.yaml`, which the attacker
    writes."""
    from agent_release.intake.token import Tokens

    tokens = Tokens(lambda: 0.0)

    with pytest.raises(ValueError, match="server"):
        tokens.mint("weather<script>alert(1)</script>", NAME)

    with pytest.raises(ValueError, match="secret"):
        tokens.mint(SERVER, "../../etc/shadow")


def test_the_form_escapes_what_it_shows() -> None:
    """Defence in depth behind the pattern. A script running on the
    intake's own origin reads the token out of the hidden field and posts
    the attacker's value for the name the operator was about to fill."""
    from agent_release.intake.service import _page

    page = _page("weather<script>steal()</script>", NAME, "tok").decode("utf-8")

    assert "<script>steal()" not in page
    assert "&lt;script&gt;" in page


def test_the_form_carries_a_policy_that_names_no_other_origin() -> None:
    """The last layer.

    A page with an inline submit handler would need a policy that allows
    inline script — which is the very thing this row is about. The page
    posts by itself, so no script may run at all.
    """
    from agent_release.intake.service import CONTENT_SECURITY_POLICY

    assert "default-src 'none'" in CONTENT_SECURITY_POLICY
    assert "script-src 'none'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "form-action 'self'" in CONTENT_SECURITY_POLICY


# -- the recipient set is a trust root with no owner ---------------------


RECIPIENTS: Final = (
    "age1dnkv7u39u3kfys05062g0lyl6s23ks0ualmjtk7alp3fw5r0s3cs4dcpuf",
    "age1hun3vrpmkr0zph9k3vsc4eu2gd7hjvffeee0qwxj2w0qsd7qe5usvg4cx6",
)


def _sops_file(directory: Path, recipients: tuple[str, ...]) -> Path:
    sops = directory / ".sops.yaml"
    sops.write_text(
        "creation_rules:\n"
        "  - path_regex: pep/secrets\\.enc\\.yaml$\n"
        f"    age: {','.join(recipients)}\n",
        "utf-8",
    )

    return sops


def test_a_sops_file_another_user_may_write_names_no_recipient(tmp_path: Path) -> None:
    """The read-a-secret path, and the worst one available. Sealing needs
    PUBLIC keys, so ADDING one recipient is enough to read every secret
    written after it. Wiring that read this out of a checkout under
    `/srv/agents/work/platform/` would put it in the attacker's own reach."""
    from agent_release.intake.store import recipients_of

    sops = _sops_file(tmp_path, RECIPIENTS)
    sops.chmod(0o666)

    assert recipients_of(sops, "pep/secrets.enc.yaml", owner_uid=os.getuid()) == ()


def test_a_sops_file_owned_by_another_user_names_no_recipient(tmp_path: Path) -> None:
    from agent_release.intake.store import recipients_of

    sops = _sops_file(tmp_path, RECIPIENTS)

    assert recipients_of(sops, "pep/secrets.enc.yaml", owner_uid=os.getuid() + 1) == ()


def test_a_recipient_that_is_not_an_age_key_refuses_the_whole_set(tmp_path: Path) -> None:
    """Fail closed on the SET, not on the one entry: a file root cannot
    read cleanly is a file root does not seal to. `make_sealer` refuses an
    empty set already."""
    from agent_release.intake.store import recipients_of

    sops = _sops_file(tmp_path, (RECIPIENTS[0], "--config=/tmp/mine"))

    assert recipients_of(sops, "pep/secrets.enc.yaml", owner_uid=os.getuid()) == ()


def test_a_good_sops_file_still_names_its_recipients(tmp_path: Path) -> None:
    from agent_release.intake.store import recipients_of

    sops = _sops_file(tmp_path, RECIPIENTS)

    assert recipients_of(sops, "pep/secrets.enc.yaml", owner_uid=os.getuid()) == RECIPIENTS


# -- two unbounded things in a root process ------------------------------


def test_a_flood_of_gaps_does_not_drop_the_operators_token() -> None:
    """A `mint` must not evict the OLDEST pending token at the cap to keep
    minting. The roster is driven by `mcp/<name>/server.yaml` files the
    attacker adds, so 64 declared servers would drop the token the operator is
    walking to the phone to use."""
    from agent_release.intake.token import MAX_PENDING_TOKENS, Tokens

    tokens = Tokens(lambda: 0.0)
    operators = tokens.mint(SERVER, NAME)
    for index in range(MAX_PENDING_TOKENS * 2):
        try:
            tokens.mint(f"flood-{index}", f"flood_{index}")
        except RuntimeError:
            break

    assert tokens.claim(operators, NAME) is not None


def test_the_intake_log_does_not_grow_without_bound(secrets_dir: Path) -> None:
    """`form()` appends on a MISS, and that route needs no token, so an
    unauthenticated LAN peer grows root's memory at its own request
    rate."""
    from agent_release.intake.service import MAX_LOG_LINES, Intake
    from agent_release.intake.token import Tokens

    intake = Intake(tokens=Tokens(lambda: 0.0), store=_store(secrets_dir, _Sealer()))
    for _ in range(MAX_LOG_LINES * 3):
        intake.form("no-such-token")

    assert len(intake.log) <= MAX_LOG_LINES


def _nothing_intake() -> object:
    from agent_release.intake.service import Intake
    from agent_release.intake.token import Tokens

    return Intake(tokens=Tokens(lambda: 0.0), store=SecretStore(Path("/nonexistent"), _Sealer()))
