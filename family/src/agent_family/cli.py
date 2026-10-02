"""`agent-family validate <registry path> [--family NAME] [--json]`.

Exit 0 when every report is clean, 1 when any error was found, 2 on a usage
mistake. The report is the product (invariant 19), so it prints either way."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from .registry import load_registry
from .report import Report, Severity

EXIT_OK: Final = 0
EXIT_INVALID: Final = 1
EXIT_USAGE: Final = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-family", description="Validate a family registry (docs/rework/contracts/01)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="read a registry and print one report per family")
    validate.add_argument("registry", type=Path, help="the registry root, holding families/")
    validate.add_argument("--family", help="report this family only")
    validate.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _select(reports: tuple[Report, ...], only: str | None) -> tuple[Report, ...] | None:
    if only is None:
        return reports

    chosen = tuple(report for report in reports if report.family == only)
    return chosen or None


def _print_text(reports: tuple[Report, ...]) -> None:
    for report in reports:
        mark = "ok" if report.ok else "INVALID"
        print(f"{report.family}: {mark} ({report.errors} errors, {report.warnings} warnings)")
        print(f"  {report.file}")
        for issue in report.issues:
            flag = " [downgraded]" if issue.downgraded else ""
            label = "error" if issue.severity is Severity.ERROR else "warning"
            print(f"  {label}: {issue.loc}: {issue.msg}{flag}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
