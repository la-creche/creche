# Generated from rust/crates/wire/schema/wire.json. Do not edit.
# To write it again, run `uv run datamodel-codegen` at the root of the repository.

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
)


class AcpError1(BaseModel):
    """
    The request has no `Upgrade` header, or the header does not
    name `AcpRoute::TOKEN` (decision CH26 rule 3). The vmm also
    sends `Upgrade: creche-acp` with this error (RFC 9110).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["upgrade_required"]


class AcpError10(BaseModel):
    """
    The body is over the cap.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["too_large"]


class AcpError11(BaseModel):
    """
    The body is not strict JSON.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_json"]


class AcpError12(BaseModel):
    """
    The body is JSON, but not the request type of the route.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_valid"]


class AcpError13(BaseModel):
    """
    The vmm failed after it took the request. The vmm logs
    the details.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["internal"]


class AcpError2(BaseModel):
    """
    No row has the id, the row belongs to another family, or a drop
    of the session runs (decisions CH21-9, CH33 rule 5).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_found"]


class AcpError3(BaseModel):
    """
    The session has an open ACP stream (decision CH33 rule 7).
    attendance opens one stream per session.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["busy"]


class AcpError4(BaseModel):
    """
    The VM does not fit on the host, and no idle VM can make room
    (decisions CH8-6, CH21-3, CH21-9). The vmm never queues: the
    client retries.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["host_full"]


class AcpError5(BaseModel):
    """
    The VM did not boot or restore, or guest init sent no
    `ControlReady` (decisions CH6-3, CH33 rule 6). The vmm also
    writes the fault to its fault file (decision CH21-1).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["sandbox_start_failed"]


class AcpError6(BaseModel):
    """
    The VM failed the deny-by-default check of its start (decisions
    CH6-3, CH33 rule 6). The vmm also writes the fault to its fault
    file (decision CH21-1).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["egress_assert_failed"]


class AcpError7(BaseModel):
    """
    No route has this path.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["no_route"]


class AcpError8(BaseModel):
    """
    The route has this path, but the method is not `POST`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_method"]


class AcpError9(BaseModel):
    """
    The `Content-Type` is not `application/json`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_content_type"]


class AcpError(
    RootModel[
        AcpError1
        | AcpError2
        | AcpError3
        | AcpError4
        | AcpError5
        | AcpError6
        | AcpError7
        | AcpError8
        | AcpError9
        | AcpError10
        | AcpError11
        | AcpError12
        | AcpError13
    ]
):
    root: Annotated[
        AcpError1
        | AcpError2
        | AcpError3
        | AcpError4
        | AcpError5
        | AcpError6
        | AcpError7
        | AcpError8
        | AcpError9
        | AcpError10
        | AcpError11
        | AcpError12
        | AcpError13,
        Field(description="The error of the route `acp`. The vmm sends each code before 101."),
    ]


class Auto(BaseModel):
    """
    The origin of a session that attendance opened for one autonomous job.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    job: Annotated[
        AutoJobId,
        Field(
            description="The id that attendance gives the job. The `SessionId` of the row is a\nseparate value."
        ),
    ]
    trigger: Annotated[Trigger, Field(description="What fired the job.")]


class AutoJobId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id that attendance gives an autonomous job of a trigger or of the\ndispatch door: `auto-<ulid>` (contract 02 §2 and §13). Contract 02 calls\nit a session id. The `SessionId` of the job's row on the host session\nlist is a separate value.",
            pattern="^auto-[0-9A-HJKMNP-TV-Z]{26}$",
        ),
    ]


class AutoOrigin(BaseModel):
    """
    attendance opened the session for one autonomous job.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    auto: Auto


class ConfigMissing(BaseModel):
    """
    The reply of a pull when `config/` is absent. It carries nothing.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class ConfigMissingReply(BaseModel):
    """
    `config/` is absent.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    config_missing: ConfigMissing


class ControlHello(BaseModel):
    """
    What the vmm sends first on each new control link, guest port 1200
    (decisions CH13, CH14). A new link replaces the old one (decision CH19
    rule 2). Guest init sets `CLOCK_REALTIME` to `sent_at` with
    `clock_settime` before it answers with `ControlReady` (decision CH28),
    because after a restore the guest clock is behind by the time the VM
    spent as a snapshot (fact 73).

    The hello carries no family name and no protocol version (decision CH32
    rules 1 and 2). The vmm writes guest init at each cold boot (decision
    CH7-6). A restore brings back the guest init of the snapshot, so the
    vmm restores a snapshot only when its key holds the same guest-init
    protocol version (decision CH7-6). So the two never skew in protocol.
    A later hello can name a session that differs from the first one. Guest
    init then closes the link, and the vmm records a fault and cold-boots
    the VM (decision CH32 rule 3). No wire type carries that case.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    sent_at: Annotated[
        UnixMillis, Field(description="The wall time of the vmm when it sent the hello.")
    ]
    session: Annotated[
        SessionId,
        Field(
            description="The session that the VM serves (decisions CH1, CH5e). The facade\nopens this one session at start, and creates it when it is new\n(decisions CH21-8, CH25 rule 2)."
        ),
    ]


