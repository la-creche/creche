"""The reach diff of two registries: what each family can reach after a
change that it could not reach before.

Every rule of a family is an exact set: tool names, `host:port`, repo names
and fence values. No rule holds a glob (contract 01 §3.7). So the set
difference of two reaches is exact, and no solver is needed.

1. `load_registry` reads the base and the head registry, the strict reader
   that `caregiver` uses.
2. `_reach_of` builds the reach of each family as typed sets. A server file
   change counts against every family that grants that server, because the
   reach reads the server file of each grant.
3. `diff` compares the two reaches of each family. Each gain is one
   `Finding` with a `Gain` kind. Each narrowing is one `Finding` with a
   `Narrowing` kind.

A family or server file that the head registry holds and that the reader
refuses has no reach to compare. `Diff.unread` names it, and the diff then
counts as a gain: a reach that is not read is not proven narrow. A file that
the base refuses has an empty reach at the base, so each grant of its head
counts as a gain.

Pure apart from the two reads: no clock, no host, no environment."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from .grammar import SECRET_PREFIX, egress_problem
from .model import FamilyFile
from .registry import FAMILIES_DIR, FAMILY_FILE, MCP_DIR, SERVER_FILE, Registry, load_registry
from .server import FenceEntry, McpServerFile
from .validate import APPROVAL_WILDCARD, CALL_SEPARATOR, INVOKE_AGENT, Index

#: The port of a bare `egress` host (contract 01 §3.7 rule 7).
BARE_HOST_PORT: Final = 443


class Gain(StrEnum):
    """One kind of new reach. Plan 2.3 step 3 names the six."""

    NEW_CREDENTIAL = "new_credential"
    NEW_WRITE_TOOL = "new_write_tool"
    NEW_TOOL = "new_tool"
    GATE_DROPPED = "gate_dropped"
    FENCE_LOOSENED = "fence_loosened"
    NEW_REACH = "new_reach"


class Narrowing(StrEnum):
    """The twin of each `Gain`: reach that the change removes."""

    CREDENTIAL_DROPPED = "credential_dropped"
    WRITE_DROPPED = "write_dropped"
    TOOL_DROPPED = "tool_dropped"
    GATE_ADDED = "gate_added"
    FENCE_TIGHTENED = "fence_tightened"
    REACH_DROPPED = "reach_dropped"


class FenceKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Fence:
    """The fence of one argument of one tool, as `caregiver` merges the
    entries of a server file (`policy_file._fences`):

    1. The deny values of every `arg_denies` entry, joined.
    2. The allow values of every `arg_allows` entry, intersected, less the
       deny values. An allow fence also refuses a call that names no value.

    An `ALLOW` fence admits only `values`. A `DENY` fence admits every value
    but `values`."""

    kind: FenceKind
    values: frozenset[str]


#: `(server, tool)`.
type ToolKey = tuple[str, str]
#: `(server, tool, argument)`.
type FenceKey = tuple[str, str, str]


@dataclass(frozen=True)
class Reach:
    """Everything one family can reach, as typed sets."""

    #: Each granted tool, and whether its catalog entry says `write = true`.
    tools: Mapping[ToolKey, bool]
    #: Each `(server, secret)` that a granted server holds.
    credentials: frozenset[tuple[str, str]]
    #: Each granted call that `approval` does not gate: `<server>__<tool>`,
    #: a verb name, or `invoke_agent`.
    ungated: frozenset[str]
    #: The fence of each argument of each granted tool.
    fences: Mapping[FenceKey, Fence]
    #: Each `egress` entry as `host:port`, the host in lower case.
    egress: frozenset[str]
    repos: frozenset[str]
    delegates: frozenset[str]
    #: Each verb scope: `embed`, `job_status`, `release:<component>`,
    #: `enqueue:<target>`, `ha_call:<domain>.<service>[:<entity>]`.
    verbs: frozenset[str]
    shell: bool
    sandbox_tools: frozenset[str]


#: The reach of a family that does not exist, or that the base refused.
NO_REACH: Final = Reach(
    tools={},
    credentials=frozenset(),
    ungated=frozenset(),
    fences={},
    egress=frozenset(),
    repos=frozenset(),
    delegates=frozenset(),
    verbs=frozenset(),
    shell=False,
    sandbox_tools=frozenset(),
)


@dataclass(frozen=True)
class Finding:
    family: str
    kind: Gain | Narrowing
    detail: str


@dataclass(frozen=True)
class Diff:
    gains: tuple[Finding, ...]
    narrowings: tuple[Finding, ...]
    #: Each head file that the reader refused, as a registry path.
    unread: tuple[str, ...]

    @property
    def widens(self) -> bool:
        return bool(self.gains or self.unread)


def reach_diff(base: Path, head: Path) -> Diff:
    """Read two registry roots and diff each family's reach."""
    return _diff(load_registry(base), load_registry(head))


