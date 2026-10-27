"""Invariant 18, with both wires in: one file becomes a roster row.

`test_handover_r7f_invariant18.py`, read against the rule "is every step
real where it can be, and does each fake stand exactly where the design
puts a human or root?", has three fakes standing where ROOT stands. Two
of them can hide a missing wire each: a test that calls the reconciler
directly cannot see that nothing else calls it, and a test that writes the
roster cannot see that nothing else writes it.

This is root's steps with those three closed. The test stops where
root's work ends: at the roster and the signal to the PEP.

| Step | Design | Here | The fake stands where |
|---|---|---|---|
| 1 the operator writes one file | a human | the test writes it | a human |
| 2 CI validates it | CI | `agent_family`, then root's own reader | CI |
| 3 the reconciler files it | `caregiver` | **`loop.look`, the real pass** | nothing |
| 4 the operator pastes | a human, ROOT seals | **the real intake, TLS, real `sops`** | a human |
| 5 the operator taps | a human | a granting transport | a human |
| 6 the PEP reloads | root writes the roster | **`executor.roster`, the real one** | nothing |

What is still faked, and why each one has to be: `uv` and `curl`
(`McpFake` — no test installs a real PyPI distribution), `systemctl` (a
recorded argv: this process may not signal a unit), and the phone (a
recorder, because the link is how a test learns a token that no route
ever answers with).

Then the same path BACKWARDS, which nothing had ever run: the operator deletes
the file, the loop asks for a release again, and root writes a roster
without that row.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

import pytest
import yaml
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, look
from caregiver.mcp_release import MARKER_NAME, McpPaths
from caregiver.policy_fleet import FakeChecker, FleetPolicy, PolicyTarget
from caregiver.reconcile import Actors
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from handover.catalog import CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.host import Command, make_sealer, seal_argv
from handover.executor.host import Result as RunResult
from handover.executor.mcpbuild import HUP_ARGV
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover.intake.gaps import GapDirectory
from handover.intake.notify import make_push
from handover.intake.run import Listener, Watch, one_pass
from handover.intake.run import Wiring as IntakeWiring
from handover.intake.service import Intake
from handover.intake.store import SecretStore
from handover.intake.token import Tokens
from handover_executor_fixtures import (
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    green_api,
    make_spool_dirs,
    stamp_tree,
    this_uid,
)
from handover_fixtures import manifest_text
from handover_mcp_fixtures import LOCK_TEXT, McpFake

pytestmark = pytest.mark.slow

COMPONENT: Final = "mcp-servers"
LIVE_VERSION: Final = "0.9.3"
NEW_VERSION: Final = "0.9.4"
NEXT_VERSION: Final = "0.9.5"
SERVER: Final = "kagi"
TOOL: Final = "echo"
SECRET: Final = "kagi_api_key"
LOOPBACK: Final = "127.0.0.1"
CLIENT_TIMEOUT_S: Final = 10.0

#: What the operator pastes at step 4. Obviously fake, and this test proves it
#: reaches no ledger, no roster and no argv word root builds.
PASTED: Final = "pasted-by-the-operator-at-step-4"
NOW: Final = 1_758_153_600.0

#: The one file the operator writes (§4.1 step 1), and the lock the same pull
#: request reviewed beside it (contract 01b §3.4).
SERVER_TOML: Final = f"""
name = "{SERVER}"
identity = "The fleet's Kagi API key. Search and page extraction, no account writes."
install.source = "pypi"
install.package = "kagimcp"
install.version = "1.0.2"
install.lock = "mcp/{SERVER}/install.lock"
install.python = "3.12"
run.entrypoint = "kagimcp"
run.env.STUB_SECRET = "secret:{SECRET}"
tools = [
    {{ name = "{TOOL}", description = "Echo the text back, prefixed with the credential." }},
    {{ name = "sleep", description = "Wait, so a test can hold a call in flight." }},
]
"""

#: `mcp-servers` as contract 06 §1 row 6 has it: one venv, no unit, so
#: nothing restarts for it and the reload is the only thing that moves.
BUILD_LINE: Final = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'

# --- the bench ---------------------------------------------------------------


class Bench:
    """One host: a registry, `caregiver`'s state, root's spool, root's
    secrets, root's MCP root and root's roster, all under `tmp_path`."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.registry = _made(tmp_path / "corpus" / "agent-registry")
        self.state = _made(tmp_path / "caregiver-state")
        self.spool = make_spool_dirs(tmp_path)
        self.secrets = _made(tmp_path / "secrets", mode=0o700)
        self.gaps = _made(tmp_path / "secret-gaps")
        self.installed = _made(tmp_path / "opt-mcp")
        self.roster = _made(tmp_path / "state") / "upstreams.yaml"
        self.components = _made(tmp_path / "components")
        self.loop_state = LoopState()
        self.now = NOW
        self.uid = os.getuid()
        _write_family(self.registry)

    # -- step 3 ---------------------------------------------------------

    def paths(self) -> McpPaths:
        return McpPaths(
            requests=self.spool / "requests",
            done=self.spool / DONE_DIR,
            secrets=self.secrets,
            gaps=self.gaps,
            installed_root=self.installed,
            marker=self.state / MARKER_NAME,
            roster=self.roster,
        )

    def reconcile(self) -> None:
        """One real pass of `caregiver`'s loop, registry read and all."""
        config = LoopConfig(
            registry_root=self.registry,
            state_root=self.state,
            image="sha256:deadbeef",
            mcp=self.paths(),
            clock=lambda: self.now,
        )
        actors = Actors(
            FakeDriver(),
            FakeLiteLLMKeys(),
            FakeSwitchClient(),
            EgressConfig(),
            FakeUnits(),
            FleetPolicy(PolicyTarget(self.state.parent / "etc-chaperone"), FakeChecker()),
        )
        look(config, actors, self.loop_state)

    def requests(self) -> list[Path]:
        return sorted(self.paths().requests.iterdir())

    # -- step 5 ---------------------------------------------------------

    def release(self, request: Path, live: str, wanted: str) -> str:
        """The real executor, over the real spool, into this temp root."""
        run = FakeRun(answers=git_host_answers())
        mcp = McpFake(entrypoint="kagimcp")
        _teach(run, mcp, self.components, _manifest(self.components))
        host = replace(
            fake_host(self.tmp, run),
            source_root=self.tmp / "corpus",
            install_roots=(self.components,),
            mcp_root=self.installed,
            roster_file=self.roster,
        )
        wiring = Wiring(
            host=host,
            transport=_grant,
            readers=fake_readers(latest={COMPONENT: wanted}),
            api=green_api(str(CATALOG_BY_NAME[COMPONENT].repo), f"{COMPONENT}-v{wanted}", _sha()),
        )
        stamp_tree(self.components, COMPONENT, live)
        spool = Spool(str(self.spool), this_uid())
        try:
            outcome = handle(spool, request.name, wiring, Counter())
        finally:
            spool.close()

        self.signals = [one for one in run.seen if tuple(one.argv) == HUP_ARGV]

        return outcome


