"""A YAML read that holds the merge keys and the aliases of one document
to three limits.

A merge key, `<<`, copies the pairs of its value into the mapping that holds
the key. An alias gives one value to many merge keys. The count of copies
can thus grow much faster than the text, and PyYAML has no limit for it. An
`except` clause does not help: the work does not end.

    text --> node graph --> flatten each mapping --> value
                 |                  |
                 |                  `--> count each copied pair, and each
                 |                       level of a chain of merge keys
                 |
                 `--> count each node that an alias stands for

PyYAML composes the whole node graph first. That step shares the node of an
alias, so its cost follows the size of the text. The count of the merge keys
runs where PyYAML copies the pairs, before each copy. A document past a
limit is refused there, with `MergeLimitError`. That error is a
`yaml.YAMLError`: each caller answers as it does for a text that will not
parse.

The two limits of the merge keys and the way to count are those of the Rust
reader of a manifest, `rust/crates/creche-contracts/src/manifest/yaml.rs`.

An alias with no merge key copies nothing in PyYAML: each alias gives the
one value of its anchor. A caller that reads each row of the value does the
work of a copy. With many aliases of one large value, that work grows with
the square of the size of the text. The third limit holds it. The count runs
on the node graph, before PyYAML builds a value. A document past the limit
is refused there, with `AliasLimitError`, which is a `yaml.YAMLError` too.

A document with no merge key and no alias reads as `yaml.safe_load` reads
it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, cast

import yaml

#: The longest chain of merge keys: a `<<` value that has a `<<` value of
#: its own, and so on.
#:
#: CONTRACT-QUESTION: `stage7-releases.md` §4.4 gives the roster no limit
#: for a merge key, and §4.3 gives the secrets file none. No Rust type reads
#: the text of one of the two. The reading here is the smaller pair of
#: limits of the two Rust YAML readers, which is the pair of the manifest
#: reader. Root writes the roster with no merge key. Another number costs
#: one line here and its tests.
MERGE_DEPTH_MAX: Final = 128

#: The largest count of pairs that the merge keys of one document copy.
MERGE_PAIRS_MAX: Final = 65_536


#: The largest count of nodes that the aliases of one document stand for.
#: An alias stands for the node of its anchor and for each node that this
#: node holds. A document with no alias has a count of zero.
#:
#: CONTRACT-QUESTION: `stage7-releases.md` §4.4 gives the roster no limit
#: for an alias, and §4.3 gives the secrets file none. The two Rust YAML
#: readers have no such limit. The reading here is four nodes for each pair
#: of `MERGE_PAIRS_MAX`. Merge keys that copy that many pairs of one scalar
#: each through aliases stand for three nodes for each pair at most, so the
#: two limits of the merge keys stay in reach. Root writes the roster with
#: no alias. Another number costs one line here and its tests.
ALIAS_NODES_MAX: Final = 4 * MERGE_PAIRS_MAX

#: What an alias inside its own anchor adds to the count. The size of that
#: anchor has no end, and PyYAML builds one value that holds itself.
_ALIAS_OF_OPEN_NODE: Final = 1


class MergeLimitError(yaml.MarkedYAMLError):
    """The merge keys of one document pass a limit of this reader."""


class AliasLimitError(yaml.MarkedYAMLError):
    """The aliases of one document pass the limit of this reader."""


class BoundedLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` with the three limits. One loader reads one text."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        #: How many calls of `flatten_mapping` are on the stack.
        self._merge_depth = 0
        self._merged_pairs = 0

    def get_single_node(self) -> yaml.Node | None:
        """PyYAML calls this for the node graph of the one document, before
        it builds a value."""
        root = super().get_single_node()
        if root is not None:
            _hold_aliases(root)

        return root

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        """PyYAML calls this for each mapping that it builds. Its own method
        then calls it for each mapping that a merge key names, and copies
        the pairs of that mapping after the call returns."""
        depth = self._merge_depth
        if depth > MERGE_DEPTH_MAX:
            raise MergeLimitError(
                problem=f"a chain of merge keys has more than {MERGE_DEPTH_MAX} levels",
                problem_mark=node.start_mark,
            )

        self._merge_depth = depth + 1
        try:
            super().flatten_mapping(node)
        finally:
            self._merge_depth = depth

        if depth == 0:
            return

        self._merged_pairs += len(node.value)
        if self._merged_pairs > MERGE_PAIRS_MAX:
            raise MergeLimitError(
                problem=f"the merge keys copy more than {MERGE_PAIRS_MAX} pairs",
                problem_mark=node.start_mark,
            )


@dataclass
class _OpenNode:
    """One node that the walk of `_hold_aliases` is inside."""

    node: yaml.Node
    #: The nodes that `node` holds and the walk did not reach yet.
    rest: Iterator[yaml.Node]
    #: `node` itself, and each node under it that the walk left.
    size: int = 1


def _held(node: yaml.Node) -> Iterator[yaml.Node]:
    """The nodes that `node` holds: each key and each value of a mapping,
    each item of a sequence. A scalar holds no node."""
    if isinstance(node, yaml.MappingNode):
        for key, value in cast("list[tuple[yaml.Node, yaml.Node]]", node.value):
            yield key
            yield value
    elif isinstance(node, yaml.SequenceNode):
        yield from cast("list[yaml.Node]", node.value)


def _hold_aliases(root: yaml.Node) -> None:
    """Raise `AliasLimitError` when the aliases of the node graph of `root`
    stand for more than `ALIAS_NODES_MAX` nodes.

    The composer gives an alias the node of its anchor. The walk thus
    reaches that node one more time for each alias. The first time is the
    anchor, and the walk goes into the node and counts its size. Each later
    time is an alias, which stands for that size.

    The walk goes into each node one time and keeps its own stack, so its
    cost follows the size of the text and not the depth of the graph.
    """
    # The size of each node that the walk left, by the identity of the
    # node. The graph keeps each node alive, so an identity names one node.
    sizes: dict[int, int] = {}
    reached = {id(root)}
    stood_for = 0
    stack = [_OpenNode(root, _held(root))]
    while stack:
        top = stack[-1]
        node = next(top.rest, None)
        if node is None:
            stack.pop()
            sizes[id(top.node)] = top.size
            if stack:
                stack[-1].size += top.size
            continue

        if id(node) not in reached:
            reached.add(id(node))
            stack.append(_OpenNode(node, _held(node)))
            continue

        size = sizes.get(id(node), _ALIAS_OF_OPEN_NODE)
        stood_for += size
        if stood_for > ALIAS_NODES_MAX:
            raise AliasLimitError(
                problem=f"the aliases stand for more than {ALIAS_NODES_MAX} nodes",
                problem_mark=node.start_mark,
            )

        top.size += size


def load(text: str) -> object:
    """The value of the one document in `text`, as `yaml.safe_load` gives
    it. Raises what `yaml.safe_load` raises, `MergeLimitError` and
    `AliasLimitError`."""
    loader = BoundedLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
