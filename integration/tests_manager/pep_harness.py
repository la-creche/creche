"""The real PEP process, built the way the real unit builds it.

`agent_pep.__main__.main()` is the only place that turns environment into a
`PepConfig`. A test that built `PepConfig` itself would prove the app and
skip the plumbing, so this module calls `main()` with `PEP_REWORK_DIR` set
and intercepts the one line that would block on a socket.

    env  --> agent_pep.__main__.main() --> create_app() --> uvicorn.run
                                                                |
                                          captured here --------'

The interception replaces nothing inside the app: `uvicorn.run` is the last
statement `main()` reaches, and what it would have served is what the test
then serves, either through a `TestClient` or on a loopback port.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

#: What the local embedding service reports (`pep/tests/test_family_app.py`,
#: measured 2026-09). The fake answers the same id so a test can assert it.
EMBED_MODEL = "BAAI/bge-base-en-v1.5"
EMBED_DIMS = 768

#: An obvious fixture. Invariant 13 forbids a real secret in a test fixture.
FIXTURE_HA_TOKEN = "FIXTURE-HA-TOKEN"

HTTP_OK = 200
HTTP_NOT_FOUND = 404


def fake_lan_transport() -> httpx.MockTransport:
    """Every LAN service the PEP's own tests fake, and nothing else.

    The PEP reaches these on the LAN with its own credentials. They are not
    the seam under test, so they are faked exactly as `pep/tests` fakes
    them -- growing this into a mock that makes a wrong PEP look right is
    the failure mode to avoid.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/embed"):
            return httpx.Response(HTTP_OK, json=[[0.1] * EMBED_DIMS])

        if url.endswith("/info"):
            return httpx.Response(HTTP_OK, json={"model_id": EMBED_MODEL})

        if "/api/services/" in url:
            return httpx.Response(HTTP_OK, text="[]")

        return httpx.Response(HTTP_NOT_FOUND, text="nope")

    return httpx.MockTransport(handle)


def build_pep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rework_dir: Path,
    *,
    secrets: dict[str, str] | None = None,
    gatekeeper: object | None = None,
) -> FastAPI:
    """The real entry point, stopped one statement before it serves.

    Two things a real host supplies from outside `main()` are supplied here
    the same way: the HA token, which rides in the sops file, and the HTTP
    client, which the lifespan builds. Both go in through `create_app`'s own
    parameters, so no code inside the app is replaced.

    `secrets` stands in for the sops file, so a test can exercise a key
    `main()` reads out of it. `gatekeeper` is contract 04 §8's phone rail,
    which no test may reach for real: it goes in through `create_app`'s own
    parameter, like the HTTP client.
    """
    # `PEP_AUDIT_DIR` is the log for a request that resolved to no family,
    # not contract 04 §6's audit — that one hangs off `PEP_REWORK_DIR`.
    monkeypatch.setenv("PEP_AUDIT_DIR", str(tmp_path / "pep-audit-unidentified"))
    monkeypatch.setenv("PEP_REWORK_DIR", str(rework_dir))

    from agent_pep import __main__ as entry

    if secrets is not None:
        # `main()` reads the file this names, so the decryption is what is
        # replaced, not the config it produces.
        monkeypatch.setenv("PEP_SECRETS", str(tmp_path / "secrets.enc.yaml"))
        monkeypatch.setattr(entry, "load_sops_secrets", lambda _path: dict(secrets))

    real_create_app = entry.create_app

    def create_with_fakes(cfg: Any, **kwargs: Any) -> FastAPI:
        # `secrets` is a plain dict on a frozen dataclass, which is how the
        # real process also gets it: `main()` fills it from sops.
        cfg.secrets["HA_TOKEN"] = FIXTURE_HA_TOKEN
        kwargs["http_client"] = httpx.AsyncClient(transport=fake_lan_transport())
        if gatekeeper is not None:
            kwargs["gatekeeper"] = gatekeeper
        return real_create_app(cfg, **kwargs)

    captured: list[FastAPI] = []

    def capture(app: object, **_: object) -> None:
        assert isinstance(app, FastAPI)
        captured.append(app)

    monkeypatch.setattr(entry, "create_app", create_with_fakes)
    monkeypatch.setattr(entry.uvicorn, "run", capture)
    assert entry.main() == 0, "agent_pep.__main__.main() refused its environment"
    return captured[0]


def free_port() -> int:
    """An ephemeral loopback port, released before the server takes it.

    A bound-then-closed port can in principle be taken by another process
    in the gap. Nothing else in this test run binds, and the alternative --
    letting uvicorn pick and reading it back -- needs the server object
    before the URL, which the bridge driver needs first.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serving(app: FastAPI, port: int) -> Iterator[str]:
    """Run `app` on 127.0.0.1 for as long as the block lasts.

    Loopback, not the LAN address: a test binds nothing another host can
    reach.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="pep-under-test", daemon=True)
    thread.start()
    try:
        _wait_until_up(server)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _wait_until_up(server: uvicorn.Server, timeout_s: float = 20.0) -> None:
    deadline = threading.Event()
    waited = 0.0
    step = 0.02
    while not server.started:
        deadline.wait(step)
        waited += step
        if waited >= timeout_s:
            raise TimeoutError("the PEP did not start inside 20s")