class ControlReady(BaseModel):
    """
    What guest init answers to `ControlHello`, after it sets its clock
    (decisions CH28, CH32). It holds the facade state (decision CH19 rule 2).
    The last turn end sits in the facade state. The answer stays an object,
    so a later field has a place.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    facade: Annotated[FacadeState, Field(description="The facade.")]


class ControlStop(BaseModel):
    """
    The graceful stop. It carries nothing.

    It is a type of its own so that the generated Python model is a class
    with no field, as `TurnStart` is.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class ControlStopCommand(BaseModel):
    """
    Stop the facade, sync and power off.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    stop: ControlStop


class ControlCommand(RootModel[ControlStopCommand]):
    root: Annotated[
        ControlStopCommand,
        Field(
            description="What the vmm sends on control link 1200 after `ControlHello` (decision\nCH36 G11).\n\n`stop` starts the graceful stop of decision CH19 rule 3. Guest init\nstops the facade, for up to 30 s when a turn runs. It then syncs the\ndisks and powers the VM off. The vmm sends SIGTERM, then SIGKILL, only\nwhen the VM does not exit in time, because a kill is a power cut for\nthe guest.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name. An externally tagged enum has\nno unit variant, so `stop` holds an empty object (decision CH22 rule 3)."
        ),
    ]


class CreateError1(BaseModel):
    """
    A row with this id exists, and it was made from a different
    request (decisions CH25 rule 2, CH33 rule 12). Two requests
    differ when their typed values differ, not their bytes.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["conflict"]


class CreateError2(BaseModel):
    """
    The family has no `config/` dir, so the vmm refuses the create
    (decision CH33 rule 3). caregiver removes the family files
    before it drops the family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["unknown_family"]


class CreateError3(BaseModel):
    """
    No route has this path.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["no_route"]


class CreateError4(BaseModel):
    """
    The route has this path, but the method is not `POST`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_method"]


class CreateError5(BaseModel):
    """
    The `Content-Type` is not `application/json`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_content_type"]


class CreateError6(BaseModel):
    """
    The body is over the cap.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["too_large"]


class CreateError7(BaseModel):
    """
    The body is not strict JSON.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_json"]


class CreateError8(BaseModel):
    """
    The body is JSON, but not the request type of the route.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_valid"]


class CreateError9(BaseModel):
    """
    The vmm failed after it took the request. The vmm logs
    the details.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["internal"]


class CreateError(
    RootModel[
        CreateError1
        | CreateError2
        | CreateError3
        | CreateError4
        | CreateError5
        | CreateError6
        | CreateError7
        | CreateError8
        | CreateError9
    ]
):
    root: Annotated[
        CreateError1
        | CreateError2
        | CreateError3
        | CreateError4
        | CreateError5
        | CreateError6
        | CreateError7
        | CreateError8
        | CreateError9,
        Field(description="The error of the route `create`."),
    ]


class CreateSession(BaseModel):
    """
    What attendance sends to the vmm to make a row of the host session list
    (decisions CH21-4, CH25 rule 2). It is a `SessionRow` without `title`
    and `updated_at`. The vmm sets `title` to `null` and `updated_at` to
    `created_at`. It makes no session disk at create (decision CH21-4).

    Two creates are the same when their typed values are equal
    (`PartialEq`), not when their bytes are equal. A client retries after
    ENOENT, ECONNREFUSED or EOF (decision CH20). attendance stores the create
    before it sends it the first time. A retry resends the same `created_at`,
    so the repeat is the same create.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    created_at: Annotated[UnixMillis, Field(description="When attendance created the session.")]
    cwd: Annotated[
        SessionCwd,
        Field(
            description="The `cwd` that the client sent in `session/new`, or that attendance\nchose for a job."
        ),
    ]
    family: Annotated[FamilyName, Field(description="The family that serves the session.")]
    id: Annotated[
        SessionId, Field(description="The id that attendance chose (decision CH25 rule 2).")
    ]
    origin: Annotated[SessionOrigin, Field(description="Who made the session.")]


class CredsMissing(BaseModel):
    """
    The reply of a pull when `creds.json` is absent. It carries nothing.
    When `config/` is absent too, this reply wins.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class CredsMissingReply(BaseModel):
    """
    `creds.json` is absent.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    creds_missing: CredsMissing


class Delegate(BaseModel):
    """
    The origin of a session that the delegate door opened for one thin job.

    It is a type of its own so that the generated Python class has a name
    of its own. An inline struct variant gets the class name of its key.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    delegation: Annotated[DelegationId, Field(description="The delegation that asked for the job.")]
    job: Annotated[
        JobId,
        Field(
            description="The id that attendance gives the job. The `SessionId` of the row is a\nseparate value."
        ),
    ]


