"""Fixture state for the noticeboard's tests.

Everything the noticeboard reads is a file another service wrote, plus one HTTP
call to `attendance`. So a test needs a state root on disk and a fake session
service, and nothing else: no host, no `caregiver`, no sandbox.
"""

from __future__ import annotations

import json
import subprocess
import tracemalloc
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

#: The comments are load-bearing. The real family files explain each grant
#: where it sits, and a form save has to bring them back.
CHAT_COMMENT = "# The vault is read-only here, and it syncs to the operator's phone."

CHAT_FAMILY_YAML = f"""\
name: chat
kind: attended
description: the house assistant
model: {{ router: agent-router, budget_usd_per_day: 15 }}
files:
  {CHAT_COMMENT}
  - {{ path: /srv/agents/vault, mode: ro }}
egress: []
shell: false
skills: []
"""

SCRUM_FAMILY_YAML = """\
name: scrum-lead
kind: autonomous
description: the morning triage agent
model: { router: agent-router, budget_usd_per_day: 5 }
triggers:
  - { cron: "0 6 * * *" }
max_running_turns: 1
"""


def stamp(offset_s: float = 0.0) -> str:
    """An RFC 3339 time `offset_s` seconds before `NOW`."""
    return (NOW - timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def status_doc(
    family: str = "chat",
    kind: str = "attended",
    state: str = "in_sync",
    *,
    age_s: float = 5.0,
    spend_age_s: float = 5.0,
    **extra: Any,
) -> dict[str, Any]:
    """A contract 05 §9 document, with the fields a test wants overridden."""
    doc: dict[str, Any] = {
        "family": family,
        "kind": kind,
        "state": state,
        "written_at": stamp(age_s),
        "registry_rev": "reg-9f21c4",
        "applied_rev": "reg-9f21c4",
        "config_rev": "reg-9f21c4",
        "validation": {
            "rev": "reg-9f21c4",
            "checked_at": stamp(age_s),
            "ok": True,
            "never_valid": False,
            "error_count": 0,
            "warning_count": 0,
            "report_path": f"/srv/agents/state/rework/families/{family}/validation.json",
            "first_error": None,
        },
        "faults": [],
        "reconcile": None,
        "sandboxes": [sandbox_doc(f"{family}-s3")],
        "credentials": {
            "epoch": 7,
            "key_id": f"sk-live-{family}-7",
            "token_id": f"chaperone-{family}-7",
            "rotated_at": stamp(3600),
            "next_rotation_at": None,
            "rotation_state": "settled",
        },
        "spend": {
            "window": "day",
            "spend_usd": 3.42,
            "budget_usd": 15.0,
            "as_of": stamp(spend_age_s),
            "source": "litellm",
        },
        "limits": {"max_running_turns": None, "max_queued_turns": 100, "job_timeout_s": None},
    }
    doc.update(extra)
    return doc


def sandbox_doc(sandbox_id: str, state: str = "ready", **extra: Any) -> dict[str, Any]:
    family = sandbox_id.rsplit("-s", 1)[0]
    row: dict[str, Any] = {
        "id": sandbox_id,
        "state": state,
        "power": "running",
        "image": "sha256:4c1f00ddeeff0011223344556677",
        "spec_hash": "9f21c401",
        "cpus": 4,
        "memory": "8g",
        "created_at": stamp(600),
        "ready_at": stamp(590),
        "channel": "open",
        "supervisor_env": (
            f"/srv/agents/state/rework/families/{family}/supervisor-{sandbox_id}.env"
        ),
    }
    row.update(extra)
    return row


def fault_doc(
    code: str = "spend_unknown", *, blocks_turns: bool = False, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": code,
        "blocks_turns": blocks_turns,
        "since": stamp(120),
        "source": "managerd",
        "message": "LiteLLM did not answer a spend read",
    }
    row.update(extra)
    return row


def outcome_doc(outcome_id: str, family: str = "scrum-lead", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": outcome_id,
        "family": family,
        "session": "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK",
        "trigger": {"kind": "timer", "name": "morning-triage", "fired_at": stamp(900)},
        "started_at": stamp(890),
        "ended_at": stamp(600),
        "status": "ok",
        "error": None,
        "turns": 3,
        "approvals": {"requested": 1, "approved": 1, "denied": 0, "timed_out": 0},
        "spend_usd": 0.21,
        "sandbox": f"{family}-s2",
    }
    row.update(extra)
    return row


def audit_line(
    family: str = "chat",
    tool: str = "kagi__kagi_search_fetch",
    decision: str = "allow",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": stamp(30),
        "family": family,
        "sandbox_id": f"{family}-s3",
        "sandbox_id_trusted": True,
        "grants_rev": "01K5J9QW3R7T0ZP4YB2H6N8M1D",
        "tool": tool,
        "args": {"query": "boiler service date"},
        "decision": decision,
        "reason": "granted",
        "latency_ms": 38,
        "waited_ms": 0,
        "gate": None,
        "claimed": {
            "session_id": "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33",
            "turn_id": "01K5J9QWB9R1V3T5Y7H9J2K4P6",
            "delegation_id": None,
        },
        "chain": [family],
    }
    row.update(extra)
    return row


def session_doc(
    session: str = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33",
    family: str = "chat",
    state: str = "idle",
    **extra: Any,
) -> dict[str, Any]:
    """A contract 02 §4.2 session object."""
    row: dict[str, Any] = {
        "family": family,
        "session": session,
        "kind": "attended",
        "title": "Boiler service date",
        "state": state,
        "created_at": stamp(1800),
        "updated_at": stamp(60),
        "journal_seq": 42,
        "writer": None,
        "turns_total": 3,
        "turns_running": 0,
        "sandbox": f"{family}-s3",
        "persona_hash": "6f1c0a55",
        "labels": {"door": "owui"},
    }
    row.update(extra)
    return row


def turn_doc(
    turn: str = "01K5J9QWB9R1V3T5Y7H9J2K4P6", state: str = "settled", **extra: Any
) -> dict[str, Any]:
    """A contract 02 §4.4 turn object."""
    row: dict[str, Any] = {
        "turn": turn,
        "state": state,
        "reason": None,
        "started_at": stamp(120),
        "ended_at": stamp(118),
        "deadline_s": 600,
        "idempotency_key": None,
        "sandbox": "chat-s3",
        "usage": {
            "input": 4120,
            "output": 188,
            "cache_read": 0,
            "cache_write": 0,
            "cost_usd": 0.014,
        },
        "approvals": 0,
        "owui": None,
    }
    row.update(extra)
    return row


def journal_line(
    seq: int, kind: str, body: dict[str, Any], turn: str | None = None
) -> dict[str, Any]:
    """A contract 02 §8 event line."""
    line: dict[str, Any] = {
        "journal_seq": seq,
        "ts": stamp(100 - seq),
        "kind": kind,
        "turn": turn,
        "body": body,
    }
    return line


def ndjson(rows: list[dict[str, Any]]) -> bytes:
    """Contract 02 §8: LF is the only delimiter."""
    return b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)


