"""A stand-in for `sbx`, with the verbs that `caregiver` runs.

    standin_sbx.py <state directory> <verb> [arguments...]

The suite runs on a machine with no microVM and no `sbx`. `caregiver` still
runs these commands, and `attendance` runs `exec`:

    create shell <mount>... -t <image> --name <id> --cpus <n> -m <size>
           [--deny-network <host>]... -q
    policy allow network --sandbox <id> <host>
    policy deny network --sandbox <id> <host>
    policy rm network --sandbox <id> --resource <host>
    policy check network --sandbox <id> <host>
    rm -f <id>
    ls
    exec --env-file <file> <id> -- <command> [arguments...]

Every fact that a later call needs is a file under the state directory, so a
test reads the same files that the next call reads:

    vms/<id>.json                 what `create` was given
    policy/<id>/allow/<host>      one file for each allow row
    policy/<id>/deny/<host>       one file for each deny row
    tune/                         what a test writes to change one call

Four things here copy what the real program does on the host:

1. `create` gives each sandbox the allow row of the `shell` kit. Only a deny
   row takes that host away.
2. `policy check` prints `Allowed` or `Denied`. A deny row wins over an
   allow row, and a host with no row is denied.
3. `rm` removes the sandbox and leaves its policy rows.
4. `exec` starts nothing in a sandbox that `create` did not make.

A test tunes one call with a file under `tune/`:

    tune/hold-<verb>          the verb waits while the file exists
    tune/hold-create-<id>     `create` of that sandbox waits while the file exists
    tune/fail-<verb>          the verb fails, with the text of the file
    tune/no-mounts/<id>       `exec` gives that sandbox no mount variable

`exec` becomes `integration/tests/fake_sbx.py`, which becomes the command.
The process keeps one pid from the wrapper to the playpen.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Final
from urllib.parse import quote, unquote

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 64

#: The kit that `caregiver` creates each sandbox from, and the host that the
#: kit allows with no flag.
KIT: Final = "shell"
KIT_ALLOW: Final = ("openrouter.ai",)

ALLOWED: Final = "Allowed"
DENIED: Final = "Denied"
NOT_FOUND: Final = "not found"
READ_ONLY_SUFFIX: Final = ":ro"

VMS_DIR: Final = "vms"
POLICY_DIR: Final = "policy"
TUNE_DIR: Final = "tune"
ALLOW: Final = "allow"
DENY: Final = "deny"
NETWORK: Final = "network"

#: The one variable that an env file keeps when a test takes the mounts away.
SANDBOX_VARIABLE: Final = "AGENT_SANDBOX"

#: `--sandbox <id> <host>`: the words after `policy <action> network`.
_POLICY_WORDS: Final = 3

_HOLD_POLL_S: Final = 0.02
_SEPARATOR: Final = "--"


class UsageError(Exception):
    """The arguments are not a command that this stand-in knows."""


class Refused(Exception):
    """The command is known, and the state refuses it."""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _fail(EXIT_USAGE, "usage: standin_sbx.py <state directory> <verb> ...")

    state = Path(argv[0])
    verb, rest = argv[1], argv[2:]

    try:
        _apply_tune(state, verb)

        return _run(state, verb, rest)
    except UsageError as error:
        return _fail(EXIT_USAGE, str(error))
    except Refused as error:
        return _fail(EXIT_FAILED, str(error))


def _run(state: Path, verb: str, rest: list[str]) -> int:
    if verb == "create":
        return _create(state, rest)

    if verb == "policy":
        return _policy(state, rest)

    if verb == "rm":
        return _remove(state, rest)

    if verb == "ls":
        return _list(state)

    if verb == "exec":
        return _exec(state, rest)

    raise UsageError(f"no verb named {verb}")


# ------------------------------------------------------------------ create


def _create(state: Path, rest: list[str]) -> int:
    if not rest or rest[0] != KIT:
        raise UsageError(f"create needs the kit {KIT}")

    mounts, flags = _split_at_flag(rest[1:])
    name = _one(flags, "--name")
    _hold(state, f"create-{name}")
    vm = _vm_file(state, name)

    # CONTRACT-QUESTION: contract 05 §10 row 7 leaves open what `sbx create`
    # does with a name that exists. Reading taken: it fails. Contract 05
    # §4.1 never uses an id again, so a `caregiver` that follows the
    # contract never meets this branch. A change costs this one check and
    # its test.
    if vm.exists():
        raise Refused(f"sandbox {name} already exists")

    document = {
        "name": name,
        "image": _one(flags, "-t"),
        "cpus": int(_one(flags, "--cpus")),
        "memory": _one(flags, "-m"),
        "mounts": [_mount(text) for text in mounts],
    }

    for host in KIT_ALLOW:
        _add_row(state, name, ALLOW, host)

    for host in _every(flags, "--deny-network"):
        _add_row(state, name, DENY, host)

    _write_whole(vm, json.dumps(document) + "\n")

    return EXIT_OK


def _mount(text: str) -> dict[str, object]:
    if text.endswith(READ_ONLY_SUFFIX):
        return {"path": text.removesuffix(READ_ONLY_SUFFIX), "readonly": True}

    return {"path": text, "readonly": False}


# ------------------------------------------------------------------ policy


def _policy(state: Path, rest: list[str]) -> int:
    if len(rest) < 2 or rest[1] != NETWORK:
        raise UsageError("policy needs an action and the word network")

    action, flags = rest[0], rest[2:]
    name = _one(flags, "--sandbox")

    if action == "rm":
        return _policy_remove(state, name, _one(flags, "--resource"))

    if not _vm_file(state, name).exists():
        raise Refused(f"sandbox {name} {NOT_FOUND}")

    if len(flags) != _POLICY_WORDS:
        raise UsageError(f"policy {action} needs one sandbox and one host")

    host = _last_word(flags)

    if action in (ALLOW, DENY):
        _add_row(state, name, action, host)

        return EXIT_OK

    if action == "check":
        return _policy_check(state, name, host)

    raise UsageError(f"no policy action named {action}")


def _policy_remove(state: Path, name: str, host: str) -> int:
    rows = [_row(state, name, kind, host) for kind in (ALLOW, DENY)]
    found = [row for row in rows if row.exists()]

    if not found:
        raise Refused(f"no policy row for {host} on {name}")

    for row in found:
        row.unlink()

    return EXIT_OK


def _policy_check(state: Path, name: str, host: str) -> int:
    """A deny row wins. A host with no row is denied."""
    denied = _row(state, name, DENY, host).exists()
    allowed = _row(state, name, ALLOW, host).exists()

    if allowed and not denied:
        sys.stdout.write(f"{ALLOWED}\n")

        return EXIT_OK

    sys.stdout.write(f"{DENIED}\n")

    return EXIT_FAILED


# -------------------------------------------------------------- rm, ls, exec


def _remove(state: Path, rest: list[str]) -> int:
    names = [word for word in rest if not word.startswith("-")]

    if len(names) != 1:
        raise UsageError("rm needs one sandbox id")

    vm = _vm_file(state, names[0])

    if not vm.exists():
        raise Refused(f"sandbox {names[0]} {NOT_FOUND}")

    vm.unlink()

    return EXIT_OK


def _list(state: Path) -> int:
    """One header line, then one line for each sandbox. The id is the first word."""
    names = sorted(unquote(path.stem) for path in (state / VMS_DIR).glob("*.json"))
    sys.stdout.write("SANDBOX  AGENT  STATUS\n")

    for name in names:
        sys.stdout.write(f"{name}  {KIT}  stopped\n")

    return EXIT_OK


def _exec(state: Path, rest: list[str]) -> int:
    """Check the sandbox, then become the program that plays `sbx exec`."""
    if _SEPARATOR not in rest:
        raise UsageError(f"exec needs {_SEPARATOR} before the command")

    before = rest[: rest.index(_SEPARATOR)]
    name = _last_word(before)

    if not _vm_file(state, name).exists():
        raise Refused(f"sandbox {name} {NOT_FOUND}")

    words = list(rest)

    if (state / TUNE_DIR / "no-mounts" / name).exists():
        at = words.index("--env-file") + 1
        words[at] = str(_env_with_no_mounts(state, name))

    program = Path(__file__).resolve().parents[1] / "tests" / "fake_sbx.py"
    os.execv(sys.executable, [sys.executable, str(program), "exec", *words])

    return EXIT_USAGE


def _env_with_no_mounts(state: Path, name: str) -> Path:
    """An env file that names the sandbox and none of its directories."""
    path = state / TUNE_DIR / f"{name}.env"
    _write_whole(path, f"{SANDBOX_VARIABLE}={name}\n")

    return path


# -------------------------------------------------------------------- shared


def _apply_tune(state: Path, verb: str) -> None:
    """Wait while a test holds the verb. Fail when a test says so."""
    _hold(state, verb)
    fail = state / TUNE_DIR / f"fail-{verb}"

    if fail.exists():
        raise Refused(fail.read_text(encoding="utf-8").strip() or f"{verb} failed")


def _hold(state: Path, what: str) -> None:
    """Wait while the file `hold-<what>` exists."""
    hold = state / TUNE_DIR / f"hold-{what}"

    while hold.exists():
        time.sleep(_HOLD_POLL_S)


def _split_at_flag(words: list[str]) -> tuple[list[str], list[str]]:
    """The words before the first flag, and the words from it."""
    for index, word in enumerate(words):
        if word.startswith("-"):
            return words[:index], words[index:]

    return words, []


def _every(flags: list[str], name: str) -> list[str]:
    """The value after each `name` in the flags."""
    values = [flags[index + 1] for index, word in enumerate(flags[:-1]) if word == name]

    return [value for value in values if value]


def _one(flags: list[str], name: str) -> str:
    values = _every(flags, name)

    if len(values) != 1:
        raise UsageError(f"the command needs {name} one time")

    return values[0]


def _last_word(words: list[str]) -> str:
    if not words or words[-1].startswith("-"):
        raise UsageError("the command ends with no name")

    return words[-1]


def _vm_file(state: Path, name: str) -> Path:
    return state / VMS_DIR / f"{quote(name, safe='')}.json"


def _row(state: Path, name: str, kind: str, host: str) -> Path:
    return state / POLICY_DIR / quote(name, safe="") / kind / quote(host, safe="")


def _add_row(state: Path, name: str, kind: str, host: str) -> None:
    row = _row(state, name, kind, host)
    row.parent.mkdir(parents=True, exist_ok=True)
    row.touch()


def _write_whole(path: Path, text: str) -> None:
    """Temp file, then rename. A reader never gets half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _fail(code: int, message: str) -> int:
    sys.stderr.write(f"standin_sbx: {message}\n")

    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
