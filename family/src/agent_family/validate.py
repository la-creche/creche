"""Value rules and cross-reference checks for `family.yaml` (contract 01).

Nothing here raises on bad input (invariant 19). Every check appends to one
accumulator, so one parse reports every violation it can see.

Two checks need facts only the host has: the model alias list (§5.1) and the
resolved path behind a mount (§5.4, §3.3 rule 5). Without a host they report a
downgraded warning and the report says which check was downgraded."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from .grammar import (
    ALL_TOOLS,
    ALLOWED_ROOTS,
    BUDGET_USD_MAX,
    CPUS_MAX,
    CPUS_MIN,
    CRON_FIELDS,
    CRON_SHORTHANDS,
    DEFAULT_MAX_INFLIGHT_DELEGATIONS,
    DELEGATE_LIMIT_S,
    DESCRIPTION_MAX,
    FAMILY_NAME,
    HA_ENTITY_ID,
    HA_IDENTIFIER,
    JOB_TIMEOUT_MAX_S,
    JOB_TIMEOUT_MIN_S,
    LOCAL_HOUR_MAX,
    MAX_INFLIGHT_DELEGATIONS_MAX,
    MAX_INFLIGHT_DELEGATIONS_MIN,
    MAX_RUNNING_TURNS_MAX,
    MAX_RUNNING_TURNS_MIN,
    MEMORY_MAX_MB,
    MEMORY_MIN_MB,
    MODEL_ALIAS,
    MOUNT_PATH,
    PLATFORM_ALLOWLIST,
    PLATFORM_MIRRORS,
    PLATFORM_ROOT,
    PLATFORM_SERVER,
    PLATFORM_WITHHELD,
    PROBE_FAMILY,
    QUIET_FLOOR_MAX_HOURS,
    QUIET_FLOOR_MIN_HOURS,
    RESIDENT_PROCS_MAX,
    RESIDENT_PROCS_MIN,
    SANDBOX_FLAVORS,
    SERVER_NAME,
    SESSION_ROOT,
    SKILL_NAME,
    SURVEY_TOOL,
    TOOL_NAME,
    EgressProblem,
    Kind,
    Mode,
    SandboxTool,
    SystemPrompt,
    Verb,
    duration_s,
    egress_problem,
    is_under,
    memory_mb,
)
from .model import DailyCall, FamilyFile
from .report import Issues
from .server import McpServerFile

#: Contract 06 §1's releasable names. `registry-data` is absent: it never
#: takes a release (contract 06 §6).
RELEASABLE: Final = frozenset(
    {"pep", "sessiond", "managerd", "ui", "playpen", "mcp-servers", "infra", "releasectl"}
)

#: Old names a family file may still list. `sandbox-image` became `playpen`,
#: and the registry's family file says the old name until its own change
#: lands after this one deploys. The entry leaves in the next rename.
RETIRING_NAMES: Final = frozenset({"sandbox-image"})

#: Probe 0a measured 155 to 170 MB per idle held-open pi process and 62 MB for
#: the playpen (contract 01 §3.9). The warning uses the LOW end, so it fires
#: only when the budget cannot carry the count even optimistically. Used only
#: for the §3.9 warning, never for a refusal.
PI_PROCESS_MB: Final = 155
PLAYPEN_MB: Final = 62

#: The approval grammar's wildcard: `<server>__*` (contract 01 §3.11).
CALL_SEPARATOR: Final = "__"
APPROVAL_WILDCARD: Final = "*"

#: The PEP synthesizes this from `delegates`; it is never granted by name
#: (contract 01 §3.5 rule 6). Contract 04 §7.2 still reads it out of
#: `approval`, so the approval grammar accepts it.
INVOKE_AGENT: Final = "invoke_agent"


class HostFacts(Protocol):
    """What only the host knows. `managerd` implements it against LiteLLM and
    the real filesystem. Tests implement it in three lines."""

    def model_aliases(self) -> frozenset[str]:
        """Every alias LiteLLM serves right now."""
        ...

    def real_path(self, path: str) -> str:
        """`path` with every symlink resolved."""
        ...


@dataclass(frozen=True)
class Index:
    """The rest of the registry, as the cross-reference checks need it."""

    kinds: Mapping[str, str] = field(default_factory=dict[str, str])
    servers: Mapping[str, McpServerFile] = field(default_factory=dict[str, McpServerFile])
    skills: frozenset[str] = frozenset()

    def granted_tools(self, family: FamilyFile, server: str) -> tuple[str, ...]:
        """What one server grant resolves to. `all` expands against the server
        file at read time (contract 01 §3.4 rule 4)."""
        if not family.grants_all(server):
            return family.tool_names(server)

        declared = self.servers.get(server)
        return declared.tool_names() if declared is not None else ()


def check_family(
    family: FamilyFile, directory: str, index: Index, issues: Issues, host: HostFacts | None
) -> None:
    """Every rule contract 01 states for one file, in file order."""
    _check_identity(family, directory, issues)
    _check_model(family, issues, host)
    _check_files(family, issues, host)
    _check_tools(family, index, issues)
    _check_verbs(family, index, issues)
    _check_delegates(family, index, issues)
    _check_inflight_delegations(family, issues)
    _check_egress(family, issues)
    _check_runtime(family, issues)
    _check_sandbox(family, issues)
    _check_skills(family, index, issues)
    _check_approval(family, index, issues)
    _check_kind_fields(family, issues)
    _check_quiet(family, index, issues)


def _kind_of(family: FamilyFile) -> Kind | None:
    try:
        return Kind(family.kind)
    except ValueError:
        return None


def _check_identity(family: FamilyFile, directory: str, issues: Issues) -> None:
    if FAMILY_NAME.fullmatch(family.name) is None:
        issues.error("name", f"'{family.name}' is not a family name; use [a-z][a-z0-9-]{{1,30}}")

    if family.name == PROBE_FAMILY:
        issues.error(
            "name",
            f"'{PROBE_FAMILY}' is reserved for the gate scripts' invariant 9 probe, which "
            "writes a grant file under it and deletes it again; pick another name",
        )

    if family.name != directory:
        issues.error("name", f"'{family.name}' does not match its directory 'families/{directory}'")

    if _kind_of(family) is None:
        allowed = ", ".join(str(member) for member in Kind)
        issues.error("kind", f"'{family.kind}' is not a kind; use one of {allowed}")

    description = " ".join(family.description.split())
    if not 1 <= len(description) <= DESCRIPTION_MAX:
        issues.error(
            "description",
            f"description is {len(description)} characters; it must be 1 to {DESCRIPTION_MAX} "
            "after whitespace collapse",
        )


def _check_model(family: FamilyFile, issues: Issues, host: HostFacts | None) -> None:
    router = family.model.router
    if APPROVAL_WILDCARD in router:
        issues.error(
            "model.router",
            f"'{router}' holds a wildcard; a wildcard alias would grant every model LiteLLM serves",
        )
    elif MODEL_ALIAS.fullmatch(router) is None:
        issues.error(
            "model.router", f"'{router}' is not a LiteLLM alias; use [a-z0-9][a-z0-9._/-]*"
        )

    budget = family.model.budget_usd_per_day
    if not 0 < budget <= BUDGET_USD_MAX:
        issues.error(
            "model.budget_usd_per_day",
            f"{budget} is outside 0 to {BUDGET_USD_MAX}; the ceiling refuses a typed extra zero "
            "here rather than on a bill",
        )

    # §5.1: without host context the check is a warning, and the report says
    # which check was downgraded.
    if host is None:
        issues.warn(
            "model.router", f"no LiteLLM context: '{router}' was not checked", downgraded=True
        )
        return

    if router not in host.model_aliases():
        issues.error("model.router", f"'{router}' is not an alias LiteLLM serves")


def _check_files(family: FamilyFile, issues: Issues, host: HostFacts | None) -> None:
    seen: set[str] = set()
    for index, mount in enumerate(family.files):
        loc = f"files[{index}]"
        if mount.path in seen:
            issues.error(loc, f"'{mount.path}' is mounted twice")

        seen.add(mount.path)
        if mount.mode not in tuple(Mode):
            issues.error(f"{loc}.mode", f"'{mount.mode}' is not a mode; use 'ro' or 'rw'")

        if not _path_shape_ok(mount.path, loc, issues):
            continue

        _check_root(family, mount.path, loc, issues, host)

    # §5.4 and §3.3 rule 5: resolving a symlink needs the host, so without one
    # the complete check downgrades. One line per family, not one per mount.
    if host is None and family.files:
        issues.warn("files", "no host context: no mount's symlinks were resolved", downgraded=True)


def _path_shape_ok(path: str, loc: str, issues: Issues) -> bool:
    """Absolute, no `.` or `..` segment, no `//`, and no glob (§3.3)."""
    if APPROVAL_WILDCARD in path or "?" in path or "[" in path:
        issues.error(
            f"{loc}.path",
            f"'{path}' is a glob; name each directory, because a glob widens a family silently "
            "when a new directory appears",
        )
        return False

    if not path.startswith("/"):
        issues.error(f"{loc}.path", f"'{path}' must be an absolute path")
        return False

    if "//" in path or any(part in (".", "..") for part in path.split("/")[1:]):
        issues.error(f"{loc}.path", f"'{path}' must hold no '.', '..' or empty segment")
        return False

    if MOUNT_PATH.fullmatch(path) is None:
        issues.error(f"{loc}.path", f"'{path}' holds a character outside [A-Za-z0-9._/-]")
        return False

    return True


def _check_root(
    family: FamilyFile, path: str, loc: str, issues: Issues, host: HostFacts | None
) -> None:
    """§5.4 the allowed roots, §5.5 the platform fence, §3.3 rule 6 the
    session store. The literal path is checked with or without a host. The
    resolved path needs one, and a symlink out of a root is an error there,
    not a silent escape (§3.3 rule 5)."""
    if is_under(path, SESSION_ROOT):
        issues.error(
            f"{loc}.path",
            f"'{path}' is the session store; the runtime mounts it, and a family file never "
            "declares it",
        )
        return

    _check_one_root(family, path, loc, issues)
    if host is None:
        return

    resolved = host.real_path(path)
    if resolved != path:
        _check_one_root(family, resolved, loc, issues)


def _check_one_root(family: FamilyFile, path: str, loc: str, issues: Issues) -> None:
    if not any(is_under(path, root) for root in ALLOWED_ROOTS):
        issues.error(
            f"{loc}.path",
            f"'{path}' is under no allowed root; the roots are {', '.join(ALLOWED_ROOTS)}",
        )
        return

    # The fence runs both ways (§5.5). Outbound: nobody else reaches the
    # platform root. Inbound: a family that does reach it mounts nothing else,
    # because `docs/rework/spec.md` §4.4 locks agent-control to the platform
    # root "and nothing else".
    inside = is_under(path, PLATFORM_ROOT)
    allowlisted = family.name in PLATFORM_ALLOWLIST
    if inside and not allowlisted:
        issues.error(
            f"{loc}.path",
            f"'{path}' is under the platform fence; only {', '.join(sorted(PLATFORM_ALLOWLIST))} "
            "may mount the platform root or hold the 'release' verb",
        )
        return

    if not inside and allowlisted and not _is_platform_mirror(family, path):
        issues.error(
            f"{loc}.path",
            f"'{family.name}' reaches only the platform root, so it may not mount '{path}'; "
            "a family that edits the platform holds no other mount",
        )


def _is_platform_mirror(family: FamilyFile, path: str) -> bool:
    """§5.5 rule 6: one of the host's own clones of the three platform repos,
    and every mount of it in this file is read-only. The mode is read off the
    file's own entries, because `_check_one_root` is also handed a RESOLVED
    path, and a symlink's target has no entry of its own: it is a mirror only
    when it is itself one of the three."""
    if path not in PLATFORM_MIRRORS:
        return False

    modes = {one.mode for one in family.files if one.path == path}
    return modes <= {Mode.RO.value}


def _check_tools(family: FamilyFile, index: Index, issues: Issues) -> None:
    kind = _kind_of(family)
    for server, grant in sorted(family.tools.items()):
        loc = f"tools.{server}"
        if SERVER_NAME.fullmatch(server) is None:
            issues.error(
                loc,
                f"'{server}' is not an MCP server name; use [a-z][a-z0-9-]{{1,30}}, because the "
                "PEP's call name is <server>__<tool>",
            )

        declared = index.servers.get(server)
        if declared is None:
            issues.error(loc, f"'{server}' has no mcp/{server}/server.yaml")

        _check_withheld(family, index, server, loc, issues)
        if isinstance(grant, str):
            _check_all_grant(grant, kind, loc, issues)
            continue

        _check_named_tools(grant, declared, server, loc, issues)


def _check_withheld(
    family: FamilyFile, index: Index, server: str, loc: str, issues: Issues
) -> None:
    """§5.5 rule 7: the operator lands every platform change. `main` may carry no
    branch protection, so a family holding the platform server's merge could land
    a registry change, its own grants included, that `managerd` then applies.
    `all` is checked through its expansion: a server file that declares the
    merge again must not re-grant it quietly."""
    if server != PLATFORM_SERVER:
        return

    reason = "a platform change lands only when the operator merges it (§5.5 rule 7)"
    if family.grants_all(server):
        for tool in index.granted_tools(family, server):
            if tool not in PLATFORM_WITHHELD:
                continue

            issues.error(
                loc,
                f"'{ALL_TOOLS}' grants '{tool}', which is never granted: {reason}; drop it "
                f"from mcp/{server}/server.yaml or name each tool",
            )
        return

    for position, tool in enumerate(family.tool_names(server)):
        if tool not in PLATFORM_WITHHELD:
            continue

        issues.error(f"{loc}[{position}]", f"'{tool}' is never granted from '{server}': {reason}")


def _check_all_grant(grant: str, kind: Kind | None, loc: str, issues: Issues) -> None:
    if grant != ALL_TOOLS:
        issues.error(loc, f"'{grant}' must be a list of tool names or '{ALL_TOOLS}'")
        return

    # §3.4 rule 5: `all` is a reach that changes without the family file
    # changing, and invariant 8 gives autonomous families a narrow reach.
    if kind is Kind.AUTONOMOUS:
        issues.error(loc, f"'{ALL_TOOLS}' is refused on an autonomous family; name each tool")


def _check_named_tools(
    grant: list[str], declared: McpServerFile | None, server: str, loc: str, issues: Issues
) -> None:
    if not grant:
        issues.error(loc, "an empty list grants nothing; write no key instead")
        return

    seen: set[str] = set()
    for position, tool in enumerate(grant):
        at = f"{loc}[{position}]"
        if tool in seen:
            issues.error(at, f"'{tool}' is granted twice")

        seen.add(tool)
        if TOOL_NAME.fullmatch(tool) is None:
            issues.error(at, f"'{tool}' is not a tool name; use [A-Za-z][A-Za-z0-9_-]*")
            continue

        if declared is not None and tool not in declared.tool_names():
            issues.error(at, f"'{tool}' is not declared in mcp/{server}/server.yaml")


def _check_verbs(family: FamilyFile, index: Index, issues: Issues) -> None:
    verbs = family.verbs
    if verbs.ha_call is not None:
        _check_ha_call(family, issues)

    if verbs.enqueue is not None:
        _check_enqueue(family, index, issues)

    if verbs.job_status is not None and verbs.enqueue is None:
        issues.error(
            "verbs.job_status",
            "job_status without enqueue can never match a record; its scope is the sessions this "
            "family enqueued",
        )

    if verbs.release is not None:
        _check_release(family, issues)


def _check_ha_call(family: FamilyFile, issues: Issues) -> None:
    fence = family.verbs.ha_call
    if fence is None or not fence.allow:
        issues.error("verbs.ha_call.allow", "ha_call needs at least one {domain, service} triple")
        return

    for position, triple in enumerate(fence.allow):
        loc = f"verbs.ha_call.allow[{position}]"
        if HA_IDENTIFIER.fullmatch(triple.domain) is None:
            issues.error(f"{loc}.domain", f"'{triple.domain}' must match [a-z][a-z0-9_]*")

        if HA_IDENTIFIER.fullmatch(triple.service) is None:
            issues.error(f"{loc}.service", f"'{triple.service}' must match [a-z][a-z0-9_]*")

        entity = triple.entity_id
        if entity is not None and HA_ENTITY_ID.fullmatch(entity) is None:
            issues.error(f"{loc}.entity_id", f"'{entity}' must be domain.object_id")


def _check_enqueue(family: FamilyFile, index: Index, issues: Issues) -> None:
    fence = family.verbs.enqueue
    if fence is None or not fence.targets:
        issues.error("verbs.enqueue.targets", "enqueue needs at least one target family")
        return

    for position, target in enumerate(fence.targets):
        loc = f"verbs.enqueue.targets[{position}]"
        kind = index.kinds.get(target)
        if kind is None:
            issues.error(loc, f"'{target}' is not a family in this registry")
            continue

        if kind != Kind.AUTONOMOUS:
            issues.error(loc, f"'{target}' is '{kind}'; an enqueue target must be autonomous")


def _check_release(family: FamilyFile, issues: Issues) -> None:
    fence = family.verbs.release
    if family.name not in PLATFORM_ALLOWLIST:
        issues.error(
            "verbs.release",
            f"'{family.name}' is outside the platform fence; only "
            f"{', '.join(sorted(PLATFORM_ALLOWLIST))} may hold the 'release' verb or mount "
            "the platform root",
        )

    if fence is None or not fence.components:
        issues.error("verbs.release.components", "release needs at least one component name")
        return

    for position, component in enumerate(fence.components):
        if component not in RELEASABLE | RETIRING_NAMES:
            issues.error(
                f"verbs.release.components[{position}]",
                f"'{component}' is not a releasable component; contract 06 §1 names "
                f"{', '.join(sorted(RELEASABLE))}",
            )


def _check_delegates(family: FamilyFile, index: Index, issues: Issues) -> None:
    kind = _kind_of(family)
    if kind is Kind.THIN and family.delegates:
        issues.error(
            "delegates",
            "a thin family's delegates must be empty, so a chain is at most one hop and cannot "
            "loop",
        )

    seen: set[str] = set()
    for position, target in enumerate(family.delegates):
        loc = f"delegates[{position}]"
        if target in seen:
            issues.error(loc, f"'{target}' is named twice")

        seen.add(target)
        if target == family.name:
            issues.error(loc, "a family may not delegate to itself")
            continue

        target_kind = index.kinds.get(target)
        if target_kind is None:
            issues.error(loc, f"'{target}' is not a family in this registry")
            continue

        if target_kind != Kind.THIN:
            issues.error(
                loc, f"'{target}' exists but its kind is '{target_kind}'; a delegate must be thin"
            )


def _check_inflight_delegations(family: FamilyFile, issues: Issues) -> None:
    """§3.6.1. The caller family's own cap on delegate calls in flight."""
    cap = family.max_inflight_delegations
    if not MAX_INFLIGHT_DELEGATIONS_MIN <= cap <= MAX_INFLIGHT_DELEGATIONS_MAX:
        issues.error(
            "max_inflight_delegations",
            f"{cap} is outside {MAX_INFLIGHT_DELEGATIONS_MIN} to {MAX_INFLIGHT_DELEGATIONS_MAX}",
        )
        return

    # §3.6.1 rule 3: the number only bites on a delegate call, so with no
    # delegate it bounds nothing. A warning, never an error: emptying
    # `delegates` must not make a family invalid over a leftover number.
    if cap != DEFAULT_MAX_INFLIGHT_DELEGATIONS and not family.delegates:
        issues.warn(
            "max_inflight_delegations",
            f"'delegates' is empty, so there is no delegate call to bound; {cap} changes nothing",
        )


