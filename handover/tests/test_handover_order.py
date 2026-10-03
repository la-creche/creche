"""Deploy order from `depends_on`, and the cycle refusal of contract 06 §10."""

from __future__ import annotations

import pytest
from handover.catalog import LAST_IN_ORDER
from handover.errors import Refusal, RefusalCode
from handover.manifest import parse_manifest
from handover.order import Graph, build_graph, check_acyclic, deploy_order
from handover_fixtures import manifest_text

#: The edges the repo's own manifests declare, written out so an order test
#: reads without a fixture tree.
REPO_GRAPH: Graph = {
    "chaperone": (),
    "infra": (),
    "playpen": (),
    "attendance": ("chaperone", "playpen"),
    "caregiver": ("chaperone", "infra"),
    "noticeboard": ("attendance", "caregiver"),
    "handover": (),
}


def _graph(edges: dict[str, str]) -> Graph:
    """Parse one manifest per name so the graph comes through the real reader."""
    manifests = {
        name: parse_manifest(manifest_text(name, depends_on=depends), f"{name}/component.yaml")
        for name, depends in edges.items()
    }

    return build_graph(manifests)


def test_a_dependency_outside_the_catalog_is_refused() -> None:
    with pytest.raises(Refusal) as caught:
        _graph({"chaperone": "attendance, infra", "noticeboard": "not-a-component"})

    assert caught.value.code is RefusalCode.CATALOG
    assert "contract 06 §1 omits" in caught.value.detail


def test_a_self_edge_is_a_cycle() -> None:
    with pytest.raises(Refusal) as caught:
        _graph({"chaperone": "chaperone"})

    assert caught.value.code is RefusalCode.CYCLE
    assert caught.value.detail == "chaperone -> chaperone"


def test_a_cycle_is_printed() -> None:
    graph = _graph(
        {"chaperone": "noticeboard", "noticeboard": "attendance", "attendance": "chaperone"}
    )

    with pytest.raises(Refusal) as caught:
        check_acyclic(graph)

    assert caught.value.code is RefusalCode.CYCLE

    # A printed cycle closes on itself: the first name is the last name.
    names = caught.value.detail.split(" -> ")
    assert len(names) == 4
    assert names[0] == names[-1]


def test_an_acyclic_graph_passes() -> None:
    check_acyclic(REPO_GRAPH)


def test_dependencies_deploy_first() -> None:
    order = deploy_order(
        REPO_GRAPH, frozenset({"noticeboard", "attendance", "chaperone", "playpen"})
    )

    assert order.index("chaperone") < order.index("attendance")
    assert order.index("playpen") < order.index("attendance")
    assert order.index("attendance") < order.index("noticeboard")


def test_a_dependency_outside_the_release_takes_no_place() -> None:
    order = deploy_order(REPO_GRAPH, frozenset({"noticeboard"}))

    assert order == ("noticeboard",)


def test_handover_is_always_last() -> None:
    order = deploy_order(
        REPO_GRAPH, frozenset({"handover", "chaperone", "noticeboard", "attendance"})
    )

    assert order[-1] == LAST_IN_ORDER


def test_handover_alone_still_deploys() -> None:
    assert deploy_order(REPO_GRAPH, frozenset({"handover"})) == ("handover",)


def test_an_empty_release_orders_nothing() -> None:
    assert deploy_order(REPO_GRAPH, frozenset()) == ()


def test_the_order_does_not_depend_on_insertion_order() -> None:
    forwards = deploy_order(REPO_GRAPH, frozenset(REPO_GRAPH))
    backwards = deploy_order(dict(reversed(list(REPO_GRAPH.items()))), frozenset(REPO_GRAPH))

    assert forwards == backwards
