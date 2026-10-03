"""The error model (contract 02 §14).

Every failure that leaves this service answers with one body shape. The HTTP
status and the retry hint are properties of the code, not of the call site, so
they live in one table here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_TOO_MANY_REQUESTS = 429
HTTP_INTERNAL = 500
HTTP_NOT_IMPLEMENTED = 501
HTTP_UNAVAILABLE = 503

_RETRY_BUSY_S = 5
_RETRY_QUEUE_S = 5
_RETRY_DEGRADED_S = 10
_RETRY_SANDBOX_S = 10
_RETRY_INTERNAL_S = 5


class ErrorCode(StrEnum):
    """Contract 02 §14's table."""

    BAD_REQUEST = "bad_request"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    TURN_NOT_FOUND = "turn_not_found"
    FAMILY_UNKNOWN = "family_unknown"
    # Contract 02 §13.4.1 rule 3: the target family carries no `enqueue: true`
    # trigger, so nothing may dispatch to it. Deny by default (invariant 11).
    DISPATCH_NOT_DECLARED = "dispatch_not_declared"
    SESSION_BUSY = "session_busy"
    LEASE_TAKEN_OVER = "lease_taken_over"
    FAMILY_INVALID = "family_invalid"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    QUEUE_FULL = "queue_full"
    FAMILY_DEGRADED = "family_degraded"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    INTERNAL = "internal"
    # Contract 02 §14: 501 and no retry hint, for an operation the contract
    # has and this build does not.
    NOT_IMPLEMENTED = "not_implemented"


class TurnReason(StrEnum):
    """Why a turn reached `failed` or `aborted` (contract 02 §14, table 2)."""

    TURN_TIMEOUT = "turn_timeout"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    CHANNEL_LOST = "channel_lost"
    PROTOCOL_VIOLATION = "protocol_violation"
    PERMISSION_REMOVED = "permission_removed"
    SANDBOX_LOST = "sandbox_lost"
    MODEL_ERROR = "model_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    USER_STOPPED = "user_stopped"
    # Contract 02 §13.3: the queue lives in memory, so a restart ends every
    # turn waiting in it. It is not `internal`: a plain restart is not a bug
    # in this service.
    QUEUE_LOST = "queue_lost"
    # Contract 02 §14, contract 05 §3.3. The PEP was not answering,
    # so an autonomous firing ended before it ran. The word is deliberately
    # the one contract 05's fault code and contract 04's `/call` failure
    # already use: one word, one meaning, across three contracts.
    PEP_UNREACHABLE = "pep_unreachable"
    INTERNAL = "internal"


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: HTTP_BAD_REQUEST,
    ErrorCode.IDEMPOTENCY_MISMATCH: HTTP_BAD_REQUEST,
    ErrorCode.UNAUTHORIZED: HTTP_UNAUTHORIZED,
    ErrorCode.FORBIDDEN: HTTP_FORBIDDEN,
    ErrorCode.NOT_FOUND: HTTP_NOT_FOUND,
    ErrorCode.TURN_NOT_FOUND: HTTP_NOT_FOUND,
    ErrorCode.FAMILY_UNKNOWN: HTTP_NOT_FOUND,
    ErrorCode.DISPATCH_NOT_DECLARED: HTTP_FORBIDDEN,
    ErrorCode.SESSION_BUSY: HTTP_CONFLICT,
    ErrorCode.LEASE_TAKEN_OVER: HTTP_CONFLICT,
    ErrorCode.FAMILY_INVALID: HTTP_CONFLICT,
    ErrorCode.PAYLOAD_TOO_LARGE: HTTP_PAYLOAD_TOO_LARGE,
    ErrorCode.QUEUE_FULL: HTTP_TOO_MANY_REQUESTS,
    ErrorCode.FAMILY_DEGRADED: HTTP_UNAVAILABLE,
    ErrorCode.SANDBOX_UNAVAILABLE: HTTP_UNAVAILABLE,
    ErrorCode.INTERNAL: HTTP_INTERNAL,
    ErrorCode.NOT_IMPLEMENTED: HTTP_NOT_IMPLEMENTED,
}

_RETRY_AFTER_S: dict[ErrorCode, int] = {
    ErrorCode.SESSION_BUSY: _RETRY_BUSY_S,
    ErrorCode.QUEUE_FULL: _RETRY_QUEUE_S,
    ErrorCode.FAMILY_DEGRADED: _RETRY_DEGRADED_S,
    ErrorCode.SANDBOX_UNAVAILABLE: _RETRY_SANDBOX_S,
    ErrorCode.INTERNAL: _RETRY_INTERNAL_S,
}


def http_status(code: ErrorCode) -> int:
    """The HTTP status contract 02 §14 pins to this code."""
    return _STATUS[code]


def retry_after_s(code: ErrorCode) -> int | None:
    """Seconds to wait, or None when the contract marks the code no-retry."""
    return _RETRY_AFTER_S.get(code)


class ApiError(Exception):
    """A refusal that a caller sees. Carries the whole §14 body."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        family: str | None = None,
        session: str | None = None,
        turn: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.family = family
        self.session = session
        self.turn = turn
        self.detail = detail

    @property
    def status(self) -> int:
        return http_status(self.code)

    def body(self) -> dict[str, Any]:
        """The response body of contract 02 §14."""
        error: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "family": self.family,
            "session": self.session,
            "turn": self.turn,
            "detail": self.detail if self.detail is not None else {},
        }
        payload: dict[str, Any] = {"error": error}
        wait = retry_after_s(self.code)

        if wait is not None:
            payload["retry_after_s"] = wait

        return payload
