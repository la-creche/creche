# Generated from rust/crates/wire/schema/wire.json. Do not edit.
# To write it again, run `uv run datamodel-codegen` at the root of the repository.

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictInt, StrictStr


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


class JobId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id that attendance gives a thin job of the delegate door:\n`job-<ulid>` (contract 02 §2 and §12). Contract 02 calls it a session\nid. The `SessionId` of the job's row on the host session list is a\nseparate value.",
            pattern="^job-[0-9A-HJKMNP-TV-Z]{26}$",
        ),
    ]


class SessionCwd(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The `cwd` of a session: the absolute path that the client sent in\n`session/new`, or that attendance chose for a job. It is a display\nvalue and a `session/list` filter, never a guest path (decision CH5e,\nCH21-4).",
            max_length=4096,
            pattern="^/[^\\u0000]*$",
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
