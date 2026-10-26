"""The two effect stand-ins of the Rust chaperone, each run by itself.

No test here starts the chaperone. Each test runs a stand-in through its
wrapper and speaks MCP to it over stdio, one JSON-RPC message per line, as
the effect host of the chaperone does (chaperone `docs/design.md` §7.7).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest
import standin_upstream
from proc_caregiver import wait_until
from proc_harness import Supervisor
from proc_stack import base_env
from proc_standins import (
    APPROVAL,
    APPROVED,
    DENIED,
    UPSTREAM,
    answer_gate,
    approval_requests,
    effect_program,
    install_effects,
    state_dir,
    upstream_calls,
)
from proc_tree import Tree, build_bare_tree

#: An obvious fixture, never a credential.
SECRET = "FIXTURE-STANDIN-KEY"
SECRET_LINE = json.dumps({"secrets": {standin_upstream.SECRET_PARAM: SECRET}})
PROTOCOL = "2025-06-18"
META = {"chaperone.app": "fam-a"}
GATE_A = "0123456789abcdef0123456789abcdef"
GATE_B = "fedcba9876543210fedcba9876543210"
GATED_TOOL = "standin__gated_echo"
EXIT_BAD_INPUT = 2
METHOD_NOT_FOUND = -32601
HELD_S = 0.3
DEADLINE_S = 10.0


class Pipe:
    """One stand-in on two pipes. Each answer is kept by its id, in any order."""

    def __init__(self, popen: subprocess.Popen[str]) -> None:
        self.popen = popen
        self._answers: dict[int, dict[str, Any]] = {}
        self._next = 0
        self._lock = threading.Condition()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def send(self, method: str, params: dict[str, Any]) -> int:
        self._next += 1
        self.write(
            json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})
        )

        return self._next

    def write(self, line: str) -> None:
        stdin = _stream(self.popen.stdin)
        stdin.write(line + "\n")
        stdin.flush()

    def answer(self, ident: int, timeout_s: float = DEADLINE_S) -> dict[str, Any]:
        with self._lock:
            if not self._lock.wait_for(lambda: ident in self._answers, timeout_s):
                raise AssertionError(f"no answer to request {ident} in {timeout_s} s")

            return self._answers[ident]

    def has_answer(self, ident: int) -> bool:
        with self._lock:
            return ident in self._answers

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.answer(self.send(method, params))

    def _read(self) -> None:
        for line in _stream(self.popen.stdout):
            message = json.loads(line)

            with self._lock:
                self._answers[message["id"]] = message
                self._lock.notify_all()


@pytest.fixture
def effects(tree: Tree, supervisor: Supervisor) -> Tree:
    """A root with the two wrappers. The `supervisor` fixture checks that each one ended."""
    build_bare_tree(tree)
    install_effects(tree)

    return tree


@pytest.fixture
def start(effects: Tree) -> Iterator[Any]:
    """Start a stand-in through its wrapper. Each one ends with the test."""
    started: list[subprocess.Popen[str]] = []

    def run(name: str) -> Pipe:
        popen = subprocess.Popen(
            [str(effect_program(effects, name))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env(effects),
            cwd=effects.root,
            text=True,
        )
        started.append(popen)

        return Pipe(popen)

    yield run

    for popen in started:
        _stream(popen.stdin).close()
        popen.wait(DEADLINE_S)


def _stream(stream: IO[str] | None) -> IO[str]:
    assert stream is not None

    return stream


def _ready(pipe: Pipe) -> dict[str, Any]:
    answer = pipe.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}})
    pipe.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    return answer


def _call(pipe: Pipe, name: str, arguments: dict[str, Any]) -> int:
    return pipe.send("tools/call", {"name": name, "arguments": arguments, "_meta": META})


def _text(answer: dict[str, Any]) -> str:
    (block,) = answer["result"]["content"]
    assert block["type"] == "text"

    return block["text"]


def _approve(gate: str, tool: str = GATED_TOOL) -> dict[str, Any]:
    return {"gate": gate, "app": "fam-a", "tool": tool, "arguments": {"text": "hi"}}


# ------------------------------------------------------------------- upstream


def test_upstream_refuses_to_serve_with_no_secret_line(start: Any) -> None:
    """The manifest declares a secret, so the first line must hold it (§7.5)."""
    pipe = start(UPSTREAM)
    pipe.send("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}})

    assert pipe.popen.wait(DEADLINE_S) == EXIT_BAD_INPUT
    assert standin_upstream.SECRET_PARAM in _stream(pipe.popen.stderr).read()


def test_upstream_lists_two_tools_and_echoes_the_arguments(start: Any, effects: Tree) -> None:
    pipe = start(UPSTREAM)
    pipe.write(SECRET_LINE)

    initialized = _ready(pipe)
    listed = pipe.call("tools/list", {})
    answer = pipe.answer(_call(pipe, standin_upstream.ECHO, {"b": 1, "a": "x"}))

    assert initialized["result"]["protocolVersion"] == PROTOCOL
    assert [entry["name"] for entry in listed["result"]["tools"]] == ["echo", "gated_echo"]
    assert _text(answer) == '{"a":"x","b":1}'
    assert answer["result"]["isError"] is False
    assert upstream_calls(effects) == [
        {"tool": "echo", "arguments": {"b": 1, "a": "x"}, "meta": META}
    ]


def test_upstream_keeps_the_secret_out_of_each_file(start: Any, effects: Tree) -> None:
    pipe = start(UPSTREAM)
    pipe.write(SECRET_LINE)
    _ready(pipe)
    pipe.answer(_call(pipe, standin_upstream.GATED_ECHO, {"text": "hi"}))

    for path in state_dir(effects, UPSTREAM).rglob("*"):
        assert SECRET not in path.read_text(encoding="utf-8"), path


def test_a_stand_in_refuses_a_method_it_does_not_know(start: Any) -> None:
    pipe = start(UPSTREAM)
    pipe.write(SECRET_LINE)
    _ready(pipe)

    assert pipe.call("resources/list", {})["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "line", ['{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', '{"n":NaN}', "[1]", "not json"]
)
def test_a_stand_in_stops_at_a_line_that_is_no_strict_json(start: Any, line: str) -> None:
    pipe = start(UPSTREAM)
    pipe.write(SECRET_LINE)
    pipe.write(line)

    assert pipe.popen.wait(DEADLINE_S) == EXIT_BAD_INPUT


# ------------------------------------------------------------------- approval


def test_approval_holds_a_gate_until_a_test_answers_it(start: Any, effects: Tree) -> None:
    pipe = start(APPROVAL)
    _ready(pipe)
    ident = _call(pipe, "approve", _approve(GATE_A))

    wait_until(lambda: len(approval_requests(effects)) == 1, "the request line")
    held = not pipe.has_answer(ident)
    threading.Event().wait(HELD_S)
    answer_gate(effects, GATE_A)

    assert held
    assert _text(pipe.answer(ident)) == APPROVED
    assert approval_requests(effects) == [{**_approve(GATE_A), "meta": META}]


def test_approval_answers_each_gate_of_a_tool_by_its_name(start: Any, effects: Tree) -> None:
    pipe = start(APPROVAL)
    _ready(pipe)
    answer_gate(effects, GATED_TOOL, DENIED)

    first = pipe.answer(_call(pipe, "approve", _approve(GATE_A)))
    second = pipe.answer(_call(pipe, "approve", _approve(GATE_B)))

    assert _text(first) == _text(second) == DENIED


def test_approval_holds_one_gate_and_answers_another(start: Any, effects: Tree) -> None:
    pipe = start(APPROVAL)
    _ready(pipe)
    held = _call(pipe, "approve", _approve(GATE_A))
    answer_gate(effects, GATE_B)

    answered = pipe.answer(_call(pipe, "approve", _approve(GATE_B)))

    assert _text(answered) == APPROVED
    assert not pipe.has_answer(held)


def test_approval_gives_a_gate_answer_as_the_test_wrote_it(start: Any, effects: Tree) -> None:
    """A test makes an answer that the chaperone must refuse (§5.3: a fault)."""
    pipe = start(APPROVAL)
    _ready(pipe)
    answer_gate(effects, GATE_A, "yes")

    assert _text(pipe.answer(_call(pipe, "approve", _approve(GATE_A)))) == "yes"


@pytest.mark.parametrize(
    "arguments",
    [
        {"gate": GATE_A, "tool": GATED_TOOL, "arguments": {}},
        {**_approve(GATE_A), "extra": 1},
        _approve("0123"),
        _approve(GATE_A, "a tool"),
    ],
)
def test_approval_refuses_arguments_of_another_shape(
    start: Any, effects: Tree, arguments: dict[str, Any]
) -> None:
    pipe = start(APPROVAL)
    _ready(pipe)

    answer = pipe.answer(_call(pipe, "approve", arguments))

    assert answer["result"]["isError"] is True
    assert approval_requests(effects) == []


def test_the_wrapper_of_each_effect_stand_in_is_an_absolute_program(effects: Tree) -> None:
    """A manifest names the program by its absolute path. The chaperone looks up no PATH for it."""
    for name in (UPSTREAM, APPROVAL):
        program = effect_program(effects, name)

        assert Path(program).is_absolute()
        assert program.stat().st_mode & 0o100
