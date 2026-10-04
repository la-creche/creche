"""What a failed test carries: the root, and what each process wrote.

`conftest.py` uses this module in two places:

1. The report hook adds `describe` to a test that failed in its setup or in
   its body. The root is still on disk then.
2. The `supervisor` fixture ends the processes with `end_processes`. The
   `tree` fixture removes the root before pytest makes the report of a
   teardown. So `end_processes` reads the output itself, and a failed
   teardown carries the output in its own text.
"""

from __future__ import annotations

from proc_harness import STOP_GRACE_S, Supervisor
from proc_services import KEEP_ROOTS_ENV
from proc_standins import end_standins
from proc_tree import Tree

TEARDOWN_FAILED = "the teardown had to end a process:"


def describe(tree: Tree, supervisor: Supervisor) -> str:
    """The root, what each process wrote, and what each playpen wrote."""
    parts = [f"root: {tree.root} (set {KEEP_ROOTS_ENV}=1 to keep it)", supervisor.output()]

    if tree.log_dir.is_dir():
        for log in sorted(tree.log_dir.iterdir()):
            text = log.read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- {log.name} ---\n{text}")

    return "\n".join(parts)


def end_processes(tree: Tree, supervisor: Supervisor, grace_s: float = STOP_GRACE_S) -> str | None:
    """End every process of one test. Returns the text of a failure, or None.

    A teardown that had to kill a process is a failure. Its text holds one
    line per problem, then the output of every process.
    """
    problems = supervisor.stop_all(grace_s) + end_standins(tree, supervisor.sessions())

    if not problems:
        return None

    return "\n".join([TEARDOWN_FAILED, *problems, describe(tree, supervisor)])
