"""Names, ranges and the small parsers every check shares.

One pattern per identifier, spelled once. A second copy of a regex is a second
meaning waiting to drift from the contract that fixed it."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

# --- identifiers (contract 01 §2) ---

#: `[a-z][a-z0-9-]{1,30}`: 2 to 31 characters, hyphens, never an underscore.
#: The PEP's call name is `<server>__<tool>` and it splits on the double
#: underscore (contract 01 §3.4 rule 6), so neither half may carry one.
FAMILY_NAME: Final = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
SERVER_NAME: Final = FAMILY_NAME
WEBHOOK_NAME: Final = FAMILY_NAME

#: Contract 01 §3.1: the one name no family may take. `bin/rework-gate-1b.sh`
#: and `bin/rework-gate-2.sh` prove invariant 9 by writing a grant file under
#: this name, calling the PEP, deleting the file and calling again. Run
#: against a real family's name, that probe overwrites the family's grants and
#: then revokes them, and the PEP raises a turn-blocking `grants_stale` on it.
#: The grammar accepts it, so only a refusal here keeps the name free.
PROBE_FAMILY: Final = "gate-probe"
TOOL_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\Z")
SKILL_NAME: Final = FAMILY_NAME
MODEL_ALIAS: Final = re.compile(r"^[a-z0-9][a-z0-9._/-]*\Z")
MOUNT_PATH: Final = re.compile(r"^[A-Za-z0-9._/-]+\Z")
ENV_VAR_NAME: Final = re.compile(r"^[A-Z][A-Z0-9_]*\Z")
HA_IDENTIFIER: Final = re.compile(r"^[a-z][a-z0-9_]*\Z")
HA_ENTITY_ID: Final = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+\Z")
SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}\Z")
GITHUB_REPO: Final = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
#: An exact version, never a range: a range makes a hash meaningless
#: (contract 01b §3.1).
EXACT_VERSION: Final = re.compile(r"^[0-9][0-9A-Za-z.+-]*\Z")
VERSION_RANGE_CHARS: Final = ("*", "^", "~", ">", "<", "=", ",", " ")

# --- ranges (contract 01 §3) ---

BUDGET_USD_MAX: Final = 500.0
CPUS_MIN: Final = 1
CPUS_MAX: Final = 8
MEMORY_MIN_MB: Final = 256
MEMORY_MAX_MB: Final = 16 * 1024
RESIDENT_PROCS_MIN: Final = 1
RESIDENT_PROCS_MAX: Final = 32
JOB_TIMEOUT_MIN_S: Final = 1
JOB_TIMEOUT_MAX_S: Final = 3600
MAX_RUNNING_TURNS_MIN: Final = 1
MAX_RUNNING_TURNS_MAX: Final = 8
#: Contract 01 §3.6.1. The ceiling is `max_running_turns`'s, for the reason
#: §3.6.1 gives: one caller must not alone exhaust a target family's default
#: budget of 12 held-open pi processes (§3.9).
MAX_INFLIGHT_DELEGATIONS_MIN: Final = 1
MAX_INFLIGHT_DELEGATIONS_MAX: Final = 8
PORT_MIN: Final = 1
PORT_MAX: Final = 65535
DESCRIPTION_MAX: Final = 200
IDENTITY_MAX: Final = 300
CRON_FIELDS: Final = 5

#: Contract 01 §3.12: a `job.timeout` past this is a warning, because the PEP
#: gives up on a delegate call first (contract 04 §7.5).
DELEGATE_LIMIT_S: Final = 120


class Kind(StrEnum):
    ATTENDED = "attended"
    THIN = "thin"
    AUTONOMOUS = "autonomous"


class Mode(StrEnum):
    RO = "ro"
    RW = "rw"


class SandboxTool(StrEnum):
    """Contract 01 §3.8. `bash` is deliberately absent: `shell` is the only
    way to get one, so one field answers "can it run commands"."""

    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    GREP = "grep"
    FIND = "find"
    LS = "ls"
    # pi's built-in extension that runs model-written JavaScript over the
    # other tools the family holds. It adds no reach: a script calls only
    # what the model could call itself, and every PEP call is still decided.
    CODEMODE = "codemode"


class SystemPrompt(StrEnum):
    """Contract 01 §3.8. How `instructions.md` meets pi's own system prompt.

    pi's own prompt opens with "You are an expert coding assistant". That
    suits a coding family and misleads a chat."""

    APPEND = "append"
    REPLACE = "replace"


class SandboxFlavor(StrEnum):
    """Contract 01 §3.9: which image the platform builds this sandbox from.

    A CLOSED set, not an image reference. Invariant 10 says only the
    platform changes the platform, and a registry file that could name an
    image would choose what code runs inside the microVM. `playpen/
    Dockerfile` has one target per member, and `caregiver` is started with
    one image reference per member (contract 05 §4.1)."""

    BASE = "base"
    PYTHON = "python"


#: Every flavor, in declaration order, for a refusal that names the set.
SANDBOX_FLAVORS: Final = tuple(str(one) for one in SandboxFlavor)

DEFAULT_SANDBOX_TOOLS: Final = ("read", "grep", "find", "ls")
DEFAULT_CPUS: Final = 2
DEFAULT_MEMORY: Final = "2g"
#: node, pi and ripgrep, with no language runtime beyond them. A family
#: that needs pandas asks for `python` (contract 01 §3.9).
DEFAULT_SANDBOX_IMAGE: Final = str(SandboxFlavor.BASE)
DEFAULT_RESIDENT_PROCS: Final = 12
DEFAULT_JOB_TIMEOUT: Final = "120s"
DEFAULT_MAX_RUNNING_TURNS: Final = 1
#: Contract 01 §3.6.1 and contract 04 §1.2. `caregiver` copies it into the
#: grant file, so this is the one place the number 2 is written down on the
#: writing side.
DEFAULT_MAX_INFLIGHT_DELEGATIONS: Final = 2


class Verb(StrEnum):
    """The five v1 verbs (contract 01 §3.5). `invoke_agent` is not here: the
    PEP synthesizes it from `delegates`, so the file has one source for who
    may be called."""

    EMBED = "embed"
    HA_CALL = "ha_call"
    ENQUEUE = "enqueue"
    JOB_STATUS = "job_status"
    RELEASE = "release"


class InstallSource(StrEnum):
    PYPI = "pypi"
    GITHUB_RELEASE = "github-release"
    AGENT_MCP = "agent-mcp"


#: Contract 01 §3.4: `all` means every tool the server file declares at read
#: time.
ALL_TOOLS: Final = "all"

#: Contract 01b §4.1 rule 2: a variable whose name holds one of these must use
#: `secret:`. A literal there is the one mistake that puts a credential in git.
SECRET_NAME_MARKERS: Final = ("TOKEN", "KEY", "PASSWORD", "SECRET")
SECRET_PREFIX: Final = "secret:"

#: Contract 01 §3.13: the three shorthands a cron trigger may use.
CRON_SHORTHANDS: Final = ("@hourly", "@daily", "@weekly")

#: One field of a five-field cron expression: ASCII digits and the signs of
#: a list, a range and a step. `caregiver.timers` converts no other character.
CRON_FIELD: Final = re.compile(r"^[0-9*,/-]+\Z")

#: Contract 01 §3.15: the tool `quiet.board` fingerprints, on the server it
#: names.
SURVEY_TOOL: Final = "survey_board"

#: Contract 01 §3.15: a board nothing touches still gets one full wake this
#: often, so a missed signal costs at most this long.
DEFAULT_QUIET_FLOOR_HOURS: Final = 24
QUIET_FLOOR_MIN_HOURS: Final = 1
QUIET_FLOOR_MAX_HOURS: Final = 7 * 24
LOCAL_HOUR_MAX: Final = 23

# --- host facts held by the manager, not by the registry ---

#: Contract 01 §5.4. A family file cannot add a root. This lives in code
#: because the registry must not be able to widen it.
ALLOWED_ROOTS: Final = (
    "/srv/agents/vault",
    "/srv/agents/code",
    "/srv/agents/state/index",
    "/srv/agents/work/projects",
    "/srv/agents/work/platform",
    "/srv/agents/work/code-sandbox",
)

PLATFORM_ROOT: Final = "/srv/agents/work/platform"

#: Contract 01 §5.5 rule 1: a constant in code, released with `caregiver`, and
#: never a registry file. A fence the fenced thing can edit is not a fence.
PLATFORM_ALLOWLIST: Final = frozenset({"agent-control"})

#: Contract 01 §5.5 rule 6. The host's OWN clones of the three platform repos,
#: which a platform family may mount READ-ONLY as the fetch source for its
#: working copies. The sandbox holds no git credential, the repos are private,
#: and nothing on the host may run git inside `work/platform` (a sandbox can
#: write `.git/config` there). Same three repos, so `docs/rework/spec.md`
#: §4.4's "nothing else" still holds. Exact paths: no corpus root, no fourth
#: repo, no sub-path.
PLATFORM_MIRRORS: Final = frozenset(
    {
        "/srv/agents/code/agent-control",
        "/srv/agents/code/agent-mcp",
        "/srv/agents/code/agent-registry",
    }
)

#: Contract 01 §5.5 rule 7: the platform's own GitHub server, and the tools no
#: family is ever granted from it. A registry's `main` may carry no branch
#: protection, and a merge there lands a change `caregiver` applies with nobody
#: looking. The platform family proposes and the operator lands.
PLATFORM_SERVER: Final = "github-platform"
PLATFORM_WITHHELD: Final = frozenset({"merge_pull_request"})

#: Contract 01 §3.3 rule 6: the runtime mounts the session store. A family file
#: that declares it is claiming what is not its own (invariant 5).
SESSION_ROOT: Final = "/srv/agents/sessions"


def is_under(path: str, root: str) -> bool:
    """Segment-wise prefix. `/srv/agents/work/platform-x` is NOT under
    `/srv/agents/work/platform`, which a string prefix would get wrong."""
    if path == root:
        return True

    return path.startswith(root + "/")


#: The most digits of a count that `int` reads here. Each range of a count
#: in this file ends below six digits. `int` raises on a run of digits past
#: the digit limit of the interpreter, so a longer run reads as
#: `_COUNT_PAST_RANGE`, and the range check of the caller refuses it.
_COUNT_DIGITS_MAX: Final = 18
_COUNT_PAST_RANGE: Final = 10**_COUNT_DIGITS_MAX


def _count(digits: str) -> int:
    """A run of ASCII digits as a number, or a number past each range."""
    if len(digits) > _COUNT_DIGITS_MAX:
        return _COUNT_PAST_RANGE

    return int(digits)


def memory_mb(value: str) -> int | None:
    """`[1-9][0-9]*[mg]` to megabytes. None when the spelling is wrong."""
    match = re.fullmatch(r"([1-9][0-9]*)([mg])", value)
    if match is None:
        return None

    size = _count(match.group(1))
    return size * 1024 if match.group(2) == "g" else size


_DURATION_SECONDS: Final = {"s": 1, "m": 60, "h": 3600}


def duration_s(value: str) -> int | None:
    """`[1-9][0-9]*[smh]` to seconds. None when the spelling is wrong."""
    match = re.fullmatch(r"([1-9][0-9]*)([smh])", value)
    if match is None:
        return None

    return _count(match.group(1)) * _DURATION_SECONDS[match.group(2)]


_IPV4: Final = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}\Z")
_HOSTNAME: Final = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\Z")
#: ASCII digits, and five at most after the zeros at the start. `str.isdigit`
#: also takes a digit that is not ASCII, and `int` raises on some of those
#: and on a run past the digit limit of the interpreter.
_PORT: Final = re.compile(r"^0*([0-9]{1,5})\Z")


class EgressProblem(StrEnum):
    """Why one `egress` entry is refused (contract 01 §3.7)."""

    IP_LITERAL = "ip_literal"
    WILDCARD = "wildcard"
    BAD_PORT = "bad_port"
    BAD_HOSTNAME = "bad_hostname"


def _is_port(port: str) -> bool:
    """Contract 01 §3.7 rule 3: a number from 1 to 65535."""
    match = _PORT.fullmatch(port)
    if match is None:
        return False

    return PORT_MIN <= int(match.group(1)) <= PORT_MAX


def egress_problem(entry: str) -> EgressProblem | None:
    """`hostname` or `hostname:port`. None when the entry is allowed."""
    host, colon, port = entry.partition(":")
    if "*" in entry:
        return EgressProblem.WILDCARD

    if _IPV4.fullmatch(host) is not None or ":" in port:
        # A bare IPv4, or an IPv6 literal, whose extra colons land in `port`.
        return EgressProblem.IP_LITERAL

    if colon and not _is_port(port):
        return EgressProblem.BAD_PORT

    labels = host.split(".")
    if not host or any(_HOSTNAME.fullmatch(label) is None for label in labels):
        return EgressProblem.BAD_HOSTNAME

    return None


def closest_name(unknown: str, known: tuple[str, ...]) -> str | None:
    """The nearest known field name, for an unknown-field message (contract 01
    §7 rule 1). difflib is imported here because this is its only caller."""
    import difflib

    matches = difflib.get_close_matches(unknown, known, n=1, cutoff=0.6)
    return matches[0] if matches else None
