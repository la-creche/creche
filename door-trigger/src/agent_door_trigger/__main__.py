"""Entry point: `python -m agent_door_trigger`, equivalent to the
`agent-trigger` console script (`pyproject.toml`'s `[project.scripts]`).
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
