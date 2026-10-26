"""The model stand-in, run by itself over its port.

No test here starts the agent module. Each test sends what an
`openai-completions` client sends, and reads the SSE stream with the rules
of `proc_sse.py`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import standin_model
from proc_harness import LOOPBACK, Supervisor
from proc_sse import Frames, parse
from proc_stack import base_env
from proc_standins import (
    MODEL,
    MODEL_DELAY_MS,
    model_calls,
    script_model,
    start_model,
    text_turn,
    tool_turn,
    tune,
)
from proc_tree import Tree, build_bare_tree

#: An obvious fixture, never a credential.
KEY = "FIXTURE-FAMILY-KEY"
TOOL = "mcp__ch__standin__echo"
BODY: dict[str, Any] = {
    "model": "family-chat",
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_FAILED = 500
DELAY_MS = 200


@dataclass(frozen=True, slots=True)
class Model:
    tree: Tree
    client: httpx.Client


@pytest.fixture
def model(tree: Tree, supervisor: Supervisor) -> Model:
    """A root with the model stand-in, listening. The `supervisor` fixture ends it."""
    build_bare_tree(tree)
    _, port = start_model(tree, supervisor, base_env(tree), KEY)
    headers = {"Authorization": f"Bearer {KEY}"}
    client = httpx.Client(base_url=f"http://{LOOPBACK}:{port}", headers=headers, timeout=30.0)

    return Model(tree, client)


def _complete(model: Model, body: dict[str, Any] = BODY) -> tuple[int, Frames | None]:
    answer = model.client.post(standin_model.COMPLETIONS_PATH, json=body)

    if answer.status_code != HTTP_OK:
        return answer.status_code, None

    assert answer.headers["content-type"] == "text/event-stream"

    return answer.status_code, parse(answer.text)


def test_model_streams_a_text_turn_one_word_in_each_chunk(model: Model) -> None:
    script_model(model.tree, [text_turn("hello brave world")])

    _, frames = _complete(model)

    assert frames is not None
    assert frames.text == "hello brave world"
    assert [chunk["choices"][0]["delta"].get("content") for chunk in frames.chunks] == [
        "",
        "hello",
        " brave",
        " world",
        None,
    ]
    assert frames.finish_reasons == ["stop"]
    assert frames.ends_with_done


def test_model_streams_a_tool_call_in_one_chunk(model: Model) -> None:
    script_model(model.tree, [tool_turn(TOOL, {"text": "hi"})])

    _, frames = _complete(model)

    assert frames is not None
    calls = [chunk["choices"][0]["delta"]["tool_calls"] for chunk in frames.chunks[1:2]]
    ((call,),) = calls
    assert call["type"] == "function"
    assert call["function"]["name"] == TOOL
    assert json.loads(call["function"]["arguments"]) == {"text": "hi"}
    assert frames.finish_reasons == ["tool_calls"]
    assert frames.ends_with_done


def test_model_takes_the_turns_in_order_and_fails_past_the_last(model: Model) -> None:
    script_model(model.tree, [text_turn("one"), text_turn("two")])

    texts = [_complete(model)[1] for _ in range(2)]
    status, _ = _complete(model)

    assert [frames.text for frames in texts if frames is not None] == ["one", "two"]
    assert status == HTTP_FAILED


def test_model_reads_turns_that_a_test_adds_while_it_runs(model: Model) -> None:
    script_model(model.tree, [text_turn("one")])
    _complete(model)
    script_model(model.tree, [text_turn("one"), text_turn("two")])

    _, frames = _complete(model)

    assert frames is not None
    assert frames.text == "two"


def test_model_refuses_another_bearer_and_records_no_line(model: Model) -> None:
    script_model(model.tree, [text_turn("one")])

    answer = model.client.post(
        standin_model.COMPLETIONS_PATH, json=BODY, headers={"Authorization": "Bearer other"}
    )

    assert answer.status_code == HTTP_UNAUTHORIZED
    assert model_calls(model.tree) == []


def test_model_refuses_a_completion_that_does_not_stream(model: Model) -> None:
    """The refused call takes no turn: the next stream gets the first one."""
    script_model(model.tree, [text_turn("one")])

    status, _ = _complete(model, {**BODY, "stream": False})
    _, frames = _complete(model)

    assert status == HTTP_BAD_REQUEST
    assert frames is not None
    assert frames.text == "one"


def test_model_lists_its_one_model(model: Model) -> None:
    answer = model.client.get(standin_model.MODELS_PATH)

    assert answer.status_code == HTTP_OK
    assert [entry["id"] for entry in answer.json()["data"]] == [standin_model.DEFAULT_MODEL]


def test_model_answers_not_found_on_a_route_it_does_not_know(model: Model) -> None:
    assert model.client.post("/v1/completions", json=BODY).status_code == HTTP_NOT_FOUND


def test_model_records_each_request_with_its_body(model: Model) -> None:
    script_model(model.tree, [text_turn("one")])

    _complete(model)
    model.client.get(standin_model.MODELS_PATH)

    assert model_calls(model.tree) == [
        {"path": standin_model.COMPLETIONS_PATH, "body": BODY},
        {"path": standin_model.MODELS_PATH, "body": None},
    ]


def test_model_waits_between_two_chunks_when_a_test_says_so(model: Model) -> None:
    """Five chunks for three words, so four waits."""
    script_model(model.tree, [text_turn("a slow turn")])
    tune(model.tree, MODEL, MODEL_DELAY_MS, str(DELAY_MS))
    started = time.monotonic()

    _, frames = _complete(model)

    assert frames is not None
    assert time.monotonic() - started >= 4 * DELAY_MS / 1000
