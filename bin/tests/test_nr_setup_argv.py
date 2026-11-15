"""`bin/nr-setup.sh` step 9 keeps the LiteLLM master key off curl's argv.

Step 9 lists and mints LiteLLM keys as the admin. `/proc/<pid>/cmdline` is
world-readable for the life of a process and Hyperion sets no hidepid
(`bin/AGENTS.md` §Secrets), so the master key reaches curl in a 0600 header
file (`-H @<file>`), and the file is gone once the call returns.

The script needs Hyperion, so each case runs step 9 alone: its own lines,
its own helpers and a `curl` binstub that records its argv and the header
file it was handed. Nothing reaches a live service.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "nr-setup.sh"

#: A value no argument and no output line may carry.
MASTER_KEY: Final = "sk-master-" + "m" * 32
#: What the stub mints. The step prints it once, on purpose, for step N5.
MINTED_KEY: Final = "sk-minted-" + "r" * 32
BEARER_LINE: Final = f"Authorization: Bearer {MASTER_KEY}"

LITELLM_URL: Final = "http://litellm.invalid:4000"
LIST_URL: Final = f"{LITELLM_URL}/key/list?page=1&size=100&return_full_object=true"
MINT_URL: Final = f"{LITELLM_URL}/key/generate"
MINT_BODY: Final = (
    '{"key_alias":"reviewer","models":["agent_max"],"max_budget":2,"budget_duration":"1d"}'
)

#: `ls -l`'s mode column for a file only its owner can read or write.
PRIVATE_MODE: Final = "-rw-------"

#: The two answers `/key/list` can give: the alias is there, or it is not.
LISTS: Final = {
    "present": '{"keys": [{"key_alias": "reviewer"}]}',
    "absent": '{"keys": [{"key_alias": "someone-else"}]}',
}

# One `call` line per call, then one `arg` line per argument. For each
# `-H @<file>`, the file's mode and content, read while curl would read it.
CURL_STUB: Final = """#!/bin/sh
printf 'call\\n' >> "$NR_STUB_LOG"
previous=""
for arg in "$@"; do
  printf 'arg %s\\n' "$arg" >> "$NR_STUB_LOG"
  case "$previous:$arg" in
    -H:@*)
      printf 'mode %s\\n' "$(ls -l "${arg#@}" | cut -c1-10)" >> "$NR_STUB_LOG"
      printf 'header %s\\n' "$(cat "${arg#@}")" >> "$NR_STUB_LOG"
      ;;
  esac
  previous="$arg"
done
[ "${NR_CURL_EXIT:-0}" = 0 ] || exit "$NR_CURL_EXIT"
case "$*" in
  */key/list*) printf '%s' "$NR_LIST_BODY" ;;
  */key/generate*) printf '{"key": "%s"}' "$NR_MINTED_KEY" ;;
esac
"""


@dataclass(frozen=True)
class Ran:
    done: subprocess.CompletedProcess[str]
    calls: list[dict[str, list[str]]]
    #: Header files still on disk once the step ended. `_run` removes them
    #: after looking, so no case leaves litter in the system temp directory.
    leftovers: list[Path]


def _block(lines: list[str], start: str, end: str) -> list[str]:
    """From the first line starting with `start` up to, not including, the
    next line starting with `end`."""
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i in range(first + 1, len(lines)) if lines[i].startswith(end))
    return lines[first:last]


def _step_nine() -> str:
    """Step 9 as a script of its own: `say`, the helpers above `REPO=`, the
    `REVIEWER_*` settings, then the step's own lines."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return "\n".join(
        [
            "set -euo pipefail",
            *(line for line in lines if line.startswith("say() ")),
            *_block(lines, "py_field() {", "REPO="),
            *(line for line in lines if line.startswith("REVIEWER_")),
            f"LITELLM={LITELLM_URL}",
            "HOOKS_ENV=/srv/agents/state/creche/hooks.env",
            *_block(lines, 'say "9. ', 'say "DONE.'),
        ]
    )


