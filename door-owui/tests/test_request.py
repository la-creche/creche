"""Reading one Open WebUI request: the ids in the headers, the body shapes."""

from __future__ import annotations

from typing import Any

import pytest
from agent_door_owui import headers as owui_headers
from agent_door_owui.errors import DoorError
from agent_door_owui.headers import (
    CHAT_ID_HEADER,
    MESSAGE_ID_HEADER,
    PARENT_ID_HEADER,
    TASK_HEADER,
    USER_MESSAGE_ID_HEADER,
    _checked,  # pyright: ignore[reportPrivateUsage]
    read_ids,
)
from agent_door_owui.openai_api import (
    MAX_PROMPT_BYTES,
    ChatRequest,
    family_of,
    is_family_name,
    models_body,
    parse_chat_request,
)

CHAT = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
MESSAGE = "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11"


def _headers(**overrides: str) -> dict[str, str]:
    base = {
        CHAT_ID_HEADER: CHAT,
        MESSAGE_ID_HEADER: MESSAGE,
        USER_MESSAGE_ID_HEADER: "a1b2c3d4-5e6f-4071-8293-a4b5c6d7e8f9",
        PARENT_ID_HEADER: "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f",
        TASK_HEADER: "",
    }
    base.update(overrides)
    return base


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "agent:chat",
        "messages": [{"role": "user", "content": "which sensor dropped out?"}],
    }
    base.update(overrides)
    return base


def test_no_custom_header_is_authorization() -> None:
    # Open WebUI applies custom headers LAST, so one named Authorization
    # would silently replace the connection key.
    names = {
        owui_headers.CHAT_ID_HEADER,
        owui_headers.MESSAGE_ID_HEADER,
        owui_headers.USER_MESSAGE_ID_HEADER,
        owui_headers.PARENT_ID_HEADER,
        owui_headers.TASK_HEADER,
    }

    assert len(names) == 5
    assert all(name == name.lower() for name in names)
    assert "authorization" not in names


def test_ids_become_a_session_id() -> None:
    ids = read_ids(_headers())

    assert ids.session == f"owui-{CHAT}"
    assert ids.message_id == MESSAGE
    assert ids.parent_id == "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f"
    assert not ids.is_background_task


def test_an_empty_chat_id_is_refused() -> None:
    # An absent token arrives present but empty. Falling back would put every
    # chat in one session.
    with pytest.raises(DoorError) as caught:
        read_ids(_headers(**{CHAT_ID_HEADER: ""}))

    assert caught.value.code == "missing_chat_id"
    assert "X-OWUI-Chat-Id" in caught.value.message


def test_a_missing_chat_header_is_refused() -> None:
    bare = {MESSAGE_ID_HEADER: MESSAGE}

    with pytest.raises(DoorError) as caught:
        read_ids(bare)

    assert caught.value.code == "missing_chat_id"


def test_a_crafted_chat_id_is_refused() -> None:
    for bad in ("../../etc/passwd", "a b", "-leading-dash", "x" * 200, "a/b"):
        with pytest.raises(DoorError) as caught:
            read_ids(_headers(**{CHAT_ID_HEADER: bad}))

        assert caught.value.code in ("bad_id", "missing_chat_id")


def test_an_id_with_a_trailing_newline_is_refused() -> None:
    # A `$` also matches before a final newline. `\Z` does not. `read_ids`
    # strips the value first, so the check is called directly.
    assert _checked(CHAT, CHAT_ID_HEADER, "chat id") == CHAT

    with pytest.raises(DoorError) as caught:
        _checked(CHAT + "\n", CHAT_ID_HEADER, "chat id")

    assert caught.value.code == "bad_id"


def test_an_empty_message_id_is_refused() -> None:
    # The message id is the idempotency key (contract 02 §6). Without it a
    # retried request runs the turn twice.
    with pytest.raises(DoorError) as caught:
        read_ids(_headers(**{MESSAGE_ID_HEADER: ""}))

    assert caught.value.code == "missing_message_id"


def test_empty_optional_ids_read_as_absent() -> None:
    ids = read_ids(_headers(**{USER_MESSAGE_ID_HEADER: "", PARENT_ID_HEADER: ""}))

    assert ids.user_message_id is None
    assert ids.parent_id is None


def test_a_task_header_marks_a_background_request() -> None:
    ids = read_ids(_headers(**{TASK_HEADER: "title_generation"}))

    assert ids.is_background_task
    assert ids.task == "title_generation"


def test_a_request_becomes_a_prompt_and_a_persona() -> None:
    request = parse_chat_request(
        _body(
            messages=[
                {"role": "system", "content": "You answer as the house assistant."},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "an old answer"},
                {"role": "user", "content": [{"type": "text", "text": "which sensor?"}]},
            ],
            stream=True,
        )
    )

    assert request == ChatRequest(
        family="chat",
        prompt="which sensor?",
        persona="You answer as the house assistant.",
        stream=True,
    )


def test_history_is_not_replayed() -> None:
    # `attendance` owns the transcript (invariant 5). Only the new prompt goes.
    request = parse_chat_request(
        _body(messages=[{"role": "user", "content": "one"}, {"role": "user", "content": "two"}])
    )

    assert request.prompt == "two"
    assert request.persona is None
    assert request.stream is False


def test_every_system_message_joins_the_persona() -> None:
    request = parse_chat_request(
        _body(
            messages=[
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
                {"role": "user", "content": "go"},
            ]
        )
    )

    assert request.persona == "first\nsecond"


def test_a_request_with_no_user_message_is_refused() -> None:
    with pytest.raises(DoorError) as caught:
        parse_chat_request(_body(messages=[{"role": "system", "content": "only a persona"}]))

    assert caught.value.code == "no_prompt"


def test_an_oversized_prompt_is_refused() -> None:
    with pytest.raises(DoorError) as caught:
        parse_chat_request(
            _body(messages=[{"role": "user", "content": "x" * (MAX_PROMPT_BYTES + 1)}])
        )

    assert caught.value.code == "payload_too_large"
    assert caught.value.status == 413


def test_a_junk_body_is_refused() -> None:
    for bad in ([], "text", {"model": "agent:chat"}, {"model": "agent:chat", "messages": "hi"}):
        with pytest.raises(DoorError):
            parse_chat_request(bad)


def test_only_an_agent_model_is_served() -> None:
    assert family_of("agent:chat") == "chat"

    for bad in ("chat", "gpt-4o", "agent:", "agent:Chat", "agent:x", "agent:../chat"):
        with pytest.raises(DoorError) as caught:
            family_of(bad)

        assert caught.value.code == "bad_model"


def test_a_family_with_a_trailing_newline_is_refused() -> None:
    # A `$` also matches before a final newline. `\Z` does not.
    assert is_family_name("chat")
    assert not is_family_name("chat\n")

    with pytest.raises(DoorError) as caught:
        family_of("agent:chat\n")

    assert caught.value.code == "bad_model"


def test_the_models_body_is_openai_shaped() -> None:
    body = models_body(["chat", "code"])

    assert body["object"] == "list"
    assert body["data"] == [
        {"id": "agent:chat", "object": "model", "created": 0, "owned_by": "agent-control"},
        {"id": "agent:code", "object": "model", "created": 0, "owned_by": "agent-control"},
    ]
