"""`bounded_yaml.load` is `yaml.safe_load` with two limits on merge keys.

Under the limits the two readers give the same value and the same error.
Past a limit, `bounded_yaml.load` raises `MergeLimitError`, which is a
`yaml.YAMLError` with the line of the mapping.

The limits are those of the Rust reader of a manifest: a chain of 128 merge
keys, and 65,536 copied pairs in one document.
"""

from __future__ import annotations

from typing import Final

import pytest
import yaml
from chaperone.bounded_yaml import MERGE_DEPTH_MAX, MERGE_PAIRS_MAX, MergeLimitError

from chaperone import bounded_yaml

#: The pairs of the mapping that `_copies` merges.
PAIRS: Final = 256


def _chain(levels: int) -> str:
    """A mapping `last` that merges the last mapping of a chain of `levels`
    merge keys.

    The mappings of the chain are items of a list. PyYAML builds `last`
    before it builds an item of that list, so the merge of `last` walks the
    whole chain.
    """
    links = "".join(f"\n  - &m{n} {{<<: *m{n - 1}}}" for n in range(1, levels))

    return "chain:\n  - &m0 {k: 1}" + links + f"\nlast: {{<<: *m{levels - 1}}}\n"


def _copies(merges: int, more: str = "") -> str:
    """A mapping `all` that merges one mapping of `PAIRS` pairs `merges`
    times, and then the mappings that `more` names."""
    pairs = ", ".join(f"k{n}: 1" for n in range(PAIRS))
    aliases = ", ".join(["*a"] * merges)

    return f"one: &one {{z: 1}}\nbase: &a {{{pairs}}}\nall: {{<<: [{aliases}{more}]}}\n"


#: Texts under the limits, with each form of a merge key that PyYAML reads
#: or refuses in a way of its own.
UNDER_THE_LIMITS: Final = (
    pytest.param("", id="no-document"),
    pytest.param("a: 1\nb: [x, y]\n", id="no-merge-key"),
    pytest.param("base: &b {a: 1}\nrow: {<<: *b, c: 2}\n", id="one-merge-key"),
    pytest.param("{<<: [{a: 1}, {a: 2, b: 3}], c: 4}", id="a-list-of-mappings"),
    pytest.param("{<<: {a: 1}, <<: {b: 2}, c: 3}", id="two-merge-keys"),
    pytest.param("{a: 0, <<: {a: 1}}", id="own-key-wins"),
    pytest.param("{<<: {<<: {a: 1}, b: 2}, c: 3}", id="a-merge-in-a-merge"),
    pytest.param("&a {k: v, <<: *a}", id="a-mapping-that-merges-itself"),
    pytest.param("&a {<<: [*a, {y: 2}], k: v}", id="a-list-that-holds-its-mapping"),
    pytest.param("!!set {<<: {a: null}, b: null}", id="a-set"),
    pytest.param("!!str {=: text, <<: {a: 1}}", id="a-text-from-a-mapping"),
    pytest.param("{=: x, a: 1}", id="a-value-key"),
    pytest.param("{<<: 5}", id="merge-of-a-number"),
    pytest.param("{<<: [5]}", id="merge-of-a-list-of-numbers"),
    pytest.param("!!omap [{<<: {a: 1}}]", id="merge-key-in-an-ordered-map"),
    pytest.param("a: 1\n---\nb: 2\n", id="two-documents"),
    pytest.param("a: [1, 2\n", id="list-with-no-end"),
)


def _outcome(text: str, *, bounded: bool) -> tuple[str, object]:
    """What one reader does with `text`: its value, or the type and the
    text of its error."""
    try:
        return "value", bounded_yaml.load(text) if bounded else yaml.safe_load(text)
    except yaml.YAMLError as error:
        return type(error).__name__, str(error)


@pytest.mark.parametrize("text", UNDER_THE_LIMITS)
def test_under_the_limits_the_reader_does_what_pyyaml_does(text: str) -> None:
    assert _outcome(text, bounded=True) == _outcome(text, bounded=False)


def test_a_chain_of_merge_keys_at_the_limit_reads() -> None:
    value = bounded_yaml.load(_chain(MERGE_DEPTH_MAX))

    assert isinstance(value, dict)
    assert value["last"] == {"k": 1}


def test_a_chain_of_merge_keys_one_level_past_the_limit_is_refused() -> None:
    with pytest.raises(MergeLimitError) as caught:
        bounded_yaml.load(_chain(MERGE_DEPTH_MAX + 1))

    assert f"more than {MERGE_DEPTH_MAX} levels" in str(caught.value)


def test_copied_pairs_at_the_limit_read() -> None:
    assert PAIRS * PAIRS == MERGE_PAIRS_MAX

    value = bounded_yaml.load(_copies(PAIRS))

    assert isinstance(value, dict)
    assert len(value["all"]) == PAIRS


def test_one_copied_pair_past_the_limit_is_refused() -> None:
    with pytest.raises(MergeLimitError) as caught:
        bounded_yaml.load(_copies(PAIRS, more=", *one"))

    assert f"more than {MERGE_PAIRS_MAX} pairs" in str(caught.value)


def test_the_refusal_is_a_yaml_error_with_the_line_of_the_mapping() -> None:
    with pytest.raises(yaml.MarkedYAMLError) as caught:
        bounded_yaml.load(_copies(PAIRS + 1))

    mark = caught.value.problem_mark
    assert mark is not None
    assert mark.line == 1


def test_the_count_is_of_one_document() -> None:
    """A text at the limit reads again after another text at the limit."""
    text = _copies(PAIRS)

    assert bounded_yaml.load(text) == bounded_yaml.load(text)
