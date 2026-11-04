"""The generated route constants are the route table (decision CH25).

`creche_wire.routes` holds one `JsonRoute` or `UpgradeRoute` per entry of
`x-routes` in
`rust/crates/wire/schema/wire.json`. The Rust schema test writes both files,
and it fails when the committed `routes.py` differs. This test holds each
constant equal to its entry, and it reads each route through its types."""

from pathlib import Path
from typing import Any, cast

import creche_wire
import pytest
from creche_wire import routes, strictjson
from creche_wire.route import JsonRoute, UpgradeRoute
from pydantic import BaseModel

WIRE = Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire"
VECTOR_FILE = WIRE / "vectors" / "wire.json"
SCHEMA_FILE = WIRE / "schema" / "wire.json"

#: A cap above each file and each case here, as in `test_wire_vectors.py`.
CAP = 1 << 20

#: The keys of a route entry that name a wire type. An upgrade route has no
#: reply.
ROUTE_TYPES = ("request", "reply", "error")

#: The class of a route of each kind of entry.
KINDS: dict[str, type] = {"json": JsonRoute, "upgrade": UpgradeRoute}


def _load(path: Path) -> dict[str, Any]:
    value = strictjson.loads(path.read_bytes(), cap=CAP)
    assert isinstance(value, dict)

    return cast(dict[str, Any], value)


TABLE: dict[str, Any] = _load(SCHEMA_FILE)["x-routes"]
ENTRIES = cast(dict[str, dict[str, Any]], TABLE["routes"])
VECTORS = cast(dict[str, list[dict[str, Any]]], _load(VECTOR_FILE))


#: A route of any types. `Request` is invariant, so a route of the table is
#: no `JsonRoute[BaseModel, BaseModel, BaseModel]`.
type AnyRoute = JsonRoute[Any, Any, Any] | UpgradeRoute[Any, Any]


def _keys(entry: dict[str, Any]) -> list[str]:
    """The keys of `entry` that name a wire type."""
    return [key for key in ROUTE_TYPES if key in entry]


def _constants() -> dict[str, AnyRoute]:
    """Each route constant of `creche_wire.routes`, by route name."""
    found: list[AnyRoute] = [
        cast(AnyRoute, value)
        for value in vars(routes).values()
        if isinstance(value, JsonRoute | UpgradeRoute)
    ]

    return {route.name: route for route in found}


def test_the_protocol_is_the_one_of_the_table() -> None:
    assert TABLE["protocol"] == routes.HOST_API_PROTOCOL
    assert TABLE["header"] == routes.HOST_API_HEADER
    assert TABLE["body_cap"] == routes.HOST_API_BODY_CAP
    assert TABLE["mismatch"]["status"] == routes.MISMATCH_STATUS
    assert getattr(creche_wire, TABLE["mismatch"]["reply"]) is creche_wire.ProtocolMismatch


def test_each_route_has_one_constant() -> None:
    assert sorted(_constants()) == sorted(ENTRIES)


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_each_constant_is_its_entry(name: str) -> None:
    route = _constants()[name]
    entry = ENTRIES[name]

    assert type(route) is KINDS[entry["kind"]]
    assert getattr(routes, name.upper()) is route
    assert route.path == f"/{name}"
    for key in _keys(entry):
        assert getattr(route, key) is getattr(creche_wire, entry[key]), key
    timeout = getattr(routes, f"{name.upper()}_CLIENT_TIMEOUT_S", None)
    assert timeout == entry.get("client_timeout_s")
    if isinstance(route, UpgradeRoute):
        assert "reply" not in entry
        assert route.token == entry["token"]
    else:
        assert "token" not in entry


def test_the_acp_token_is_the_one_of_the_table() -> None:
    assert routes.ACP.token == ENTRIES["acp"]["token"] == "creche-acp"


ACCEPTED = [
    pytest.param(name, key, case, id=f"{name} {key}: {case['note']}")
    for name, entry in sorted(ENTRIES.items())
    for key in _keys(entry)
    for case in VECTORS[entry[key]]
    if case["written"] is not None
]


@pytest.mark.parametrize(("name", "key", "case"), ACCEPTED)
def test_each_route_reads_its_vectors_through_its_types(
    name: str, key: str, case: dict[str, Any]
) -> None:
    """Each accepted case of a route type reads through the class of the
    route. A request also goes back to the wire through `encode`, and the
    text is what the Rust type writes."""
    route = _constants()[name]
    model: type[BaseModel] = getattr(route, key)
    value = creche_wire.read(model, case["json"].encode("utf-8"), cap=CAP)

    assert value.model_dump_json() == case["written"]
    if key == "request":
        assert route.encode(value) == case["written"].encode("utf-8")
