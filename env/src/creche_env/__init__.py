"""creche-env: the one reader of the env files of the product.

`envfile` reads `default.env`, `example.env` and a `.env` file in one
grammar and resolves the layers. `cli` is the command `creche-env`, which a
script calls in place of a reader of its own. `env/AGENTS.md` has the rules.
"""

__all__ = ["__doc__"]
