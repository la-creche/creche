"""A stand-in for `acp-relay`: it writes each stdin line back to stdout.

`fake_sbx.py` starts it, so its environment comes from the env file only.

- `FAKE_RELAY_RECORD=<path>` appends `started` to the file at start.
- `FAKE_RELAY_MODE=one` exits after the first line.
- `FAKE_RELAY_MODE=orphan` exits after the first line, and leaves a
  descendant that holds its stdout, as `sbx exec` can. The descendant's pid
  goes to the record file as `orphan <pid>`.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: How long the descendant of `orphan` holds stdout. The test kills it first.
ORPHAN_HOLD_S = "30"


def main() -> int:
    record = os.environ.get("FAKE_RELAY_RECORD", "")

    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write("started\n")

    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

        mode = os.environ.get("FAKE_RELAY_MODE")

        if mode == "orphan":
            orphan = subprocess.Popen(["sleep", ORPHAN_HOLD_S], stdin=subprocess.DEVNULL)

            with open(record, "a", encoding="utf-8") as handle:
                handle.write(f"orphan {orphan.pid}\n")

        if mode in ("one", "orphan"):
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
