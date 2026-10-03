"""The tag a merge gets, decided at the merge (contract 06 §2.1).

Pain 1: two sessions bump the root version to the same number in two pull
requests. The first merge tags it. The second finds the tag on another
commit and stops, so `main`'s tip carries no tag.

The cause is that the number is chosen in the pull request, hours before the
merge, by a writer who cannot see the other writer. So nobody chooses a
number. A pull request declares a bump LEVEL, CI reads the newest tag at
merge time, and one `concurrency` group serializes every merge on the branch.
Two allocations therefore never overlap, and two pull requests cannot carry
the same number because no number lives in a file.

This module is the whole decision, and it is pure: paths, labels and tags in,
a plan out. The workflow gathers the inputs and performs the plan.

A malformed input refuses with the `request` code. Nothing here reaches the
ledger — tag allocation runs in CI, not in the executor — so the closed code
list of `errors.py` needs no entry of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .catalog import ARRIVING, CATALOG, RETIRING, CatalogRow, Kind, Repo
from .errors import Refusal, RefusalCode, safe_token

#: Contract 06 §2: one expression reads every component tag, in every repo.
TAG_RE = re.compile(r"([a-z][a-z0-9-]{1,30})-v(\d+)\.(\d+)\.(\d+)")

TAG_FORMAT = "{component}-v{version}"

#: A component's first tag, whatever level the pull request asked for. A
#: component that has never released has no number to bump.
#:
#: It is a constant here and NOT a field of `component.yaml`: contract 06 §2
#: says "no component's version is written in a file that a human or an agent
#: edits" and "nothing reads a version from a tracked file", and §8's field
#: table has no version field to read.
#:
#: CONTRACT-QUESTION: contract 06 §2.1's table is silent on the first tag,
#: and on how a component with no tag gets one. `plan_tags` answers both.
FIRST_VERSION = "0.1.0"

#: The label prefix that raises the level above `patch`.
LABEL_PREFIX = "bump:"

#: The one file every venv build reads beside its own packages: `uv sync
#: --frozen` installs the third-party versions it pins. A change to it
#: changes every venv tree, so it moves every venv component's tag (contract
#: 06 §1 rule 10). Before this rule a dependency upgrade moved none, and
#: shipped only when something else changed the component.
LOCK_FILE = "uv.lock"

#: How many fields a level line holds: component, label, path.
LEVEL_FIELDS = 3

#: Caps on what the workflow hands in. A tag list grows with the repository,
#: so the cap is generous. Past it, refuse rather than quietly drop a line:
#: a dropped tag line is exactly how a collision would come back.
MAX_INPUT_LINES = 20_000
MAX_LINE_CHARS = 512

REPO_ROOT_PATH = "."

#: A changed-path line may name the component whose RANGE it came from:
#: `<component><RANGE_SEPARATOR><path>`.
#:
#: WHY A RANGE AT ALL. A run can be missed: a red `main` tags nothing, and
#: a pending run gives way to a newer one. Merge A changes `noticeboard/` and its
#: run is missed. Merge B changes only `docs/`, its run passes, and the head
#: commit's own diff names no component. `noticeboard` is never tagged and nothing
#: says so. A component is
#: therefore tagged when its paths changed since ITS OWN newest tag, and each
#: component has its own range, so one flat list of paths cannot say
#: which window a line belongs to.
#:
#: `release/bin/allocate-tags.sh` writes this form. A line with no separator,
#: or one whose prefix names no component of this repo, is the bare path
#: every caller wrote before: the head commit's own diff. Both forms may sit
#: in one file.
RANGE_SEPARATOR = "\t"


class BumpLevel(StrEnum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


class TagOutcome(StrEnum):
    """Contract 06 §2.1's four cases, minus the one that produces no row.

    The planner emits `CREATE` and `NOOP` only. `CONFLICT` is the contract's
    third row, "the tag exists on another SHA", and the planner cannot reach
    it: the target is the newest tag plus one level, which by arithmetic is
    above every tag in the list it was given. That row's real detection point
    is tag creation, where `git tag` refuses an existing ref — a list that
    under-reads, such as a fetch without tags, produces it there and nowhere
    else. `release/bin/allocate-tags.sh` reports it with this name so a
    reader sees one vocabulary either way.
    """

    CREATE = "create"
    NOOP = "noop"
    CONFLICT = "conflict"


#: Highest first: a pull request carrying both labels takes the larger bump.
_LEVEL_ORDER: tuple[BumpLevel, ...] = (BumpLevel.MAJOR, BumpLevel.MINOR, BumpLevel.PATCH)


@dataclass(frozen=True)
class TagPlan:
    """What this merge does to one component's tag."""

    component: str
    outcome: TagOutcome
    tag: str
    from_version: str | None
    detail: str