class DelegateOrigin(BaseModel):
    """
    The delegate door opened the session for one thin job.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    delegate: Delegate


class DelegationId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id the PEP mints for one delegation, a ULID (contract 04 §7.4).",
            pattern="^[0-9A-HJKMNP-TV-Z]{26}$",
        ),
    ]


class Dispatch(BaseModel):
    """
    The trigger of a dispatch: the `enqueue` verb of another family started
    the job (contract 01 §3.13, contract 02 §13.4).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    chain: Annotated[
        DispatchChain,
        Field(
            description="The delegation chain that reached the job. attendance today stores a\ndispatch with no chain when `POST /dispatch` carries none, so it must\nrefuse a missing or empty chain before it writes this origin."
        ),
    ]
    name: Annotated[
        FamilyName,
        Field(
            description="The calling family. attendance takes it from the family token, never\nfrom the request body (contract 02 §13.2 rule 5)."
        ),
    ]


class DispatchTrigger(BaseModel):
    """
    A dispatch from another family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    dispatch: Dispatch


class Door(BaseModel):
    """
    The origin of a session that a door client opened. It carries nothing.

    It is a type of its own so that the generated Python model is a class
    with no field. An inline empty object generates a `dict` with lax keys.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class DoorOrigin(BaseModel):
    """
    A door client opened the session. No job owns it.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    door: Door


class DropError1(BaseModel):
    """
    No row has the id of a `SessionDrop`, or the row belongs to
    another family (decision CH33 rules 4 and 5). A drop of a family
    never gives it.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_found"]


class DropError2(BaseModel):
    """
    No route has this path.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["no_route"]


class DropError3(BaseModel):
    """
    The route has this path, but the method is not `POST`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_method"]


class DropError4(BaseModel):
    """
    The `Content-Type` is not `application/json`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_content_type"]


class DropError5(BaseModel):
    """
    The body is over the cap.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["too_large"]


class DropError6(BaseModel):
    """
    The body is not strict JSON.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_json"]


class DropError7(BaseModel):
    """
    The body is JSON, but not the request type of the route.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_valid"]


class DropError8(BaseModel):
    """
    The vmm failed after it took the request. The vmm logs
    the details.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["internal"]


class DropError(
    RootModel[
        DropError1
        | DropError2
        | DropError3
        | DropError4
        | DropError5
        | DropError6
        | DropError7
        | DropError8
    ]
):
    root: Annotated[
        DropError1
        | DropError2
        | DropError3
        | DropError4
        | DropError5
        | DropError6
        | DropError7
        | DropError8,
        Field(description="The error of the route `drop`."),
    ]


class DropFamily(BaseModel):
    """
    Every session of one family (decision CH21-10). A family with no rows
    gives `Dropped` too, so this drop has no `not_found`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    family: Annotated[FamilyName, Field(description="The family whose sessions the vmm drops.")]


class DropSession(BaseModel):
    """
    One session to drop. It names the family that owns the session (decision
    CH33 rule 5). The vmm answers `not_found` when no row has the id, and
    when the row of the id belongs to another family. So a client learns
    nothing of the sessions of another family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    family: Annotated[FamilyName, Field(description="The family that owns the session.")]
    id: Annotated[SessionId, Field(description="The id of the session.")]


class Dropped(BaseModel):
    """
    The reply of the drop route. It carries nothing: the vmm sends it after
    the target has no VM, no snapshot, no session disk and no row.

    It is a type of its own so that the generated Python model is a class
    with no field. An inline empty object generates a `dict` with lax keys.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class FacadeExited(BaseModel):
    """
    A facade that exited: `waitpid` reported it gone, and guest init did not
    start the next one yet (decision CH19 rule 1). No turn runs.

    Each key is required. `last_turn_end` reads with `Option::deserialize`,
    so a missing key is refused.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    last_turn_end: Annotated[
        TurnEnd | None,
        Field(
            description="The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. `FacadeState` says how the vmm applies it."
        ),
    ]
    starts: Annotated[
        StartCount, Field(description="How many times guest init started the facade.")
    ]


class FacadeExitedState(BaseModel):
    """
    The facade exited, and the next one did not start yet.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    exited: FacadeExited


class FacadeNotStarted(BaseModel):
    """
    The facade before guest init starts it the first time. It carries
    nothing: no turn can end before a facade starts.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class FacadeNotStartedState(BaseModel):
    """
    Guest init did not start the facade yet.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    not_started: FacadeNotStarted


