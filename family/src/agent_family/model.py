"""`family.yaml` schema v1 (contract 01 §2, §3).

These models fix the SHAPE only: which fields exist, their types, and that an
unknown field is a refusal. Every value rule — patterns, ranges, fences,
cross-references — lives in `validate.py`, so one parse can report every
violation it can see instead of stopping at the first type error with a
message nobody wrote."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from .grammar import (
    ALL_TOOLS,
    DEFAULT_CPUS,
    DEFAULT_JOB_TIMEOUT,
    DEFAULT_MAX_INFLIGHT_DELEGATIONS,
    DEFAULT_MEMORY,
    DEFAULT_QUIET_FLOOR_HOURS,
    DEFAULT_RESIDENT_PROCS,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_TOOLS,
    SystemPrompt,
)

#: One tool grant: a list of tool names, or the literal `all` (contract 01
#: §3.4). `all` resolves against the server file at read time, which is why
#: `caregiver` expands it before the PEP ever sees it (contract 04 §1.2).
ToolGrant = list[str] | str


class Strict(BaseModel):
    """Unknown field = refusal, everywhere (contract 01 §7 rule 1)."""

    # CONTRACT-QUESTION: contract 01 §2 and contract 01b give a type for each
    # field and do not say how strict the read of a type is. These models
    # read in the lax mode of pydantic: `"2"` is the integer 2, `"yes"` is
    # true and `true` is the number 1. Root refuses such a value in a server
    # file. The strict mode refuses a file that passes today.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelBlock(Strict):
    router: str
    budget_usd_per_day: float


class FileMount(Strict):
    path: str
    mode: str


class SandboxBlock(Strict):
    cpus: int = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY
    max_resident_processes: int = DEFAULT_RESIDENT_PROCS
    # A FLAVOR the platform builds, never an image reference (contract 01
    # §3.9). `validate.py` holds it to the closed set.
    image: str = DEFAULT_SANDBOX_IMAGE


class JobBlock(Strict):
    timeout: str = DEFAULT_JOB_TIMEOUT


class Trigger(Strict):
    """Exactly one key of the three. Two, or none, is an error
    (contract 01 §3.13). `enqueue` is the dispatch form: another family's
    `enqueue` verb starts the session, so this form opens no route and needs
    no token."""

    cron: str | None = None
    webhook: str | None = None
    enqueue: bool | None = None


class DailyCall(Strict):
    """One call owed once a day, from a local hour on (contract 01 §3.15).
    `call` is the approval grammar's `<server>__<tool>` or a verb."""

    call: str
    hour: int
    zone: str
    weekdays_only: bool = False


class QuietBlock(Strict):
    """What a cron firing checks before it wakes the family (contract 01
    §3.15). Every field is optional, so the noticeboard can probe an empty
    block for the rule that forbids it."""

    board: str | None = None
    daily: DailyCall | None = None
    floor_hours: int = DEFAULT_QUIET_FLOOR_HOURS


class HaTriple(Strict):
    domain: str
    service: str
    entity_id: str | None = None


class NoFence(Strict):
    """`embed` and `job_status` take `{}`. Nothing may ride along."""


class HaCallFence(Strict):
    allow: list[HaTriple]


class EnqueueFence(Strict):
    targets: list[str]


class ReleaseFence(Strict):
    components: list[str]


class VerbsBlock(Strict):
    """The five v1 verbs. An unknown verb name is refused by `extra=forbid`,
    which is contract 01 §3.5 rule 1: a new verb arrives with a new version of
    the contract and a PEP release, never as a typo that silently does
    nothing."""

    embed: NoFence | None = None
    ha_call: HaCallFence | None = None
    enqueue: EnqueueFence | None = None
    job_status: NoFence | None = None
    release: ReleaseFence | None = None

    def granted(self) -> tuple[str, ...]:
        """The verb names this file grants, in contract order."""
        return tuple(name for name, fence in self.__dict__.items() if fence is not None)


class FamilyFile(Strict):
    name: str
    kind: str
    description: str
    model: ModelBlock
    files: list[FileMount] = Field(default_factory=list[FileMount])
    tools: dict[str, ToolGrant] = Field(default_factory=dict[str, ToolGrant])
    verbs: VerbsBlock = Field(default_factory=VerbsBlock)
    delegates: list[str] = Field(default_factory=list[str])
    # How many of those delegate calls may run at once (contract 01 §3.6.1).
    # It sits beside the list it bounds, and `caregiver` copies it into the
    # grant file the PEP re-reads per call (contract 04 §1.2).
    max_inflight_delegations: int = DEFAULT_MAX_INFLIGHT_DELEGATIONS
    egress: list[str] = Field(default_factory=list[str])
    shell: bool = False
    sandbox_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_SANDBOX_TOOLS))
    system_prompt: str = SystemPrompt.APPEND
    sandbox: SandboxBlock = Field(default_factory=SandboxBlock)
    skills: list[str] = Field(default_factory=list[str])
    approval: list[str] = Field(default_factory=list[str])
    # Kind-fenced. Present on the wrong kind is an error naming the kind, so
    # the fix is one edit (contract 01 §2).
    job: JobBlock | None = None
    triggers: list[Trigger] | None = None
    max_running_turns: int | None = None
    quiet: QuietBlock | None = None

    def tool_names(self, server: str) -> tuple[str, ...]:
        """The named tools granted from one server. `all` answers empty: the
        caller expands it against the server file, never this model."""
        grant = self.tools.get(server)
        if not isinstance(grant, list):
            return ()

        return tuple(grant)

    def grants_all(self, server: str) -> bool:
        return self.tools.get(server) == ALL_TOOLS


#: Every top-level field name, for the "closest known field" hint.
FAMILY_FIELDS: Final = tuple(FamilyFile.model_fields)
VERB_FIELDS: Final = tuple(VerbsBlock.model_fields)
