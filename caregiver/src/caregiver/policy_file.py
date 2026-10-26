"""The policy file of the Rust chaperone, `policy.toml`: caregiver's writer
side (chaperone design section 6).

`chaperone check` is the only authority on a valid file. The types here
mirror the Rust types of `crates/core/src/policy.rs` one to one: `RawPolicy`
is `PolicyFile`, `RawApp` is `App`, and `UpstreamGrant`, `ToolGrant`,
`Limits` and `ArgumentFence` keep their names. `AttendanceSection` is the
section that the `attendance` fence reads (design section 12). Each type
refuses a value that the Rust reader refuses for that one field, so a wrong
file is hard to write. The rules across apps (a shared digest, one hop)
stay with `chaperone check`.

`build_app` maps one family file and its server files to one app table,
with the argument fences of each server file on each granted tool."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from agent_family import FamilyFile, Index, McpServerFile
from agent_family.server import FenceEntry

#: `RawPolicy.version`: the one version this chaperone reads.
POLICY_VERSION: Final = 1

#: `DIGESTS_MAX`: two digests while a token rotates.
DIGESTS_MAX: Final = 2

#: `UpstreamBound` and `InflightLimit`.
TIMEOUT_S_MIN: Final = 1
TIMEOUT_S_MAX: Final = 120
INFLIGHT_MIN: Final = 1
INFLIGHT_MAX: Final = 8

#: `Limits` holds two `u32`. `Scalar::Int` is an `i64`.
U32_MAX: Final = 2**32 - 1
I64_MIN: Final = -(2**63)
I64_MAX: Final = 2**63 - 1

#: `MAX_DELEGATES` and `MAX_ENQUEUE_TARGETS` of the `attendance` fence.
MAX_DELEGATES: Final = 32
MAX_ENQUEUE_TARGETS: Final = 64

#: `ids.rs`: an app ID and a plugin name are slugs, and a plugin name has
#: no `__`. A tool name and an argument name share one grammar. A digest is
#: 64 lower-case hex characters. The `attendance` fence names a family.
_SLUG: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TOOL: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_FAMILY: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")
_PLUGIN_SEPARATOR: Final = "__"

#: A TOML key that needs no quotes.
_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")

#: The plugin upstreams that serve the verbs (design section 12).
ATTENDANCE: Final = "attendance"
RELEASE: Final = "release"
INVOKE_AGENT: Final = "invoke_agent"
ENQUEUE: Final = "enqueue"
JOB_STATUS: Final = "job_status"
RELEASE_REQUEST: Final = "request"

#: A delegate call may run for 120 seconds (design section 12).
DELEGATE_TIMEOUT_S: Final = 120

#: The two limits that the family file does not hold. They keep the values
#: of the grant file: `pep_rpm` and `max_open_gates`.
CALLS_PER_MINUTE: Final = 60
MAX_PENDING_GATES: Final = 10

NOTE_PREFIX: Final = "registry "

#: `ExposedName::MAX`: an app sees a tool as `<upstream>__<tool>`, and the
#: Rust chaperone lists no tool with a longer name.
EXPOSED_NAME_MAX: Final = 50

#: A server file fences a pair of arguments as `owner/repo`, with each value
#: as `<owner>/<repo>`. `policy.toml` fences one argument per name.
_PAIR_SEPARATOR: Final = "/"

#: An approval entry with no `__` names a verb. Each verb with a home is one
#: tool of one plugin upstream. `embed` and `ha_call` have none.
_VERB_TOOLS: Final[Mapping[str, tuple[str, str]]] = {
    INVOKE_AGENT: (ATTENDANCE, INVOKE_AGENT),
    ENQUEUE: (ATTENDANCE, ENQUEUE),
    JOB_STATUS: (ATTENDANCE, JOB_STATUS),
    RELEASE: (RELEASE, RELEASE_REQUEST),
}
_CALL_SEPARATOR: Final = "__"
_WILDCARD: Final = "*"

_TOML_ESCAPES: Final[Mapping[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class PolicyShapeError(ValueError):
    """A value that the Rust reader of `policy.toml` refuses. The text names
    the rule."""


class Approval(StrEnum):
    NONE = "none"
    REQUIRED = "required"


class FenceKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


Scalar = bool | int | str


@dataclass(frozen=True)
class ArgumentFence:
    """A fence on one top-level argument: `{ allow = [...] }` or
    `{ deny = [...] }`. An empty list does not load."""

    kind: FenceKind
    values: tuple[Scalar, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise PolicyShapeError("an argument fence needs at least one value")

        for value in self.values:
            if isinstance(value, int) and not isinstance(value, bool):
                _check_range(value, I64_MIN, I64_MAX, "an integer of a fence")


@dataclass(frozen=True)
class ToolGrant:
    approval: Approval = Approval.NONE
    arguments: Mapping[str, ArgumentFence] = field(default_factory=dict[str, ArgumentFence])
    note: str | None = None

    def __post_init__(self) -> None:
        for name in self.arguments:
            _check_name(name, _TOOL, "an argument name")


@dataclass(frozen=True)
class UpstreamGrant:
    tools: Mapping[str, ToolGrant]
    timeout_s: int | None = None
    max_inflight: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.tools:
            raise PolicyShapeError("an upstream grant needs at least one tool")

        for name in self.tools:
            _check_name(name, _TOOL, "a tool name")

        if self.timeout_s is not None:
            _check_range(self.timeout_s, TIMEOUT_S_MIN, TIMEOUT_S_MAX, "timeout_s")

        if self.max_inflight is not None:
            _check_range(self.max_inflight, INFLIGHT_MIN, INFLIGHT_MAX, "max_inflight")


@dataclass(frozen=True)
class Limits:
    calls_per_minute: int
    max_pending_gates: int

    def __post_init__(self) -> None:
        _check_range(self.calls_per_minute, 0, U32_MAX, "calls_per_minute")
        _check_range(self.max_pending_gates, 0, U32_MAX, "max_pending_gates")


@dataclass(frozen=True)
class AttendanceSection:
    """`[apps.<id>.plugins.attendance]`. A missing list is empty."""

    delegates: tuple[str, ...] = ()
    enqueue_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_families(self.delegates, MAX_DELEGATES, "delegates")
        _check_families(self.enqueue_targets, MAX_ENQUEUE_TARGETS, "enqueue_targets")


@dataclass(frozen=True)
class PluginSections:
    """`RawApp.plugins`: one section per active fence. This writer knows
    the section of `attendance` only."""

    attendance: AttendanceSection | None = None


@dataclass(frozen=True)
class App:
    token_sha256: tuple[str, ...]
    limits: Limits
    grants: Mapping[str, UpstreamGrant] = field(default_factory=dict[str, UpstreamGrant])
    plugins: PluginSections = field(default_factory=PluginSections)
    note: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.token_sha256) <= DIGESTS_MAX:
            raise PolicyShapeError(f"an app needs 1 to {DIGESTS_MAX} token digests")

        if len(set(self.token_sha256)) != len(self.token_sha256):
            raise PolicyShapeError("an app holds one token digest twice")

        for digest in self.token_sha256:
            _check_name(digest, _DIGEST, "a token digest")

        for name in self.grants:
            _check_plugin(name)


@dataclass(frozen=True)
class PolicyFile:
    """`RawPolicy`. The version is always `POLICY_VERSION`."""

    apps: Mapping[str, App]

    def __post_init__(self) -> None:
        for app_id in self.apps:
            _check_name(app_id, _SLUG, "an app ID")


def is_app_id(text: str) -> bool:
    """Whether `text` matches the grammar of an app ID."""
    return _SLUG.fullmatch(text) is not None


def _check_name(text: str, grammar: re.Pattern[str], what: str) -> None:
    if grammar.fullmatch(text) is None:
        raise PolicyShapeError(f"{what} must match {grammar.pattern}")


def _check_plugin(name: str) -> None:
    _check_name(name, _SLUG, "a plugin name")
    if _PLUGIN_SEPARATOR in name:
        raise PolicyShapeError(f"a plugin name must not hold {_PLUGIN_SEPARATOR}")


def _check_range(value: int, low: int, high: int, what: str) -> None:
    if not low <= value <= high:
        raise PolicyShapeError(f"{what} must be {low} to {high}")


def _check_families(families: tuple[str, ...], cap: int, what: str) -> None:
    if len(families) > cap:
        raise PolicyShapeError(f"{what} holds more than {cap} families")

    for family in families:
        _check_name(family, _FAMILY, f"a family of {what}")


def build_app(family: FamilyFile, index: Index, digests: tuple[str, ...], *, rev: str) -> App:
    """One app table for one family (phase 3 plan, section (b)). The app ID
    is the family name. `all` expands against the current server files.
    `rev` is the registry revision, kept in the note."""
    gated = _gated(family, index)
    granted = _granted(family, index)
    _check_exposed(granted)
    grants = {
        upstream: _upstream_grant(family, upstream, tools, gated, index.servers.get(upstream))
        for upstream, tools in granted.items()
    }
    return App(
        note=f"{NOTE_PREFIX}{rev}",
        token_sha256=digests,
        limits=Limits(calls_per_minute=CALLS_PER_MINUTE, max_pending_gates=MAX_PENDING_GATES),
        grants=grants,
        plugins=PluginSections(attendance=_attendance_section(family)),
    )


def _granted(family: FamilyFile, index: Index) -> dict[str, tuple[str, ...]]:
    """Every granted tool by upstream. A server with no tool has no grant,
    because an empty grant does not load."""
    granted = {server: index.granted_tools(family, server) for server in sorted(family.tools)}
    for upstream in (ATTENDANCE, RELEASE):
        if upstream in granted:
            raise PolicyShapeError(f"the server name {upstream} is the name of a plugin upstream")

    granted = {server: tools for server, tools in granted.items() if tools}
    attendance = _attendance_tools(family)
    if attendance:
        granted[ATTENDANCE] = attendance

    if family.verbs.release is not None:
        granted[RELEASE] = (RELEASE_REQUEST,)

    return granted


def _check_exposed(granted: Mapping[str, tuple[str, ...]]) -> None:
    for upstream, tools in granted.items():
        for tool in tools:
            exposed = f"{upstream}{_CALL_SEPARATOR}{tool}"
            if len(exposed) > EXPOSED_NAME_MAX:
                raise PolicyShapeError(
                    f"the exposed name {exposed} is longer than {EXPOSED_NAME_MAX} characters"
                )


def _attendance_tools(family: FamilyFile) -> tuple[str, ...]:
    tools: list[str] = []
    if family.delegates:
        tools.append(INVOKE_AGENT)

    if family.verbs.enqueue is not None:
        tools.append(ENQUEUE)

    if family.verbs.job_status is not None:
        tools.append(JOB_STATUS)

    return tuple(tools)


def _upstream_grant(
    family: FamilyFile,
    upstream: str,
    tools: tuple[str, ...],
    gated: set[tuple[str, str]],
    server: McpServerFile | None,
) -> UpstreamGrant:
    """`max_inflight` counts every call to `attendance`, not only the
    delegate calls. `timeout_s` is set only for a delegate call. A plugin
    upstream has no server file, so its tools have no argument fence."""
    grants = {
        tool: ToolGrant(
            approval=Approval.REQUIRED if (upstream, tool) in gated else Approval.NONE,
            arguments=_fences(server, tool) if server is not None else {},
        )
        for tool in tools
    }
    if upstream != ATTENDANCE:
        return UpstreamGrant(tools=grants)

    return UpstreamGrant(
        tools=grants,
        timeout_s=DELEGATE_TIMEOUT_S if family.delegates else None,
        max_inflight=family.max_inflight_delegations,
    )


def _gated(family: FamilyFile, index: Index) -> set[tuple[str, str]]:
    """Each `(upstream, tool)` that the approval list names. `<server>__*`
    expands to each granted tool of that server."""
    gated: set[tuple[str, str]] = set()
    for entry in family.approval:
        server, separator, tool = entry.partition(_CALL_SEPARATOR)
        if not separator:
            home = _VERB_TOOLS.get(entry)
            if home is not None:
                gated.add(home)
            continue

        names = index.granted_tools(family, server) if tool == _WILDCARD else (tool,)
        gated.update((server, name) for name in names)

    return gated


def _fences(server: McpServerFile, tool: str) -> dict[str, ArgumentFence]:
    """The fences of the server file that cover `tool`, one per argument.

    1. Each `arg_denies` entry adds its values to the deny list of its
       argument.
    2. Each `arg_allows` entry keeps only its values in the allow list of
       its argument. A call must pass every allow entry.
    3. An argument with an allow list loses the denied values from it.
       An allow list with no value left is refused, because an empty fence
       does not load."""
    declared = server.tool_names()
    denied: dict[str, set[str]] = {}
    allowed: dict[str, set[str]] = {}
    for entry in server.arg_denies:
        if entry.covers(tool, declared):
            for name, values in _split(entry, FenceKind.DENY).items():
                denied.setdefault(name, set()).update(values)

    for entry in server.arg_allows:
        if entry.covers(tool, declared):
            for name, values in _split(entry, FenceKind.ALLOW).items():
                allowed[name] = allowed[name] & values if name in allowed else set(values)

    fences: dict[str, ArgumentFence] = {}
    for name in sorted(denied.keys() | allowed.keys()):
        if name not in allowed:
            fences[name] = ArgumentFence(FenceKind.DENY, tuple(sorted(denied[name])))
            continue

        values = allowed[name] - denied.get(name, set())
        if not values:
            raise PolicyShapeError(
                f"the fences of {server.name} on {tool} allow no value of {name}"
            )

        fences[name] = ArgumentFence(FenceKind.ALLOW, tuple(sorted(values)))

    return fences


def _split(entry: FenceEntry, kind: FenceKind) -> dict[str, frozenset[str]]:
    """The values of one entry by argument name. A pair `owner/repo` gives
    two arguments.

    An allow of pairs gives the owners and the repos. That is the same
    fence only when the pairs are every owner with every repo, so other
    pairs are refused. A deny of pairs gives the repos alone. It also
    denies a repo of the same name under another owner: a stricter fence."""
    names = entry.arg.split(_PAIR_SEPARATOR)
    if len(names) == 1:
        return {entry.arg: frozenset(entry.values)}

    pairs = [value.split(_PAIR_SEPARATOR) for value in entry.values]
    if len(names) != 2 or any(len(pair) != 2 or not all(pair) for pair in pairs):
        raise PolicyShapeError(f"each value of the fence on {entry.arg} must be two names")

    first, second = names
    firsts = frozenset(pair[0] for pair in pairs)
    seconds = frozenset(pair[1] for pair in pairs)
    if kind is FenceKind.DENY:
        return {second: seconds}

    if {(a, b) for a, b in pairs} != {(a, b) for a in firsts for b in seconds}:
        raise PolicyShapeError(
            f"the allowed values of {entry.arg} are not each {first} with each {second}"
        )

    return {first: firsts, second: seconds}


def _attendance_section(family: FamilyFile) -> AttendanceSection | None:
    enqueue = family.verbs.enqueue
    section = AttendanceSection(
        delegates=tuple(sorted(set(family.delegates))),
        enqueue_targets=tuple(sorted(set(enqueue.targets))) if enqueue is not None else (),
    )
    if not section.delegates and not section.enqueue_targets:
        return None

    return section


def render_policy(policy: PolicyFile) -> str:
    """The text of `policy.toml`: the version, then each app in ID order."""
    parts = [f"version = {POLICY_VERSION}\n"]
    parts.extend("\n" + render_app(app_id, policy.apps[app_id]) for app_id in sorted(policy.apps))
    return "".join(parts)


def render_app(app_id: str, app: App) -> str:
    """The tables of one app. Each map is in key order, as the Rust
    `BTreeMap` holds it."""
    _check_name(app_id, _SLUG, "an app ID")
    prefix = f"apps.{app_id}"
    lines = [f"[{prefix}]"]
    if app.note is not None:
        lines.append(f"note = {_string(app.note)}")

    lines.append(f"token_sha256 = {_array(app.token_sha256)}")
    limits = app.limits
    lines.append(
        f"limits = {{ calls_per_minute = {limits.calls_per_minute}, "
        f"max_pending_gates = {limits.max_pending_gates} }}"
    )
    for upstream in sorted(app.grants):
        lines.extend(_grant_lines(f"{prefix}.grants.{upstream}", app.grants[upstream]))

    attendance = app.plugins.attendance
    if attendance is not None:
        lines.extend(("", f"[{prefix}.plugins.{ATTENDANCE}]"))
        if attendance.delegates:
            lines.append(f"delegates = {_array(attendance.delegates)}")

        if attendance.enqueue_targets:
            lines.append(f"enqueue_targets = {_array(attendance.enqueue_targets)}")

    return "\n".join(lines) + "\n"


def _grant_lines(table: str, grant: UpstreamGrant) -> list[str]:
    head: list[str] = []
    if grant.note is not None:
        head.append(f"note = {_string(grant.note)}")

    if grant.timeout_s is not None:
        head.append(f"timeout_s = {grant.timeout_s}")

    if grant.max_inflight is not None:
        head.append(f"max_inflight = {grant.max_inflight}")

    lines = ["", f"[{table}]", *head] if head else []
    lines.extend(("", f"[{table}.tools]"))
    lines.extend(f"{_key(tool)} = {_tool_value(grant.tools[tool])}" for tool in sorted(grant.tools))
    return lines


def _tool_value(grant: ToolGrant) -> str:
    pairs: list[str] = []
    if grant.note is not None:
        pairs.append(f"note = {_string(grant.note)}")

    if grant.approval is not Approval.NONE:
        pairs.append(f"approval = {_string(grant.approval)}")

    if grant.arguments:
        fences = ", ".join(
            f"{_key(name)} = {{ {grant.arguments[name].kind} = "
            f"{_array(grant.arguments[name].values)} }}"
            for name in sorted(grant.arguments)
        )
        pairs.append(f"arguments = {{ {fences} }}")

    return "{ " + ", ".join(pairs) + " }" if pairs else "{}"


def _key(name: str) -> str:
    """A bare key, or a quoted key for a name that holds a `.`."""
    return name if _BARE_KEY.fullmatch(name) else _string(name)


def _array(values: tuple[Scalar, ...]) -> str:
    return "[" + ", ".join(_scalar(value) for value in values) + "]"


def _scalar(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int):
        return str(value)

    return _string(value)


def _string(text: str) -> str:
    """A TOML basic string. Each control character is escaped."""
    out: list[str] = []
    for char in text:
        escaped = _TOML_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)

    return '"' + "".join(out) + '"'
