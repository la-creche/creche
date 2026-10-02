"""What the door refuses, and the exit code each refusal carries.

The door ends in one of two ways. It runs `agent-pi-launch` and passes that
program's exit code straight back, or it refuses first and exits with a code
of its own.

Contract 03 §7.6 owns 0 to 11 (0 is pi's own, 2 a bad argument list, 8 a
session a live rpc process holds). This door's codes therefore start at 64,
so a reader never has to ask which program answered.
"""

from __future__ import annotations

from enum import IntEnum

# Contract 03 §7.6's highest code. Nothing this door mints may reach it.
LAUNCHER_EXIT_MAX = 11


class Exit(IntEnum):
    """One code per way the door can end before pi runs."""

    OK = 0
    #: The command line asked for something the door cannot do.
    BAD_USAGE = 64
    #: Another door holds the writer lease. Contention refuses (contract 02 §7.2).
    SESSION_BUSY = 65
    #: A switch is in progress, or no sandbox serves (contract 05 §4.2).
    NO_SANDBOX = 66
    #: `sessiond` refused the call, or could not be reached.
    SESSIOND = 67
    #: The picker chose nothing.
    NO_CHOICE = 68


class DoorError(Exception):
    """A refusal with the exit code the operator's shell will see."""

    def __init__(self, code: Exit, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
