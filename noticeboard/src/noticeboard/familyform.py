"""The edit form, generated from `agent_family`'s schema.

Invariant 16: one file defines a family. This module turns that file into
controls and the controls back into that file. It holds NO list of field
names and NO copy of a validation rule, and that is the whole design.

A mirror of a rule drifts from the rule. So here:

1. **The controls come from `FamilyFile.model_fields`.** The annotation
   picks the control. A field added to the schema appears on the page
   with no edit here.
2. **A lock comes from ASKING the validator.** For a field that a rule
   can forbid, this module builds a probe document with that field set,
   runs `check_family`, and reads the refusal. The rule's own sentence
   becomes the field's label, and a rule that changes changes the page.
   Nothing to keep in step, so nothing can fall out of step.

A locked field renders disabled AND is dropped from the document on
save. A disabled input posts nothing, so without the drop a lock would
grey out a field while leaving its old value in the file, and the save
would be refused by the very rule the form just displayed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import UnionType
from typing import Any, Final, Union, cast, get_args, get_origin

from agent_family import (
    FamilyFile,
    Index,
    Issue,
    Issues,
    Severity,
    check_family,
    classify,
    parse_family,
)
from pydantic import BaseModel

from .yamlout import to_yaml

#: How much text one control accepts. A `verbs` block is the largest
#: thing here and is still far under this (invariant 14).
MAX_FIELD_CHARS: Final = 64 * 1024

#: Where a family's own directory sits, for `check_family`'s path checks.
_PROBE_DIR: Final = "families"


class Control(StrEnum):
    """What a field looks like on the page."""

    TEXT = "text"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    #: A list of names, one per line. `delegates`, `egress`, `skills`.
    LINES = "lines"
    #: A YAML sub-document, key line included. `files`, `tools`, `verbs`,
    #: `triggers`: shapes no flat control can hold honestly.
    BLOCK = "block"


@dataclass(frozen=True)
class Field:
    """One control. `name` is both the posted name and the document path."""

    name: str
    control: Control
    value: str = ""
    checked: bool = False
    #: The rule that forbids this field, straight from the validator.
    #: Empty means the field is free. This is the field's LABEL when set.
    locked: str = ""
    #: What the field holds, for a reader who is not holding contract 01.
    note: str = ""
    #: The schema says this field must be present. It decides what a LOCK
    #: does on save: a locked optional field is dropped, a locked required
    #: field keeps the value it has, because a document without it would
    #: not parse at all.
    required: bool = False

    @property
    def free(self) -> bool:
        return not self.locked


@dataclass(frozen=True)
class Form:
    family: str
    fields: tuple[Field, ...]

    def field(self, name: str) -> Field | None:
        return next((one for one in self.fields if one.name == name), None)

    @property
    def locked(self) -> tuple[Field, ...]:
        return tuple(one for one in self.fields if one.locked)


def form_of(family: FamilyFile, index: Index) -> Form:
    """Every control for one family, with every lock already applied."""
    rules = locks_of(family, index)
    document = family.model_dump(mode="json")

    return Form(
        family=family.name,
        fields=tuple(_fields(document, rules)),
    )


def with_posted(form: Form, posted: Mapping[str, str]) -> Form:
    """The same controls, with what the reader typed in each free one.

    The page that answers a post renders this form. The next post from that
    page then holds the same values, so a save after a preview writes what
    the preview showed. A locked control keeps the value of the registry: a
    disabled control posts nothing.
    """
    return replace(form, fields=tuple(_typed(one, posted) for one in form.fields))


def _typed(own: Field, posted: Mapping[str, str]) -> Field:
    """One control as the post left it.

    A browser names each free text control in a post. A control that the
    post does not name shows what the document takes.
    """
    if own.locked:
        return own

    if own.control is Control.CHECKBOX:
        return replace(own, checked=own.name in posted)

    return replace(own, value=posted.get(own.name, _unnamed(own))[:MAX_FIELD_CHARS])


def _unnamed(own: Field) -> str:
    """What a free text control shows when the post does not name it.

    The document takes the block of the registry for a block, and the
    control shows it. The document drops each other key, so the family takes
    the default of the schema. A list shows that default, because an empty
    list control posts an empty list. Each other control is empty, and an
    empty control drops the key again.
    """
    if own.control is Control.BLOCK:
        return own.value

    info = FamilyFile.model_fields.get(own.name)

    if own.control is not Control.LINES or info is None:
        return ""

    return _rendered(own.name, own.control, info.get_default(call_default_factory=True))


def document_of(form: Form, posted: Mapping[str, str]) -> tuple[str, str]:
    """The whole `family.yaml` text the form describes, plus a problem.

    Assembled in schema order, one chunk per top-level field, so the file
    a save writes reads in the same order as the file it replaced.
    """
    chunks: list[str] = []

    for top in FamilyFile.model_fields:
        chunk, problem = _chunk(form, posted, top)

        if problem:
            return "", problem

        if chunk:
            chunks.append(chunk)

    return "".join(chunks), ""


def parse_posted(form: Form, posted: Mapping[str, str]) -> tuple[FamilyFile | None, list[Issue]]:
    """The form's document, read back through the one YAML reader."""
    text, problem = document_of(form, posted)

    if problem:
        return None, [Issue(severity=Severity.ERROR, loc="form", msg=problem)]

    return parse_family(text)


