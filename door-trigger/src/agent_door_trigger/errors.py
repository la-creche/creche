"""Contract 02 §14's error model, as this door's two callers need it.

`attendance` answers every refusal with `{"error": {"code", "message", ...}}`
(contract 02 §14). This module carries that shape into the process and maps
each code onto what this door's two callers need: the CLI wants an exit code
and one clear stderr line (systemd's journal is where the operator reads it),
and the webhook listener wants an HTTP status for whatever external
system posted the trigger.
"""

from __future__ import annotations

from enum import IntEnum

#: The status this module falls back to for an attendance code the table below
#: does not name. Contract 02 §14's code set is closed, so an unmapped code
#: means attendance shipped one this door has not caught up to yet — worth
#: surfacing as a server error, never guessed at as a 4xx.
_FALLBACK_STATUS = 502


#: Not a contract 02 §14 code. `attendance` never sends it: the webhook
#: listener answers with it when a call to `attendance` gets no answer.
#:
#: CONTRACT-QUESTION: contract 02 §14 gives the codes of `attendance` and no
#: status or code for a failure of a door itself. The listener answers 502
#: with this code when `attendance` does not answer. It answers 500
#: `internal` for a failure that no handler names (webhooks.py). A code that
#: a contract fixes later costs a change to `WEBHOOK_STATUS` and to each
#: caller that reads the code.
CODE_UNREACHABLE = "attendance_unreachable"


class ExitCode(IntEnum):
    """`agent-trigger fire`'s exit status: 0 when the job was accepted or
    queued, non-zero otherwise."""

    ACCEPTED = 0
    REFUSED = 1
    USAGE = 2


class AttendanceError(Exception):
    """One refusal from `attendance`, in the shape of contract 02 §14."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


#: Contract 02 §14's request-level codes, mapped to the status this door's
#: webhook listener answers an EXTERNAL caller with. This is NOT attendance's
#: own status: attendance's 400/403/409/etc. describe the call between
#: attendance and this door, and a caller outside the platform gets this
#: door's own reading of what each one means for it.
WEBHOOK_STATUS: dict[str, int] = {
    "bad_request": 400,
    "idempotency_mismatch": 500,
    "unauthorized": 502,
    "forbidden": 403,
    "not_found": 502,
    "turn_not_found": 502,
    "family_unknown": 404,
    "session_busy": 409,
    "family_invalid": 409,
    "payload_too_large": 413,
    "queue_full": 429,
    "not_implemented": 501,
    "family_degraded": 503,
    "sandbox_unavailable": 503,
    "internal": 502,
    CODE_UNREACHABLE: 502,
}


def webhook_status(code: str) -> int:
    """The HTTP status this door's webhook listener answers with."""
    return WEBHOOK_STATUS.get(code, _FALLBACK_STATUS)
