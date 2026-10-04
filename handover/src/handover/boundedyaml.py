"""`yaml.safe_load`, with a bound on what the merge keys of a text copy.

PyYAML gives a merge key no limit. A mapping that merges one mapping two
times holds two times its pairs. In a chain of such mappings, each level
holds two times the pairs of the level before. A text of a few hundred
bytes then makes the reader use time and memory with no bound. No `except`
clause stops that: the reader does not raise, and it does not end.

`load` reads as `yaml.safe_load` reads. PyYAML makes the nodes of the whole
text first. An alias there is the node of its anchor and no copy, so that
step costs what the text costs. The constructor then puts the pairs of each
`<<` value into its mapping. This loader counts before each copy:

1. the pairs that the merge keys of the text copy, and
2. the levels of a chain of merge keys: a `<<` value that has a `<<` value
   of its own.

Past a limit it raises `MergeLimit`. That is a `yaml.YAMLError`, so a caller
that refuses each error of the reader refuses this text too.

The Rust readers of the same files count the same two things at the same
places (`rust/crates/creche-contracts/src/manifest/yaml.rs` and
`rust/crates/agent-family/src/yaml/construct.rs`). A caller gives the limits
of the Rust reader of its file, so the two languages refuse the same text.

Pure: no child, no network and no file.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, SequenceNode

MERGE_TAG: Final = "tag:yaml.org,2002:merge"
VALUE_TAG: Final = "tag:yaml.org,2002:value"
STR_TAG: Final = "tag:yaml.org,2002:str"

#: The context and the two problems of an error of a merge key, as PyYAML
#: writes them.
MERGE_CONTEXT: Final = "while constructing a mapping"
NO_MERGE_VALUE: Final = "expected a mapping or list of mappings for merging"
NO_MERGE_ITEM: Final = "expected a mapping for merging"

Pairs = list[tuple[Node, Node]]


@dataclass(frozen=True)
class MergeLimits:
    """The two limits of one reader."""

    #: The longest chain of merge keys.
    depth: int
    #: The most pairs that the merge keys of one text copy.
    pairs: int


class MergeLimit(yaml.YAMLError):
    """The merge keys of a text pass a limit of its reader."""


class MergeTooDeep(MergeLimit):
    """A chain of merge keys is longer than `MergeLimits.depth`."""


class MergeTooLarge(MergeLimit):
    """The merge keys copy more than `MergeLimits.pairs` pairs."""


class _Loader(yaml.SafeLoader):
    """`yaml.SafeLoader`, with the two counts in its merge step."""

    def __init__(self, text: str, limits: MergeLimits) -> None:
        super().__init__(text)
        self._limits = limits
        self._copied = 0

    def flatten_mapping(self, node: MappingNode) -> None:
        self._flatten(node, 0)

    def _flatten(self, node: MappingNode, depth: int) -> None:
        """`SafeConstructor.flatten_mapping` of PyYAML 6.0, step for step.
        It puts the pairs of each `<<` value at the start of the mapping.
        The step changes the node, so a node is flat after its first call.

        One level of a chain is one call of this function and no other
        call, as in PyYAML. The limit of a caller is thus a count of
        Python frames."""
        if depth > self._limits.depth:
            raise MergeTooDeep

        # Each turn reads the pairs of the node again, as PyYAML does. A
        # mapping can merge itself, and the call for the inner one then
        # gives this node a new list.
        merge: Pairs = []
        index = 0
        while index < len(node.value):
            key_node, value_node = cast("tuple[Node, Node]", node.value[index])
            if key_node.tag == MERGE_TAG:
                del node.value[index]
                submerge: list[Pairs] = []
                for named in _named(node, value_node):
                    self._flatten(named, depth + 1)
                    submerge.append(self._counted(named))

                for pairs in reversed(submerge):
                    merge.extend(pairs)
            elif key_node.tag == VALUE_TAG:
                key_node.tag = STR_TAG
                index += 1
            else:
                index += 1

        if merge:
            node.value = merge + node.value

    def _counted(self, node: MappingNode) -> Pairs:
        """The pairs of one flat mapping that a merge key names. The count
        is before the copy, so a text past the limit costs no copy."""
        pairs = cast("Pairs", node.value)
        self._copied += len(pairs)
        if self._copied > self._limits.pairs:
            raise MergeTooLarge

        return pairs


def _named(node: MappingNode, value_node: Node) -> Iterator[MappingNode]:
    """Each mapping that one `<<` value names: the value, or each item of a
    list. The check of an item comes after the work on the item before it,
    as in PyYAML, so the first fault of a text is the same one."""
    if isinstance(value_node, MappingNode):
        yield value_node

        return

    if not isinstance(value_node, SequenceNode):
        problem = f"{NO_MERGE_VALUE}, but found {_id(value_node)}"
        raise ConstructorError(MERGE_CONTEXT, node.start_mark, problem, value_node.start_mark)

    for subnode in cast("list[Node]", value_node.value):
        if not isinstance(subnode, MappingNode):
            problem = f"{NO_MERGE_ITEM}, but found {_id(subnode)}"
            raise ConstructorError(MERGE_CONTEXT, node.start_mark, problem, subnode.start_mark)

        yield subnode


def _id(node: Node) -> str:
    """`scalar`, `sequence` or `mapping`. Each node type of PyYAML has the
    word as a class attribute, and the type of a node declares none."""
    return str(getattr(node, "id", "node"))


def load(text: str, limits: MergeLimits) -> Any:
    """What `yaml.safe_load(text)` gives, or `MergeLimit` for a text whose
    merge keys pass a limit. Each other error of PyYAML leaves as it does
    there."""
    loader = _Loader(text, limits)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
