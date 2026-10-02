"""`HttpLiteLLMKeys` sends the payload probe 0b proved works, and never logs
a response body a success could carry the new key in. `FakeLiteLLMKeys`
gives every other test a LiteLLM that never leaves this process."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from agent_managerd.litellm_keys import (
    ALIAS_PREFIX,
    FakeLiteLLMKeys,
    HttpLiteLLMKeys,
    LiteLLMError,
    key_alias,
)

Handler = Callable[[httpx.Request], httpx.Response]


def client_with(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_key_alias_is_the_fixed_form() -> None:
    assert key_alias("chat") == f"{ALIAS_PREFIX}chat"


def test_master_key_must_not_be_empty() -> None:
    with pytest.raises(LiteLLMError, match="LITELLM_MASTER_KEY"):
        HttpLiteLLMKeys("http://litellm.test:4000", "")


# --- ensure_key ----------------------------------------------------------


def test_ensure_key_posts_the_probed_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"key": "sk-live-chat-1"})

    keys = HttpLiteLLMKeys("http://litellm.test:4000", "sk-master", client=client_with(handler))
    key = keys.ensure_key("chat", ["agent-router"], 15.0)

    assert key == "sk-live-chat-1"
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/key/generate"
    assert request.headers["authorization"] == "Bearer sk-master"
    body = json.loads(request.content)
    assert body == {
        "key_alias": "family-chat",
        "models": ["agent-router"],
        "max_budget": 15.0,
        "budget_duration": "1d",
    }


def test_ensure_key_raises_on_a_bad_status() -> None:
    keys = HttpLiteLLMKeys(
        "http://x", "sk-master", client=client_with(lambda r: httpx.Response(500))
    )
    with pytest.raises(LiteLLMError, match="HTTP 500"):
        keys.ensure_key("chat", ["agent-router"], 15.0)


def test_ensure_key_raises_when_no_key_comes_back() -> None:
    keys = HttpLiteLLMKeys(
        "http://x", "sk-master", client=client_with(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(LiteLLMError, match="no usable key"):
        keys.ensure_key("chat", ["agent-router"], 15.0)


def test_a_failed_mint_never_logs_the_response_body() -> None:
    """A 200 body IS the key; a non-200 body might still leak one on a
    misbehaving proxy. Neither path may put resp.text in the exception."""
    secret_marker = "sk-should-never-appear-in-an-error-message"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret_marker)

    keys = HttpLiteLLMKeys("http://x", "sk-master", client=client_with(handler))
    with pytest.raises(LiteLLMError) as excinfo:
        keys.ensure_key("chat", ["agent-router"], 15.0)

    assert secret_marker not in str(excinfo.value)


# --- update_key ------------------------------------------------------------


def test_update_key_posts_the_key_itself() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    keys = HttpLiteLLMKeys("http://x", "sk-master", client=client_with(handler))
    keys.update_key("sk-live-chat-1", ["code-router"], 30.0)

    body = json.loads(seen[0].content)
    assert body == {
        "key": "sk-live-chat-1",
        "models": ["code-router"],
        "max_budget": 30.0,
        "budget_duration": "1d",
    }
    assert seen[0].url.path == "/key/update"


def test_update_key_raises_on_a_bad_status() -> None:
    keys = HttpLiteLLMKeys(
        "http://x", "sk-master", client=client_with(lambda r: httpx.Response(404))
    )
    with pytest.raises(LiteLLMError, match="HTTP 404"):
        keys.update_key("sk-live-chat-1", ["agent-router"], 15.0)


# --- rotate_key --------------------------------------------------------------


def test_rotate_key_mints_a_fresh_key() -> None:
    """Rotate mints under the same alias, same as ensure_key, until a live
    probe settles the overlap question."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"key": f"sk-live-chat-{calls}"})

    keys = HttpLiteLLMKeys("http://x", "sk-master", client=client_with(handler))
    first = keys.ensure_key("chat", ["agent-router"], 15.0)
    second = keys.rotate_key("chat", ["agent-router"], 15.0)

    assert first != second
    assert calls == 2


# --- delete_key --------------------------------------------------------------


def test_delete_key_deletes_by_alias() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    keys = HttpLiteLLMKeys("http://x", "sk-master", client=client_with(handler))
    keys.delete_key("chat")

    assert seen[0].url.path == "/key/delete"
    assert json.loads(seen[0].content) == {"key_aliases": ["family-chat"]}


def test_delete_key_raises_on_a_bad_status() -> None:
    keys = HttpLiteLLMKeys(
        "http://x", "sk-master", client=client_with(lambda r: httpx.Response(500))
    )
    with pytest.raises(LiteLLMError, match="HTTP 500"):
        keys.delete_key("chat")


# --- FakeLiteLLMKeys -----------------------------------------------------


def test_fake_mints_a_distinct_key_per_call() -> None:
    fake = FakeLiteLLMKeys()
    first = fake.ensure_key("chat", ["agent-router"], 15.0)
    second = fake.rotate_key("chat", ["agent-router"], 15.0)
    assert first != second
    assert fake.minted == 2


def test_fake_update_key_needs_a_minted_key() -> None:
    fake = FakeLiteLLMKeys()
    with pytest.raises(LiteLLMError, match="no such key"):
        fake.update_key("sk-never-minted", ["agent-router"], 15.0)


