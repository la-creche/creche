"""Upstream argument fences (contract 01b §7).

A fence sits on the upstream **by name, before any grant**. A family that holds
no grant for a server is still fenced, which is why the fence lives in the
server file and not in a family file. Contract 04 §5 row 3 therefore runs ahead
of row 4.

Order and meaning (§7.1):

1. `arg_denies` runs first. A value in `values` is refused.
2. `arg_allows` runs next. A value **not** in `values` is refused.
3. A call that does not carry the named argument at all is refused by an
   `arg_allows` entry that covers it. Deny by default (invariant 11).

A composite argument (§7.2) joins two argument names with `/` and compares them
as one string, so `repo` alone can never pass a fence that means `owner/repo`.

Root copies each `mcp/<name>/server.yaml`'s `arg_denies` into the roster row
it writes, and `from_upstreams` builds the fences from the rows that serve: at
start, and again at every reload. A fence therefore applies from
the reload that carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True)
class ArgDeny:
    """One argument fence on an upstream (`arg_denies` in a roster row, which
    root copies from the server file): a call to any of `tools` whose `arg`
    is one of `values` is refused. It sits on the upstream by name, ahead of
    any grant.

    `mcp_client.py` parses it out of the roster and `from_upstreams`
    turns it into the `FenceRule` this module decides with. It lives here,
    beside its consumer, and not in the parser, so the decision core reaches
    it without importing the MCP client stack.
    """

    tools: frozenset[str]
    arg: str
    values: frozenset[str]


#: `tools: all` in the server file. Held as None inside a rule, so "covers
#: every tool" is a state rather than a magic name comparison per call.
ALL_TOOLS: Final = "all"

#: Contract 01b §7.2 joins a composite argument's parts with this.
COMPOSITE_SEPARATOR: Final = "/"


@dataclass(frozen=True)
class FenceRule:
    """One `arg_denies` or `arg_allows` entry. `tools` is None for `all`."""

    tools: frozenset[str] | None
    arg: str
    values: frozenset[str]

    def covers(self, tool: str) -> bool:
        if self.tools is None:
            return True
        return tool in self.tools


@dataclass(frozen=True)
class ServerFence:
    denies: tuple[FenceRule, ...] = ()
    allows: tuple[FenceRule, ...] = ()


#: Server name -> its fences. Configuration, so the decision core takes it as
#: an input and stays pure.
ServerFences = Mapping[str, ServerFence]

#: A read-only view, so "no fences" can be a shared default: a plain `{}` is a
#: mutable default, which a dataclass field refuses outright.
NO_FENCES: Final[ServerFences] = MappingProxyType({})


def _composite(arg: str, args: Mapping[str, object]) -> str | None:
    """The value a rule compares, or None when the call does not carry it."""
    pieces: list[str] = []
    for part in arg.split(COMPOSITE_SEPARATOR):
        value = args.get(part)
        if not isinstance(value, str):
            return None
        pieces.append(value)
    return COMPOSITE_SEPARATOR.join(pieces)


def _refuse_denies(
    rules: tuple[FenceRule, ...], tool: str, args: Mapping[str, object]
) -> str | None:
    for rule in rules:
        if not rule.covers(tool):
            continue
        value = _composite(rule.arg, args)
        if value is not None and value in rule.values:
            return f"{tool!r} with {rule.arg}={value!r} is refused on this upstream"
    return None


def _refuse_allows(
    rules: tuple[FenceRule, ...], tool: str, args: Mapping[str, object]
) -> str | None:
    for rule in rules:
        if not rule.covers(tool):
            continue
        value = _composite(rule.arg, args)
        if value is None:
            # §7.1 rule 3: a tool with no such argument is not reachable under
            # an allow. To reach it, name it in a second, narrower entry.
            return f"{tool!r} carries no {rule.arg} and this upstream allows by it"
        if value not in rule.values:
            return f"{tool!r} with {rule.arg}={value!r} is outside this upstream's allow"
    return None


def refuse(fences: ServerFences, server: str, tool: str, args: Mapping[str, object]) -> str | None:
    """Why this upstream refuses the call, or None. Pure."""
    fence = fences.get(server)
    if fence is None:
        return None
    denied = _refuse_denies(fence.denies, tool, args)
    if denied is not None:
        return denied
    return _refuse_allows(fence.allows, tool, args)


def from_upstreams(arg_denies: Mapping[str, tuple[ArgDeny, ...]]) -> ServerFences:
    """The fences of these roster rows, by server name.

    `reload_pool.serving_of` calls it with the rows that serve, so a reload
    moves the fences with the rows. The roster carries `arg_denies` alone:
    `arg_allows` is declared in the server file and applied by nothing yet
    (contract 01b §7), so no family holds an allow rule. A granted server
    the PEP has not loaded is denied `not_implemented` rather than reached
    unfenced (contract 04 §5, the seam row).
    """
    built: dict[str, ServerFence] = {}
    for server, rules in arg_denies.items():
        if not rules:
            continue
        built[server] = ServerFence(
            denies=tuple(
                FenceRule(tools=rule.tools, arg=rule.arg, values=rule.values) for rule in rules
            )
        )
    return built
