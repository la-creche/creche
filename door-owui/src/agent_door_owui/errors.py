"""OpenAI-shaped refusals, and the map from `attendance`'s error model.

Open WebUI shows `error.message` from an OpenAI-shaped body and shows
nothing else, so every refusal here names the thing to change.
Contract 02 §14 fixes `attendance`'s codes. This module turns each one into
a status, a type and a sentence a reader can act on.
"""

from __future__ import annotations

from enum import StrEnum

# HTTP statuses this door answers with. Named because they come from a spec.
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_TOO_MANY_REQUESTS = 429
HTTP_INTERNAL = 500
HTTP_BAD_GATEWAY = 502
HTTP_UNAVAILABLE = 503


# Not a contract 02 §14 code. `attendance` never sends it: the door's own
# client reports a call that got no answer with it (attendance.py).
#
# CONTRACT-QUESTION: contract 02 §14 gives the codes of `attendance` and no
# status or code for a failure of a door itself. This door answers 502 with
# this code when `attendance` does not answer. It answers 500 `internal`
# for a failure that no handler names (app.py). A code that a contract
# fixes later costs a change to the map below and to the text that Open
# WebUI shows.
CODE_UNREACHABLE = "attendance_unreachable"


class ErrorType(StrEnum):
    """The `error.type` values an OpenAI client expects."""

    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    RATE_LIMIT = "rate_limit_error"
    SERVER = "server_error"


class DoorError(Exception):
    """A refusal the door answers with, in the OpenAI error shape."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str,
        error_type: ErrorType = ErrorType.INVALID_REQUEST,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.error_type = error_type

    def body(self) -> dict[str, dict[str, str]]:
        return error_body(self.message, code=self.code, error_type=self.error_type)


def error_body(
    message: str,
    *,
    code: str,
    error_type: ErrorType = ErrorType.INVALID_REQUEST,
) -> dict[str, dict[str, str]]:
    """The one error shape this door emits, in a body or in an SSE frame."""
    return {"error": {"message": message, "type": str(error_type), "code": code}}


# Contract 02 §14's codes, mapped to what an Open WebUI user should see.
# `unauthorized` and `forbidden` are deliberately not passed through: they
# mean the DOOR's own token was refused, and answering 401 would tell the
# user to fix a connection key that is not the broken one.
_ATTENDANCE_CODES: dict[str, tuple[int, ErrorType]] = {
    "bad_request": (HTTP_BAD_REQUEST, ErrorType.INVALID_REQUEST),
    "idempotency_mismatch": (HTTP_BAD_REQUEST, ErrorType.INVALID_REQUEST),
    "unauthorized": (HTTP_BAD_GATEWAY, ErrorType.SERVER),
    "forbidden": (HTTP_BAD_GATEWAY, ErrorType.SERVER),
    "not_found": (HTTP_NOT_FOUND, ErrorType.INVALID_REQUEST),
    "turn_not_found": (HTTP_NOT_FOUND, ErrorType.INVALID_REQUEST),
    "family_unknown": (HTTP_NOT_FOUND, ErrorType.INVALID_REQUEST),
    "session_busy": (HTTP_CONFLICT, ErrorType.INVALID_REQUEST),
    "family_invalid": (HTTP_CONFLICT, ErrorType.INVALID_REQUEST),
    "payload_too_large": (HTTP_PAYLOAD_TOO_LARGE, ErrorType.INVALID_REQUEST),
    "queue_full": (HTTP_TOO_MANY_REQUESTS, ErrorType.RATE_LIMIT),
    "family_degraded": (HTTP_UNAVAILABLE, ErrorType.SERVER),
    "sandbox_unavailable": (HTTP_UNAVAILABLE, ErrorType.SERVER),
    "internal": (HTTP_BAD_GATEWAY, ErrorType.SERVER),
    CODE_UNREACHABLE: (HTTP_BAD_GATEWAY, ErrorType.SERVER),
}

# Sentences that beat the raw code in a chat window. A code with no entry
# keeps `attendance`'s own message.
_ATTENDANCE_TEXT: dict[str, str] = {
    "session_busy": (
        "this session is open in another door and only one may write at a time. "
        "Close it there, or stop the running turn, then send again."
    ),
    "unauthorized": "the door's own attendance token was refused. Check the token file.",
    "forbidden": "the door's own attendance token may not reach this family.",
    "family_invalid": "this family has never had a valid definition, so nothing can serve it.",
    "family_degraded": "this family is degraded and a fault is blocking turns.",
    "sandbox_unavailable": "this family has no sandbox to run the turn on. Try again shortly.",
    CODE_UNREACHABLE: (
        "the door cannot reach attendance. Check that attendance runs, then send again."
    ),
}


def from_attendance(code: str, message: str) -> DoorError:
    """Turn one contract 02 §14 error into the refusal Open WebUI shows."""
    status, error_type = _ATTENDANCE_CODES.get(code, (HTTP_BAD_GATEWAY, ErrorType.SERVER))
    text = _ATTENDANCE_TEXT.get(code, message or code)

    return DoorError(status, text, code=code, error_type=error_type)


def turn_failure(reason: str) -> DoorError:
    """The visible error for a turn that ended any way other than settled.

    A turn's `reason` (contract 02 §14's second table) is a different
    namespace from the request-level codes `from_attendance` maps, so it is
    never passed through that function. Both the streaming translator and
    the non-streaming path show this same sentence, so a reader sees the
    same words regardless of which `wait` mode answered.
    """
    return DoorError(
        HTTP_BAD_GATEWAY,
        f"the turn did not finish: {reason}",
        code=reason,
        error_type=ErrorType.SERVER,
    )