def _calls(log: Path) -> list[dict[str, list[str]]]:
    calls: list[dict[str, list[str]]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        kind, _, value = line.partition(" ")
        if kind == "call":
            calls.append({"arg": [], "mode": [], "header": []})
            continue

        calls[-1][kind].append(value)

    return calls


def _values(args: list[str], flag: str) -> list[str]:
    """Every argument that follows `flag`."""
    return [args[i + 1] for i in range(len(args) - 1) if args[i] == flag]


def _header_files(calls: list[dict[str, list[str]]]) -> list[Path]:
    """Each `-H @<file>` curl was handed. By path, not by directory: the
    Mac's `mktemp` ignores `$TMPDIR`."""
    return [
        Path(value.removeprefix("@"))
        for call in calls
        for value in _values(call["arg"], "-H")
        if value.startswith("@")
    ]


def _run(tmp_path: Path, listed: str, curl_exit: int = 0) -> Ran:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text(CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)

    step = tmp_path / "step9.sh"
    step.write_text(_step_nine(), encoding="utf-8")
    log = tmp_path / "curl.log"

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{env['PATH']}",
            "LITELLM_MASTER_KEY": MASTER_KEY,
            "NR_STUB_LOG": str(log),
            "NR_LIST_BODY": LISTS[listed],
            "NR_MINTED_KEY": MINTED_KEY,
            "NR_CURL_EXIT": str(curl_exit),
        }
    )
    done = subprocess.run(["bash", str(step)], capture_output=True, text=True, env=env, timeout=60)

    calls = _calls(log) if log.exists() else []
    leftovers = [path for path in _header_files(calls) if path.exists()]
    for path in leftovers:
        path.unlink()

    return Ran(done, calls, leftovers)


@pytest.fixture(params=sorted(LISTS))
def ran(request: pytest.FixtureRequest, tmp_path: Path) -> Ran:
    return _run(tmp_path, request.param)


def test_no_curl_argv_carries_a_bearer() -> None:
    """The house idiom: a credential reaches curl through `-H @<file>`,
    never through an interpolated header value (`bin/AGENTS.md` §Secrets)."""
    text = SCRIPT.read_text(encoding="utf-8")

    assert '-H "Authorization: Bearer $' not in text
    assert "-H 'Authorization: Bearer $" not in text


def test_the_master_key_never_reaches_argv(ran: Ran) -> None:
    """The stub records every argument. A key in any of them would be a key
    in `/proc/<pid>/cmdline` on Hyperion. Nor does the step print it."""
    assert ran.done.returncode == 0, ran.done.stdout + ran.done.stderr
    assert ran.calls

    for call in ran.calls:
        assert not any(MASTER_KEY in arg for arg in call["arg"]), call["arg"]
    assert MASTER_KEY not in ran.done.stdout + ran.done.stderr


def test_every_call_reads_the_key_from_a_private_file(ran: Ran) -> None:
    """Off argv must not mean off the request: each call still sends the
    bearer, from a file only its owner can read."""
    assert ran.calls

    for call in ran.calls:
        assert call["header"] == [BEARER_LINE]
        assert call["mode"] == [PRIVATE_MODE]


def test_no_header_file_outlives_its_call(ran: Ran) -> None:
    assert ran.done.returncode == 0, ran.done.stdout + ran.done.stderr
    assert len(_header_files(ran.calls)) == len(ran.calls)
    assert ran.leftovers == []


def test_a_failed_call_leaves_no_header_file(tmp_path: Path) -> None:
    """The file goes whatever curl answers. Exit 7: LiteLLM did not answer,
    so the list reads as absent and the mint is tried as well."""
    ran = _run(tmp_path, "absent", curl_exit=7)

    assert len(_header_files(ran.calls)) == 2
    assert ran.leftovers == []


def test_a_present_alias_is_left_alone(tmp_path: Path) -> None:
    """One list call, the same URL and timeout, and no mint."""
    ran = _run(tmp_path, "present")
    (list_call,) = ran.calls

    assert LIST_URL in list_call["arg"]
    assert _values(list_call["arg"], "-m") == ["10"]
    assert "reviewer already exists at" in ran.done.stdout


def test_an_absent_alias_is_minted_and_printed_once(tmp_path: Path) -> None:
    """The same two URLs, the same body and the minted key printed once, for
    step N5."""
    ran = _run(tmp_path, "absent")
    list_call, mint_call = ran.calls

    assert LIST_URL in list_call["arg"]
    assert MINT_URL in mint_call["arg"]
    assert _values(mint_call["arg"], "-m") == ["10"]
    assert _values(mint_call["arg"], "-d") == [MINT_BODY]
    assert "Content-Type: application/json" in _values(mint_call["arg"], "-H")
    assert "minted reviewer: agent_max, $2 per 1d" in ran.done.stdout
    assert ran.done.stdout.count(MINTED_KEY) == 1