def test_fake_update_key_changes_the_budget() -> None:
    fake = FakeLiteLLMKeys()
    key = fake.ensure_key("chat", ["agent-router"], 15.0)
    fake.update_key(key, ["agent-router"], 30.0)
    assert fake.budgets[key_alias("chat")] == (["agent-router"], 30.0)


def test_fake_delete_key_records_the_alias_and_forgets_the_key() -> None:
    fake = FakeLiteLLMKeys()
    fake.ensure_key("chat", ["agent-router"], 15.0)
    fake.delete_key("chat")
    assert fake.deleted == ["family-chat"]
    assert "family-chat" not in fake.keys


# --- read_spend (contract 05 section 7) ----------------------------------


def test_read_spend_bears_the_family_key_itself() -> None:
    """`POST /key/info` answers 405, and a key value may never ride in a
    URL (invariant 13). Presenting the key as its own bearer is what is
    left, and the master key is not used at all."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"info": {"spend": 3.42, "max_budget": 15.0}})

    keys = HttpLiteLLMKeys("http://litellm.test:4000", "sk-master", client=client_with(handler))
    spend = keys.read_spend("sk-family-chat")

    assert seen[0].method == "GET"
    assert "sk-family-chat" not in str(seen[0].url)
    assert seen[0].headers["authorization"] == "Bearer sk-family-chat"
    assert spend.spend_usd == 3.42
    assert spend.budget_usd == 15.0


def test_read_spend_reads_a_flat_body_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"spend": 1.5, "max_budget": None})

    keys = HttpLiteLLMKeys("http://litellm.test:4000", "sk-master", client=client_with(handler))
    spend = keys.read_spend("sk-family-chat")
    assert spend.spend_usd == 1.5
    assert spend.budget_usd is None


def test_read_spend_raises_on_a_refusal() -> None:
    keys = HttpLiteLLMKeys(
        "http://litellm.test:4000",
        "sk-master",
        client=client_with(lambda _: httpx.Response(401, json={})),
    )
    with pytest.raises(LiteLLMError):
        keys.read_spend("sk-family-chat")


def test_fake_read_spend_answers_the_recorded_number() -> None:
    fake = FakeLiteLLMKeys()
    key = fake.ensure_key("chat", ["agent-router"], 15.0)
    fake.spends[key] = 2.5
    spend = fake.read_spend(key)
    assert (spend.spend_usd, spend.budget_usd) == (2.5, 15.0)


# --- a LiteLLM that is away ----------------------------------------------

KeysCall = Callable[[HttpLiteLLMKeys], object]

EVERY_VERB: tuple[tuple[str, KeysCall], ...] = (
    ("ensure_key", lambda keys: keys.ensure_key("chat", ["agent-router"], 15.0)),
    ("update_key", lambda keys: keys.update_key("sk-family-chat", ["agent-router"], 15.0)),
    ("rotate_key", lambda keys: keys.rotate_key("chat", ["agent-router"], 15.0)),
    ("delete_key", lambda keys: keys.delete_key("chat")),
    ("read_spend", lambda keys: keys.read_spend("sk-family-chat")),
)

VERBS_THAT_READ_A_BODY: tuple[tuple[str, KeysCall], ...] = (EVERY_VERB[0], EVERY_VERB[4])


def refuse_the_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno 111] Connection refused", request=request)


def never_answer(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize("away", [refuse_the_connection, never_answer])
@pytest.mark.parametrize(("verb", "call"), EVERY_VERB, ids=[verb for verb, _ in EVERY_VERB])
def test_a_litellm_that_is_away_is_a_litellm_error(
    verb: str, call: KeysCall, away: Handler
) -> None:
    """`sudo systemctl restart ai-stack` takes LiteLLM away for about half a
    minute. A raw `httpx.ConnectError` from a spend read reaches no caller
    that catches it and would leave `serve` through the top of its loop.
    Every caller already turns a
    `LiteLLMError` into a fault the status document carries."""
    keys = HttpLiteLLMKeys("http://litellm.test:4000", "sk-master", client=client_with(away))

    with pytest.raises(LiteLLMError) as caught:
        call(keys)

    assert "sk-" not in str(caught.value), verb


@pytest.mark.parametrize(
    "body", [b"<html>502 Bad Gateway</html>", b"[]", b'"sk-not-an-object"'], ids=repr
)
@pytest.mark.parametrize(
    ("verb", "call"), VERBS_THAT_READ_A_BODY, ids=[verb for verb, _ in VERBS_THAT_READ_A_BODY]
)
def test_a_body_that_is_no_json_object_is_a_litellm_error(
    verb: str, call: KeysCall, body: bytes
) -> None:
    """A proxy in front of a LiteLLM that is starting answers 200 with a
    page. The body is never echoed: on a mint it may hold the key."""
    keys = HttpLiteLLMKeys(
        "http://litellm.test:4000",
        "sk-master",
        client=client_with(lambda _: httpx.Response(200, content=body)),
    )

    with pytest.raises(LiteLLMError) as caught:
        call(keys)

    assert "sk-" not in str(caught.value), verb
    assert "html" not in str(caught.value), verb
