"""The roster reader holds the merge keys of one file to two limits.

A merge key copies the pairs of its value into the mapping that holds the
key. An alias gives one value to many merge keys, so the count of copies can
grow much faster than the text of the file.

    file --> node graph --> count each copy --> rows
                                 |
                                 `--> past a limit: the file will not parse

The reader refuses such a file as it refuses YAML that will not parse: a
reload keeps the set that serves, and a start serves no MCP server. The two
limits are the limits of the Rust reader of a manifest, so a file at a limit
reads and a file past it does not.
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

#: The variables of the mapping that the rows below merge.
PAIRS: Final = 256

#: `PAIRS` times this count is the limit: 65,536 copied pairs.
MERGES_AT_THE_LIMIT: Final = 256

#: The limits of the child that reads a file with many copies. The read of a
#: refused file needs a small part of each one.
CHILD_CPU_S: Final = 10
CHILD_MEMORY_BYTES: Final = 2 * 1024 * 1024 * 1024
CHILD_WALL_S: Final = 60

#: The levels of `_doubling`. Each level copies two times the pairs of the
#: level before it.
DOUBLING_LEVELS: Final = 24

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


def _row(name: str, env: str) -> str:
    return f"{name}:\n  command: {COMMAND}\n  env: {env}\n"


#: The alias of the row `one` of `_copies`, which has one variable.
ONE_MORE_PAIR: Final = "*one"


def _copies(merges: int, *more: str) -> str:
    """A roster whose row `b` merges the variables of row `a` `merges`
    times, and then each alias of `more`."""
    variables = ", ".join(f"K{n}: v" for n in range(PAIRS))
    aliases = ", ".join(["*e"] * merges + list(more))
    head = _row("one", "&one {Z: v}") + _row("a", f"&e {{{variables}}}")

    return head + _row("b", f"{{<<: [{aliases}]}}")


def _doubling(levels: int) -> str:
    """A roster of `levels` rows. Each row merges the row before it two times."""
    rows = [_row("r0", "&e0 {A: a, B: b}")]
    rows += [_row(f"r{n}", f"&e{n} {{<<: [*e{n - 1}, *e{n - 1}]}}") for n in range(1, levels + 1)]

    return "".join(rows)


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "upstreams.yaml"
    path.write_text(text, encoding="utf-8")

    return path


def test_a_row_can_use_a_merge_key(tmp_path: Path) -> None:
    text = _row("a", "&e {LOG_LEVEL: info}") + _row("b", "{<<: *e, MODE: stdio}")

    specs = load_upstreams(_file(tmp_path, text))

    assert specs["b"].env == {"LOG_LEVEL": "info", "MODE": "stdio"}


def test_merge_keys_that_copy_as_many_pairs_as_the_limit_read(tmp_path: Path) -> None:
    specs = load_upstreams(_file(tmp_path, _copies(MERGES_AT_THE_LIMIT)))

    assert len(specs["b"].env) == PAIRS


def test_one_copied_pair_past_the_limit_is_a_file_that_will_not_parse(tmp_path: Path) -> None:
    path = _file(tmp_path, _copies(MERGES_AT_THE_LIMIT, ONE_MORE_PAIR))

    with pytest.raises(yaml.YAMLError) as caught:
        load_upstreams(path)

    assert isinstance(caught.value, UNREADABLE)


def test_the_refusal_names_the_file_as_each_file_failure_does(tmp_path: Path) -> None:
    path = _file(tmp_path, _copies(MERGES_AT_THE_LIMIT, ONE_MORE_PAIR))

    with pytest.raises(UpstreamError) as caught:
        RosterSource(upstreams_file=path).upstreams()

    assert str(path) in str(caught.value)
    assert "merge" in str(caught.value)


@pytest.mark.slow
def test_a_small_file_with_many_copies_is_refused_inside_its_limits(tmp_path: Path) -> None:
    """The read runs in a child with a CPU limit and a memory limit. A reader
    that makes each copy passes the CPU limit and does not answer."""
    text = _doubling(DOUBLING_LEVELS)
    assert len(text.encode()) < 2048

    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(_file(tmp_path, text))],
        capture_output=True,
        text=True,
        check=False,
        timeout=CHILD_WALL_S,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == "refused", done.stdout
