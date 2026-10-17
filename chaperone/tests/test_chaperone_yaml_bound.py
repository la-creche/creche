"""The two YAML readers of the chaperone refuse three kinds of small text
inside the limits of a child process.

The readers are the roster reader and the reader of the decrypted secrets
file. Each kind is a text of less than 4 KiB.

    kind             what the text holds
    merge-tag        merge keys with the merge tag, and no key `<<`
    first-document   merge keys in a document that comes before a document
                     with an error
    merges-itself    one mapping that merges itself

A reader with no limit makes more than 16,000,000 copies from a text of the
first kind or of the third kind. PyYAML refuses a text of the second kind
before it builds a value, and the test holds that.

The count of the merge keys runs while PyYAML merges, so the form of a merge
key and the place of a document do not change it (`bounded_yaml`). Each read
runs in a child with a CPU limit and two memory limits. The child must
answer `refused`.

A character that PyYAML does not read is also a file that will not parse,
and not an error that leaves the reader.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from chaperone.mcp_client import UpstreamError, load_upstreams
from chaperone.reload_wiring import UNREADABLE, RosterSource

COMMAND: Final = "/opt/mcp/web-search/bin/web-search"

#: The limits of the child. The read of a refused text needs a small part
#: of each one.
CHILD_CPU_S: Final = 10
CHILD_MEMORY_BYTES: Final = 2 * 1024 * 1024 * 1024
CHILD_WALL_S: Final = 60

#: The largest resident memory of the child. A thread of the child ends it
#: past this limit, with `CHILD_PAST_MEMORY`. Not each system holds a
#: process to `CHILD_MEMORY_BYTES`, and this limit holds on each one.
CHILD_RESIDENT_BYTES: Final = 512 * 1024 * 1024
CHILD_PAST_MEMORY: Final = 9

#: The levels of `_doubling`, and the merge keys of `_itself`. A reader with
#: no limit makes more than 16,000,000 copies from each of the two.
LEVELS: Final = 24

#: Each text is smaller than this.
TEXT_BYTES_MAX: Final = 4096

#: What the child runs. It sets its limits after the imports, then reads the
#: file that its second argument names with the reader that its first
#: argument names. A system that does not take a limit leaves it out: the
#: CPU limit and the limit of the thread hold on each supported system.
CHILD: Final = f"""
import os
import resource
import sys
import threading
import time
from pathlib import Path

from chaperone.mcp_client import load_upstreams
from chaperone.reload_wiring import UNREADABLE
from chaperone.secrets import SecretsFormatError, load_sops_secrets

limits = ((resource.RLIMIT_CPU, {CHILD_CPU_S}), (resource.RLIMIT_AS, {CHILD_MEMORY_BYTES}))
for kind, limit in limits:
    try:
        resource.setrlimit(kind, (limit, limit))
    except (OSError, ValueError):
        pass

# The largest resident memory is bytes on macOS and KiB on Linux.
unit = 1 if sys.platform == "darwin" else 1024


def watch():
    while True:
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit > {CHILD_RESIDENT_BYTES}:
            os._exit({CHILD_PAST_MEMORY})

        time.sleep(0.02)


threading.Thread(target=watch, daemon=True).start()

reader, path = sys.argv[1], Path(sys.argv[2])
try:
    if reader == "roster":
        load_upstreams(path)
    else:
        load_sops_secrets(path)
except (*UNREADABLE, SecretsFormatError) as error:
    print("refused", type(error).__name__)
else:
    print("read")
"""

#: The size of the value that `HOLDER` holds: the resident limit of the
#: child, and 64 MiB more.
HELD_BYTES: Final = CHILD_RESIDENT_BYTES + 64 * 1024 * 1024

#: Holds a value of `HELD_BYTES` while it runs the command of its arguments
#: with its own standard streams, and ends with the status of that command.
#: `bytearray` writes each byte of the value, so the peak resident size of
#: this process passes the resident limit of the child. The value is not in
#: the test process, so the peak of the test process does not change. The
#: command gets half of the wall time limit of the child, so this process
#: stops the command before the test stops this process.
HOLDER: Final = f"""
import subprocess
import sys

