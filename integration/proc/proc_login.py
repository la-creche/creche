"""Give a command a controlling terminal, then become the command.

    proc_login.py <program> [args...]

The harness starts this file as the leader of a new session, with the slave
side of a pseudo-terminal as stdin, stdout and stderr. A session leader that
only holds a terminal open does not own it: a key such as Ctrl-C sends no
signal, and a closed terminal sends no SIGHUP. One `ioctl` makes the terminal
the controlling terminal of the session, the way `login` does it for a shell.

This file ends in `exec`. The command keeps the pid, the session and the
process group, so every signal reaches it unchanged. This file is a part of
the harness. It is no stand-in and it is not under test.
"""

from __future__ import annotations

import fcntl
import os
import sys
import termios

STDIN = 0

#: The command was not found or cannot run, as a shell reports it.
EXIT_NO_COMMAND = 127
EXIT_USAGE = 64


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("proc_login: no command to run\n")

        return EXIT_USAGE

    try:
        fcntl.ioctl(STDIN, termios.TIOCSCTTY, 0)
    except OSError as error:
        sys.stderr.write(f"proc_login: stdin is no terminal this session can own: {error}\n")

        return EXIT_USAGE

    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        sys.stderr.write(f"proc_login: cannot run {argv[0]}: {error.strerror}\n")

    return EXIT_NO_COMMAND


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