Version = tuple[int, int, int]


def read_lines(text: str, label: str) -> tuple[str, ...]:
    """Split one input file into lines, refusing anything oversized."""
    lines = [line.strip() for line in text.splitlines()]
    kept = [line for line in lines if line]
    if len(kept) > MAX_INPUT_LINES:
        detail = f"{label} holds more than {MAX_INPUT_LINES} lines"
        raise Refusal(RefusalCode.REQUEST, label, detail)

    for line in kept:
        if len(line) > MAX_LINE_CHARS:
            raise Refusal(RefusalCode.REQUEST, label, f"{label} holds a line over {MAX_LINE_CHARS}")

    return tuple(kept)


def read_level(labels: tuple[str, ...]) -> BumpLevel:
    """`bump:major` beats `bump:minor`. Anything else leaves `patch`.

    The labels are the ones `levels_of` kept for one component: a tag covers
    a range, a range can hold several merged pull requests, and the level is
    the highest among those that changed the component's own paths.
    """
    wanted = {label[len(LABEL_PREFIX) :] for label in labels if label.startswith(LABEL_PREFIX)}
    for level in _LEVEL_ORDER:
        if str(level) in wanted:
            return level

    return BumpLevel.PATCH


def _matches(path: str, row: CatalogRow) -> bool:
    if row.path == REPO_ROOT_PATH:
        return True

    # The lock file pins what every venv build installs (`LOCK_FILE`).
    if row.kind is Kind.VENV and path == LOCK_FILE:
        return True

    # A directory the build installs counts as the component's own (contract
    # 06 §1 rule 9): `door-trigger/x.py` reaches `attendance`.
    tops = (row.path, *row.bundles)

    return any(path == top or path.startswith(f"{top}/") for top in tops)


def _check_path(path: str) -> str:
    if path.startswith("/") or ".." in path.split("/") or "\x00" in path:
        detail = f"changed path is not repo-relative: {safe_token(path)}"
        raise Refusal(RefusalCode.REQUEST, "changed paths", detail)

    return path


def _split_range(line: str, names: frozenset[str]) -> tuple[str | None, str]:
    """`<component><TAB><path>` when the prefix names a component of this repo.

    Anything else is the whole line as one path. The prefix must be a name
    this repo owns, so a path that happens to hold a tab cannot invent a
    component, and a run in agent-control cannot be handed a line for
    `mcp-servers`.
    """
    head, found, rest = line.partition(RANGE_SEPARATOR)
    if found and head in names:
        return head, rest

    return None, line


def touched(paths: tuple[str, ...], repo: Repo) -> tuple[str, ...]:
    """The components a set of changed lines reaches, in catalog order.

    A line is either a bare path from the head commit's own diff, or
    `<component><TAB><path>` from that one component's range (see
    `RANGE_SEPARATOR`). A range line counts for its own component and for no
    other: `attendance<TAB>chaperone` is a change to `chaperone` that happened inside
    `attendance`'s window, and it tags neither.

    A path under no component reaches nothing, which is contract 06 §2.1's
    fourth case: a docs-only merge produces no tag.
    """
    rows = [row for row in CATALOG if row.repo is repo]
    names = frozenset(row.name for row in rows)
    hit: set[str] = set()
    for line in paths:
        owner, path = _split_range(line, names)
        _check_path(path)
        for row in rows:
            if owner is not None and row.name != owner:
                continue

            if _matches(path, row):
                hit.add(row.name)

    return tuple(row.name for row in rows if row.name in hit)