class FacadeRunning(BaseModel):
    """
    A facade process that runs and opened its local link to guest init.

    Each key is required. `last_turn_end` reads with `Option::deserialize`,
    so a missing key is refused.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    last_turn_end: Annotated[
        TurnEnd | None,
        Field(
            description="The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. `FacadeState` says how the vmm applies it."
        ),
    ]
    pid: Annotated[
        StrictInt,
        Field(
            description="The pid of the facade in the guest. Guest init is pid 1, and a\n64-bit Linux keeps each pid below 2^22, so the facade's pid is 2 to\n2^22 - 1. The type accepts 1 to 2^32 - 1, the range of `Child::id`.",
            ge=1,
            le=4294967295,
        ),
    ]
    starts: Annotated[
        StartCount,
        Field(description="How many times guest init started the facade, this start too."),
    ]
    turn_running: Annotated[
        StrictBool,
        Field(
            description="True when the facade sent a turn start and no turn end after it\n(decision CH21)."
        ),
    ]


class FacadeRunningState(BaseModel):
    """
    The facade runs and opened its local link.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    running: FacadeRunning


class FacadeStarting(BaseModel):
    """
    A facade process that guest init started, which did not open its local
    link to guest init yet (decision CH32 rule 6). Guest init gets no turn
    event before that link opens, so this state has no turn flag.

    Each key is required. `last_turn_end` reads with `Option::deserialize`,
    so a missing key is refused.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    last_turn_end: Annotated[
        TurnEnd | None,
        Field(
            description="The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. A facade that exited and starts again keeps it,\nbecause guest init keeps it. On the first start (`starts` 1) it is\nalways `null`, because no local link opened yet (decision CH32 rule\n7). The type does not refuse a value there. `FacadeState` says how\nthe vmm applies it."
        ),
    ]
    pid: Annotated[
        StrictInt,
        Field(
            description="The pid of the facade in the guest, as in `FacadeRunning`.",
            ge=1,
            le=4294967295,
        ),
    ]
    starts: Annotated[
        StartCount,
        Field(description="How many times guest init started the facade, this start too."),
    ]


class FacadeStartingState(BaseModel):
    """
    The facade process is alive and did not open its local link yet.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    starting: FacadeStarting


class FacadeState(
    RootModel[FacadeNotStartedState | FacadeStartingState | FacadeRunningState | FacadeExitedState]
):
    root: Annotated[
        FacadeNotStartedState | FacadeStartingState | FacadeRunningState | FacadeExitedState,
        Field(
            description="The facade as guest init sees it (decision CH19 rule 2). Only a live\nfacade has a pid, only a running facade has a turn, and a facade that\nnever started has no start count.\n\nThe facade is up for decision CH19 rule 6 only in `running`. So the vmm\nsnapshots a VM only when the facade is `running` and `turn_running` is\nfalse. A `starting` facade is not up: guest init cannot see its turns\nyet, so the vmm never snapshots a turn that guest init cannot see\n(decision CH32 rule 6). When the local link of a live facade closes,\nguest init kills the facade, and the state moves to `exited` after\n`waitpid` (decision CH32 rule 7). So a facade never goes back from\n`running` to `starting`.\n\nEach state after the first start holds `last_turn_end`, so\n`not_started` cannot carry one (decision CH32 rule 5). A restore can\ndrop a turn event on the old link (fact 45), so guest init reports the\nlast `TurnEnd`, not a current title (decision CH32 rule 4). The vmm\napplies it as it applies `TurnEvent::End`: it sets the row's\n`updated_at`, and it sets the row's title only while that is `null`.\nSo an end that a restore dropped still arrives.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name."
        ),
    ]


class FamilyConfig(BaseModel):
    """
    The family's part of `config/` that the guest reads: what caregiver writes
    in `write_config` (`steps.py:229-249`). Instructions, skills, sandbox
    tools and the model change at the next prompt of a running session
    (decision CH5c).

    `shell` of `runtime.json` is not here: the agent reads it and grants
    nothing with it (`agent/src/runtime-config.ts:18`).

    `model_live_at` of `runtime.json` is not here either. The vmm holds the
    reply on it (`FamilyStateReply`).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    instructions: Annotated[
        StrictStr,
        Field(
            description="The text of `config/instructions.md`. The agent trims it, and an empty\ntext gives no instructions, as today (`agent/src/agent.ts:72-85`)."
        ),
    ]
    model_alias: Annotated[ModelAlias, Field(description="The alias of the model.")]
    sandbox_tools: Annotated[SandboxGrant, Field(description="The granted sandbox tools.")]
    skills: Annotated[FamilySkills, Field(description="The granted skills.")]
    system_prompt: Annotated[
        SystemPrompt,
        Field(
            description="How the instructions meet the agent's own prompt. caregiver leaves\n`system_prompt` out of `runtime.json` for `append`\n(`config_mount.py:54`), so the vmm reads a missing key as `append`.",
            title="SystemPrompt",
        ),
    ]


class FamilyCreds(BaseModel):
    """
    The family's part of `creds.json` that the guest reads (decision CH23
    default 1). `epoch`, `written_at`, `previous_pep_token` and
    `previous_expires_at` stay on the host, so the old token never sits in
    guest memory or in a snapshot.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    litellm_key: Annotated[LitellmKey, Field(description="The key for the LiteLLM plane.")]
    pep_token: Annotated[PepToken, Field(description="The token for the chaperone plane.")]


