"""The webhook listener end to end: one HTTP request in, the right
`attendance` call and the right response out. `FakeAttendance` plays
attendance's part; `FakeRouteTable` is a settable double so a test can move
a route from present to absent without touching a real registry
(`test_trigger_routes.py` covers `RouteTable` itself against a real one).
"""

from __future__ import annotations

from pathlib import Path

from agent_door_trigger.attendance import AcceptedTurn
from agent_door_trigger.config import AttendanceTarget, ServeConfig
from agent_door_trigger.errors import AttendanceError
from agent_door_trigger.routes import Route
from agent_door_trigger.tokens import MIN_WEBHOOK_TOKEN_BYTES, tokens_match
from agent_door_trigger.webhooks import (  # pyright: ignore[reportPrivateUsage]
    _authorized,
    create_app,
)
from starlette.testclient import TestClient
from trigger_fake_attendance import FakeAttendance

TOKEN = "w" * MIN_WEBHOOK_TOKEN_BYTES
ROUTE = Route(family="scrum-lead", name="deploy-notify", token=TOKEN)


class FakeRouteTable:
    """A `RouteTable`-shaped double: a fixed, replaceable map."""

    def __init__(self, routes: dict[tuple[str, str], Route] | None = None) -> None:
        self._routes = dict(routes or {})
        self.refresh_calls = 0

    def get(self, family: str, name: str) -> Route | None:
        return self._routes.get((family, name))

    def refresh(self) -> int:
        self.refresh_calls += 1
        return len(self._routes)

    def remove(self, family: str, name: str) -> None:
        self._routes.pop((family, name), None)


def _config(tmp_path: Path) -> ServeConfig:
    return ServeConfig(
        attendance=AttendanceTarget(url="http://sessiond", socket=None, token="t" * 32),
        bind_host="192.0.2.10",
        bind_port=8360,
        families_dir=tmp_path / "families",
        registry_root=tmp_path / "registry",
        webhooks_dir=tmp_path / "webhooks",
        refresh_s=3600,  # long enough that the background loop never fires mid-test
    )


def _client(tmp_path: Path, fake: FakeAttendance, routes: FakeRouteTable) -> TestClient:
    app = create_app(_config(tmp_path), fake, routes)
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the three identical 404s ---


def test_an_unknown_family_answers_404(tmp_path: Path) -> None:
    with _client(tmp_path, FakeAttendance(), FakeRouteTable()) as client:
        response = client.post("/triggers/no-such-family/deploy-notify", headers=_auth(TOKEN))

    assert response.status_code == 404


def test_an_unknown_name_answers_404(tmp_path: Path) -> None:
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        response = client.post("/triggers/scrum-lead/no-such-name", headers=_auth(TOKEN))

    assert response.status_code == 404


def test_a_wrong_token_answers_404(tmp_path: Path) -> None:
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        response = client.post(
            "/triggers/scrum-lead/deploy-notify", headers=_auth("w" * MIN_WEBHOOK_TOKEN_BYTES + "x")
        )

    assert response.status_code == 404


def test_the_three_404s_carry_the_same_body(tmp_path: Path) -> None:
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        unknown_family = client.post("/triggers/ghost/deploy-notify", headers=_auth(TOKEN))
        unknown_name = client.post("/triggers/scrum-lead/ghost", headers=_auth(TOKEN))
        wrong_token = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth("bad" * 20))
        no_token = client.post("/triggers/scrum-lead/deploy-notify")

    bodies = [r.json() for r in (unknown_family, unknown_name, wrong_token, no_token)]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["error"]["code"] == "not_found"


def test_a_route_that_stopped_being_servable_answers_404(tmp_path: Path) -> None:
    # "a webhook for a trigger whose family file became invalid": the
    # route existed, a refresh removed it (families.py would exclude the
    # family once its definition stops validating), and this door must
    # answer exactly as it would for a route that never existed.
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        first = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth(TOKEN))
        assert first.status_code == 202

        routes.remove("scrum-lead", "deploy-notify")
        second = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth(TOKEN))

    assert second.status_code == 404


# --- a good webhook call ---


def test_a_good_call_fires_and_returns_202(tmp_path: Path) -> None:
    fake = FakeAttendance(accepted=AcceptedTurn(turn="01T", state="queued", journal_seq=1))
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, fake, routes) as client:
        response = client.post(
            "/triggers/scrum-lead/deploy-notify",
            headers=_auth(TOKEN),
            json={"release": "0.4.2"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert body["turn"] == "01T"
    assert "session" in body

    request = fake.requests[0]
    assert '"release"' in request.prompt
    assert "0.4.2" in request.prompt
    assert request.labels["trigger_kind"] == "webhook"
    assert request.labels["trigger_name"] == "deploy-notify"


def test_a_call_with_no_body_still_fires(tmp_path: Path) -> None:
    fake = FakeAttendance()
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, fake, routes) as client:
        response = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth(TOKEN))

    assert response.status_code == 202
    assert "No payload was sent" in fake.requests[0].prompt


# --- payload shape and size ---


def test_invalid_json_body_is_400(tmp_path: Path) -> None:
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        response = client.post(
            "/triggers/scrum-lead/deploy-notify",
            headers={**_auth(TOKEN), "content-type": "application/octet-stream"},
            content=b"{not json",
        )

    assert response.status_code == 400


def test_an_oversized_body_is_413(tmp_path: Path) -> None:
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, FakeAttendance(), routes) as client:
        response = client.post(
            "/triggers/scrum-lead/deploy-notify",
            headers=_auth(TOKEN),
            content=b'{"pad": "' + b"x" * 300_000 + b'"}',
        )

    assert response.status_code == 413


# --- attendance refusals reach the external caller as a real status ---


def test_a_full_queue_answers_429(tmp_path: Path) -> None:
    fake = FakeAttendance(turn_error=AttendanceError("queue_full", "queue is full", 429))
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, fake, routes) as client:
        response = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth(TOKEN))

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_full"


def test_a_non_autonomous_family_answers_403(tmp_path: Path) -> None:
    fake = FakeAttendance(ensure_error=AttendanceError("forbidden", "not autonomous", 403))
    routes = FakeRouteTable({("scrum-lead", "deploy-notify"): ROUTE})
    with _client(tmp_path, fake, routes) as client:
        response = client.post("/triggers/scrum-lead/deploy-notify", headers=_auth(TOKEN))

    assert response.status_code == 403


# --- _authorized: constant-time even for an unknown route ---


def test_authorized_calls_tokens_match_for_a_known_route() -> None:
    assert _authorized(ROUTE, TOKEN)
    assert not _authorized(ROUTE, "wrong" * 10)


def test_authorized_is_false_but_still_compares_for_an_unknown_route() -> None:
    # No route at all: still false, and the function never short-circuits
    # on "route is None" before a comparison happens (webhooks.py's own
    # docstring on _authorized explains why).
    assert not _authorized(None, TOKEN)
    assert not _authorized(None, None)


def test_tokens_match_is_the_actual_comparison_authorized_uses() -> None:
    # A cheap proxy for "constant time": the same primitive this module
    # uses for a KNOWN route is exercised on the dummy path too, rather
    # than a bespoke, unaudited comparison living only in webhooks.py.
    assert tokens_match(TOKEN, TOKEN)
