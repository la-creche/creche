"""YAML text to a model, or to issues. Never an exception (invariant 19).

A parse failure is data: the caller gets `None` plus every issue pydantic and
the YAML reader could see, and the family keeps its last good state."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, cast

import yaml
from pydantic import BaseModel, ValidationError

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

#: A list index in a container path. The index does not change which fields
#: the container knows, so every index collapses onto one key.
ANY_INDEX: Final = "*"

#: The two refusals for a text that is past the syntax check and still has no
#: value. Each one is fixed text: the message of the interpreter changes with
#: its version, and a report is the same on each of them.
_NESTS_TOO_DEEP: Final = "YAML will not parse: the text nests too deep"
_NO_VALUE: Final = "YAML will not parse: the text holds a value that cannot be read"

#: The two limits on merge keys: the longest chain of merge keys that the
#: reader follows, and the most entries that the merge keys of one text
#: make. A merged value with no entry counts as one entry. The reader
#: refuses a text past a limit, so that a short text cannot use much time
#: and memory.
#:
#: CONTRACT-QUESTION: contract 01 §1 and contract 01b §1 give no limit for
#: merge keys, and PyYAML has none. The reading here is two limits. The
#: numbers are those of the Rust reader of the same files
#: (`rust/crates/agent-family/src/yaml/construct.rs`), so the two readers
#: refuse the same texts. Another number costs one line here and one line
#: there.
_MERGE_DEPTH_MAX: Final = 400
_MERGED_ENTRIES_MAX: Final = 100_000

#: The two refusals for a text past a limit on merge keys.
_MERGE_TOO_DEEP: Final = "YAML will not parse: the merge keys nest too deep"
_MERGE_TOO_MANY: Final = "YAML will not parse: the merge keys make too many entries"

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


def _ints_print(documents: list[Any]) -> bool:
    """Whether each integer of the documents has a decimal text.

    The interpreter refuses to print an integer past its digit limit. The
    limit does not apply when YAML reads a scalar in base 2, 8, 16 or 60, so
    the reader can give such an integer. Each message that names the integer
    then raises. The walk keeps a stack and the identity of each container:
    a deep document costs no recursion, and an anchor that holds itself ends."""
    seen: set[int] = set()
    stack: list[object] = [documents]
    while stack:
        item = stack.pop()
        identity = id(item)
        if isinstance(item, int):
            try:
                str(item)
            except ValueError:
                return False

            continue

        if not isinstance(item, (dict, list, tuple, set)) or identity in seen:
            continue

        seen.add(identity)
        if isinstance(item, dict):
            stack.extend(cast("dict[object, object]", item).values())

        stack.extend(cast("Iterable[object]", item))

    return True


class _MergeRefusal(Exception):
    """A text past a limit on merge keys. The argument is the refusal."""


class _BoundedLoader(yaml.SafeLoader):
    """The safe loader of PyYAML, with the two limits on merge keys.

    PyYAML puts the entries of each merge key into its mapping in
    `flatten_mapping`, and that function calls itself for the value of a
    merge key. This class counts the levels of those calls and the entries
    of each value, while PyYAML does the work. The count of entries is for
    the text: a second document does not start it again."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._merge_depth = 0
        self._merged_entries = 0

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if self._merge_depth > _MERGE_DEPTH_MAX:
            raise _MergeRefusal(_MERGE_TOO_DEEP)

        self._merge_depth += 1
        try:
            super().flatten_mapping(node)
        finally:
            self._merge_depth -= 1

        if self._merge_depth == 0:
            return

        # The caller is the merge key of another mapping. It takes these
        # entries next. A value with no entry counts as one entry, so that
        # the limit also holds the count of values that a text merges.
        self._merged_entries += max(1, len(node.value))
        if self._merged_entries > _MERGED_ENTRIES_MAX:
            raise _MergeRefusal(_MERGE_TOO_MANY)


def _read_documents(text: str, issues: list[Issue]) -> list[Any] | None:
    """Each document of the text, or None and the reason in `issues`."""
    try:
        # `load_all` makes the loader at the first document. The loader
        # reads each character when it starts, so that refusal is inside
        # this `try` too.
        documents = list(yaml.load_all(text, Loader=_BoundedLoader))
    except _MergeRefusal as exc:
        issues.append(Issue(Severity.ERROR, "<document>", str(exc)))
        return None
    except yaml.YAMLError as exc:
        issues.append(Issue(Severity.ERROR, "<document>", f"YAML will not parse: {exc}"))
        return None
    except RecursionError:
        issues.append(Issue(Severity.ERROR, "<document>", _NESTS_TOO_DEEP))
        return None
    except Exception:
        # Every exception, not a list of types. The reader builds a value
        # with `int`, `float`, a date and a table of words, and each one
        # raises its own type for a scalar that has no value: ValueError,
        # OverflowError, IndexError, KeyError and AttributeError.
        issues.append(Issue(Severity.ERROR, "<document>", _NO_VALUE))
        return None

    if not _ints_print(documents):
        issues.append(Issue(Severity.ERROR, "<document>", _NO_VALUE))
        return None

    return documents


def _one_document(text: str, issues: list[Issue]) -> dict[str, Any] | None:
    """Contract 01 §1 rules 3 and 4: one YAML document, a mapping at the top."""
    documents = _read_documents(text, issues)
    if documents is None:
        return None

    if len(documents) > 1:
        issues.append(
            Issue(Severity.ERROR, "<document>", "one YAML document per file; found more than one")
        )
        return None

    body: object = documents[0] if documents else None
    if not isinstance(body, dict):
        found = type(body).__name__
        issues.append(
            Issue(Severity.ERROR, "<document>", f"the top level must be a mapping; found {found}")
        )
        return None

    return cast("dict[str, Any]", body)


def _load[T: BaseModel](
    text: str, model: type[T], containers: dict[tuple[str, ...], tuple[str, ...]]
) -> tuple[T | None, list[Issue]]:
    issues: list[Issue] = []
    body = _one_document(text, issues)
    if body is None:
        return None, issues

    try:
        return model.model_validate(body), issues
    except ValidationError as exc:
        issues.extend(_issues_from(exc, containers))
        return None, issues


def parse_family(text: str) -> tuple[FamilyFile | None, list[Issue]]:
    return _load(text, FamilyFile, _FAMILY_CONTAINERS)


def parse_server(text: str) -> tuple[McpServerFile | None, list[Issue]]:
    return _load(text, McpServerFile, _SERVER_CONTAINERS)