class FamilyDrop(BaseModel):
    """
    Every session of one family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    family: DropFamily


class FamilyName(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The name of a family: `FAMILY_NAME` of `family/grammar.py`.",
            pattern="^[a-z][a-z0-9-]{1,30}$",
        ),
    ]


class DispatchChain(RootModel[list[FamilyName]]):
    """
    The delegation chain that reached a dispatched job, caller first and the
    job's family last (contract 02 §13.2 rule 6). It holds 1 to 8 family
    names. The PEP builds it from the delegations it minted (contract 04
    §6.3).
    """

    root: Annotated[
        list[FamilyName],
        Field(
            description="The delegation chain that reached a dispatched job, caller first and the\njob's family last (contract 02 §13.2 rule 6). It holds 1 to 8 family\nnames. The PEP builds it from the delegations it minted (contract 04\n§6.3).",
            max_length=8,
            min_length=1,
        ),
    ]


class FamilyState(BaseModel):
    """
    The whole family state of one pull: the full typed state, every time
    (decision CH23). The facade compares the fields itself.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    config: Annotated[FamilyConfig, Field(description="What the vmm read from `config/`.")]
    creds: Annotated[FamilyCreds, Field(description="What the vmm read from `creds.json`.")]


class JobId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id that attendance gives a thin job of the delegate door:\n`job-<ulid>` (contract 02 §2 and §12). Contract 02 calls it a session\nid. The `SessionId` of the job's row on the host session list is a\nseparate value.",
            pattern="^job-[0-9A-HJKMNP-TV-Z]{26}$",
        ),
    ]


class ListCursor(RootModel[StrictInt]):
    root: Annotated[
        StrictInt,
        Field(
            description="A place on the stored order of the host session list (decision CH21-5).\nThe vmm gives it as `next` of a `SessionPage`, and the client sends it\nback as `cursor` of the next `ListSessions`. It is opaque: a client never\nmakes one or reads a meaning from it.\n\nOn the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads\nit exactly. Any such integer reads, so the list route has no cursor\nerror. attendance turns it into the string cursor of ACP `session/list`.",
            ge=1,
            le=9007199254740991,
        ),
    ]


class ListError1(BaseModel):
    """
    No route has this path.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["no_route"]


class ListError2(BaseModel):
    """
    The route has this path, but the method is not `POST`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_method"]


class ListError3(BaseModel):
    """
    The `Content-Type` is not `application/json`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["bad_content_type"]


class ListError4(BaseModel):
    """
    The body is over the cap.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["too_large"]


class ListError5(BaseModel):
    """
    The body is not strict JSON.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_json"]


class ListError6(BaseModel):
    """
    The body is JSON, but not the request type of the route.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["not_valid"]


class ListError7(BaseModel):
    """
    The vmm failed after it took the request. The vmm logs
    the details.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Literal["internal"]


class ListError(
    RootModel[
        ListError1 | ListError2 | ListError3 | ListError4 | ListError5 | ListError6 | ListError7
    ]
):
    root: Annotated[
        ListError1 | ListError2 | ListError3 | ListError4 | ListError5 | ListError6 | ListError7,
        Field(
            description="The error of the route `list`. It holds the shared codes only: a\nfamily with no rows gives an empty page, and any cursor reads."
        ),
    ]


class ListSessions(BaseModel):
    """
    What a client sends to the vmm for one page of the host session list
    (decisions CH21-5, CH21-7). The list is scoped to one family, as the
    list entry of the route table plan gives it. A family with no rows, or
    one the vmm does not know, gives an empty page, so the list route has no
    error of its own.

    Each key is required, and `null` is the one form of no value.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    cursor: Annotated[
        ListCursor | None,
        Field(description="The `next` of the page before, or `null` for the first page."),
    ]
    cwd: Annotated[
        SessionCwd | None,
        Field(
            description="Only the rows with this `cwd`, or `null` for every row. The vmm\napplies it before it cuts the page, as `session-index.ts` does today."
        ),
    ]
    family: Annotated[FamilyName, Field(description="The family whose rows the page holds.")]


class LitellmKey(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The family's LiteLLM key, `litellm_key` of `creds.json`. caregiver\nkeeps only a key that starts with `sk-` (`litellm_keys.py:155`). The\nrest is visible ASCII, with no space or control character, so the key\ncannot break the header that carries it. Decision CH31 keeps this rule\nand gives caregiver's check the same rule in a later change.",
            pattern="^sk-[!-~]*$",
        ),
    ]


