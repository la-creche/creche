"""The per-connection custom headers Open WebUI sends, and their traps.

Open WebUI pops `metadata` out of the body before the upstream call, so
nothing in the body names the chat (contract 02 §10, probe 0c). The ids
travel in per-connection custom headers, which the operator writes once as
JSON in the connection settings.

Three facts about those headers shape this module.

1. An absent value arrives PRESENT BUT EMPTY, not omitted. An empty chat id
   is therefore the normal failure, and it must never fall back to a shared
   session: every chat would land in one conversation.
2. Custom headers are applied LAST and win, so a header named
   `Authorization` would silently replace the connection key. No name here
   may be that name, and `test_headers.py` pins it.
3. Contract 02 §10 fixes the five header names. HTTP header names are
   case-insensitive, so the door matches them folded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import HTTP_BAD_REQUEST, DoorError

CHAT_ID_HEADER = "x-owui-chat-id"
MESSAGE_ID_HEADER = "x-owui-message-id"
USER_MESSAGE_ID_HEADER = "x-owui-user-message-id"
PARENT_ID_HEADER = "x-owui-parent-id"
TASK_HEADER = "x-owui-task"

# The session id charset of contract 02 §2, applied to the chat id before it
# becomes `owui-<chat id>`. A chat id is a UUID today; the check is what
# keeps a crafted one out of a path segment.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# An Open WebUI id is a 36-character UUID. The cap is generous and bounded,
# because an id arrives from outside this system (invariant 14).
MAX_ID_LENGTH = 128

SESSION_PREFIX = "owui-"

_SET_HEADERS_HINT = (
    "Set the connection's custom headers to "
    '{"X-OWUI-Chat-Id": "{{CHAT_ID}}", "X-OWUI-Message-Id": "{{MESSAGE_ID}}", '
    '"X-OWUI-User-Message-Id": "{{USER_MESSAGE_ID}}", '
    '"X-OWUI-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}", "X-OWUI-Task": "{{TASK}}"}.'
)


@dataclass(frozen=True)
class OwuiIds:
    """What one Open WebUI request says about where it belongs."""

    chat_id: str
    message_id: str
    user_message_id: str | None
    parent_id: str | None
    task: str | None

    @property
    def session(self) -> str:
        """The session id of contract 02 §2: `owui-<chat id>`."""
        return f"{SESSION_PREFIX}{self.chat_id}"

    @property
    def is_background_task(self) -> bool:
        """True for Open WebUI's own title, tag and follow-up requests."""
        return self.task is not None


def read_ids(headers: dict[str, str]) -> OwuiIds:
    """Read and validate the ids of one request. Raises `DoorError`."""
    chat_id = _required(headers, CHAT_ID_HEADER, "chat id")
    message_id = _required(headers, MESSAGE_ID_HEADER, "message id")

    return OwuiIds(
        chat_id=chat_id,
        message_id=message_id,
        user_message_id=_optional(headers, USER_MESSAGE_ID_HEADER, "user message id"),
        parent_id=_optional(headers, PARENT_ID_HEADER, "parent message id"),
        task=_task(headers),
    )


def _required(headers: dict[str, str], name: str, label: str) -> str:
    value = headers.get(name, "").strip()
    if not value:
        # An empty value is what an absent token looks like, so this is the
        # misconfigured-connection case as well as the missing-chat case.
        raise DoorError(
            HTTP_BAD_REQUEST,
            f"this request carries no {label}, so it has no session to run in. {_SET_HEADERS_HINT}",
            code="missing_chat_id" if name == CHAT_ID_HEADER else "missing_message_id",
        )

    return _checked(value, name, label)


def _optional(headers: dict[str, str], name: str, label: str) -> str | None:
    value = headers.get(name, "").strip()
    if not value:
        return None

    return _checked(value, name, label)


def _checked(value: str, name: str, label: str) -> str:
    if len(value) > MAX_ID_LENGTH:
        raise DoorError(
            HTTP_BAD_REQUEST,
            f"the {label} is longer than {MAX_ID_LENGTH} characters.",
            code="bad_id",
        )
    if _ID_PATTERN.match(value) is None:
        raise DoorError(
            HTTP_BAD_REQUEST,
            f"the {label} in {name} is not a valid id.",
            code="bad_id",
        )

    return value


def _task(headers: dict[str, str]) -> str | None:
    value = headers.get(TASK_HEADER, "").strip()
    if not value:
        return None

    # The task name is Open WebUI's own word and never reaches a session, so
    # it is bounded and otherwise left alone.
    return value[:MAX_ID_LENGTH]