def locks_of(family: FamilyFile, index: Index) -> dict[str, str]:
    """Every field a rule forbids right now, with the rule as its value.

    Two sources, both live. `classify` names the fields a change to which
    is refused outright (contract 01 §3.1). `check_family` names the
    fields this family's `kind` does not take (§3.11 to §3.13).
    """
    found: dict[str, str] = {}
    found.update(_immutable(family))
    found.update(_forbidden(family, index))

    return found


def _immutable(family: FamilyFile) -> dict[str, str]:
    """Ask the classifier which fields cannot move at all."""
    found: dict[str, str] = {}

    for name in FamilyFile.model_fields:
        probe = _moved(family, name)

        if probe is None:
            continue

        for change in classify(family, probe).refused:
            found[change.field] = change.detail

    return found


def _forbidden(family: FamilyFile, index: Index) -> dict[str, str]:
    """Ask the validator which optional fields this kind does not take.

    Only a field that is currently unset is probed. A field the family
    already carries is either legal or already named in its report, and
    a lock on it would hide the value rather than explain it.
    """
    found: dict[str, str] = {}

    for name, info in FamilyFile.model_fields.items():
        if getattr(family, name, None) is not None:
            continue

        probe = _filled(family, name, info.annotation)

        if probe is None:
            continue

        issues = Issues()
        check_family(probe, _PROBE_DIR, index, issues, None)
        refusal = _refusal_at(issues.frozen(), name)

        if refusal:
            found[name] = refusal

    return found


def _refusal_at(issues: tuple[Issue, ...], name: str) -> str:
    """The first error the validator put on this exact field."""
    for one in issues:
        if one.severity is Severity.ERROR and one.loc == name:
            return one.msg

    return ""


def _moved(family: FamilyFile, name: str) -> FamilyFile | None:
    """The same family with one field changed, for a classify probe.

    `model_copy` skips validation on purpose: the probe never reaches a
    file, and `classify` compares values rather than checking them.
    """
    other = _other(getattr(family, name, None))

    if other is None:
        return None

    return family.model_copy(update={name: other})


def _other(value: object) -> object | None:
    """Any value that differs from this one, or None when there is none."""
    if isinstance(value, bool):
        return not value

    if isinstance(value, str):
        return value + "-probe"

    if isinstance(value, int | float):
        return value + 1

    return None


