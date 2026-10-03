"""Every setting in `creche-chaperone.service` is one something still reads.

The unit outlived the old system: five `Environment=` lines named its paths
and nothing read them, and four `ReadWritePaths` entries without the leading
`-` named its directories. Those four were worse than dead. systemd refuses
to START a unit when such an entry is absent (226/NAMESPACE), so removing a
directory the PEP never used would have stopped the one enforcement point.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
UNIT: Final = REPO / "systemd" / "creche-chaperone.service"
SOURCE: Final = REPO / "chaperone" / "src" / "chaperone"

#: Read by a child, never by name in `chaperone/src`. `sops -d` inherits the PEP's
#: environment: it is found on PATH and reads SOPS_AGE_KEY_FILE. HOME and PATH
#: are two of the six variables the MCP SDK gives every child it starts.
FOR_A_CHILD: Final = frozenset({"HOME", "PATH", "SOPS_AGE_KEY_FILE"})

ENVIRONMENT: Final = "Environment="
RW_PATHS: Final = "ReadWritePaths="

#: systemd's "skip this entry when the path is absent" prefix.
OPTIONAL: Final = "-"


def _directives() -> list[str]:
    text = UNIT.read_text(encoding="utf-8")

    return [line.strip() for line in text.splitlines() if line.strip() and line[0] not in "#;"]


def _environment() -> dict[str, str]:
    pairs = (
        one.removeprefix(ENVIRONMENT).split("=", 1)
        for one in _directives()
        if one.startswith(ENVIRONMENT)
    )

    return {name: value for name, value in pairs}


def _strings_in_source() -> set[str]:
    """Every string constant in the package. A variable the PEP reads is one
    of them: `os.environ.get("PEP_BIND")`, `BIND_ENV = "PEP_BIND"`."""
    found: set[str] = set()
    for module in SOURCE.glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        found |= {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

    return found


def test_every_variable_is_one_the_pep_or_a_child_reads() -> None:
    named = _strings_in_source() | FOR_A_CHILD

    assert [one for one in _environment() if one not in named] == []


def test_every_rw_path_without_the_dash_is_one_a_variable_names() -> None:
    """An entry without the `-` must exist when the unit starts. So it is a
    directory the PEP is pointed at, never one a retired system left."""
    line = next(one for one in _directives() if one.startswith(RW_PATHS))
    entries = line.removeprefix(RW_PATHS).split()
    required = [one for one in entries if not one.startswith(OPTIONAL)]
    named = set(_environment().values())

    assert [one for one in required if one not in named] == []


#: The site's values reach the PEP through this one file, and the unit names
#: no address, no Home Assistant and no sops path of its own.
SITE_FILE_LINE: Final = "EnvironmentFile=/etc/agent-control/site.env"
FROM_THE_SITE: Final = ("PEP_BIND", "HA_URL", "PEP_SECRETS")


def test_the_site_file_supplies_what_differs_per_site() -> None:
    assert SITE_FILE_LINE in _directives()
    assert [one for one in FROM_THE_SITE if one in _environment()] == []