# --- helpers root and the design already have --------------------------------


def _grant(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
    """§4.1 step 5: one tap. The human, and one of only two here."""
    del action_id, summary, wait_s

    return Decision(Verdict.GRANTED, gate, NOW + 10.0)


def _sha() -> str:
    return SHA_OF[COMPONENT]


def _manifest(components: Path) -> str:
    text = manifest_text(COMPONENT).replace("/opt/components", str(components))

    return text.replace("install:", BUILD_LINE, 1)


def _teach(run: FakeRun, mcp: McpFake, components: Path, manifest: str) -> None:
    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(manifest, encoding="utf-8")

    git = GitFake(write_tree)

    def dynamic(command: Command) -> RunResult | None:
        answered = git(command)
        if answered is not None:
            return answered

        if command.argv[0].endswith("/uv") and "sync" in command.argv:
            stamp_tree(components, f"{COMPONENT}.new", NEW_VERSION)

            return RunResult(0, "", "")

        if command.argv[0].endswith(("/uv", "/curl", "/install", "/chown", "/chmod")):
            return mcp(command)

        return None

    run.dynamic = dynamic


def _write_family(registry: Path) -> None:
    """One family, so `load_registry` has something beside the server."""
    directory = _made(registry / "families" / "chat")
    (directory / "family.toml").write_text(
        'name = "chat"\n'
        'kind = "attended"\n'
        'description = "Test family."\n'
        'model.router = "agent-router"\n'
        "model.budget_usd_per_day = 15\n",
        encoding="utf-8",
    )
    (directory / "instructions.md").write_text("Be helpful.\n", encoding="utf-8")


def _write_server(registry: Path) -> Path:
    directory = _made(registry / "mcp" / SERVER)
    (directory / "server.toml").write_text(SERVER_TOML, encoding="utf-8")
    (directory / "install.lock").write_text(LOCK_TEXT, encoding="utf-8")

    return directory


def _made(path: Path, mode: int = 0o755) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=mode)

    return path


# --- the real seal, and the real intake --------------------------------------


def _age_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A throwaway recipient. Root seals to PUBLIC keys only (§4.3), so
    this test holds the private half exactly as the PEP does.

    `host.SOPS` is absolute — `/usr/local/bin/sops`, which is where
    the host's is and not where this machine's is. Pointing the constant at
    the local binary keeps `seal_argv`'s own argv, which is the thing
    under test, and changes only where the binary sits.
    """
    from handover.executor import host as host_module

    sops, keygen = shutil.which("sops"), shutil.which("age-keygen")
    if sops is None or keygen is None:
        pytest.skip("sops and age-keygen are needed to seal and read back a real secret")

    monkeypatch.setattr(host_module, "SOPS", sops)
    key_file = tmp_path / "key.txt"
    made = subprocess.run(
        [keygen, "-o", str(key_file)], capture_output=True, timeout=60, check=True
    )

    return made.stderr.decode("utf-8").split(": ", 1)[1].strip(), key_file


def _certificate(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for loopback, made the way the
    host's own is: `openssl`, by hand, once."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not on this machine, so no TLS listener can be built here")

    cert, key = tmp_path / "intake.crt", tmp_path / "intake.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=agent-intake",
            "-addext",
            f"subjectAltName=IP:{LOOPBACK}",
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )

    return str(cert), str(key)


class _Phone:
    """The fake phone. It keeps each push whole, which is how the test
    learns the token: no route of the intake ever answers with one."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def __call__(self, body: dict[str, object]) -> bool:
        self.sent.append(body)

        return True

    def last_link(self) -> str:
        fields = self.sent[-1]["fields"]
        assert isinstance(fields, dict)

        return str(cast("dict[str, object]", fields)["link"])