_EGRESS_MSG: Final[dict[EgressProblem, str]] = {
    EgressProblem.IP_LITERAL: "is an IP literal; name a hostname, so an egress list can never "
    "quietly reach a LAN host",
    EgressProblem.WILDCARD: "holds a wildcard, which is ungrantable",
    EgressProblem.BAD_PORT: "has a port outside 1 to 65535",
    EgressProblem.BAD_HOSTNAME: "is not a hostname or hostname:port",
}


def _check_egress(family: FamilyFile, issues: Issues) -> None:
    for position, entry in enumerate(family.egress):
        problem = egress_problem(entry)
        if problem is not None:
            issues.error(f"egress[{position}]", f"'{entry}' {_EGRESS_MSG[problem]}")


def _check_runtime(family: FamilyFile, issues: Issues) -> None:
    """`shell`, `sandbox_tools` and `system_prompt` (§3.8). None is part of
    the perimeter, so a wrong value costs behaviour, not reach."""
    allowed = tuple(str(tool) for tool in SandboxTool)
    seen: set[str] = set()
    for position, tool in enumerate(family.sandbox_tools):
        loc = f"sandbox_tools[{position}]"
        if tool == "bash":
            issues.error(
                loc, "'bash' is never a sandbox tool; 'shell: true' is the only way to get one"
            )
            continue

        if tool not in allowed:
            issues.error(loc, f"'{tool}' is not a sandbox tool; use one of {', '.join(allowed)}")
            continue

        if tool in seen:
            issues.error(loc, f"'{tool}' is listed twice")

        seen.add(tool)

    modes = tuple(str(mode) for mode in SystemPrompt)
    if family.system_prompt not in modes:
        issues.error(
            "system_prompt",
            f"'{family.system_prompt}' is not a system prompt mode; use one of {', '.join(modes)}",
        )