def levels_of(levels: tuple[str, ...], repo: Repo) -> dict[str, tuple[str, ...]]:
    """The bump labels that count for each component of this repo.

    A line is `<component><TAB><label><TAB><path>`: inside that component's
    range, a merged pull request carrying that label changed that path. The
    label counts only when the path is one of the component's own. A merge
    queue lands several pull requests in one push, and a `bump:minor` meant
    for `attendance` must not raise `chaperone` because both merged together.

        attendance<TAB>bump:minor<TAB>attendance   counts: attendance's own path
        chaperone<TAB>bump:minor<TAB>attendance        does not: chaperone's range, not its path

    A line for a component this repo does not own is not this run's to read,
    and refuses: the script writes every line itself.
    """
    rows = {row.name: row for row in CATALOG if row.repo is repo}
    found: dict[str, list[str]] = {}
    for line in levels:
        fields = line.split(RANGE_SEPARATOR)
        if len(fields) != LEVEL_FIELDS or fields[0] not in rows:
            detail = f"not <component>, <label>, <path>: {safe_token(line)}"
            raise Refusal(RefusalCode.REQUEST, "bump levels", detail)

        owner, label, path = fields
        _check_path(path)
        if _matches(path, rows[owner]):
            found.setdefault(owner, []).append(label)

    return {name: tuple(labels) for name, labels in found.items()}


def untagged(tags: tuple[str, ...], repo: Repo) -> tuple[str, ...]:
    """This repo's components that carry no tag at all, in catalog order.

    Scoped by repo exactly as `touched` is: a run in agent-control must not
    invent a tag for `mcp-servers`, whose repository it cannot write.
    """
    rows = [row for row in CATALOG if row.repo is repo]

    return tuple(row.name for row in rows if not _versions_of(tags, row.name))


def tagged_here(at_sha: tuple[str, ...], repo: Repo) -> tuple[str, ...]:
    """This repo's components whose own tag already sits on this commit.

    They are reported so that a second run SAYS it is a no-op. Their
    range is empty by construction — the tag the first run made is the
    range's own base — so without this they would fall out of the plan and a
    re-run would print exactly what a docs-only merge prints.
    """
    rows = [row for row in CATALOG if row.repo is repo]

    return tuple(row.name for row in rows if _versions_of(at_sha, row.name))


def _allocated(
    paths: tuple[str, ...], tags: tuple[str, ...], at_sha: tuple[str, ...], repo: Repo
) -> tuple[str, ...]:
    """Every component this run reports, in catalog order.

    Three sources. The components whose own range changed one of their
    paths. The components that have no tag yet, which is `plan_tags`'s
    first-tag rule. And the components already tagged on this commit, which
    report a no-op rather than nothing.

    A retiring component (`catalog.RETIRING`) is in none of them. A tag is a
    version somebody can release, and the commit that deletes its directory
    changes every one of its paths. An arriving one (`catalog.ARRIVING`) is
    in none of them either: it has no directory to release yet.
    """
    skipped = RETIRING | ARRIVING
    rows = [row for row in CATALOG if row.repo is repo and row.name not in skipped]
    hit = set(touched(paths, repo)) | set(untagged(tags, repo)) | set(tagged_here(at_sha, repo))

    return tuple(row.name for row in rows if row.name in hit)


def _versions_of(tags: tuple[str, ...], component: str) -> list[Version]:
    """Every version this component already carries. An alien tag is ignored.

    The repository's own history holds `v*` and `schema-v*` tags, which the
    new scheme does not reuse (contract 06 §2). They must not read as a
    version of anything.
    """
    found: list[Version] = []
    for tag in tags:
        matched = TAG_RE.fullmatch(tag)
        if matched is None or matched.group(1) != component:
            continue

        found.append((int(matched.group(2)), int(matched.group(3)), int(matched.group(4))))

    return found