def _paste_for_real(bench: Bench, recipient: str) -> None:
    """§4.3 steps 2 to 7, with root's own code and a real `sops`.

    The only fake is the phone. `SecretStore.fill` runs `make_sealer`,
    which runs `seal_argv`'s exact command, and the file it leaves is the
    file the PEP opens with `sops -d` at the reload.
    """
    certfile, keyfile = _certificate(bench.tmp)
    clock = lambda: bench.now  # noqa: E731 - a frozen clock, not a function
    phone = _Phone()
    tokens = Tokens(clock)
    directory = GapDirectory(bench.gaps, bench.uid)
    sealer = make_sealer((recipient,), bench.uid)
    store = SecretStore(bench.secrets, sealer, owner_uid=bench.uid)
    intake = Intake(tokens=tokens, store=store, close_gap=directory.close_named)
    listener = Listener(intake, certfile, keyfile, LOOPBACK, 0)
    wiring = IntakeWiring(
        gaps=directory,
        store=store,
        tokens=tokens,
        push=make_push("https://hook.invalid", "bearer", f"https://{LOOPBACK}:0", phone),
        watch=Watch(clock),
        listener=listener,
        say=lambda line: None,
    )
    try:
        assert one_pass(wiring) == (1, 0)
        token = urlsplit(phone.last_link()).path.strip("/").split("/")[-1]
        connection = _client(listener.port)
        body = urlencode({"token": token, "name": SECRET, "value": PASTED})
        connection.request(
            "POST",
            "/secret",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        answer = connection.getresponse()

        assert answer.status == 200, answer.read()
        connection.close()
    finally:
        listener.close()

    assert (bench.secrets / f"{SECRET}.enc").is_file()
    assert not list(bench.gaps.iterdir())


def _client(port: int) -> http.client.HTTPSConnection:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    return http.client.HTTPSConnection(LOOPBACK, port, context=context, timeout=CLIENT_TIMEOUT_S)


# --- the test ----------------------------------------------------------------


def test_one_file_one_secret_one_tap_and_then_the_same_backwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path)
    recipient, _ = _age_key(tmp_path, monkeypatch)

    # -- step 1: the operator writes one file ---------------------------
    _write_server(bench.registry)

    # -- step 3: the REAL reconcile loop files one request ----------------
    bench.reconcile()
    filed = bench.requests()

    assert len(filed) == 1, "the loop filed no `mcp-servers` request"
    assert (bench.gaps / f"{SECRET}.json").is_file()
    assert not any(bench.installed.iterdir()), "the reconciler installed something"

    # -- step 4: the operator pastes into the real intake, root seals for real ---
    _paste_for_real(bench, recipient)

    # -- step 5: one tap, and root does the rest --------------------------
    outcome = bench.release(filed[0], LIVE_VERSION, NEW_VERSION)

    assert outcome.endswith("succeeded"), outcome
    assert (bench.installed / SERVER / "bin" / "kagimcp").is_file()
    assert bench.signals, "root never signalled the PEP"

    # -- step 6a: root wrote the roster -----------------------------------
    written = yaml.safe_load(bench.roster.read_text(encoding="utf-8"))

    assert list(written) == [SERVER]
    assert written[SERVER]["command"] == str(bench.installed / SERVER / "bin" / "kagimcp")

    # -- backwards: the operator deletes the file -------------------------
    shutil.rmtree(bench.registry / "mcp" / SERVER)
    bench.now += 1.0
    bench.reconcile()
    removal = [one for one in bench.requests()]

    assert len(removal) == 1, "a removed server file asked for no release"

    outcome = bench.release(removal[0], NEW_VERSION, NEXT_VERSION)

    assert outcome.endswith("succeeded"), outcome
    assert yaml.safe_load(bench.roster.read_text(encoding="utf-8")) == {}
    assert bench.signals, "root never signalled the PEP for the removal"

    # Invariant 13, over the whole path: the pasted value is in no ledger,
    # no roster, no gap file and no argv word root ever built.
    assert PASTED not in bench.roster.read_text(encoding="utf-8")
    for entry in sorted((bench.spool / DONE_DIR).iterdir()):
        assert PASTED not in entry.read_text(encoding="utf-8")

    assert PASTED not in json.dumps(seal_argv((recipient,)))
