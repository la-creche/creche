"""What each page needs, assembled from the readers.

`app.py` owns HTTP and this module owns the page. Nothing here touches a
request, a header or a cookie, so a test builds a page from fixtures and
reads it as data.

Every reader below answers with a `problem` string instead of raising, so
a page renders even when one of its sources is missing. That is the whole
point of invariant 20: the one view stays the truth by saying what it
cannot see, never by going blank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from agent_family import (
    Diff,
    FamilyFile,
    Index,
    Issue,
    McpServerFile,
    Registry,
    classify,
    load_registry,
)

from . import auditfiles, statusdocs, transcript
from .auditfiles import AuditFilter, AuditPage
from .config import Config
from .familyform import Form, form_of
from .jsonfiles import Json
from .sessions import SessionList, SessionReader, SessionRow, TurnRow
from .statusdocs import FamilyRow, OutcomeRow

#: How many audit lines a family page shows before pointing at /audit.
FAMILY_AUDIT_LINES: Final = 10

#: How many outcome records an autonomous family page shows.
FAMILY_OUTCOMES: Final = 10

#: How much of a session's journal one page replays. The window is taken
#: from the END of the journal: `journal_seq` is gapless from 1, so
#: `seq - WINDOW` is a legal `from_seq` and costs no extra call.
TRANSCRIPT_WINDOW: Final = 400


@dataclass(frozen=True)
class Home:
    families: tuple[FamilyRow, ...]
    now: datetime
    #: Empty unless the state root itself could not be read.
    problem: str = ""

    @property
    def stale(self) -> tuple[FamilyRow, ...]:
        return tuple(one for one in self.families if one.stale)


@dataclass(frozen=True)
class FamilyPage:
    row: FamilyRow
    sessions: SessionList
    issues: tuple[Json, ...] = ()
    issues_problem: str = ""
    outcomes: tuple[OutcomeRow, ...] = ()
    audit: AuditPage | None = None

    @property
    def autonomous(self) -> bool:
        return self.row.kind == "autonomous"


@dataclass(frozen=True)
class SessionPage:
    family: str
    session: SessionRow | None
    turns: tuple[TurnRow, ...]
    entries: tuple[transcript.Entry, ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditPage:
    """The edit form, and everything a save has to say afterwards."""

    family: str
    form: Form
    #: Contract 01 §6, through `agent_family`'s classifier. Present only
    #: after a preview or a save, never on a first render.
    diff: Diff | None = None
    issues: tuple[Issue, ...] = ()
    problem: str = ""
    saved: str = ""
    posted: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def previewing(self) -> bool:
        return self.diff is not None and not self.saved


def home(config: Config, now: datetime) -> Home:
    """One row per family."""
    directory = config.families_dir

    if not directory.is_dir():
        return Home(families=(), now=now, problem=f"no families directory at {directory}")

    return Home(families=statusdocs.read_families(directory, now), now=now)


def family_page(config: Config, reader: SessionReader, name: str, now: datetime) -> FamilyPage:
    """One family: status, validation issues, sessions, outcomes and audit."""
    row = statusdocs.read_family(config.families_dir, name, now)
    issues, problem = _report(config, row)

    return FamilyPage(
        row=row,
        sessions=reader.sessions(family=name, limit=config.page_size),
        issues=issues,
        issues_problem=problem,
        outcomes=_outcomes(config, row),
        audit=auditfiles.read_page(
            config.audit_dir, AuditFilter(family=name), limit=FAMILY_AUDIT_LINES
        ),
    )


def _report(config: Config, row: FamilyRow) -> tuple[tuple[Json, ...], str]:
    """Contract 05 §3.2 points at a file. It is read only when named."""
    if row.validation is None or row.validation.ok:
        return (), ""

    named = row.validation.report_path

    if not named:
        return (), "the status document says this family is invalid and names no report"

    return statusdocs.read_report(Path(named))


def _outcomes(config: Config, row: FamilyRow) -> tuple[OutcomeRow, ...]:
    if row.kind != "autonomous":
        return ()

    return statusdocs.read_outcomes(config.outcomes_dir, row.name, FAMILY_OUTCOMES)


def session_page(reader: SessionReader, family: str, session: str) -> SessionPage:
    """The transcript and the turns, read only."""
    detail = reader.detail(family, session)
    problems: list[str] = []

    if detail.problem:
        problems.append(detail.problem)

    stream = reader.events(family, session, from_seq=_window(detail.session))
    problems.extend(stream.problems)

    return SessionPage(
        family=family,
        session=detail.session,
        turns=detail.turns,
        entries=transcript.fold(stream.lines),
        problems=tuple(problems),
    )


def _window(row: SessionRow | None) -> int:
    """Where the replay starts. 0 for a session this view could not read,
    which replays the whole journal and is bounded by the reader anyway."""
    if row is None:
        return 0

    return max(0, row.journal_seq - TRANSCRIPT_WINDOW)


def audit_page(config: Config, wanted: AuditFilter, offset: int) -> AuditPage:
    """One page of the audit, filtered."""
    return auditfiles.read_page(config.audit_dir, wanted, offset=offset, limit=config.page_size)


def edit_page(config: Config, name: str) -> EditPage:
    """The edit form's first render, straight from the registry."""
    registry = load_registry(config.registry_dir)
    family = registry.families.get(name)

    if family is None:
        return EditPage(family=name, form=Form(family=name, fields=()), problem=_missing(name))

    return EditPage(family=name, form=form_of(family, index_of(registry)))


def _missing(name: str) -> str:
    return f"the registry holds no family named {name!r}, or its file does not parse"


def index_of(registry: Registry) -> Index:
    """What `check_family` needs to judge one family: its neighbours.

    Built from the registry it was just loaded from, so a lock the form
    shows is a lock the validator would apply to this exact tree.
    """
    servers: dict[str, McpServerFile] = dict(registry.servers)

    return Index(
        kinds={one: found.kind for one, found in registry.families.items()},
        servers=servers,
        skills=registry.skills,
    )


def preview_of(current: FamilyFile, wanted: FamilyFile) -> Diff:
    """Contract 01 §6: which changes land live, and which replace the
    sandbox. Shown BEFORE the save."""
    return classify(current, wanted)