class ModelAlias(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The LiteLLM alias of the family's model: `model.router` of the family\nfile, which caregiver writes as `model_alias` in `config/runtime.json`\n(`steps.py:242`).",
            pattern="^[a-z0-9][a-z0-9._/-]*$",
        ),
    ]


class NotValidReply(BaseModel):
    """
    A file is present and does not read.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    not_valid: StateNotValid


class OpenAcp(BaseModel):
    """
    The session of an ACP stream: the request of the `acp` Upgrade route. It
    names the family that owns the session (decision CH33 rule 5). The vmm
    answers `not_found` when no row has the id, and when the row of the id
    belongs to another family. So a client learns nothing of the sessions
    of another family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    family: Annotated[FamilyName, Field(description="The family that owns the session.")]
    id: Annotated[SessionId, Field(description="The id of the session.")]


class PepToken(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The family token for the chaperone, `pep_token` of `creds.json`: 32\nbytes as 64 lowercase hex digits, the one form the chaperone takes\n(`credentials.py:21-25`).",
            pattern="^[0-9a-f]{64}$",
        ),
    ]


class ProtocolMismatch(BaseModel):
    """
    The one reply of the vmm when the protocol header of a request is
    missing or names another protocol number (decisions CH25 rule 1, CH33
    rule 1). The vmm checks the header before the route, so each route and
    each path gets this reply. The wire form is
    `{"code":"protocol_mismatch","protocol":<n>}`, with the status
    `MISMATCH_STATUS`.

    The reply is frozen for all protocol numbers: a new protocol number
    never changes its body or its status. A client of an older protocol
    must still read it. A test pins the bytes and the status.

    It is in no route error. A client checks the status `MISMATCH_STATUS`
    before it reads a route error.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: Annotated[Literal["protocol_mismatch"], Field(description="Always `protocol_mismatch`.")]
    protocol: Annotated[
        StrictInt,
        Field(
            description="The host API protocol of the vmm that answers: its\n`HOST_API_PROTOCOL`.",
            ge=0,
            le=4294967295,
        ),
    ]


class SandboxGrant(BaseModel):
    """
    The built-in sandbox tools of Flue that the family grants: `sandbox_tools`
    of the family file, a subset of `SandboxTool` of `family/grammar.py`. The
    validator refuses a tool listed twice, so a grant is a set, and each tool
    is one key here. An empty grant gives no sandbox.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    bash: Annotated[StrictBool, Field(description="`bash`, the one way to run commands.")]
    edit: Annotated[StrictBool, Field(description="`edit`.")]
    glob: Annotated[StrictBool, Field(description="`glob`.")]
    grep: Annotated[StrictBool, Field(description="`grep`.")]
    read: Annotated[StrictBool, Field(description="`read`.")]
    write: Annotated[StrictBool, Field(description="`write`.")]


class SessionCwd(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The `cwd` of a session: the absolute path that the client sent in\n`session/new`, or that attendance chose for a job. It is a display\nvalue and a `session/list` filter, never a guest path (decision CH5e,\nCH21-4).",
            max_length=4096,
            pattern="^/[^\\u0000]*$",
        ),
    ]


class SessionDrop(BaseModel):
    """
    One session of one family.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    session: DropSession


class DropTarget(RootModel[SessionDrop | FamilyDrop]):
    root: Annotated[
        SessionDrop | FamilyDrop,
        Field(
            description="What a client sends to the vmm to drop sessions: the request of the drop\nroute. On the wire the variant name is the one key of an object, as in\n`SessionOrigin`, and each variant has a `title` for its class name.\n\nattendance drops one session on an ACP `session/delete` (decision CH27)\nand at the end of a job (decision CH21-2). caregiver drops a family when\nit deletes the family (decision CH21-10)."
        ),
    ]


class SessionId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id of one session, and of the VM that serves it (decision CH1).\n\nThe host mints the id at `session/new`. The text is a UUID in its\nlowercase hyphenated form, the form the facade mints today with\n`randomUUID`. A value of this type is always in that form. On the wire it\nis a JSON string, and a string of any other form is refused.",
            pattern="^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        ),
    ]


class SessionOrigin(RootModel[DoorOrigin | DelegateOrigin | AutoOrigin]):
    root: Annotated[
        DoorOrigin | DelegateOrigin | AutoOrigin,
        Field(
            description="Who made a session, kept on its row of the host session list (decision\nCH21-4). On the wire the variant name is the one key of an object.\n\nEach variant has a `title`. datamodel-codegen names a class of a `oneOf`\nmember by its title, and by its position without one."
        ),
    ]


class SessionPage(BaseModel):
    """
    One page of the host session list: the reply of the list route. The rows
    are newest stored first (decision CH21-5).

    The vmm sets the most rows of a page. The page also ends early when one
    more row would take the reply past `HOST_API_BODY_CAP` (decision CH33
    rule 2). A client then gets fewer rows and a `next` that is not `null`.
    The largest row is far under the cap, so a page with a `next` holds at
    least one row.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    next: Annotated[
        ListCursor | None,
        Field(description="The cursor of the next page, or `null` when no row follows."),
    ]
    rows: Annotated[list[SessionRow], Field(description="The rows of this page.")]


