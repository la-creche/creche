"""The registry loader: `families/`, `mcp/` and `skills/` under one root.

Reads every file once, cross-references them, and answers validated objects
plus one report per family. A bad file never stops the others: one bad file is
not a registry outage (contract 01 §7 rule 5)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .model import FamilyFile
from .parse import parse_family, parse_server
from .report import FamilyState, Issue, Issues, Report
from .server import McpServerFile
from .serverrules import check_server
from .validate import HostFacts, Index, check_family

FAMILY_FILE: Final = "family.yaml"
SERVER_FILE: Final = "server.yaml"
SKILL_FILE: Final = "SKILL.md"
INSTRUCTIONS_FILE: Final = "instructions.md"
FAMILIES_DIR: Final = "families"
MCP_DIR: Final = "mcp"
SKILLS_DIR: Final = "skills"

#: `reg-9f21c4` in contract 05's examples. A content hash, so two reads of the
#: same bytes answer the same revision and a reconciler can compare them.
REVISION_PREFIX: Final = "reg-"
REVISION_CHARS: Final = 6


@dataclass(frozen=True)
class Registry:
    """One read of one registry root."""

    root: Path
    revision: str
    families: Mapping[str, FamilyFile]
    servers: Mapping[str, McpServerFile]
    skills: frozenset[str]
    reports: Mapping[str, Report]
    server_reports: Mapping[str, Report]

    @property
    def ok(self) -> bool:
        return all(report.ok for report in self.all_reports())

    def all_reports(self) -> tuple[Report, ...]:
        by_name = {**self.server_reports, **self.reports}
        return tuple(by_name[name] for name in sorted(by_name))

    def instructions(self, family: str) -> Path:
        """The layout fixes this path, and the family file never names it, so
        prose can never widen reach (contract 01 §1 rule 2)."""
        return self.root / FAMILIES_DIR / family / INSTRUCTIONS_FILE

    def skill_file(self, skill: str) -> Path:
        return self.root / SKILLS_DIR / skill / SKILL_FILE


def load_registry(root: Path, host: HostFacts | None = None) -> Registry:
    """Read, parse and cross-reference one registry. Never raises on content."""
    servers, server_reports = _read_servers(root)
    parsed, per_family = _read_families(root)
    skills = _read_skills(root)
    index = Index(
        kinds={name: family.kind for name, family in parsed.items()},
        servers=servers,
        skills=skills,
    )

    reports: dict[str, Report] = {}
    for name, family in parsed.items():
        issues = Issues(list(per_family[name]))
        check_family(family, name, index, issues, host)
        reports[name] = _report(name, f"{FAMILIES_DIR}/{name}/{FAMILY_FILE}", issues.frozen())

    # Directories whose file failed to parse still get their report.
    for name, entries in per_family.items():
        if name not in reports:
            reports[name] = _report(name, f"{FAMILIES_DIR}/{name}/{FAMILY_FILE}", tuple(entries))

    _check_webhooks(parsed, reports)
    _check_dispatch_triggers(parsed, reports)
    return Registry(
        root=root,
        revision=revision_of(root),
        families=parsed,
        servers=servers,
        skills=skills,
        reports=reports,
        server_reports=server_reports,
    )


def revision_of(root: Path) -> str:
    """A content revision over every registry file, path included. Renaming a
    file therefore moves the revision even when no byte inside it changed."""
    digest = hashlib.sha256()
    for path in sorted(_registry_files(root)):
        # A file name that is not UTF-8 gives surrogate characters in its path
        # string. `surrogateescape` turns them back into their bytes, so this
        # never raises and a bad name cannot stop a read of the registry.
        digest.update(str(path.relative_to(root)).encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(_read(path)[0].encode())
        digest.update(b"\0")

    return REVISION_PREFIX + digest.hexdigest()[:REVISION_CHARS]


def _registry_files(root: Path) -> Iterator[Path]:
    for directory in (FAMILIES_DIR, MCP_DIR, SKILLS_DIR):
        base = root / directory
        if not base.is_dir():
            continue

        for path in base.rglob("*"):
            if path.is_file():
                yield path


def _read(path: Path) -> tuple[str, str | None]:
    """Text, or an empty string and the reason it could not be read."""
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return "", f"cannot read {path.name}: {exc.strerror or exc}"
    except UnicodeDecodeError:
        return "", f"cannot read {path.name}: it is not UTF-8 text"


def _subdirectories(base: Path) -> list[Path]:
    if not base.is_dir():
        return []

    return sorted(path for path in base.iterdir() if path.is_dir())


def _read_families(root: Path) -> tuple[dict[str, FamilyFile], dict[str, tuple[Issue, ...]]]:
    parsed: dict[str, FamilyFile] = {}
    issues_by_name: dict[str, tuple[Issue, ...]] = {}
    for directory in _subdirectories(root / FAMILIES_DIR):
        name = directory.name
        path = directory / FAMILY_FILE
        if not path.is_file():
            # §5.6 rule 3: a warning, and `caregiver` ignores the directory.
            issues = Issues()
            issues.warn("<document>", f"no {FAMILY_FILE}; this directory is ignored")
            issues_by_name[name] = issues.frozen()
            continue

        text, problem = _read(path)
        if problem is not None:
            issues = Issues()
            issues.error("<document>", problem)
            issues_by_name[name] = issues.frozen()
            continue

        family, entries = parse_family(text)
        issues_by_name[name] = tuple(entries)
        if family is not None:
            parsed[name] = family

    return parsed, issues_by_name


def _read_servers(root: Path) -> tuple[dict[str, McpServerFile], dict[str, Report]]:
    servers: dict[str, McpServerFile] = {}
    reports: dict[str, Report] = {}
    for directory in _subdirectories(root / MCP_DIR):
        name = directory.name
        path = directory / SERVER_FILE
        file = f"{MCP_DIR}/{name}/{SERVER_FILE}"
        if not path.is_file():
            issues = Issues()
            issues.warn("<document>", f"no {SERVER_FILE}; this directory is ignored")
            reports[name] = _report(name, file, issues.frozen())
            continue

        text, problem = _read(path)
        if problem is not None:
            issues = Issues()
            issues.error("<document>", problem)
            reports[name] = _report(name, file, issues.frozen())
            continue

        server, entries = parse_server(text)
        issues = Issues(list(entries))
        if server is not None:
            servers[name] = server
            check_server(server, name, issues)

        reports[name] = _report(name, file, issues.frozen())

    return servers, reports


def _read_skills(root: Path) -> frozenset[str]:
    return frozenset(
        directory.name
        for directory in _subdirectories(root / SKILLS_DIR)
        if (directory / SKILL_FILE).is_file()
    )


def _check_webhooks(families: Mapping[str, FamilyFile], reports: dict[str, Report]) -> None:
    """§5.6 rule 1: a webhook name is claimed once, and the error is named on
    both files, because either author can fix it."""
    claims: dict[str, list[str]] = {}
    for name, family in families.items():
        for trigger in family.triggers or ():
            if trigger.webhook is not None:
                claims.setdefault(trigger.webhook, []).append(name)

    for webhook, owners in claims.items():
        if len(owners) < 2:
            continue

        for owner in owners:
            others = ", ".join(sorted(set(owners) - {owner}))
            report = reports[owner]
            issues = Issues(list(report.issues))
            issues.error("triggers", f"webhook '{webhook}' is also claimed by {others}")
            reports[owner] = _report(report.family, report.file, issues.frozen())


def _check_dispatch_triggers(
    families: Mapping[str, FamilyFile], reports: dict[str, Report]
) -> None:
    """§3.13 rule 6: an `enqueue` trigger that no family targets can never
    fire. A warning, never an error, because the caller's grant and the
    target's trigger land in two files that may be committed in either order
    (the same reasoning as §3.6.1 rule 3)."""
    targeted: set[str] = set()
    for family in families.values():
        fence = family.verbs.enqueue
        if fence is not None:
            targeted.update(fence.targets)

    for name, family in families.items():
        dispatches = any(trigger.enqueue for trigger in family.triggers or ())
        if not dispatches or name in targeted:
            continue

        report = reports[name]
        issues = Issues(list(report.issues))
        issues.warn("triggers", f"no family enqueues '{name}', so this trigger can never fire")
        reports[name] = _report(report.family, report.file, issues.frozen())


def _report(family: str, file: str, issues: tuple[Issue, ...]) -> Report:
    state = FamilyState.RECONCILING
    if any(issue.severity == "error" for issue in issues):
        state = FamilyState.INVALID

    return Report(family=family, file=file, issues=issues, state=state, applied=False)
