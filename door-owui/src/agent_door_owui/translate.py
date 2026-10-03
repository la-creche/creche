"""`sessiond` event lines to OpenAI SSE frames.

The input is contract 02 §8 journal lines. `tests/test_translate.py`'s
golden vectors fix the output.

Two rules are load-bearing and both are security rules:

1. A tool result never enters `content`. It travels as a JSON string in a
   Responses-API item, so a hostile result cannot forge a closing tag and
   escape its own block (invariant 14: tool results are untrusted data).
2. A failure never echoes provider detail. The turn's `reason` is a code
   this system owns (contract 02 §14). `message` may carry upstream text,
   so it stays out of the chat.

A turn that ends any way other than settled ends the stream with a visible
error. Failing silently would leave a chat that looks like it answered.
"""

from __future__ import annotations

from .errors import turn_failure
from .journal import JournalLine, LineKind
from .sse import DONE_FRAME, KEEPALIVE_FRAME, SseWriter, ToolOutcome, tool_result_text
from .untrusted import field_text, is_object

_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_UNKNOWN_REASON = "interrupted"
_NO_TERMINAL_REASON = "stream_ended_before_the_turn_settled"

# pi event types this door renders. Anything else is recorded by `sessiond`
# and shown by the noticeboard; the chat surface has no place for it.
_MESSAGE_UPDATE = "message_update"
_MESSAGE_END = "message_end"
_TOOL_START = "tool_execution_start"
_TOOL_END = "tool_execution_end"
_PARENT_CALL = "parentToolCallId"
_RPC_RESPONSE = "response"
_AGENT_SETTLED = "agent_settled"
_TEXT_DELTA = "text_delta"
_THINKING_DELTA = "thinking_delta"
_FAILED_STOP_REASONS = frozenset({"error", "aborted"})


class TurnTranslator:
    """Turns one turn's event lines into the frames of one SSE response."""

    def __init__(self, writer: SseWriter) -> None:
        self._writer = writer
        self._settled = False
        self._failed = False
        self._turn: str | None = None
        self._text: list[str] = []

    @property
    def settled(self) -> bool:
        return self._settled

    @property
    def answer(self) -> str:
        """The assistant text so far. Reasoning and tool chips are not it."""
        return "".join(self._text)

    def start(self) -> list[str]:
        return [self._writer.role()]

    def feed(self, line: JournalLine) -> list[str]:
        """Frames for one event line. Empty when the line has nothing to show."""
        if self._settled:
            return []

        # Latch this turn's id from the first line that carries one, then
        # ignore any line addressed to a different turn. `sessiond` streams
        # one turn here, and a line from another one is a bug, not content.
        if line.turn is not None and self._turn is None:
            self._turn = line.turn
        if line.turn is not None and line.turn != self._turn:
            return []

        if line.kind == LineKind.HEARTBEAT:
            return [KEEPALIVE_FRAME]

        if line.kind == LineKind.PI_EVENT:
            return self._pi_event(line.body)

        if line.kind == LineKind.TURN_SETTLED:
            return self._end_by_state()

        if line.kind in (LineKind.TURN_FAILED, LineKind.TURN_ABORTED):
            return self.fail(line.text("reason") or _UNKNOWN_REASON)

        return []

    def finish(self) -> list[str]:
        """Close a stream that ended with no terminal line."""
        if self._settled:
            return []

        return self.fail(_NO_TERMINAL_REASON)

    def fail(self, reason: str) -> list[str]:
        """End the stream with an error the reader can see and act on."""
        if self._settled:
            return []

        frames = [
            self._writer.text(f"\n\n[error: {reason}]"),
            self._writer.stop(),
            self._writer.error(turn_failure(reason).body()),
        ]
        frames.extend(self._close(_STATUS_FAILED))

        return frames

    def _end_by_state(self) -> list[str]:
        if self._failed:
            return self.fail(_UNKNOWN_REASON)

        return [self._writer.stop(), *self._close(_STATUS_DONE)]

    def _close(self, status: str) -> list[str]:
        self._settled = True

        return [self._writer.settled(status), DONE_FRAME]

    def _pi_event(self, event: dict[str, object]) -> list[str]:
        event_type = field_text(event, "type")
        if not event_type:
            return []

        # A pi rpc command response should never reach the journal: contract
        # 03 §5.1 forwards events, and the playpen answers a refused
        # command with `turn_failed`. Handled anyway, because a stalled chat
        # is a worse outcome than a visible error.
        if event_type == _RPC_RESPONSE and event.get("success") is False:
            return self.fail(_UNKNOWN_REASON)

        if event_type == _MESSAGE_END:
            self._note_stop_reason(event)
            return []

        if event_type == _AGENT_SETTLED:
            return self._end_by_state()

        if event_type == _MESSAGE_UPDATE:
            return self._message_update(event)

        # A call a codemode script made (pi 0.99.1). It folds into the
        # script's own chip: a loop over 50 items would otherwise draw 51
        # chips. The PEP audit holds every nested call.
        if event_type in (_TOOL_START, _TOOL_END) and field_text(event, _PARENT_CALL):
            return []

        if event_type == _TOOL_START:
            return [
                self._writer.tool_start(
                    field_text(event, "toolCallId"),
                    field_text(event, "toolName") or "tool",
                    event.get("args"),
                )
            ]

        if event_type == _TOOL_END:
            # Closing the chip is the arrival of the result item, not a status
            # flip: the surface matches the pair by call id. Full arguments and
            # the decision live in the PEP audit.
            outcome = ToolOutcome.FAILED if event.get("isError") is True else ToolOutcome.OK
            return [
                self._writer.tool_end(
                    field_text(event, "toolCallId"),
                    tool_result_text(event.get("result"), outcome),
                    outcome,
                )
            ]

        return []

    def _message_update(self, event: dict[str, object]) -> list[str]:
        inner = event.get("assistantMessageEvent")
        if not is_object(inner):
            return []

        inner_type = field_text(inner, "type")
        delta = inner.get("delta")
        if not isinstance(delta, str):
            return []

        if inner_type == _TEXT_DELTA:
            self._text.append(delta)
            return [self._writer.text(delta)]

        # Reasoning is presentation, never the answer: it stays out of the
        # text, so the non-streaming body carries the reply alone.
        if inner_type == _THINKING_DELTA:
            return [self._writer.reasoning(delta)]

        return []

    def _note_stop_reason(self, event: dict[str, object]) -> None:
        message = event.get("message")
        if not is_object(message):
            return

        if field_text(message, "stopReason") in _FAILED_STOP_REASONS:
            self._failed = True