#: `FIRST_VERSION`, as the tuple the arithmetic works in.
_FIRST = (0, 1, 0)


def print_version(version: Version) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def format_tag(component: str, version: Version) -> str:
    return TAG_FORMAT.format(component=component, version=print_version(version))


def next_version(current: Version | None, level: BumpLevel) -> Version:
    """The number this merge allocates. Nobody writes it down anywhere."""
    if current is None:
        return _FIRST

    major, minor, patch = current
    if level is BumpLevel.MAJOR:
        return (major + 1, 0, 0)

    if level is BumpLevel.MINOR:
        return (major, minor + 1, 0)

    return (major, minor, patch + 1)


#: The `detail` column for a component that has no tag yet. The level is not
#: named there, because a first tag ignores it.
FIRST_TAG_DETAIL = "first tag"


def _plan_one(
    component: str, level: BumpLevel, tags: tuple[str, ...], at_sha: tuple[str, ...]
) -> TagPlan:
    """Contract 06 §2.1's table, for one component."""
    existing = _versions_of(tags, component)
    newest = max(existing) if existing else None
    was = print_version(newest) if newest is not None else None

    # A re-run reaches this commit with its own tag already made. Check that
    # first: by then the newest tag IS this one, and bumping again would
    # allocate a second number for one commit.
    mine = _versions_of(at_sha, component)
    if mine:
        detail = "already tagged on this commit"

        return TagPlan(component, TagOutcome.NOOP, format_tag(component, max(mine)), was, detail)

    made = format_tag(component, next_version(newest, level))
    detail = f"{level} bump" if newest is not None else FIRST_TAG_DETAIL

    return TagPlan(component, TagOutcome.CREATE, made, was, detail)


def plan_tags(
    paths: tuple[str, ...],
    levels: tuple[str, ...],
    tags: tuple[str, ...],
    at_sha: tuple[str, ...],
    repo: Repo,
) -> tuple[TagPlan, ...]:
    """What this run allocates: one row per component it reports.

    Three sets of components. The ones the changed lines reach, which is
    contract 06 §2.1 read over each component's OWN range
    (`RANGE_SEPARATOR`). **Every component that carries no tag at all**,
    whatever the commit touched. And every component already tagged on this
    commit, which reports a no-op (`tagged_here`).

    WHY A RANGE AND NOT ONE COMMIT. A run can be missed: a red `main` tags
    nothing, and a pending run gives way. A component is therefore tagged
    when its paths changed since its OWN newest tag, not since the previous
    commit. `release/bin/allocate-tags.sh` runs one `git diff` per component
    and labels each line with the component whose range it came from. This
    function never learns what a range IS: it reads labelled lines.

    WHY THE SECOND ONE EXISTS. Contract 06 §2 makes the tag the version, and
    a component with no tag therefore has no version: nothing can name it in
    a release request, and nothing puts that right until a merge happens to
    change a file under its path.

    WHY IT IS NOT A `--bootstrap` FLAG, which was the other design. A flag
    fixes today and surprises later: a component ADDED in a year is untagged
    the same way, nothing says so, and the first person to notice is whoever
    tries to release it. This rule needs nobody to remember anything, and it
    is self-extinguishing — once a component has one tag, the path rule alone
    decides for ever after.

    Every existing rule survives it. A tag is never moved and never deleted.
    A second run on one commit creates nothing, because the at-sha check in
    `_plan_one` runs before any arithmetic. The level is still read from a
    label, each component from its own (`levels_of`), and it still cannot
    raise a first tag above `FIRST_VERSION`.

    CONTRACT-QUESTION: contract 06 §2.1's fourth row says a docs-only merge
    produces no tag. That row holds for a component that HAS a version. A
    component with none is not releasable, so a first tag withholds no
    release — it makes the component nameable.
    """
    asked = levels_of(levels, repo)
    names = _allocated(paths, tags, at_sha, repo)

    return tuple(_plan_one(name, read_level(asked.get(name, ())), tags, at_sha) for name in names)
