"""Deploy order from `depends_on`, and the cycle refusal of contract 06 §10."""

from __future__ import annotations

import pytest
from agent_release.catalog import LAST_IN_ORDER
from agent_release.errors import Refusal, RefusalCode
from agent_release.manifest import parse_manifest
from agent_release.order import Graph, build_graph, check_acyclic, deploy_order
from release_fixtures import manifest_text

#: The edges the repo's own manifests declare, written out so an order test
#: reads without a fixture tree.
REPO_GRAPH: Graph = {
    "pep": (),
    "infra": (),
    "playpen": (),
    "sessiond": ("pep", "playpen"),
    "managerd": ("pep", "infra"),
    "ui": ("sessiond", "managerd"),
    "releasectl": (),
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
        _graph({"pep": "sessiond, infra", "ui": "not-a-component"})

    assert caught.value.code is RefusalCode.CATALOG
    assert "contract 06 §1 omits" in caught.value.detail


def test_a_self_edge_is_a_cycle() -> None:
    with pytest.raises(Refusal) as caught:
        _graph({"pep": "pep"})

    assert caught.value.code is RefusalCode.CYCLE
    assert caught.value.detail == "pep -> pep"


def test_a_cycle_is_printed() -> None:
    graph = _graph({"pep": "ui", "ui": "sessiond", "sessiond": "pep"})

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
    order = deploy_order(REPO_GRAPH, frozenset({"ui", "sessiond", "pep", "playpen"}))

    assert order.index("pep") < order.index("sessiond")
    assert order.index("playpen") < order.index("sessiond")
    assert order.index("sessiond") < order.index("ui")


def test_a_dependency_outside_the_release_takes_no_place() -> None:
    order = deploy_order(REPO_GRAPH, frozenset({"ui"}))

    assert order == ("ui",)


def test_releasectl_is_always_last() -> None:
    order = deploy_order(REPO_GRAPH, frozenset({"releasectl", "pep", "ui", "sessiond"}))

    assert order[-1] == LAST_IN_ORDER


def test_releasectl_alone_still_deploys() -> None:
    assert deploy_order(REPO_GRAPH, frozenset({"releasectl"})) == ("releasectl",)


def test_an_empty_release_orders_nothing() -> None:
    assert deploy_order(REPO_GRAPH, frozenset()) == ()


def test_the_order_does_not_depend_on_insertion_order() -> None:
    forwards = deploy_order(REPO_GRAPH, frozenset(REPO_GRAPH))
    backwards = deploy_order(dict(reversed(list(REPO_GRAPH.items()))), frozenset(REPO_GRAPH))

    assert forwards == backwards
