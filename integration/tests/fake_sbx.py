"""A stand-in for `sbx exec --env-file`, for a harness with no sbx.

    fake_sbx.py exec --env-file <file> <sandbox id> -- <command> [args...]

The harness runs on a Mac, so there is no microVM and no `sbx`. What it
still has to reproduce is the ONE mechanism the supervisor depends on
(contract 03 §7.1):

1. `sbx exec` forwards no host environment. The child starts from the
   image's own variables and nothing else.
2. `--env-file` is the only thing that carries a value in. Each
   `NAME=value` line of that file becomes one variable.

So a `sessiond` that built its command without `--env-file` would start a
child with no `AGENT_CRED_DIR`, no `AGENT_FAMILY_CONFIG_DIR` and no
`AGENT_CONTROL_DIR`, exactly as on the host — and the supervisor would
answer `fatal` instead of serving.

`FAKE_SBX_IMAGE_ENV` names the variables that stand in for what the
sandbox image provides (`AGENT_PI_BIN`, `AGENT_LOCK_BEAT_MS`). It is read
here, in the stand-in for `sbx` itself, never inside the child.

This file `exec`s, so the supervisor stays ONE process: stdin, stdout,
stderr and every signal reach it unchanged, the way they do through a
real `sbx exec`.
"""

from __future__ import annotations

import os
import sys

EXEC_VERB = "exec"
ENV_FILE_FLAG = "--env-file"
COMMAND_SEPARATOR = "--"

#: What a sandbox image supplies to every process in it (contract 03 §7).
IMAGE_DEFAULTS = ("PATH", "HOME", "TMPDIR", "LANG", "NODE_ENV")

#: Extra image variables, comma separated. The harness's two test seams.
IMAGE_ENV_LIST = "FAKE_SBX_IMAGE_ENV"

EXIT_USAGE = 64


def fail(message: str) -> int:
    """stdout carries the channel, so a complaint goes to stderr only."""
    sys.stderr.write(f"fake_sbx: {message}\n")

    return EXIT_USAGE


def read_env_file(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    values: dict[str, str] = {}

    for line in lines:
        name, separator, value = line.partition("=")

        if separator:
            values[name] = value

    return values


def image_env() -> dict[str, str]:
    """What the child would inherit from the image, and nothing else."""
    named = os.environ.get(IMAGE_ENV_LIST, "").split(",")
    wanted = [*IMAGE_DEFAULTS, *(name.strip() for name in named if name.strip())]

    return {name: os.environ[name] for name in wanted if name in os.environ}


def main(argv: list[str]) -> int:
    if not argv or argv[0] != EXEC_VERB:
        return fail(f"the first argument must be {EXEC_VERB}")

    rest = argv[1:]

    if ENV_FILE_FLAG not in rest:
        return fail(f"{ENV_FILE_FLAG} is required: sbx exec forwards no host environment")

    at = rest.index(ENV_FILE_FLAG)
    env_file = rest[at + 1] if at + 1 < len(rest) else ""
    rest = rest[:at] + rest[at + 2 :]

    if COMMAND_SEPARATOR not in rest:
        return fail(f"no {COMMAND_SEPARATOR} before the command")

    split = rest.index(COMMAND_SEPARATOR)
    sandbox = rest[:split]
    command = rest[split + 1 :]

    if len(sandbox) != 1 or not sandbox[0]:
        return fail("exactly one sandbox id belongs before the command")

    if not command:
        return fail("no command to run")

    # execvpe, not a child: one process means stdin, stdout, stderr and every
    # signal reach the supervisor exactly as they would through `sbx exec`.
    # The program is looked up on the PATH of the environment being built,
    # which is the image's PATH.
    env = image_env() | read_env_file(env_file)
    os.execvpe(command[0], command, env)

    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
