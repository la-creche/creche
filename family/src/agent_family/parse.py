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

#: The tag PyYAML gives a `<<` merge key.
_MERGE_TAG: Final = "tag:yaml.org,2002:merge"

#: The two bounds on merge keys. The constructor of PyYAML copies the entries
#: of each merged mapping into the mapping that holds the `<<` key. A chain of
#: merged aliases then doubles the work at each level, so one small file can
#: make the reader use time and memory with no bound. PyYAML sets no limit.
#:
#: CONTRACT-QUESTION: contract 01 gives no limit for merge keys. The reading
#: here is a limit, so that a short text cannot use much time and memory. The
#: Rust reader of a family file takes these same two numbers
#: (`rust/crates/agent-family/src/yaml/construct.rs`), so that Python and Rust
#: refuse the same documents. Another number costs one line here.
_MERGE_DEPTH_MAX: Final = 400
_MERGED_ENTRIES_MAX: Final = 100_000

#: The two refusals for merge keys past a bound. The Rust reader gives the
#: same two texts, so a report is the same on Python and on Rust.
_MERGE_TOO_MANY: Final = "YAML will not parse: the merge keys make too many entries"
_MERGE_TOO_DEEP: Final = "YAML will not parse: the merge keys nest too deep"

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


def _pairs(node: yaml.MappingNode) -> list[tuple[yaml.Node, yaml.Node]]:
    """The entries of a mapping node. The stubs leave `node.value` untyped."""
    return cast("list[tuple[yaml.Node, yaml.Node]]", node.value)


def _merge_targets(node: yaml.MappingNode) -> list[yaml.MappingNode]:
    """Each mapping that a `<<` key of `node` merges: one mapping, or each
    mapping of a sequence. A merge value of another shape is left to the
    constructor, which names the error."""
    out: list[yaml.MappingNode] = []
    for key, value in _pairs(node):
        if key.tag != _MERGE_TAG:
            continue

        if isinstance(value, yaml.MappingNode):
            out.append(value)
        elif isinstance(value, yaml.SequenceNode):
            items = cast("list[yaml.Node]", value.value)
            out.extend(item for item in items if isinstance(item, yaml.MappingNode))

    return out


def _own_entries(node: yaml.MappingNode) -> int:
    """The entries of a mapping node that no `<<` key adds."""
    return sum(1 for key, _ in _pairs(node) if key.tag != _MERGE_TAG)


def _mapping_nodes(roots: list[yaml.Node]) -> list[yaml.MappingNode]:
    """Every mapping node of the graph, each one once."""
    seen: set[int] = set()
    work: list[yaml.Node] = list(roots)
    found: list[yaml.MappingNode] = []
    while work:
        node = work.pop()
        if id(node) in seen:
            continue

        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            found.append(node)
            for key, value in _pairs(node):
                work.append(key)
                work.append(value)
        elif isinstance(node, yaml.SequenceNode):
            work.extend(cast("list[yaml.Node]", node.value))

    return found


def _measure_merges(roots: list[yaml.Node]) -> str | None:
    """A refusal for a graph whose merge keys pass a bound, or None.

    The flattened size of a mapping is its own entries plus the flattened
    size of each mapping its `<<` keys merge. An aliased mapping is one shared
    node, so this walk measures that size once for each node and refuses a
    graph before any value is built. The entry count matches the running
    total of the Rust constructor: the full size of each merged mapping, once
    for each place a `<<` key names it."""
    flat: dict[int, int] = {}
    depth: dict[int, int] = {}
    done: set[int] = set()
    mappings = _mapping_nodes(roots)
    for start in mappings:
        if id(start) in done:
            continue

        started: set[int] = set()
        stack: list[tuple[yaml.MappingNode, bool]] = [(start, False)]
        while stack:
            node, ready = stack.pop()
            key = id(node)
            if ready:
                entries = _own_entries(node)
                levels = 0
                targets = _merge_targets(node)
                for target in targets:
                    entries = min(entries + flat.get(id(target), 0), _MERGED_ENTRIES_MAX + 1)
                    levels = max(levels, depth.get(id(target), 0))

                flat[key] = entries
                depth[key] = levels + 1 if targets else 0
                done.add(key)
                if depth[key] > _MERGE_DEPTH_MAX:
                    return _MERGE_TOO_DEEP

                continue

            if key in done or key in started:
                continue

            started.add(key)
            stack.append((node, True))
            stack.extend((target, False) for target in _merge_targets(node))

    total = 0
    for node in mappings:
        for target in _merge_targets(node):
            total = min(total + flat[id(target)], _MERGED_ENTRIES_MAX + 1)
            if total > _MERGED_ENTRIES_MAX:
                return _MERGE_TOO_MANY

    return None


def _merge_bound(text: str) -> str | None:
    """A refusal for a document whose merge keys pass a bound, or None.

    The reader composes the node graph first. Compose shares an aliased node,
    so that step is cheap. A text with no `<<` token makes no merge, so the
    common file skips the walk."""
    if "<<" not in text:
        return None

    loader = yaml.SafeLoader(text)
    roots: list[yaml.Node] = []
    try:
        while loader.check_node():
            node = loader.get_node()
            if node is not None:
                roots.append(node)
    except Exception:
        # A text the composer refuses has no node graph. `_read_documents`
        # reads it next and names the error, so this check defers.
        return None
    finally:
        loader.dispose()

    return _measure_merges(roots)


def _read_documents(text: str, issues: list[Issue]) -> list[Any] | None:
    """Each document of the text, or None and the reason in `issues`."""
    too_many_merges = _merge_bound(text)
    if too_many_merges is not None:
        issues.append(Issue(Severity.ERROR, "<document>", too_many_merges))
        return None

    try:
        documents = list(yaml.safe_load_all(text))
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