class SessionRow(BaseModel):
    """
    One row of the host session list (decision CH1, CH21, CH21-4). The vmm
    keeps it, so `session/list` and the noticeboard need no VM boot.

    The list holds the rows in the order the vmm stored them, newest first,
    as `session-index.ts:130-158` does today with its `rowid` (decision
    CH21-5). Two creates can share a millisecond, so the order is not the
    order of `created_at`. The list reply carries an opaque cursor on that
    stored order. The order is no field of the row.

    Each key is required. A field that may be `null` reads with
    `Option::deserialize`, so a missing key is refused, and the schema lists
    it in `required`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    created_at: Annotated[UnixMillis, Field(description="When attendance created the session.")]
    cwd: Annotated[SessionCwd, Field(description="The `cwd` that attendance sent in the create.")]
    family: Annotated[FamilyName, Field(description="The family that serves the session.")]
    id: Annotated[
        SessionId, Field(description="The id that attendance chose (decision CH25 rule 2).")
    ]
    origin: Annotated[SessionOrigin, Field(description="Who made the session.")]
    title: Annotated[
        SessionTitle | None,
        Field(
            description="The first title from the facade, or `null` before a turn names the\nsession. The vmm sets it only while it is `null`, so the first title\nstays, as `nameOnce` keeps it today (`session-index.ts:122-125`)."
        ),
    ]
    updated_at: Annotated[
        UnixMillis,
        Field(
            description="The time of the last Flue record of the last turn, from the facade.\nAt create the vmm sets it to `created_at`, as `SessionIndex.create`\ndoes today. Each turn end sets it again."
        ),
    ]


class SessionTitle(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The title of a session on the host session list (decision CH21). The\nfacade makes it at the end of each turn, as `titleOf` makes it today,\nwith a trim of the end after the cut: the first line of the prompt\nwith words, white space folded to one space, cut to 256 bytes, then\ntrimmed.",
            max_length=256,
            min_length=1,
            pattern="^[^\\u0009-\\u000D\\u0020\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000\\uFEFF]+( [^\\u0009-\\u000D\\u0020\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000\\uFEFF]+)*$",
        ),
    ]


class SkillName(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The name of a granted skill: `SKILL_NAME` of `family/grammar.py`,\nwhich has the rule of a family name.",
            pattern="^[a-z][a-z0-9-]{1,30}$",
        ),
    ]


class FamilySkills(
    RootModel[
        dict[
            Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,30}$", strict=True)],
            StrictStr,
        ]
    ]
):
    root: Annotated[
        dict[
            Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,30}$", strict=True)],
            StrictStr,
        ],
        Field(description="The granted skills: each name, and the text of its `SKILL.md`."),
    ]


class StartCount(RootModel[StrictInt]):
    root: Annotated[
        StrictInt,
        Field(
            description="How many times guest init started the facade since the VM booted\n(decision CH19 rule 1). A restore keeps guest init's memory, so it keeps\nthe count. It is at least 1: a facade that never started has no count.\n\nOn the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads\nit exactly.",
            ge=1,
            le=9007199254740991,
        ),
    ]


class StateNotValid(BaseModel):
    """
    The reply of a pull when `creds.json` or `config/` is present and does
    not read as `FamilyState`. It carries nothing, so it holds no value of
    the files. The vmm names the file that failed in its host log only.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class StateReply(BaseModel):
    """
    The vmm read the whole state.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    state: FamilyState


class StateTooLarge(BaseModel):
    """
    The reply of a pull when the files read, and the `state` reply is over
    the cap of 256 KiB (decision CH31). It carries nothing, not even the
    size. The vmm writes the size in its host log only.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class SystemPrompt(StrEnum):
    """
    How the instructions meet the agent's own prompt. caregiver leaves
    `system_prompt` out of `runtime.json` for `append`
    (`config_mount.py:54`), so the vmm reads a missing key as `append`.
    """

    APPEND = "append"
    REPLACE = "replace"


class Timer(BaseModel):
    """
    The trigger of a cron firing. It carries nothing, because a timer has no
    name (contract 02 §13.2).
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class TimerTrigger(BaseModel):
    """
    A cron firing.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    timer: Timer


