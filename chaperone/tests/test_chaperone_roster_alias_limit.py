"""The roster reader holds the aliases of one file to a limit.

An alias gives one value to many rows. The reader makes the variables of
each row from that value, so the memory of a read can grow with the square
of the size of the file.

    file --> node graph --> count the nodes of each alias --> rows
                                     |
                                     `--> past the limit: the file will not
                                          parse

The reader refuses such a file as it refuses YAML that will not parse: a
reload keeps the set that serves, and a start serves no MCP server. A file
at the limit reads and a file one node past it does not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from chaperone.mcp_client import UpstreamError, load_upstreams
from chaperone.reload_wiring import UNREADABLE, RosterSource

COMMAND: Final = "/opt/mcp/web-search/bin/web-search"

#: The variables of the mapping that the rows of `_shared` take. An alias of
#: that mapping stands for the mapping, each name and each value: 513 nodes.
PAIRS: Final = 256

#: 511 aliases of the mapping and one alias of a text stand for 262,144
#: nodes, which is the limit.
ROWS_AT_THE_LIMIT: Final = 511

#: The limits of the child that reads a file past the limit. The read of a
#: refused file needs a small part of each one.
CHILD_CPU_S: Final = 10
CHILD_MEMORY_BYTES: Final = 2 * 1024 * 1024 * 1024
CHILD_WALL_S: Final = 60

#: The rows and the variables of `_square`. A reader with no limit makes
#: 1,000 mappings of 1,000 variables from that file.
SQUARE_SIDE: Final = 1000

#: The file of `_square` is smaller than this.
SQUARE_BYTES_MAX: Final = 72 * 1024

#: What the child runs. It sets its own limits after the imports, then reads
#: the file that its argument names. A system that does not take a limit
#: leaves it out: the CPU limit holds on each supported system.
CHILD: Final = f"""
import resource
import sys
from pathlib import Path

from chaperone.mcp_client import load_upstreams
from chaperone.reload_wiring import UNREADABLE

limits = ((resource.RLIMIT_CPU, {CHILD_CPU_S}), (resource.RLIMIT_AS, {CHILD_MEMORY_BYTES}))
for kind, limit in limits:
    try:
        resource.setrlimit(kind, (limit, limit))
    except (OSError, ValueError):
        pass

try:
    load_upstreams(Path(sys.argv[1]))
except UNREADABLE as error:
    print("refused", type(error).__name__)
else:
    print("read")
"""


def _row(name: str, command: str, env: str) -> str:
    return f"{name}:\n  command: {command}\n  env: {env}\n"


def _shared(rows: int, texts: int) -> str:
    """A roster in which `rows` rows take the variables of row `a` through
    an alias, and `texts` rows take its command through an alias."""
    variables = ", ".join(f"K{n}: v" for n in range(PAIRS))
    head = _row("a", f"&c {COMMAND}", f"&e {{{variables}}}")
    shared = "".join(_row(f"r{n}", COMMAND, "*e") for n in range(rows))
    commands = "".join(_row(f"t{n}", "*c", "{}") for n in range(texts))

    return head + shared + commands


def _square(side: int) -> str:
    """A roster of `side` rows. Each row takes one mapping of `side`
    variables through an alias."""
    variables = ", ".join(f"K{n}: v" for n in range(side))
    rows = [_row("r0", COMMAND, f"&e {{{variables}}}")]
    rows += [_row(f"r{n}", COMMAND, "*e") for n in range(1, side)]

    return "".join(rows)


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "upstreams.yaml"
    path.write_text(text, encoding="utf-8")

    return path


def test_a_row_can_use_an_alias(tmp_path: Path) -> None:
    text = _row("a", COMMAND, "&e {LOG_LEVEL: info}") + _row("b", COMMAND, "*e")

    specs = load_upstreams(_file(tmp_path, text))

    assert specs["b"].env == {"LOG_LEVEL": "info"}


def test_aliases_that_stand_for_as_many_nodes_as_the_limit_read(tmp_path: Path) -> None:
    specs = load_upstreams(_file(tmp_path, _shared(ROWS_AT_THE_LIMIT, texts=1)))

    assert len(specs) == ROWS_AT_THE_LIMIT + 2
    assert len(specs["r0"].env) == PAIRS
    assert specs["t0"].command == COMMAND


def test_one_node_past_the_limit_is_a_file_that_will_not_parse(tmp_path: Path) -> None:
    path = _file(tmp_path, _shared(ROWS_AT_THE_LIMIT, texts=2))

    with pytest.raises(yaml.YAMLError) as caught:
        load_upstreams(path)

    assert isinstance(caught.value, UNREADABLE)


def test_the_refusal_names_the_file_as_each_file_failure_does(tmp_path: Path) -> None:
    path = _file(tmp_path, _shared(ROWS_AT_THE_LIMIT, texts=2))

    with pytest.raises(UpstreamError) as caught:
        RosterSource(upstreams_file=path).upstreams()

    assert str(path) in str(caught.value)
    assert "aliases" in str(caught.value)


@pytest.mark.slow
def test_a_file_with_one_value_for_many_rows_is_refused_inside_its_limits(tmp_path: Path) -> None:
    """The read runs in a child with a CPU limit and a memory limit. A reader
    with no limit reads the file, and makes a copy of the variables for each
    row."""
    text = _square(SQUARE_SIDE)
    assert len(text.encode()) < SQUARE_BYTES_MAX

    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(_file(tmp_path, text))],
        capture_output=True,
        text=True,
        check=False,
        timeout=CHILD_WALL_S,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == "refused", done.stdout
