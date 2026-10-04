"""`bounded_yaml.load` is `yaml.safe_load` with two limits on merge keys
and one limit on aliases.

Under the limits the two readers give the same value and the same error.
Past a limit of the merge keys, `bounded_yaml.load` raises `MergeLimitError`,
which is a `yaml.YAMLError` with the line of the mapping. Past the limit of
the aliases it raises `AliasLimitError`, which is a `yaml.YAMLError` with
the line of the anchor.

The limits of the merge keys are those of the Rust reader of a manifest: a
chain of 128 merge keys, and 65,536 copied pairs in one document. The
aliases of one document stand for 262,144 nodes at most.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, cast

import pytest
import yaml
from chaperone.bounded_yaml import (
    ALIAS_NODES_MAX,
    MERGE_DEPTH_MAX,
    MERGE_PAIRS_MAX,
    AliasLimitError,
    MergeLimitError,
)

from chaperone import bounded_yaml

#: The pairs of the mapping that `_copies` merges.
PAIRS: Final = 256

#: The two ways that a text writes a merge key: the key `<<`, and the merge
#: tag on a key of another name. PyYAML merges the two in the same way.
MERGE_KEYS: Final = (
    pytest.param("<<", id="the-merge-key"),
    pytest.param("!!merge m", id="the-merge-tag"),
)

#: The merge keys of `_itself` that copy 65,535 pairs: one pair less than
#: the limit. One more merge key copies 131,071 pairs.
ITSELF_AT_THE_LIMIT: Final = 16

#: The items of the list that `_aliases` shares. One alias of that list
#: stands for the list and for each item: 512 nodes.
ITEMS: Final = 511
ALIAS_NODES: Final = ITEMS + 1

#: A text whose aliases stand for 28 nodes. The list `a` is 4 nodes, so `b`
#: is 9 nodes and its two aliases stand for 8. The two aliases of `b` stand
#: for 18, and the two aliases of the text `s` for 2.
NESTED_ALIASES: Final = "a: &a [1, 1, 1]\nb: &b [*a, *a]\nc: [*b, *b]\ns: &s x\nd: {*s : *s}\n"
NESTED_ALIAS_NODES: Final = 28


def _chain(levels: int, key: str = "<<") -> str:
    """A mapping `last` that merges the last mapping of a chain of `levels`
    merge keys. `key` is the text of each merge key.

    The mappings of the chain are items of a list. PyYAML builds `last`
    before it builds an item of that list, so the merge of `last` walks the
    whole chain.
    """
    links = "".join(f"\n  - &m{n} {{{key}: *m{n - 1}}}" for n in range(1, levels))

    return "chain:\n  - &m0 {k: 1}" + links + f"\nlast: {{{key}: *m{levels - 1}}}\n"


def _copies(merges: int, more: str = "", key: str = "<<") -> str:
    """A mapping `all` that merges one mapping of `PAIRS` pairs `merges`
    times, and then the mappings that `more` names. `key` is the text of
    the merge key."""
    pairs = ", ".join(f"k{n}: 1" for n in range(PAIRS))
    aliases = ", ".join(["*a"] * merges)

    return f"one: &one {{z: 1}}\nbase: &a {{{pairs}}}\nall: {{{key}: [{aliases}{more}]}}\n"


def _itself(merges: int, key: str = "<<") -> str:
    """A mapping of one pair that merges itself `merges` times."""
    keys = ", ".join([f"{key}: *a"] * merges)

    return f"&a {{k: v, {keys}}}"


def _aliases(count: int, more: str = "") -> str:
    """A list `all` of `count` aliases of one list of `ITEMS` items, and
    then the aliases that `more` names."""
    items = ", ".join(["1"] * ITEMS)
    aliases = ", ".join(["*a"] * count)

    return f"one: &one 1\nbase: &a [{items}]\nall: [{aliases}{more}]\n"


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
    pytest.param(_itself(3), id="a-mapping-that-merges-itself-three-times"),
    pytest.param("base: &b {a: 1}\nrow: {!!merge m: *b, c: 2}\n", id="the-merge-tag"),
    pytest.param("base: &b {a: 1}\nrow: {!!merge m: [*b, *b]}\n", id="the-merge-tag-on-a-list"),
    pytest.param("&a {<<: [*a, {y: 2}], k: v}", id="a-list-that-holds-its-mapping"),
    pytest.param("!!set {<<: {a: null}, b: null}", id="a-set"),
    pytest.param("!!str {=: text, <<: {a: 1}}", id="a-text-from-a-mapping"),
    pytest.param("{=: x, a: 1}", id="a-value-key"),
    pytest.param("{<<: 5}", id="merge-of-a-number"),
    pytest.param("{<<: [5]}", id="merge-of-a-list-of-numbers"),
    pytest.param("!!omap [{<<: {a: 1}}]", id="merge-key-in-an-ordered-map"),
    pytest.param("a: &a [1, 2]\nb: [*a, *a]\n", id="an-alias"),
    pytest.param("a: &a [1, 2]\nb: {*a : 1}\n", id="an-alias-of-a-list-as-a-key"),
    pytest.param("a: *b\n", id="an-alias-of-no-anchor"),
    pytest.param(NESTED_ALIASES, id="an-alias-in-the-value-of-an-alias"),
    pytest.param("a: 1\n---\nb: 2\n", id="two-documents"),
    pytest.param("a: [1, 2\n", id="list-with-no-end"),
)


def _outcome(load: Callable[[str], object], text: str) -> tuple[str, object]:
    """What one reader does with `text`: its value, or the type and the
    text of its error."""
    try:
        return "value", load(text)
    except yaml.YAMLError as error:
        return type(error).__name__, str(error)


@pytest.mark.parametrize("text", UNDER_THE_LIMITS)
def test_under_the_limits_the_reader_does_what_pyyaml_does(text: str) -> None:
    assert _outcome(bounded_yaml.load, text) == _outcome(yaml.safe_load, text)


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_a_chain_of_merge_keys_at_the_limit_reads(key: str) -> None:
    assert MERGE_DEPTH_MAX == 128

    value = bounded_yaml.load(_chain(MERGE_DEPTH_MAX, key))

    assert isinstance(value, dict)
    assert value["last"] == {"k": 1}


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_a_chain_of_merge_keys_one_level_past_the_limit_is_refused(key: str) -> None:
    with pytest.raises(MergeLimitError) as caught:
        bounded_yaml.load(_chain(MERGE_DEPTH_MAX + 1, key))

    assert f"more than {MERGE_DEPTH_MAX} levels" in str(caught.value)


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_copied_pairs_at_the_limit_read(key: str) -> None:
    assert PAIRS * PAIRS == MERGE_PAIRS_MAX == 65_536

    value = bounded_yaml.load(_copies(PAIRS, key=key))

    assert isinstance(value, dict)
    assert len(value["all"]) == PAIRS


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_one_copied_pair_past_the_limit_is_refused(key: str) -> None:
    with pytest.raises(MergeLimitError) as caught:
        bounded_yaml.load(_copies(PAIRS, more=", *one", key=key))

    assert f"more than {MERGE_PAIRS_MAX} pairs" in str(caught.value)


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_a_mapping_that_merges_itself_reads_under_the_limit(key: str) -> None:
    assert 2**ITSELF_AT_THE_LIMIT - 1 == MERGE_PAIRS_MAX - 1

    assert bounded_yaml.load(_itself(ITSELF_AT_THE_LIMIT, key)) == {"k": "v"}


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_a_mapping_that_merges_itself_past_the_limit_is_refused(key: str) -> None:
    with pytest.raises(MergeLimitError) as caught:
        bounded_yaml.load(_itself(ITSELF_AT_THE_LIMIT + 1, key))

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


def test_alias_nodes_at_the_limit_read() -> None:
    assert ALIAS_NODES * ALIAS_NODES == ALIAS_NODES_MAX == 262_144

    value = bounded_yaml.load(_aliases(ALIAS_NODES))

    assert isinstance(value, dict)
    rows = cast("dict[str, list[object]]", value)
    assert len(rows["all"]) == ALIAS_NODES
    assert all(item is rows["base"] for item in rows["all"])


def test_one_alias_node_past_the_limit_is_refused() -> None:
    with pytest.raises(AliasLimitError) as caught:
        bounded_yaml.load(_aliases(ALIAS_NODES, more=", *one"))

    assert f"more than {ALIAS_NODES_MAX} nodes" in str(caught.value)


def test_the_alias_refusal_is_a_yaml_error_with_the_line_of_the_anchor() -> None:
    with pytest.raises(yaml.MarkedYAMLError) as caught:
        bounded_yaml.load(_aliases(ALIAS_NODES + 1))

    mark = caught.value.problem_mark
    assert mark is not None
    assert mark.line == 1


def test_an_alias_stands_for_each_node_that_its_anchor_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count of `NESTED_ALIASES` is `NESTED_ALIAS_NODES`: the text reads
    with that limit, and does not read with one node less."""
    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", NESTED_ALIAS_NODES)
    assert bounded_yaml.load(NESTED_ALIASES) == yaml.safe_load(NESTED_ALIASES)

    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", NESTED_ALIAS_NODES - 1)
    with pytest.raises(AliasLimitError):
        bounded_yaml.load(NESTED_ALIASES)


