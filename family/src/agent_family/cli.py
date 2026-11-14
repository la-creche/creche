"""`agent-family validate <registry path> [--family NAME] [--json]` and
`agent-family reach --base <base path> <head path>`.

`validate` exits 0 when every report is clean, 1 when any error was found,
2 on a usage mistake. The report is the product (invariant 19), so it prints
either way.

`reach` prints one Markdown report of the reach diff (`reach.py`). It also
appends the report to the file that `GITHUB_STEP_SUMMARY` names, when the
variable is set: a GitHub Actions step shows that file on its run page. It
exits 1 when any family gains reach or a head file is unread, 0 when the
change only narrows or changes nothing, and 2 on a usage mistake."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from .reach import markdown, reach_diff
from .registry import load_registry
from .report import Report, Severity

EXIT_OK: Final = 0
EXIT_INVALID: Final = 1
EXIT_USAGE: Final = 2

#: The file of the step summary of a GitHub Actions step. GitHub sets it.
STEP_SUMMARY_ENV: Final = "GITHUB_STEP_SUMMARY"

#: The codec for an escaped line when the output stream names none.
_FALLBACK_ENCODING: Final = "utf-8"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-family", description="Validate a family registry (docs/rework/contracts/01)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="read a registry and print one report per family")
    validate.add_argument("registry", type=Path, help="the registry root, holding families/")
    validate.add_argument("--family", help="report this family only")
    validate.add_argument("--json", action="store_true", help="print the report as JSON")
    reach = sub.add_parser("reach", help="print what each family gains from base to head")
    reach.add_argument("--base", type=Path, required=True, help="the base registry root")
    reach.add_argument("head", type=Path, help="the head registry root")
    return parser


def _select(reports: tuple[Report, ...], only: str | None) -> tuple[Report, ...] | None:
    if only is None:
        return reports

    chosen = tuple(report for report in reports if report.family == only)
    return chosen or None


def _emit(line: str) -> None:
    """Print one line of the text report.

    A report line can hold a character that the terminal refuses: a message
    quotes a value of the file. Such a character goes out as a backslash
    escape, so the print does not raise. The codec of the terminal makes the
    escape and reads it back, so each other character stays as it is."""
    try:
        print(line)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or _FALLBACK_ENCODING
        print(line.encode(encoding, "backslashreplace").decode(encoding))


def _print_text(reports: tuple[Report, ...]) -> None:
    for report in reports:
        mark = "ok" if report.ok else "INVALID"
        _emit(f"{report.family}: {mark} ({report.errors} errors, {report.warnings} warnings)")
        _emit(f"  {report.file}")
        for issue in report.issues:
            flag = " [downgraded]" if issue.downgraded else ""
            label = "error" if issue.severity is Severity.ERROR else "warning"
            _emit(f"  {label}: {issue.loc}: {issue.msg}{flag}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "reach":
        return _reach(Path(args.base), Path(args.head))

    root = Path(args.registry)
    if not root.is_dir():
        print(f"agent-family: {root} is not a directory", file=sys.stderr)
        return EXIT_USAGE

    registry = load_registry(root)
    reports = _select(registry.all_reports(), args.family)
    if reports is None:
        print(f"agent-family: no family or server named '{args.family}'", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(
            json.dumps(
                {
                    "registry": str(root),
                    "revision": registry.revision,
                    "reports": [report.as_json() for report in reports],
                },
                indent=2,
            )
        )
    else:
        _print_text(reports)

    return EXIT_OK if all(report.ok for report in reports) else EXIT_INVALID


def _reach(base: Path, head: Path) -> int:
    for root in (base, head):
        if not root.is_dir():
            print(f"agent-family: {root} is not a directory", file=sys.stderr)
            return EXIT_USAGE

    result = reach_diff(base, head)
    report = markdown(result)
    for line in report.splitlines():
        _emit(line)

    summary = os.environ.get(STEP_SUMMARY_ENV)
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(report)

    return EXIT_INVALID if result.widens else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
