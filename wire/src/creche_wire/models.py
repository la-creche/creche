# Generated from rust/crates/wire/schema/wire.json. Do not edit.
# To write it again, run `uv run datamodel-codegen` at the root of the repository.

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictStr


class AutoJobId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id that attendance gives an autonomous job of a trigger or of the\ndispatch door: `auto-<ulid>` (contract 02 §2 and §13). Contract 02 calls\nit a session id. The `SessionId` of the job's row on the host session\nlist is a separate value.",
            pattern="^auto-[0-9A-HJKMNP-TV-Z]{26}$",
        ),
    ]


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


class SessionId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The id of one session, and of the VM that serves it (decision CH1).\n\nThe host mints the id at `session/new`. The text is a UUID in its\nlowercase hyphenated form, the form the facade mints today with\n`randomUUID`. A value of this type is always in that form. On the wire it\nis a JSON string, and a string of any other form is refused.",
            pattern="^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        ),
    ]


class SessionOrigin(RootModel[DoorOrigin | DelegateOrigin]):
    root: Annotated[
        DoorOrigin | DelegateOrigin,
        Field(
            description="Who made a session, kept on its row of the host session list (decision\nCH21-4). On the wire the variant name is the one key of an object.\n\nEach variant has a `title`. datamodel-codegen names a class of a `oneOf`\nmember by its title, and by its position without one."
        ),
    ]


class WebhookName(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            description="The name of a declared webhook trigger: `WEBHOOK_NAME` of\n`family/grammar.py`, which has the rule of a family name.",
            pattern="^[a-z][a-z0-9-]{1,30}$",
        ),
    ]
