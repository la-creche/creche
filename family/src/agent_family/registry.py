"""The registry loader: `families/`, `mcp/` and `skills/` under one root.

Reads every file once, cross-references them, and answers validated objects
plus one report per family. A bad file never stops the others: one bad file is
not a registry outage (contract 01 §7 rule 5)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import grammar, tomlfile
from .fences import Fences
from .model import FamilyFile, SkillContent
from .parse import parse_family_toml, parse_server_toml
from .report import FamilyState, Issue, Issues, Report
from .server import McpServerFile
from .serverrules import check_server
from .skillmd import parse_declared_files
from .validate import HostFacts, Index, check_family

#: The name of a family file and the name of a server file. `grammar.py`
#: holds them, so that `validate.py` names a file without an import of this
#: module. A package that imports `agent_family` takes each name from here
#: and keeps no copy.
FAMILY_FILE: Final = grammar.FAMILY_FILE
SERVER_FILE: Final = grammar.SERVER_FILE
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
    skill_texts: Mapping[str, SkillContent]
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


def load_registry(
    root: Path, host: HostFacts | None = None, fences: Fences | None = None
) -> Registry:
    """Read, parse and cross-reference one registry. Never raises on content.
    With no `fences`, each fence check is a downgraded warning: the reader
    that applies the registry passes the fences of its host."""
    servers, server_reports = _read_servers(root)
    parsed, per_family, files = _read_families(root)
    skills = _read_skills(root)
    skill_texts = _read_skill_texts(root)
    index = Index(
        kinds={name: family.kind for name, family in parsed.items()},
        servers=servers,
        skills=skills,
        skill_texts=skill_texts,
    )

    reports: dict[str, Report] = {}
    for name, family in parsed.items():
        issues = Issues(list(per_family[name]))
        check_family(family, name, index, issues, host, fences)
        reports[name] = _report(name, f"{FAMILIES_DIR}/{name}/{files[name]}", issues.frozen())

    # Directories whose file failed to parse still get their report.
    for name, entries in per_family.items():
        if name not in reports:
            reports[name] = _report(name, f"{FAMILIES_DIR}/{name}/{files[name]}", tuple(entries))

    _check_webhooks(parsed, reports)
    _check_dispatch_triggers(parsed, reports)
    return Registry(
        root=root,
        revision=revision_of(root),
        families=parsed,
        servers=servers,
        skills=skills,
        skill_texts=skill_texts,
        reports=reports,
        server_reports=server_reports,
    )


def revision_of(root: Path) -> str:
    """A content revision over every registry file, path included. Renaming a
    file therefore moves the revision even when no byte inside it changed."""
    digest = hashlib.sha256()
    for path in sorted(_registry_files(root)):
        # A file name that is not UTF-8 has surrogate characters in its path
        # text. `surrogateescape` gives the bytes of the name back, so the
        # strict codec does not raise on such a name.
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


def _read_toml(path: Path, kind: tomlfile.FileKind) -> tuple[bytes, str | None]:
    """The bytes of a TOML file of one kind, or no bytes and the reason it
    could not be read. The read stops one byte past the byte limit of the
    kind, so the reader refuses a larger file with its own text."""
    try:
        with path.open("rb") as handle:
            return handle.read(kind.byte_limit + 1), None
    except OSError as exc:
        return b"", f"cannot read {path.name}: {exc.strerror or exc}"


@dataclass(frozen=True)
class _Kind[T]:
    """One kind of file of the registry: its name, its kind for the TOML
    reader and its parser."""

    name: str
    toml_kind: tomlfile.FileKind
    parse_toml: Callable[[bytes], tuple[T | None, list[Issue]]]


_FAMILY: Final = _Kind[FamilyFile](FAMILY_FILE, tomlfile.FileKind.FAMILY, parse_family_toml)
_SERVER: Final = _Kind[McpServerFile](SERVER_FILE, tomlfile.FileKind.SERVER, parse_server_toml)


@dataclass(frozen=True)
class _Read[T]:
    """What the registry read in one directory: the name of the file that
    the report names, the parsed model, and the issues of the read and of
    the parse."""

    file: str
    parsed: T | None
    issues: tuple[Issue, ...]


def _read_directory[T](directory: Path, kind: _Kind[T]) -> _Read[T]:
    """The one file of a directory, read and parsed."""
    file = kind.name
    issues = Issues()
    if not (directory / file).is_file():
        # §5.6 rule 3: a warning, and `caregiver` ignores the directory.
        issues.warn("<document>", f"no {file}; this directory is ignored")
        return _Read(file, None, issues.frozen())

    data, problem = _read_toml(directory / file, kind.toml_kind)
    if problem is None:
        model, entries = kind.parse_toml(data)
        return _Read(file, model, tuple(entries))

    issues.error("<document>", problem)
    return _Read(file, None, issues.frozen())


def _read_families(
    root: Path,
) -> tuple[dict[str, FamilyFile], dict[str, tuple[Issue, ...]], dict[str, str]]:
    """Each parsed family, the issues of each directory, and the name of the
    file that the report of each directory names."""
    parsed: dict[str, FamilyFile] = {}
    issues_by_name: dict[str, tuple[Issue, ...]] = {}
    files: dict[str, str] = {}
    for directory in _subdirectories(root / FAMILIES_DIR):
        name = directory.name
        read = _read_directory(directory, _FAMILY)
        files[name] = read.file
        issues_by_name[name] = read.issues
        if read.parsed is not None:
            parsed[name] = read.parsed

    return parsed, issues_by_name, files


def _read_servers(root: Path) -> tuple[dict[str, McpServerFile], dict[str, Report]]:
    """Each parsed server and the report of each directory."""
    servers: dict[str, McpServerFile] = {}
    reports: dict[str, Report] = {}
    for directory in _subdirectories(root / MCP_DIR):
        name = directory.name
        read = _read_directory(directory, _SERVER)
        issues = Issues(list(read.issues))
        if read.parsed is not None:
            servers[name] = read.parsed
            check_server(read.parsed, name, issues)

        reports[name] = _report(name, f"{MCP_DIR}/{name}/{read.file}", issues.frozen())

    return servers, reports


def _read_skills(root: Path) -> frozenset[str]:
    return frozenset(
        directory.name
        for directory in _subdirectories(root / SKILLS_DIR)
        if (directory / SKILL_FILE).is_file()
    )


def _read_skill_texts(root: Path) -> dict[str, SkillContent]:
    """Every skill directory's staged texts and faults (contract 01 §6.1).

    A skill stages its SKILL.md plus every helper its frontmatter declares.
    A declaration the directory does not honor — a helper missing, a stray
    file undeclared, a frontmatter the parser cannot read — is a fault for
    the validator, never a silent loss and never a raise."""
    texts: dict[str, SkillContent] = {}
    for directory in _subdirectories(root / SKILLS_DIR):
        text, fault = _read(directory / SKILL_FILE)
        if fault is not None:
            # No readable SKILL.md is no skill at all: `_read_skills` leaves
            # the name out and the validator reports the grant against it.
            continue

        files = {SKILL_FILE: text}
        faults: list[str] = []
        declared, parse_faults = parse_declared_files(text)
        faults.extend(parse_faults)
        for relative in declared:
            if not (directory / relative).is_file():
                faults.append(f"declares '{relative}', which the skill directory does not hold")
                continue
            helper, helper_fault = _read(directory / relative)
            if helper_fault is not None:
                faults.append(f"has an unreadable helper '{relative}' ({helper_fault})")
                continue
            files[relative] = helper

        held = {
            str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()
        } - files.keys()
        for extra in sorted(held):
            faults.append(f"holds '{extra}', which its frontmatter does not declare")
        texts[directory.name] = SkillContent(files=files, faults=tuple(faults))

    return texts


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