def _diff(base: Registry, head: Registry) -> Diff:
    before = _reaches(base)
    after = _reaches(head)
    gains: list[Finding] = []
    narrowings: list[Finding] = []
    for family in sorted(before.keys() | after.keys()):
        old = before.get(family, NO_REACH)
        new = after.get(family, NO_REACH)
        for kind, detail in _compare(old, new):
            target = gains if isinstance(kind, Gain) else narrowings
            target.append(Finding(family, kind, detail))

    return Diff(tuple(gains), tuple(narrowings), _unread(head))


def _reaches(registry: Registry) -> dict[str, Reach]:
    """The reach of each family that the reader accepted."""
    index = Index(servers=registry.servers)
    return {
        name: _reach_of(family, index, registry.servers)
        for name, family in registry.families.items()
    }


def _unread(registry: Registry) -> tuple[str, ...]:
    """Each family file and server file that the reader refused. A
    directory with no such file holds a warning only, and `caregiver`
    ignores it, so it is not unread."""
    families = (
        f"{FAMILIES_DIR}/{name}/{FAMILY_FILE}"
        for name, report in registry.reports.items()
        if name not in registry.families and not report.ok
    )
    servers = (
        f"{MCP_DIR}/{name}/{SERVER_FILE}"
        for name, report in registry.server_reports.items()
        if name not in registry.servers and not report.ok
    )
    return tuple(sorted((*families, *servers)))


def _reach_of(family: FamilyFile, index: Index, servers: Mapping[str, McpServerFile]) -> Reach:
    """Fail closed on a grant that the catalog does not resolve. A tool that
    the server file does not declare counts as a write tool. `all` on a
    server that the registry does not declare counts as one write tool,
    `<server>__*`. `check_family` refuses both, and the reach still counts
    them."""
    tools: dict[ToolKey, bool] = {}
    fences: dict[FenceKey, Fence] = {}
    credentials: set[tuple[str, str]] = set()
    for server in family.tools:
        declared = servers.get(server)
        writes = {} if declared is None else {one.name: one.write for one in declared.tools}
        if declared is None and family.grants_all(server):
            tools[server, APPROVAL_WILDCARD] = True

        for tool in index.granted_tools(family, server):
            tools[server, tool] = writes.get(tool, True)
            if declared is not None:
                for argument, fence in _fences(declared, tool).items():
                    fences[server, tool, argument] = fence

        if declared is not None:
            credentials.update((server, secret) for secret in _secrets(declared))

    return Reach(
        tools=tools,
        credentials=frozenset(credentials),
        ungated=_ungated(family, index, tools),
        fences=fences,
        egress=frozenset(_egress(entry) for entry in family.egress),
        repos=frozenset(family.repos),
        delegates=frozenset(family.delegates),
        verbs=frozenset(_verb_scopes(family)),
        shell=family.shell,
        sandbox_tools=frozenset(family.sandbox_tools),
    )


def _secrets(server: McpServerFile) -> Iterator[str]:
    for value in server.run.env.values():
        if value.startswith(SECRET_PREFIX):
            yield value[len(SECRET_PREFIX) :]

    for shared in server.shared_secrets:
        yield shared.secret


