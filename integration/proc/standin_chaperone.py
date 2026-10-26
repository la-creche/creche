"""A stand-in for `chaperone check`, the one verb that `caregiver` runs.

    standin_chaperone.py check <policy.toml> --plugins-dir <dir>

`caregiver` runs the check on each new text of `policy.toml`, in a temp
directory that holds a copy of `install.toml`. This program checks less
than the real one:

1. The verb is `check`, and `--plugins-dir` names a directory path.
2. `install.toml` is beside the file.
3. The file is TOML with `version = 1`.

Each failure prints one line on stderr and exits 1, as the real program
does. A scenario that needs the real check runs the Rust chaperone of
`chaperone.pin`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Final

VERB: Final = "check"
PLUGINS_FLAG: Final = "--plugins-dir"
INSTALL_FILE: Final = "install.toml"
VERSION: Final = 1


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] != VERB or argv[2] != PLUGINS_FLAG:
        print(f"usage: chaperone {VERB} <file> {PLUGINS_FLAG} <dir>", file=sys.stderr)
        return 1

    policy = Path(argv[1])
    if not (policy.parent / INSTALL_FILE).is_file():
        print(f"{policy.name}: no {INSTALL_FILE} beside it", file=sys.stderr)
        return 1

    try:
        table = tomllib.loads(policy.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"{policy.name}: {error}", file=sys.stderr)
        return 1

    if table.get("version") != VERSION:
        print(f"{policy.name}: version is not {VERSION}", file=sys.stderr)
        return 1

    print(f"{policy.name}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