def test_a_text_with_no_alias_has_no_node_to_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit holds what the aliases add, and not the size of a text."""
    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", 0)

    assert bounded_yaml.load("a: [1, 2, 3]\nb: {c: d}\n") == {"a": [1, 2, 3], "b": {"c": "d"}}
    with pytest.raises(AliasLimitError):
        bounded_yaml.load("a: &a 1\nb: *a\n")


def test_a_list_alias_inside_its_own_anchor_stands_for_one_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Such an alias has no size that ends. PyYAML reads the text, and the
    value holds itself."""
    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", 2)

    value = bounded_yaml.load("&a [*a, *a]")

    assert isinstance(value, list)
    items = cast("list[object]", value)
    assert items[0] is value
    assert items[1] is value

    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", 1)
    with pytest.raises(AliasLimitError):
        bounded_yaml.load("&a [*a, *a]")


def test_a_mapping_alias_inside_its_own_anchor_stands_for_one_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bounded_yaml, "ALIAS_NODES_MAX", 1)

    value = bounded_yaml.load("&a {k: *a}")

    assert isinstance(value, dict)
    assert cast("dict[str, object]", value)["k"] is value


def test_a_text_whose_aliases_double_is_refused_before_a_value_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each list holds the list before it two times. The limit stops the
    read at the node graph, where the text is still small."""
    levels = 64
    text = "l0: &l0 [1]\n" + "".join(
        f"l{n}: &l{n} [*l{n - 1}, *l{n - 1}]\n" for n in range(1, levels)
    )
    built: list[yaml.Node] = []

    def build(_loader: bounded_yaml.BoundedLoader, node: yaml.Node) -> None:
        built.append(node)

    monkeypatch.setattr(bounded_yaml.BoundedLoader, "construct_document", build)

    with pytest.raises(AliasLimitError):
        bounded_yaml.load(text)

    assert built == []