def _filled(family: FamilyFile, name: str, annotation: object) -> FamilyFile | None:
    """The same family with one unset optional field given a value."""
    value = _probe_value(annotation)

    if value is None:
        return None

    return family.model_copy(update={name: value})


def _probe_value(annotation: object) -> object | None:
    """A plausible value for an optional field, built from its type.

    A model gets its defaults, a list of models gets one default member,
    and a number gets one. Anything else is not probed, so a field this
    function does not understand is simply never locked.
    """
    inner = _without_none(annotation)

    if inner is int:
        return 1

    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner()

    origin = get_origin(inner)

    if origin is list:
        member = next(iter(get_args(inner)), None)

        if isinstance(member, type) and issubclass(member, BaseModel):
            return [member()]

    return None


def _without_none(annotation: object) -> object:
    """`X | None` becomes `X`. Anything else is unchanged."""
    if get_origin(annotation) not in (Union, UnionType):
        return annotation

    kept = [one for one in get_args(annotation) if one is not type(None)]

    return kept[0] if len(kept) == 1 else annotation


def _fields(document: Mapping[str, Any], rules: Mapping[str, str]) -> list[Field]:
    found: list[Field] = []

    for name, info in FamilyFile.model_fields.items():
        value = document.get(name)
        control = _control(info.annotation)

        if control is not None:
            found.append(_one(name, control, value, rules, info.is_required()))
            continue

        found.extend(_nested(name, info.annotation, value, rules))

    return found


def _control(annotation: object) -> Control | None:
    """The control an annotation asks for, or None to look inside it."""
    inner = _without_none(annotation)

    if inner is bool:
        return Control.CHECKBOX

    if inner is int or inner is float:
        return Control.NUMBER

    if inner is str:
        return Control.TEXT

    if get_origin(inner) is list and next(iter(get_args(inner)), None) is str:
        return Control.LINES

    if _is_flat_model(inner):
        return None

    # A grant map, a fence block, a list of mounts. Real shapes that a
    # text box would flatten and a checkbox would lie about.
    return Control.BLOCK


def _is_flat_model(inner: object) -> bool:
    """A model whose every field is a scalar, so it can be unfolded.

    `model` and `sandbox` unfold into `model.router`, `sandbox.cpus` and
    the rest. `verbs` does not: its members are fence models, and a form
    that pretended otherwise would drop a fence on save.
    """
    if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
        return False

    return all(_is_scalar(one.annotation) for one in inner.model_fields.values())


def _is_scalar(annotation: object) -> bool:
    inner = _without_none(annotation)

    return inner is str or inner is int or inner is float or inner is bool


def _nested(
    parent: str, annotation: object, value: object, rules: Mapping[str, str]
) -> list[Field]:
    """One flat model, unfolded into `parent.child` controls."""
    inner = _without_none(annotation)

    if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
        return []

    empty: Mapping[str, object] = {}
    body = cast("Mapping[str, object]", value) if isinstance(value, Mapping) else empty
    found: list[Field] = []

    for child, info in inner.model_fields.items():
        control = _control(info.annotation)

        if control is None:
            continue

        # A parent a rule forbids forbids each of its children too, and
        # with the parent's own sentence: that is the rule they break.
        inherited = {f"{parent}.{child}": rules[parent]} if parent in rules else {}
        merged = {**rules, **inherited}
        found.append(
            _one(f"{parent}.{child}", control, body.get(child), merged, info.is_required())
        )

    return found


def _one(
    name: str,
    control: Control,
    value: object,
    rules: Mapping[str, str],
    required: bool,
) -> Field:
    return Field(
        name=name,
        control=control,
        value=_rendered(name, control, value),
        checked=value is True,
        locked=rules.get(name, ""),
        note=_note(control),
        required=required,
    )


