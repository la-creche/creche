"""A stand-in for `acp-relay`: it writes each stdin line back to stdout.

`fake_sbx.py` starts it, so its environment comes from the env file only.

- `FAKE_RELAY_RECORD=<path>` appends `started` to the file at start.
- `FAKE_RELAY_MODE=one` exits after the first line.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    record = os.environ.get("FAKE_RELAY_RECORD", "")

    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write("started\n")

    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

        if os.environ.get("FAKE_RELAY_MODE") == "one":
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