class TooLargeReply(BaseModel):
    """
    The `state` reply is over the cap.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    too_large: StateTooLarge


class FamilyStateReply(
    RootModel[StateReply | CredsMissingReply | ConfigMissingReply | NotValidReply | TooLargeReply]
):
    root: Annotated[
        StateReply | CredsMissingReply | ConfigMissingReply | NotValidReply | TooLargeReply,
        Field(
            description="What the vmm answers on host port 1100 when the facade pulls the family\nstate, before each prompt (decisions CH5a, CH5b, CH23). The vmm reads\ncaregiver's `creds.json` and `config/` at each pull.\n\nA read failure is a typed reply, and the link stays open (decision CH23\ndefault 2). The facade fails that prompt and keeps running. At start it\npulls again with a backoff, and never exits on a typed error. When\n`creds.json` and `config/` are both absent, `creds_missing` wins\n(decision CH31).\n\nA reply text has a cap of 256 KiB (decision CH23 default 4). The cap is\na rule of the read surface, not of this type: the vmm's writer and the\n`cap` of `creche_wire.read` hold it, because JSON Schema counts code\npoints, not bytes. A `state` reply over the cap is `too_large`, not\n`not_valid` (decision CH31).\n\nThe vmm reads `model_live_at` from `config/runtime.json` (decision\nCH23-1). It holds the `state` reply until the host time passes that\ntime, for at most 10 s (decision CH31). No wire type carries\n`model_live_at`, and the guest needs no clock for it. A held pull can\ntake up to 10 s longer.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name."
        ),
    ]


class TurnEnd(BaseModel):
    """
    A turn ended: it completed, the client cancelled it, or it failed. The
    facade makes both fields as it makes them today, at the end of each
    turn (`facade/src/turns.ts:139-151`).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    title: Annotated[
        SessionTitle,
        Field(
            description="The title that `titleOf` makes from the prompt of this turn, with the\nend trimmed after the cut. The facade refuses a prompt with no text\nbefore the turn starts, so each turn has a title. The vmm stores it\nonly while the row's title is `null`, so the first title stays."
        ),
    ]
    updated_at: Annotated[
        UnixMillis,
        Field(
            description="The time of the last Flue record of this turn, or the facade's clock\nwhen the turn wrote no record. The vmm stores it at each end."
        ),
    ]


class TurnEndEvent(BaseModel):
    """
    A turn ended.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    end: TurnEnd


class TurnStart(BaseModel):
    """
    A turn started. It carries nothing.

    It is a type of its own so that the generated Python model is a class
    with no field, as `Door` is.
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class TurnStartEvent(BaseModel):
    """
    A turn started.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    start: TurnStart


class TurnEvent(RootModel[TurnStartEvent | TurnEndEvent]):
    root: Annotated[
        TurnStartEvent | TurnEndEvent,
        Field(
            description="What the facade tells guest init at each turn start and turn end, and\nwhat guest init relays to the vmm on control link 1200 (decision CH21).\nAt each end the vmm sets the row's `updated_at`, and it sets the row's\n`title` only while that is `null`, as `nameOnce` does today. It\nsnapshots a VM only between an end and the next start (decision CH19\nrule 6).\n\nThe event names no session: a VM serves one session (decision CH1), and\nthe link names the VM. On the wire the variant name is the one key of an\nobject, and each variant has a `title` for its class name."
        ),
    ]


class UnixMillis(RootModel[StrictInt]):
    root: Annotated[
        StrictInt,
        Field(
            description="A time on the wire: whole milliseconds since 1970-01-01T00:00:00Z, UTC\n(decision CH22-1). It is the one wire form of a time. No wire type holds\na `date-time` string.\n\nOn the wire it is a JSON integer from 0 to 2^53 - 1, so JavaScript reads\nit exactly. A larger integer, a negative integer, a float such as `1.0`\nor `1e3`, and a string are refused.",
            ge=0,
            le=9007199254740991,
        ),
    ]


class Webhook(BaseModel):
    """
    The trigger of a webhook firing.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    name: Annotated[
        WebhookName, Field(description="The name of the webhook that the family declares.")
    ]


class WebhookName(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The name of a declared webhook trigger: `WEBHOOK_NAME` of\n`family/grammar.py`, which has the rule of a family name.",
            pattern="^[a-z][a-z0-9-]{1,30}$",
        ),
    ]


class WebhookTrigger(BaseModel):
    """
    A webhook firing.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    webhook: Webhook


class Trigger(RootModel[TimerTrigger | WebhookTrigger | DispatchTrigger]):
    root: Annotated[
        TimerTrigger | WebhookTrigger | DispatchTrigger,
        Field(
            description="What fired an autonomous job, typed per kind (decision CH21-4a). A timer\nhas no name, and only a dispatch has a chain. There is no fired time: the\ncreated time of the row stands in for it.\n\nOn the wire the variant name is the one key of an object, as in\n`SessionOrigin`, and each variant has a `title` for its class name."
        ),
    ]
