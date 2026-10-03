"""Every Python file under a tests directory needs a repo-unique basename.

pytest imports test modules by basename in this repo's import mode, so two
packages that both hold `tests/test_faults.py`, or both hold a helper named
`family_helpers.py`, break collection for the WHOLE repo. A scoped run such as
`uv run pytest chaperone/tests` never shows it. Only the full run does, and CI is
the full run, so every scoped pre-push run carries this file as well
(`bin/quality-gate.sh --tests-for`). The rule gets a check instead of a
memory.
"""

from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR_NAMES = frozenset({"tests", "tests_manager"})
SHARED_BY_DESIGN = frozenset({"__init__.py", "conftest.py"})
SKIPPED_PARTS = frozenset({".venv", "node_modules", ".claude", ".git"})


def _test_files() -> list[Path]:
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & SKIPPED_PARTS:
            continue

        if not parts & TEST_DIR_NAMES:
            continue

        if path.name in SHARED_BY_DESIGN:
            continue

        found.append(path)

    return found


def test_test_basenames_are_unique() -> None:
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in _test_files():
        by_name[path.name].append(str(path.relative_to(REPO_ROOT)))

    clashes = {name: sorted(paths) for name, paths in by_name.items() if len(paths) > 1}

    assert not clashes, f"rename one side, prefix it with its package: {clashes}"
