"""TOML text to a model, or to issues. Never an exception (invariant 19).

A parse failure is data: the caller gets `None` plus every issue pydantic and
the reader could see, and the family keeps its last good state.

The reader reads the text with `tomlfile.loads` and validates in the strict
mode of pydantic: each field takes the type of the model, and a float field
also takes an integer."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ValidationError

from . import tomlfile
from .grammar import closest_name
from .model import (
    FAMILY_FIELDS,
    VERB_FIELDS,
    EnqueueFence,
    FamilyFile,
    FileMount,
    HaCallFence,
    HaTriple,
    JobBlock,
    ModelBlock,
    ReleaseFence,
    SandboxBlock,
    Trigger,
)
from .report import Issue, Severity
from .server import (
    INSTALL_FIELDS,
    RUN_FIELDS,
    SERVER_FIELDS,
    FenceEntry,
    McpServerFile,
    ToolEntry,
)


class Markup(StrEnum):
    """The markup of the text of a family file or of a server file. TOML is
    the one markup."""

    TOML = "toml"


#: A list index in a container path. The index does not change which fields
#: the container knows, so every index collapses onto one key.
ANY_INDEX: Final = "*"

_FAMILY_CONTAINERS: Final[dict[tuple[str, ...], tuple[str, ...]]] = {
    (): FAMILY_FIELDS,
    ("model",): tuple(ModelBlock.model_fields),
    ("sandbox",): tuple(SandboxBlock.model_fields),
    ("job",): tuple(JobBlock.model_fields),
    ("verbs",): VERB_FIELDS,
    ("files", ANY_INDEX): tuple(FileMount.model_fields),
    ("triggers", ANY_INDEX): tuple(Trigger.model_fields),
    ("verbs", "ha_call"): tuple(HaCallFence.model_fields),
    ("verbs", "ha_call", "allow", ANY_INDEX): tuple(HaTriple.model_fields),
    ("verbs", "enqueue"): tuple(EnqueueFence.model_fields),
    ("verbs", "release"): tuple(ReleaseFence.model_fields),
}

_SERVER_CONTAINERS: Final[dict[tuple[str, ...], tuple[str, ...]]] = {
    (): SERVER_FIELDS,
    ("install",): INSTALL_FIELDS,
    ("run",): RUN_FIELDS,
    ("tools", ANY_INDEX): tuple(ToolEntry.model_fields),
    ("arg_allows", ANY_INDEX): tuple(FenceEntry.model_fields),
    ("arg_denies", ANY_INDEX): tuple(FenceEntry.model_fields),
}


def fmt_loc(loc: tuple[int | str, ...]) -> str:
    """Pydantic's tuple to the contract's field path: `tools.kagi[1]`."""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
            continue

        out = f"{out}.{part}" if out else str(part)

    return out or "<document>"


def _container_key(loc: tuple[int | str, ...]) -> tuple[str, ...]:
    return tuple(ANY_INDEX if isinstance(part, int) else part for part in loc)


def _unknown_field_msg(
    loc: tuple[int | str, ...], containers: dict[tuple[str, ...], tuple[str, ...]]
) -> str:
    """Contract 01 §7 rule 1: name the field and the closest known name."""
    field = str(loc[-1]) if loc else "<document>"
    known = containers.get(_container_key(loc[:-1]), ())
    near = closest_name(field, known)
    if near is not None:
        return f"unknown field '{field}'; did you mean '{near}'?"

    if known:
        return f"unknown field '{field}'; known fields here are {', '.join(known)}"

    return f"unknown field '{field}'"


def _issues_from(
    exc: ValidationError, containers: dict[tuple[str, ...], tuple[str, ...]]
) -> list[Issue]:
    issues: list[Issue] = []
    for error in exc.errors():
        loc = error["loc"]
        msg = (
            _unknown_field_msg(loc, containers)
            if error["type"] == "extra_forbidden"
            else error["msg"]
        )
        issues.append(Issue(Severity.ERROR, fmt_loc(loc), msg))

    return issues


def _load_toml[T: BaseModel](
    data: bytes,
    kind: tomlfile.FileKind,
    model: type[T],
    containers: dict[tuple[str, ...], tuple[str, ...]],
) -> tuple[T | None, list[Issue]]:
    try:
        body = tomlfile.loads(data, kind)
    except tomlfile.TomlRefusal as refusal:
        return None, [Issue(Severity.ERROR, "<document>", str(refusal))]

    try:
        return model.model_validate(body, strict=True), []
    except ValidationError as exc:
        return None, _issues_from(exc, containers)


def parse_family(text: str, markup: Markup = Markup.TOML) -> tuple[FamilyFile | None, list[Issue]]:
    """The text of a family file. `markup` is TOML, the one markup."""
    # A lone surrogate has no UTF-8 form. `surrogatepass` gives it the three
    # bytes of its code point, and the reader refuses those bytes.
    return parse_family_toml(text.encode("utf-8", "surrogatepass"))


def parse_family_toml(data: bytes) -> tuple[FamilyFile | None, list[Issue]]:
    """The TOML path for the bytes of a file. The registry gives the bytes
    that it read, so the reader judges each byte of the file."""
    return _load_toml(data, tomlfile.FileKind.FAMILY, FamilyFile, _FAMILY_CONTAINERS)


def parse_server(
    text: str, markup: Markup = Markup.TOML
) -> tuple[McpServerFile | None, list[Issue]]:
    """The text of a server file. `markup` is TOML, the one markup."""
    # The same encoding as `parse_family`: the reader refuses the bytes of a
    # lone surrogate.
    return parse_server_toml(text.encode("utf-8", "surrogatepass"))


def parse_server_toml(data: bytes) -> tuple[McpServerFile | None, list[Issue]]:
    """The TOML path for the bytes of a server file. The byte limit of the
    file kind is the one size cap of this path."""
    return _load_toml(data, tomlfile.FileKind.SERVER, McpServerFile, _SERVER_CONTAINERS)
