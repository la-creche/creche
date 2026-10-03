"""chaperone: deterministic per-call authorization proxy (contract 04).

Nothing is re-exported here. `app` pulls in fastapi, httpx and the `mcp`
client stack — ~1.3s and ~70 MB on the host — and a package that re-exported
it would put that behind every importer of a small module. Import the server
from `chaperone.app`, as `__main__` and the tests do, and a decision input
from its own module: `bin`'s gate tests read `family_grants` and
`family_decisions` that way and pay for neither.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
