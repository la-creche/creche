"""`chaperone-as` with the switch faked, because a test cannot setuid.

    fake_run_as.py [--trace FILE] [--unchecked] -- mcp-<name> /command [args...]

By default this IS the real launcher: `chaperone.run_as.launch`, with its
argv checks, its read of `PEP_CHILD_ENV`, its refusal of root and of a name
outside the pattern, and its exec. Two things are faked and nothing else:

- the user database: every name resolves to `FAKE_UID` and `FAKE_GID`,
- the switch: recorded, not performed.

`--trace FILE` appends one JSON line per exec: the user the launcher would
have become, and the exact argv and environment it handed to `execve`.

`--unchecked` is for the reload tests, whose upstreams are called `a` and
`b`. Those are no server names, so the real launcher refuses them. This
mode reads the environment the real way and execs the command, and checks
nothing else.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
from collections.abc import Mapping, Sequence
from typing import NoReturn

from chaperone import run_as

#: Nobody's ids on a test machine. The switch is never performed, so these
#: are only ever recorded.
FAKE_UID = 64_001
FAKE_GID = 64_002

_became: list[run_as.Target] = []


def _pretend(user: str) -> pwd.struct_passwd:
    return pwd.struct_passwd((user, "x", FAKE_UID, FAKE_GID, "", "/nonexistent", "/nologin"))


def _record(target: run_as.Target) -> None:
    _became.append(target)


def _exec(trace: str | None) -> run_as.Execve:
    def execve(command: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        if trace is not None:
            target = _became[-1] if _became else None
            line = {
                "user": target.user if target else None,
                "uid": target.uid if target else None,
                "gid": target.gid if target else None,
                "command": command,
                "argv": list(argv),
                "env": dict(env),
            }
            with open(trace, "a", encoding="utf-8") as out:
                out.write(json.dumps(line) + "\n")

        os.execve(command, list(argv), dict(env))

    return execve


def main(argv: list[str]) -> int:
    split = argv.index("--")
    options, rest = argv[:split], argv[split + 1 :]
    trace = options[options.index("--trace") + 1] if "--trace" in options else None

    if "--unchecked" in options:
        env = run_as.child_env(os.environ)
        _exec(trace)(rest[1], rest[1:], env)

    return run_as.launch(rest, os.environ, lookup=_pretend, become=_record, execve=_exec(trace))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
