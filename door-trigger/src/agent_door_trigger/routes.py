"""The webhook route table: which `(family, name)` pairs this door will
answer, and each one's expected token.

Two independent gates must both pass before a route is live, so an invalid
family file never opens a route:

1. `families.py`: the family is `autonomous` and has an applied, valid
   definition (contract 05's status document).
2. The registry's own family file (contract 01 §3.13) currently declares
   that webhook name.

Gate 1 is what makes the severe case safe: a family that has never
validated has no status document worth trusting, so it never reaches
gate 2 at all. `README.md`'s Known gaps names the narrower race this
does NOT close (an already-valid family edited to ADD a webhook name
managerd has not yet re-validated).

A route also needs its own token file (`tokens.py`). Three ways to be
"not a route" — unknown family, undeclared name, unconfigured token —
collapse to one outcome here: absence from this table. `webhooks.py`
turns every absence into the same 404, so the listener leaks nothing about
which triggers exist.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_family import FamilyFile, load_registry

from .families import AutonomousFamilies
from .tokens import read_webhook_token, webhook_token_file

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    family: str
    name: str
    token: str


class RouteLookup(Protocol):
    """What `webhooks.py` needs from a route table: look one up, and
    refresh the whole set. A test double implements this without a real
    registry or a real status directory behind it
    (`test_trigger_webhooks_app.py`'s `FakeRouteTable`)."""

    def get(self, family: str, name: str) -> Route | None: ...

    def refresh(self) -> int: ...


class RouteTable:
    """Refreshed on a timer and on demand (SIGHUP, `webhooks.py`). Reads
    never block on a refresh: a refresh builds a whole new mapping and
    swaps one reference under a lock, so a request never observes half of
    an update."""

    def __init__(
        self,
        *,
        registry_root: Path,
        webhooks_dir: Path,
        families: AutonomousFamilies,
    ) -> None:
        self._registry_root = registry_root
        self._webhooks_dir = webhooks_dir
        self._families = families
        self._routes: dict[tuple[str, str], Route] = {}
        self._lock = threading.Lock()

    def refresh(self) -> int:
        """Rebuild the table. Returns the route count, for a log line."""
        routes = _build(self._registry_root, self._webhooks_dir, self._families)
        with self._lock:
            self._routes = routes

        return len(routes)

    def get(self, family: str, name: str) -> Route | None:
        with self._lock:
            return self._routes.get((family, name))


def _build(
    registry_root: Path, webhooks_dir: Path, families: AutonomousFamilies
) -> dict[tuple[str, str], Route]:
    servable = families.servable()
    if not servable:
        return {}

    registry = load_registry(registry_root)
    routes: dict[tuple[str, str], Route] = {}

    for name, family in registry.families.items():
        if name not in servable:
            continue

        for webhook in _webhook_names(family):
            route = _route_of(name, webhook, webhooks_dir)
            if route is not None:
                routes[(name, webhook)] = route

    return routes


def _webhook_names(family: FamilyFile) -> list[str]:
    triggers = family.triggers or ()
    return [trigger.webhook for trigger in triggers if trigger.webhook is not None]


def _route_of(family: str, name: str, webhooks_dir: Path) -> Route | None:
    token = read_webhook_token(webhook_token_file(webhooks_dir, family, name))
    if token is None:
        _LOG.warning("trigger route %s/%s has no usable token file; not serving it", family, name)
        return None

    return Route(family=family, name=name, token=token)