def _fences(server: McpServerFile, tool: str) -> dict[str, Fence]:
    declared = server.tool_names()
    denied: dict[str, frozenset[str]] = {}
    allowed: dict[str, frozenset[str]] = {}
    for entry in _covering(server.arg_denies, tool, declared):
        denied[entry.arg] = denied.get(entry.arg, frozenset()) | frozenset(entry.values)

    for entry in _covering(server.arg_allows, tool, declared):
        values = frozenset(entry.values)
        allowed[entry.arg] = allowed[entry.arg] & values if entry.arg in allowed else values

    fences: dict[str, Fence] = {}
    for argument in denied.keys() | allowed.keys():
        no = denied.get(argument, frozenset())
        if argument in allowed:
            fences[argument] = Fence(FenceKind.ALLOW, allowed[argument] - no)
        else:
            fences[argument] = Fence(FenceKind.DENY, no)

    return fences


def _covering(
    entries: list[FenceEntry], tool: str, declared: tuple[str, ...]
) -> Iterator[FenceEntry]:
    return (entry for entry in entries if entry.covers(tool, declared))


def _ungated(family: FamilyFile, index: Index, tools: Mapping[ToolKey, bool]) -> frozenset[str]:
    calls = {_call(server, tool) for server, tool in tools}
    calls.update(family.verbs.granted())
    if family.delegates:
        calls.add(INVOKE_AGENT)

    gated: set[str] = set()
    for entry in family.approval:
        server, separator, tool = entry.partition(CALL_SEPARATOR)
        if separator and tool == APPROVAL_WILDCARD:
            gated.update(_call(server, one) for one in index.granted_tools(family, server))
        else:
            gated.add(entry)

    return frozenset(calls - gated)


def _call(server: str, tool: str) -> str:
    return f"{server}{CALL_SEPARATOR}{tool}"


def _egress(entry: str) -> str:
    """The form of `caregiver.reach_files`: the host in lower case and an
    explicit port. `github.com` and `GitHub.com:443` are one entry."""
    if egress_problem(entry) is not None:
        # `check_family` refuses the entry. It still counts, as it is written.
        return entry.lower()

    host, colon, port = entry.partition(":")
    return f"{host.lower()}:{int(port) if colon else BARE_HOST_PORT}"


def _verb_scopes(family: FamilyFile) -> Iterator[str]:
    verbs = family.verbs
    if verbs.embed is not None:
        yield "embed"
    if verbs.job_status is not None:
        yield "job_status"
    if verbs.release is not None:
        yield from (f"release:{one}" for one in verbs.release.components)
    if verbs.enqueue is not None:
        yield from (f"enqueue:{one}" for one in verbs.enqueue.targets)
    if verbs.ha_call is not None:
        for triple in verbs.ha_call.allow:
            entity = "" if triple.entity_id is None else f":{triple.entity_id}"
            yield f"ha_call:{triple.domain}.{triple.service}{entity}"


type Row = tuple[Gain | Narrowing, str]


def _compare(old: Reach, new: Reach) -> Iterator[Row]:
    yield from _credentials(old, new)
    yield from _tools(old, new)
    yield from _gates(old, new)
    yield from _all_fences(old, new)
    yield from _sandbox(old, new)


def _credentials(old: Reach, new: Reach) -> Iterator[Row]:
    for server, secret in sorted(new.credentials - old.credentials):
        yield Gain.NEW_CREDENTIAL, f"{secret} through {server}"
    for server, secret in sorted(old.credentials - new.credentials):
        yield Narrowing.CREDENTIAL_DROPPED, f"{secret} through {server}"


def _tools(old: Reach, new: Reach) -> Iterator[Row]:
    for key in sorted(new.tools.keys() | old.tools.keys()):
        name = _call(*key)
        if key not in old.tools:
            yield (Gain.NEW_WRITE_TOOL if new.tools[key] else Gain.NEW_TOOL), name
        elif key not in new.tools:
            yield Narrowing.TOOL_DROPPED, name
        elif new.tools[key] and not old.tools[key]:
            yield Gain.NEW_WRITE_TOOL, f"{name} is now write = true"
        elif old.tools[key] and not new.tools[key]:
            yield Narrowing.WRITE_DROPPED, f"{name} is now write = false"