held = bytearray({HELD_BYTES})
sys.exit(subprocess.run(sys.argv[1:], timeout={CHILD_WALL_S / 2}).returncode)
"""


def _row(name: str, env: str) -> str:
    return f"{name}:\n  command: {COMMAND}\n  env: {env}\n"


def _doubling(levels: int, key: str) -> str:
    """`levels` rows. Each row merges the row before it two times. `key` is
    the text of each merge key."""
    rows = [_row("r0", "&e0 {A: a, B: b}")]
    rows += [
        _row(f"r{n}", f"&e{n} {{{key}: [*e{n - 1}, *e{n - 1}]}}") for n in range(1, levels + 1)
    ]

    return "".join(rows)


def _itself(merges: int) -> str:
    """One row whose variables merge themselves `merges` times."""
    keys = ", ".join(["<<: *e"] * merges)

    return _row("r0", f"&e {{A: a, {keys}}}")


#: Each kind of text. Each one is also a mapping of names, as the secrets
#: file is.
KINDS: Final = (
    pytest.param(_doubling(LEVELS, "!!merge m"), id="merge-tag"),
    pytest.param(_doubling(LEVELS, "<<") + "---\nrow: *none\n", id="first-document"),
    pytest.param(_itself(LEVELS), id="merges-itself"),
)

READERS: Final = (
    pytest.param("roster", id="roster"),
    pytest.param("secrets", id="secrets"),
)


@pytest.fixture
def fake_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `sops` that prints the file it is asked to decrypt, as
    `test_chaperone_sec_start_no_decrypted_line.py` fakes it. The real one
    needs a key."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "sops"
    stub.write_text('#!/bin/sh\nexec cat "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")

    return binaries


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "file.yaml"
    path.write_text(text, encoding="utf-8")

    return path


def _run_child(
    program: str, reader: str, path: Path, *parent: str
) -> subprocess.CompletedProcess[str]:
    """Runs `program` in a child process inside the wall time limit, with
    `reader` and `path` as its arguments. `parent` is the command of a
    process that starts the child, or no word: then this process starts
    it."""
    return subprocess.run(
        [*parent, sys.executable, "-c", program, reader, str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=CHILD_WALL_S,
    )


@pytest.mark.slow
@pytest.mark.usefixtures("fake_sops")
@pytest.mark.parametrize("text", KINDS)
@pytest.mark.parametrize("reader", READERS)
def test_a_small_text_of_each_kind_is_refused_inside_the_limits(
    tmp_path: Path, reader: str, text: str
) -> None:
    assert len(text.encode()) < TEXT_BYTES_MAX

    done = _run_child(CHILD, reader, _file(tmp_path, text))

    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == "refused", done.stdout


@pytest.mark.slow
def test_the_child_watches_only_its_own_memory(tmp_path: Path) -> None:
    """The parent of the child holds a peak past the resident limit of the
    child while the child runs. On Linux, the `ru_maxrss` of the child holds
    the memory of its parent from before its exec. The thread of the child
    does not count that memory."""
    path = _file(tmp_path, _itself(LEVELS))

    done = _run_child(CHILD, "roster", path, sys.executable, "-c", HOLDER)

    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == "refused", done.stdout


#: A text with a character that PyYAML does not read. PyYAML raises on it
#: before it reads the first token.
NOT_READ: Final = (
    pytest.param(_row("a", '{A: "x\x00y"}'), id="a-null-character"),
    pytest.param("\x01" + _row("a", "{A: x}"), id="a-control-character-at-the-start"),
    pytest.param(_row("a", "{A: x￾}"), id="a-character-that-is-no-character"),
)


@pytest.mark.parametrize("text", NOT_READ)
def test_a_roster_with_a_character_that_pyyaml_does_not_read_will_not_parse(
    tmp_path: Path, text: str
) -> None:
    path = _file(tmp_path, text)

    with pytest.raises(yaml.YAMLError) as caught:
        load_upstreams(path)

    assert isinstance(caught.value, UNREADABLE)

    with pytest.raises(UpstreamError) as named:
        RosterSource(upstreams_file=path).upstreams()

    assert str(path) in str(named.value)
