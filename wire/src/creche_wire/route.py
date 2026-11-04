"""The type of a route of the vmm host API (decision CH25).

`routes.py` holds one constant of this type per route of the table in
`rust/crates/wire/src/routes.rs`. The Rust schema test writes that file. A
constant pairs the route with its request, reply and error types, so pyright
refuses a wrong model on the client.
"""

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class JsonRoute[Request: BaseModel, Reply: BaseModel, Error: BaseModel]:
    """A route that takes a JSON request and gives a JSON reply or a typed
    error (decision CH26 rule 4). A caller matches the error on its `code`
    (decision CH25 rule 3)."""

    name: str
    request: type[Request]
    reply: type[Reply]
    error: type[Error]

    @property
    def path(self) -> str:
        """The path of the route. Each route is `POST /<name>`."""
        return f"/{self.name}"

    def encode(self, body: Request) -> bytes:
        """The JSON text of `body`, the request of this route.

        It also makes `Request` invariant. A dataclass that only holds
        `type[Request]` makes the parameter covariant, and pyright then takes
        a request of a different model."""
        return body.model_dump_json().encode()


@dataclass(frozen=True, slots=True)
class UpgradeRoute[Request: BaseModel, Error: BaseModel]:
    """A route that takes a JSON request and, on success, turns the
    connection into a raw byte stream (decisions CH20, CH25). The client
    sends `Connection: Upgrade` and `Upgrade: <token>`. The vmm answers 101
    with no JSON body, or a typed error before 101 (decision CH26 rule 3)."""

    name: str
    token: str
    request: type[Request]
    error: type[Error]

    @property
    def path(self) -> str:
        """The path of the route. Each route is `POST /<name>`."""
        return f"/{self.name}"

    def encode(self, body: Request) -> bytes:
        """The JSON text of `body`, the request of this route. It also makes
        `Request` invariant, as `JsonRoute.encode` does."""
        return body.model_dump_json().encode()