#: Past the nesting limit of the JSON reader of every supported Python.
VERY_DEEP = 400_000


def deep_object(depth: int = VERY_DEEP) -> bytes:
    """A JSON object whose one field nests `depth` lists."""
    return b'{"x":' + b"[" * depth + b"]" * depth + b"}"


def peak_memory_of[T](call: Callable[[], T]) -> tuple[T, int]:
    """What `call` returns, and the most bytes it held at one time."""
    tracemalloc.start()
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return result, peak


class FakeAttendance:
    """A `sessions.Transport` that answers from a table, and records the
    bearer it was given so a test can prove the token never reached a URL."""

    def __init__(self, replies: dict[str, tuple[int, bytes]] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.bearers: list[str] = []
        self.fault = ""

    def answer(self, path: str, body: object, status: int = 200) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.replies[path] = (status, raw)

    def get(self, path: str, params: Mapping[str, str], bearer: str):
        from noticeboard.sessions import Reply

        self.calls.append((path, dict(params)))
        self.bearers.append(bearer)

        if self.fault:
            return Reply(problem=self.fault)

        status, body = self.replies.get(path, (404, b'{"error":{"code":"not_found"}}'))
        return Reply(status=status, body=body)


def write_token(root: Path, value: str = "v" * 40) -> Path:
    """The `view-ro` bearer file (contract 02 §3 rule 5)."""
    path = root / "tokens" / "view-ro.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return path


def write_json(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def make_state_root(base: Path) -> Path:
    """A state root holding two families, an outcome and one audit day."""
    root = base / "state"
    families = root / "families"

    write_json(families / "chat" / "status.json", status_doc())
    write_json(
        families / "chat" / "validation.json",
        {
            "family": "chat",
            "file": "families/chat/family.yaml",
            "status": "reconciling",
            "applied": False,
            "issues": [],
            "errors": 0,
            "warnings": 0,
        },
    )
    write_json(
        families / "scrum-lead" / "status.json",
        # A different spend from chat's, so a test can count one family's
        # number on a page that lists both.
        status_doc(
            family="scrum-lead",
            kind="autonomous",
            spend={
                "window": "day",
                "spend_usd": 1.11,
                "budget_usd": 5.0,
                "as_of": stamp(5),
                "source": "litellm",
            },
        ),
    )
    write_json(
        root / "outcomes" / "scrum-lead" / "01JBQ80M4F7S2YQ1VZK6W3TDEN.json",
        outcome_doc("01JBQ80M4F7S2YQ1VZK6W3TDEN"),
    )
    write_audit_day(root / "audit", NOW.strftime("%Y-%m-%d"), [audit_line()])

    return root


def write_audit_day(audit_dir: Path, day: str, rows: list[dict[str, Any]]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{day}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def make_registry(base: Path) -> Path:
    """A git registry with two families, as `caregiver` reads it."""
    root = base / "registry"
    (root / "families" / "chat").mkdir(parents=True)
    (root / "families" / "chat" / "family.yaml").write_text(CHAT_FAMILY_YAML, encoding="utf-8")
    (root / "families" / "chat" / "instructions.md").write_text("Be useful.\n", encoding="utf-8")
    (root / "families" / "scrum-lead").mkdir(parents=True)
    (root / "families" / "scrum-lead" / "family.yaml").write_text(
        SCRUM_FAMILY_YAML, encoding="utf-8"
    )
    (root / "mcp").mkdir()
    (root / "skills").mkdir()

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "noticeboard@example.test")
    git(root, "config", "user.name", "noticeboard test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")

    return root


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the fixture registry. Never in the repo under test."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def commit_count(root: Path) -> int:
    return len(git(root, "log", "--format=%H").stdout.split())


#: What a browser posts for a checked box that names no value.
CHECKED_VALUE = "on"


class _FormReader(HTMLParser):
    """Collects what a browser posts for the controls of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        #: The name of the open text area. Empty for one that posts nothing.
        self._area: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        held = dict(attrs)
        name = held.get("name") or ""
        posts = bool(name) and "disabled" not in held

        if tag == "textarea":
            self._area = name if posts else ""
            self._text = []
            return

        if tag != "input" or not posts:
            return

        if held.get("type") != "checkbox":
            self.values[name] = held.get("value") or ""
        elif "checked" in held:
            self.values[name] = held.get("value") or CHECKED_VALUE

    def handle_data(self, data: str) -> None:
        if self._area is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "textarea" or self._area is None:
            return

        if self._area:
            # A browser drops one line end directly after the start tag.
            self.values[self._area] = "".join(self._text).removeprefix("\n")

        self._area = None


def browser_values(page: str) -> dict[str, str]:
    """What a browser posts for the form of a page, before a button adds its
    own value.

    A control with no name posts nothing. A disabled control posts nothing.
    A box that is not checked posts nothing.
    """
    reader = _FormReader()
    reader.feed(page)
    reader.close()

    return reader.values
