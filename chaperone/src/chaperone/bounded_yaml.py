"""A YAML read that holds the merge keys of one document to two limits.

A merge key, `<<`, copies the pairs of its value into the mapping that holds
the key. An alias gives one value to many merge keys. The count of copies
can thus grow much faster than the text, and PyYAML has no limit for it. An
`except` clause does not help: the work does not end.

    text --> node graph --> flatten each mapping --> value
                                    |
                                    `--> count each copied pair, and each
                                         level of a chain of merge keys

PyYAML composes the whole node graph first. That step shares the node of an
alias, so its cost follows the size of the text. The count runs where PyYAML
copies the pairs, before each copy. A document past a limit is refused there,
with `MergeLimitError`. That error is a `yaml.YAMLError`: each caller answers
as it does for a text that will not parse.

The two limits and the way to count are those of the Rust reader of a
manifest, `rust/crates/creche-contracts/src/manifest/yaml.rs`. A document
with no merge key reads as `yaml.safe_load` reads it.
"""

from __future__ import annotations

from typing import Final

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


class MergeLimitError(yaml.MarkedYAMLError):
    """The merge keys of one document pass a limit of this reader."""


class BoundedLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` with the two limits. One loader reads one text."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        #: How many calls of `flatten_mapping` are on the stack.
        self._merge_depth = 0
        self._merged_pairs = 0

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


def load(text: str) -> object:
    """The value of the one document in `text`, as `yaml.safe_load` gives
    it. Raises what `yaml.safe_load` raises, and `MergeLimitError`."""
    loader = BoundedLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