def _check_sandbox(family: FamilyFile, issues: Issues) -> None:
    box = family.sandbox
    if box.image not in SANDBOX_FLAVORS:
        # §3.9: a closed set, so the message names every member. A value
        # that looks like an image reference lands here too, which is the
        # point: invariant 10 keeps the choice of what code runs with the
        # platform, not with a registry file.
        issues.error(
            "sandbox.image",
            f"'{box.image}' is not an image flavor; use one of {', '.join(SANDBOX_FLAVORS)}",
        )

    if not CPUS_MIN <= box.cpus <= CPUS_MAX:
        issues.error("sandbox.cpus", f"{box.cpus} is outside {CPUS_MIN} to {CPUS_MAX}")

    megabytes = memory_mb(box.memory)
    if megabytes is None:
        issues.error("sandbox.memory", f"'{box.memory}' must match [1-9][0-9]*[mg]")
    elif not MEMORY_MIN_MB <= megabytes <= MEMORY_MAX_MB:
        issues.error("sandbox.memory", f"'{box.memory}' is outside 256m to 16g")

    resident = box.max_resident_processes
    if not RESIDENT_PROCS_MIN <= resident <= RESIDENT_PROCS_MAX:
        issues.error(
            "sandbox.max_resident_processes",
            f"{resident} is outside {RESIDENT_PROCS_MIN} to {RESIDENT_PROCS_MAX}",
        )
        return

    if megabytes is None:
        return

    # §3.9: raising it above what `memory` can carry is a warning, not an
    # error. The playpen reaps under pressure either way.
    carried = max(0, (megabytes - PLAYPEN_MB) // PI_PROCESS_MB)
    if resident > carried:
        issues.warn(
            "sandbox.max_resident_processes",
            f"{resident} held-open processes need about {resident * PI_PROCESS_MB + PLAYPEN_MB}"
            f" MB; '{box.memory}' carries about {carried}",
        )


def _check_skills(family: FamilyFile, index: Index, issues: Issues) -> None:
    seen: set[str] = set()
    for position, skill in enumerate(family.skills):
        loc = f"skills[{position}]"
        if skill in seen:
            issues.error(loc, f"'{skill}' is granted twice")

        seen.add(skill)
        if SKILL_NAME.fullmatch(skill) is None:
            issues.error(loc, f"'{skill}' is not a skill name; use [a-z][a-z0-9-]{{1,30}}")
            continue

        if skill not in index.skills:
            issues.error(loc, f"'{skill}' has no skills/{skill}/SKILL.md")


def _check_approval(family: FamilyFile, index: Index, issues: Issues) -> None:
    """§3.11. Every entry names something this file grants, and no two entries
    overlap: protection that does not exist reads as protection."""
    servers_covered: set[str] = set()
    seen: set[str] = set()
    for position, entry in enumerate(family.approval):
        loc = f"approval[{position}]"
        if entry in seen:
            issues.error(loc, f"'{entry}' is listed twice")
            continue

        seen.add(entry)
        if CALL_SEPARATOR not in entry:
            _check_approval_verb(family, entry, loc, issues)
            continue

        server, _, tool = entry.partition(CALL_SEPARATOR)
        granted = index.granted_tools(family, server)
        if server not in family.tools:
            issues.error(loc, f"'{server}' is not a server this file grants")
            continue

        if tool == APPROVAL_WILDCARD:
            servers_covered.add(server)
            continue

        if tool not in granted:
            issues.error(loc, f"'{entry}' is not a tool this file grants from '{server}'")

    _check_overlaps(family, servers_covered, issues)


def _check_approval_verb(family: FamilyFile, entry: str, loc: str, issues: Issues) -> None:
    if entry == INVOKE_AGENT:
        if not family.delegates:
            issues.error(loc, "invoke_agent is granted by a non-empty 'delegates', which is empty")
        return

    if entry not in family.verbs.granted():
        issues.error(loc, f"'{entry}' is not a verb this file grants")


def _check_overlaps(family: FamilyFile, servers_covered: set[str], issues: Issues) -> None:
    for position, entry in enumerate(family.approval):
        server, separator, tool = entry.partition(CALL_SEPARATOR)
        if not separator or tool == APPROVAL_WILDCARD or server not in servers_covered:
            continue

        issues.error(
            f"approval[{position}]",
            f"'{entry}' overlaps '{server}{CALL_SEPARATOR}{APPROVAL_WILDCARD}'; keep one of them",
        )


def _check_kind_fields(family: FamilyFile, issues: Issues) -> None:
    kind = _kind_of(family)
    if kind is None:
        return

    _check_job(family, kind, issues)
    _check_triggers(family, kind, issues)
    _check_running_turns(family, kind, issues)


def _check_job(family: FamilyFile, kind: Kind, issues: Issues) -> None:
    if family.job is None:
        return

    if kind is not Kind.THIN:
        issues.error("job", f"'job' is thin only; this family is '{kind}'")
        return

    seconds = duration_s(family.job.timeout)
    if seconds is None:
        issues.error("job.timeout", f"'{family.job.timeout}' must match [1-9][0-9]*[smh]")
        return

    if not JOB_TIMEOUT_MIN_S <= seconds <= JOB_TIMEOUT_MAX_S:
        issues.error("job.timeout", f"'{family.job.timeout}' is outside 1s to 1h")
        return

    if seconds > DELEGATE_LIMIT_S:
        issues.warn(
            "job.timeout",
            f"'{family.job.timeout}' is past the PEP's {DELEGATE_LIMIT_S}s delegate limit; the "
            "caller gives up first",
        )


def _check_triggers(family: FamilyFile, kind: Kind, issues: Issues) -> None:
    if kind is not Kind.AUTONOMOUS:
        if family.triggers is not None:
            issues.error("triggers", f"'triggers' is autonomous only; this family is '{kind}'")
        return

    if not family.triggers:
        issues.error("triggers", "an autonomous family needs at least one trigger")
        return

    dispatch_seen = False
    for position, trigger in enumerate(family.triggers):
        loc = f"triggers[{position}]"
        named = sum(
            1 for key in (trigger.cron, trigger.webhook, trigger.enqueue) if key is not None
        )
        if named != 1:
            issues.error(loc, "a trigger takes exactly one of 'cron', 'webhook' or 'enqueue'")
            continue

        if trigger.cron is not None:
            _check_cron(trigger.cron, loc, issues)
            continue

        if trigger.enqueue is not None:
            dispatch_seen = _check_dispatch(trigger.enqueue, dispatch_seen, loc, issues)
            continue

        if trigger.webhook is not None and FAMILY_NAME.fullmatch(trigger.webhook) is None:
            issues.error(f"{loc}.webhook", f"'{trigger.webhook}' must match [a-z][a-z0-9-]{{1,30}}")


def _check_dispatch(enabled: bool, already_seen: bool, loc: str, issues: Issues) -> bool:
    """§3.13 rules 5 and 6. `enqueue: true` is the only legal value, and one
    entry says all there is to say."""
    if not enabled:
        issues.error(
            f"{loc}.enqueue",
            "'enqueue: false' is not a trigger; absence is denial, so write no entry instead",
        )
        return already_seen

    if already_seen:
        issues.error(loc, "a second 'enqueue' trigger says the same thing twice; keep one")

    return True


def _check_cron(expression: str, loc: str, issues: Issues) -> None:
    if expression in CRON_SHORTHANDS:
        return

    if len(expression.split()) != CRON_FIELDS:
        issues.error(
            f"{loc}.cron",
            f"'{expression}' is not a five-field cron expression or one of "
            f"{', '.join(CRON_SHORTHANDS)}",
        )


def _check_running_turns(family: FamilyFile, kind: Kind, issues: Issues) -> None:
    turns = family.max_running_turns
    if turns is None:
        return

    if kind is not Kind.AUTONOMOUS:
        issues.error(
            "max_running_turns", f"'max_running_turns' is autonomous only; this family is '{kind}'"
        )
        return

    if not MAX_RUNNING_TURNS_MIN <= turns <= MAX_RUNNING_TURNS_MAX:
        issues.error(
            "max_running_turns",
            f"{turns} is outside {MAX_RUNNING_TURNS_MIN} to {MAX_RUNNING_TURNS_MAX}",
        )


def _check_quiet(family: FamilyFile, index: Index, issues: Issues) -> None:
    """§3.15. Every name the block reads is one this file grants, so the
    check a cron firing runs can never ask for something the family could
    not ask for itself."""
    quiet = family.quiet
    kind = _kind_of(family)
    if quiet is None or kind is None:
        return

    if kind is not Kind.AUTONOMOUS:
        issues.error("quiet", f"'quiet' is autonomous only; this family is '{kind}'")
        return

    if not any(trigger.cron is not None for trigger in family.triggers or ()):
        issues.warn("quiet", "'quiet' checks cron firings, and this family has no cron trigger")

    # §3.15 item 2: a family that starts jobs is never quiet while one runs,
    # and `job_status` is the only way it can see them.
    granted = family.verbs.granted()
    if Verb.ENQUEUE in granted and Verb.JOB_STATUS not in granted:
        issues.error(
            "quiet",
            f"'quiet' reads this family's jobs with '{Verb.JOB_STATUS}', which this file does "
            "not grant",
        )

    if quiet.board is not None:
        _check_quiet_board(family, quiet.board, index, issues)

    if quiet.daily is not None:
        _check_daily(family, quiet.daily, index, issues)

    if not QUIET_FLOOR_MIN_HOURS <= quiet.floor_hours <= QUIET_FLOOR_MAX_HOURS:
        issues.error(
            "quiet.floor_hours",
            f"{quiet.floor_hours} is outside {QUIET_FLOOR_MIN_HOURS} to {QUIET_FLOOR_MAX_HOURS}",
        )


def _check_quiet_board(family: FamilyFile, server: str, index: Index, issues: Issues) -> None:
    if server not in family.tools:
        issues.error("quiet.board", f"'{server}' is not a server this file grants")
        return

    if SURVEY_TOOL not in index.granted_tools(family, server):
        issues.error(
            "quiet.board", f"'{server}' does not grant '{SURVEY_TOOL}', which 'quiet.board' reads"
        )


def _check_daily(family: FamilyFile, daily: DailyCall, index: Index, issues: Issues) -> None:
    """The call is read from the audit, so it must be one the PEP can record
    for this family: a granted verb or a granted `<server>__<tool>`."""
    if not _granted_call(family, daily.call, index):
        issues.error(
            "quiet.daily.call",
            f"'{daily.call}' is not a verb or a '<server>{CALL_SEPARATOR}<tool>' this file grants",
        )

    if not 0 <= daily.hour <= LOCAL_HOUR_MAX:
        issues.error("quiet.daily.hour", f"{daily.hour} is outside 0 to {LOCAL_HOUR_MAX}")

    try:
        ZoneInfo(daily.zone)
    except (KeyError, ValueError, OSError):
        issues.error("quiet.daily.zone", f"'{daily.zone}' is not an IANA time zone")


def _granted_call(family: FamilyFile, call: str, index: Index) -> bool:
    server, separator, tool = call.partition(CALL_SEPARATOR)
    if not separator:
        return call in family.verbs.granted()

    return tool in index.granted_tools(family, server)