def _rendered(name: str, control: Control, value: object) -> str:
    if control is Control.CHECKBOX:
        return ""

    if control is Control.LINES:
        if not isinstance(value, list):
            return ""

        return "".join(f"{one}\n" for one in cast("list[object]", value))

    if control is Control.BLOCK:
        # The key line is part of the text, so what the reader edits is
        # exactly what the file will hold.
        return to_yaml({name: value}) if value is not None else f"{name}: null\n"

    return "" if value is None else str(value)


def _note(control: Control) -> str:
    if control is Control.LINES:
        return "one name per line"

    if control is Control.BLOCK:
        return "YAML, including the key line"

    return ""


def _chunk(form: Form, posted: Mapping[str, str], top: str) -> tuple[str, str]:
    """One top-level field's YAML, or the reason it could not be built."""
    own = form.field(top)

    if own is not None:
        return _leaf_chunk(own, posted)

    return _block_chunk(form, posted, top)


def _leaf_chunk(own: Field, posted: Mapping[str, str]) -> tuple[str, str]:
    if own.control is Control.BLOCK and own.free:
        return _raw_block(own, posted)

    value, present = _saved(own, posted)

    if not present:
        return "", ""

    return to_yaml({own.name: value}), ""


def _block_chunk(form: Form, posted: Mapping[str, str], top: str) -> tuple[str, str]:
    """A flat model, rebuilt from its `parent.child` controls."""
    body: dict[str, object] = {}

    for own in form.fields:
        if not own.name.startswith(f"{top}."):
            continue

        value, present = _saved(own, posted)

        if present:
            body[own.name.split(".", 1)[1]] = value

    if not body:
        return "", ""

    return to_yaml({top: body}), ""


def _saved(own: Field, posted: Mapping[str, str]) -> tuple[object, bool]:
    """What a control contributes to the document, lock included.

    A locked OPTIONAL field is dropped. A disabled control posts nothing,
    and keeping its old value would fail the save on the rule the page is
    displaying. A locked REQUIRED field keeps what it has: `name` and
    `kind` cannot be changed and cannot be absent either.
    """
    if not own.locked:
        return _value_of(own, posted)

    if not own.required:
        return None, False

    return _held(own)


def _held(own: Field) -> tuple[object, bool]:
    """A field's current value, without consulting the post."""
    if own.control is Control.CHECKBOX:
        return own.checked, True

    return _value_of(own, {own.name: own.value})


def _raw_block(own: Field, posted: Mapping[str, str]) -> tuple[str, str]:
    """A YAML sub-document, as the reader typed it."""
    text = posted.get(own.name, own.value)[:MAX_FIELD_CHARS]
    first = next((one for one in text.splitlines() if one.strip()), "")

    if not first:
        return "", ""

    # The block must define its own key and no other. Without this check
    # a pasted block could add a second top-level key to the document,
    # and YAML would keep the last one silently.
    if not first.startswith(f"{own.name}:"):
        return "", f"the {own.name} block must start with '{own.name}:'"

    return text if text.endswith("\n") else text + "\n", ""


def _value_of(own: Field, posted: Mapping[str, str]) -> tuple[object, bool]:
    """One control's value, and whether the document should carry it."""
    if own.control is Control.CHECKBOX:
        # An unchecked box posts nothing, which is the answer "false".
        return own.name in posted, True

    raw = posted.get(own.name)

    if raw is None:
        return None, False

    text = raw.strip()[:MAX_FIELD_CHARS]

    if own.control is Control.LINES:
        return [one.strip() for one in text.splitlines() if one.strip()], True

    if not text:
        # An emptied box removes the key, so an optional field can be
        # cleared and a required one is refused by the schema, loudly.
        return None, False

    if own.control is Control.NUMBER:
        return _number(text), True

    return text, True


def _number(text: str) -> object:
    """An int when it is one, a float when it is one, else the text.

    Text that is not a number goes to the schema as text, so the reader
    sees the schema's own message about it rather than this module's.
    """
    try:
        return int(text)
    except ValueError:
        pass

    try:
        return float(text)
    except ValueError:
        return text