def _gates(old: Reach, new: Reach) -> Iterator[Row]:
    """A gate counts only on a call that both sides grant. A new call shows
    as its grant, and a dropped call as its drop."""
    granted_old = _calls(old)
    granted_new = _calls(new)
    both = granted_old & granted_new
    for call in sorted((new.ungated - old.ungated) & both):
        yield Gain.GATE_DROPPED, f"{call} no longer waits for approval"
    for call in sorted((old.ungated - new.ungated) & both):
        yield Narrowing.GATE_ADDED, f"{call} now waits for approval"


def _calls(reach: Reach) -> frozenset[str]:
    calls = {_call(*key) for key in reach.tools}
    calls.update(verb.partition(":")[0] for verb in reach.verbs)
    if reach.delegates:
        calls.add(INVOKE_AGENT)

    return frozenset(calls)


def _all_fences(old: Reach, new: Reach) -> Iterator[Row]:
    """The fences of a tool that both sides grant. An argument with no fence
    admits every value."""
    shared = old.tools.keys() & new.tools.keys()
    keys = {key for key in old.fences.keys() | new.fences.keys() if key[:2] in shared}
    open_fence = Fence(FenceKind.DENY, frozenset())
    for key in sorted(keys):
        before = old.fences.get(key, open_fence)
        after = new.fences.get(key, open_fence)
        where = f"{_call(key[0], key[1])} {key[2]}"
        for text in _admitted(before, after):
            yield Gain.FENCE_LOOSENED, f"{where}: {text}"
        for text in _admitted(after, before):
            yield Narrowing.FENCE_TIGHTENED, f"{where}: {text}"


def _admitted(before: Fence, after: Fence) -> Iterator[str]:
    """What `after` admits that `before` does not, as detail texts."""
    match before.kind, after.kind:
        case FenceKind.ALLOW, FenceKind.ALLOW:
            yield from (f"'{one}' is allowed" for one in sorted(after.values - before.values))
        case FenceKind.ALLOW, FenceKind.DENY:
            yield "the allow list is gone, so any value passes"
        case FenceKind.DENY, FenceKind.ALLOW:
            yield from (f"'{one}' is allowed" for one in sorted(after.values & before.values))
        case FenceKind.DENY, FenceKind.DENY:
            freed = sorted(before.values - after.values)
            yield from (f"'{one}' is no longer denied" for one in freed)


def _sandbox(old: Reach, new: Reach) -> Iterator[Row]:
    axes = (
        ("egress", old.egress, new.egress),
        ("repo", old.repos, new.repos),
        ("delegate", old.delegates, new.delegates),
        ("verb", old.verbs, new.verbs),
        ("sandbox_tool", old.sandbox_tools, new.sandbox_tools),
    )
    for axis, before, after in axes:
        for one in sorted(after - before):
            yield Gain.NEW_REACH, f"{axis} {one}"
        for one in sorted(before - after):
            yield Narrowing.REACH_DROPPED, f"{axis} {one}"

    if new.shell and not old.shell:
        yield Gain.NEW_REACH, "shell"
    if old.shell and not new.shell:
        yield Narrowing.REACH_DROPPED, "shell"


def markdown(result: Diff) -> str:
    """One report: the gains, the unread files, then the narrowings."""
    lines = ["## Reach diff", ""]
    if not result.widens:
        lines += ["No family gains reach.", ""]
    else:
        lines += [f"### Gains ({len(result.gains)})", "", *_table(result.gains), ""]

    if result.unread:
        lines += [
            f"### Not read ({len(result.unread)})",
            "",
            "The reader refused these head files, so their reach is not proven narrow.",
            "",
            *(f"- `{path}`" for path in result.unread),
            "",
        ]

    if result.narrowings:
        lines += [f"### Narrowings ({len(result.narrowings)})", "", *_table(result.narrowings), ""]

    return "\n".join(lines).rstrip("\n") + "\n"


def _table(findings: tuple[Finding, ...]) -> list[str]:
    rows = ["| Family | Kind | Detail |", "|---|---|---|"]
    rows += [f"| {_cell(one.family)} | `{one.kind}` | {_cell(one.detail)} |" for one in findings]
    return rows


def _cell(text: str) -> str:
    """A registry value in one table cell: no pipe and no line end."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
