"""Packet C2's seam tests.

A package, not a bare directory, so this `conftest.py` imports as
`tests_manager.conftest` and cannot collide with the one another
integration directory owns. The repo's usual dodge -- a helper module
with a unique basename and no `conftest.py` at all -- is the same fix by
a different route (root `AGENTS.md`'s gotcha)."""
