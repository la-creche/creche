"""Deploy order from `depends_on` (contract 06 §8, §9, §10).

Two jobs. Refuse a cycle, naming it. Then order the components this release
deploys, dependencies first. `handover` is forced last whatever the graph
says, because it cannot replace the code under its own running process
(contract 06 §1.1 rule 1).

The order is deterministic: ties break on the component name, so the same set
always hashes to the same manifest.
"""

from __future__ import annotations

from .catalog import CATALOG_BY_NAME, LAST_IN_ORDER
from .errors import Refusal, RefusalCode, safe_token
from .manifest import ComponentManifest

Graph = dict[str, tuple[str, ...]]


def build_graph(manifests: dict[str, ComponentManifest]) -> Graph:
    """`name -> the components it waits for`, checked against the catalog."""
    graph: Graph = {}
    for name, manifest in manifests.items():
        for dependency in manifest.depends_on:
            if dependency not in CATALOG_BY_NAME:
                detail = f"depends_on names {safe_token(dependency)}, which contract 06 §1 omits"
                raise Refusal(RefusalCode.CATALOG, name, detail)

            if dependency == name:
                raise Refusal(RefusalCode.CYCLE, name, f"{name} -> {name}")

        graph[name] = manifest.depends_on

    return graph


def _find_cycle(graph: Graph) -> list[str] | None:
    """Depth-first search that returns the first cycle it closes, in order."""
    done: set[str] = set()
    on_stack: list[str] = []
    in_stack: set[str] = set()

    def walk(name: str) -> list[str] | None:
        if name in done:
            return None

        if name in in_stack:
            return [*on_stack[on_stack.index(name) :], name]

        on_stack.append(name)
        in_stack.add(name)
        for dependency in graph.get(name, ()):
            cycle = walk(dependency)
            if cycle is not None:
                return cycle

        in_stack.discard(name)
        on_stack.pop()
        done.add(name)

        return None

    for name in sorted(graph):
        cycle = walk(name)
        if cycle is not None:
            return cycle

    return None


def check_acyclic(graph: Graph) -> None:
    """Contract 06 §10: a cycle is refused, and the refusal prints the cycle."""
    cycle = _find_cycle(graph)
    if cycle is None:
        return

    raise Refusal(RefusalCode.CYCLE, cycle[0], " -> ".join(cycle))


def deploy_order(graph: Graph, deploying: frozenset[str]) -> tuple[str, ...]:
    """The components this release deploys, dependencies first.

    A dependency outside `deploying` is already live and needs no place in the
    order. `handover` is appended last, whatever its edges say.
    """
    check_acyclic(graph)

    placed: list[str] = []
    seen: set[str] = set()

    def place(name: str) -> None:
        # `handover` never arrives through an edge. It is appended below.
        if name in seen or name not in deploying or name == LAST_IN_ORDER:
            return

        seen.add(name)
        for dependency in sorted(graph.get(name, ())):
            place(dependency)

        placed.append(name)

    for name in sorted(deploying):
        if name != LAST_IN_ORDER:
            place(name)

    if LAST_IN_ORDER in deploying:
        placed.append(LAST_IN_ORDER)

    return tuple(placed)
