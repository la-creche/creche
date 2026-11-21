"""Checks P1 to P6 of a release from the Forgejo instance
(`docs/design/forgejo-release-path.md`, section 5).

```text
  verify_forge_release
    |
    |-- P1a  releases/tags/{tag}            -> check_release
    |-- P2   git/refs/tags/{tag}            -> the one exact, annotated ref
    |        git/tags/{object}              -> check_tag_commit
    |-- P1   the tag message                -> decode_message
    |        cosign verify-blob --key       -> check_signature  (no network)
    |        the signed statement           -> read_statement
    |        the statement and the catalog  -> check_statement
    |-- P3   compare/main...{sha}           -> check_on_main
    |-- P4   commits/{sha}/pull             -> check_merge
    |-- P5   commits/{head}/status          -> check_green
    v
  ForgeVerified --> check_digests (P6, after the copy of step 2)
```

Each rule is a function from typed facts to a pass, or it raises one
`Refusal` with its code and a fixed reason. No rule returns a bool. A pass
of the whole chain gives a `ForgeVerified`, and only `verify_forge_release`
makes one. P6 takes the signed tag entry from that value, so no caller can
give P6 an entry that no signature covers.

Nothing calls this module yet. The wiring PR connects it to step 3 with the
HTTP client, the site keys and the key file of FR10. The GitHub path of
`provenance.py` does not change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..artifacts import BUNDLE_SUFFIX, ArtifactRefusal, asset_limits
from ..catalog import CATALOG_BY_NAME, RELEASE_WORKFLOW, Kind, Releases, Repo
from ..errors import Refusal, RefusalCode
from .forge_api import (
    MAIN,
    CommitStatuses,
    CompareFacts,
    ForgeGetFn,
    ForgePath,
    ForgeReason,
    ForgeReply,
    ForgeRepo,
    ForgeValueError,
    ObjectId,
    PullFacts,
    ReleaseFacts,
    TagName,
    TagObject,
    commit_pull_path,
    commit_status_path,
    compare_path,
    read_commit_pull,
    read_commit_status,
    read_compare,
    read_release,
    read_tag_object,
    read_tag_refs,
    release_path,
    tag_object_path,
    tag_refs_path,
)
from .host import RunFn
from .provenance import Verified
from .release_statement import (
    ReleaseStatement,
    StatementTag,
    check_signature,
    decode_message,
    read_statement,
)

#: The required status contexts of each repository (note section 6). Each
#: name is `<workflow name> / <job name> (<event>)`.
# CONTRACT-QUESTION: forgejo-release-path.md section 6 takes these names
# from the workflow files on GitHub. The recordings hold push contexts
# only, so no pull_request context of the five repositories is measured on
# Forgejo. A missing context refuses P5 (fail closed). A change is this map.
REQUIRED_CONTEXTS: Final[Mapping[Repo, frozenset[str]]] = {
    Repo.CRECHE: frozenset({"gate / gate (pull_request)"}),
    Repo.MCP: frozenset({"gate / gate (pull_request)"}),
    Repo.CHAPERONE: frozenset({"ci / gate (pull_request)"}),
    Repo.REGISTRY: frozenset(
        {"validate / validate (pull_request)", "validate / reach (pull_request)"}
    ),
    Repo.CRECHE_RELEASE: frozenset(
        {"gate / gate (pull_request)", "secrets-guard / guard (pull_request_target)"}
    ),
}

#: The status of a green context. A context that is not required may also
#: be skipped (M6: a skipped job posts `skipped`).
GREEN: Final = "success"
SKIPPED: Final = "skipped"

#: The status words of a commit status that root knows.
# CONTRACT-QUESTION: forgejo-release-path.md section 5, P5, names only
# `success` and `skipped`. The reading here knows the other words of the
# Forgejo commit status and refuses each one. Each other word refuses as
# UNKNOWN_STATUS. A change is this set.
KNOWN_STATUSES: Final = frozenset({GREEN, SKIPPED, "pending", "failure", "error", "warning"})


class ProvenanceReason(StrEnum):
    """The reasons of a refusal of a rule of this module, a closed list."""

    BAD_FORGE_NAME = "the owner or the repository name on the forge has a wrong shape"
    BAD_TAG = "the tag is not a release tag of the component"
    BAD_COMMIT = "the resolved commit is not 40 lower-case hex digits"
    OTHER_REPOSITORY = "the statement names another repository"
    OTHER_WORKFLOW = "the statement names another workflow"
    OTHER_COMMIT = "the statement names another commit"
    TAG_NOT_SIGNED = "the statement does not name the tag"
    FOREIGN_TAG = "the statement names a tag of no releasable component of the repository"
    ASSET_ON_SOURCE = "the statement names an asset for a venv or units component"
    NO_RELEASE_WORKFLOW = "the catalog names no release workflow for this repository"


def _refuse(code: RefusalCode, component: str, reason: StrEnum) -> Refusal:
    return Refusal(code, component, reason.value)


# -- the inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredContexts:
    """The status contexts that P5 requires of one repository. Never
    empty, so P5 never passes a pull request that ran no required job."""

    names: frozenset[str]

    def __post_init__(self) -> None:
        if not self.names:
            raise ValueError("a repository requires at least one context")


def required_for(repo: Repo) -> RequiredContexts:
    """The required contexts of `repo` (note section 6)."""
    return RequiredContexts(REQUIRED_CONTEXTS[repo])


def forge_repo(component: str, repo: Repo, owner: str) -> ForgeRepo:
    """The repository `repo` of the catalog under `owner` on the forge. An
    owner with a wrong shape refuses with the `site` code."""
    try:
        return ForgeRepo(owner, repo)
    except ForgeValueError:
        raise _refuse(RefusalCode.SITE, component, ProvenanceReason.BAD_FORGE_NAME) from None


@dataclass(frozen=True)
class ExpectedOrigin:
    """What the statement of a release must name: the repository on the
    forge, its release workflow and the resolved commit (FR7)."""

    forge: ForgeRepo
    workflow: str
    commit: str


def expected_origin(forge: ForgeRepo, commit: ObjectId) -> ExpectedOrigin:
    """The origin of a release of `forge.repo`. The workflow comes from
    `catalog.RELEASE_WORKFLOW`. A repository that the map does not hold
    refuses with the `catalog` code, as `artifacts.signer_policy` does."""
    # Decided by the operator on 2026-11-16: a release of mcp or of
    # creche-release refuses here until its row of RELEASE_WORKFLOW exists
    # (FR9, FR29). Both rows exist since PR 2 of the note (2026-11-21).
    workflow = RELEASE_WORKFLOW.get(forge.repo)
    if workflow is None:
        raise _refuse(RefusalCode.CATALOG, str(forge.repo), ProvenanceReason.NO_RELEASE_WORKFLOW)

    return ExpectedOrigin(forge, workflow, commit.text)


# -- the rules ----------------------------------------------------------------------


def check_release(component: str, facts: ReleaseFacts, tag: TagName) -> None:
    """P1a: the Release names the tag and is no draft and no prerelease. No
    author check (FR2)."""
    if facts.tag_name != tag.text:
        raise _refuse(RefusalCode.P1, component, ForgeReason.OTHER_TAG)

    if facts.draft or facts.prerelease:
        raise _refuse(RefusalCode.P1, component, ForgeReason.DRAFT_OR_PRERELEASE)


def check_tag_commit(component: str, tag_object: TagObject, commit: ObjectId) -> None:
    """P2: the annotated tag names the resolved commit."""
    if tag_object.commit != commit:
        raise _refuse(RefusalCode.P2, component, ForgeReason.OTHER_COMMIT)


def check_statement(
    component: str, statement: ReleaseStatement, origin: ExpectedOrigin, tag: TagName
) -> StatementTag:
    """P1b: the signed statement names the repository, its release workflow
    and the resolved commit, and it names the tag. The entry of the tag is
    the answer.

    Each tag of the statement must name a releasable component of the
    catalog in this repository, and a tag of a `venv` or `units` component
    names no asset (section 4.5 step 3)."""
    # Decided by the operator on 2026-11-16: root checks every tag of a
    # statement against the catalog. The signer does not know component
    # kinds, so this loop is the one check of section 4.5 step 3.
    if statement.repository != origin.forge.full_name:
        raise _refuse(RefusalCode.P1, component, ProvenanceReason.OTHER_REPOSITORY)

    if statement.workflow != origin.workflow:
        raise _refuse(RefusalCode.P1, component, ProvenanceReason.OTHER_WORKFLOW)

    if statement.commit != origin.commit:
        raise _refuse(RefusalCode.P1, component, ProvenanceReason.OTHER_COMMIT)

    for entry in statement.tags:
        row = CATALOG_BY_NAME.get(entry.component)
        if row is None or row.repo is not origin.forge.repo or row.releases is not Releases.YES:
            raise _refuse(RefusalCode.P1, component, ProvenanceReason.FOREIGN_TAG)

        if row.kind in (Kind.VENV, Kind.UNITS) and entry.assets:
            raise _refuse(RefusalCode.P1, component, ProvenanceReason.ASSET_ON_SOURCE)

    for entry in statement.tags:
        if entry.tag == tag.text:
            return entry

    raise _refuse(RefusalCode.P1, component, ProvenanceReason.TAG_NOT_SIGNED)


def check_on_main(component: str, facts: CompareFacts) -> None:
    """P3: `main` holds the commit: `total_commits` is 0 and `commits` is
    empty (M4)."""
    if facts.total_commits != 0 or facts.commits != 0:
        raise _refuse(RefusalCode.P3, component, ForgeReason.NOT_ON_MAIN)


def check_merge(component: str, pull: PullFacts, commit: ObjectId) -> None:
    """P4: the commit is the merge of a pull request into `main` (M5)."""
    merge_of = pull.merged and pull.base_ref == MAIN and pull.merge_commit_sha == commit.text
    if not merge_of:
        raise _refuse(RefusalCode.P4, component, ForgeReason.NOT_A_MERGE)


def check_green(component: str, statuses: CommitStatuses, required: RequiredContexts) -> None:
    """P5: each required context is `success`, and each other context is
    `success` or `skipped` (note section 5, M6)."""
    # CONTRACT-QUESTION: forgejo-release-path.md section 5, P5, says "a
    # count above one page refuses" and does not measure the page size. The
    # reading here refuses when `total_count` is not the count of the
    # listed entries. A change is this one comparison.
    if statuses.total_count != len(statuses.entries):
        raise _refuse(RefusalCode.P5, component, ForgeReason.STATUS_PAGE)

    seen: dict[str, str] = {}
    for context, status in statuses.entries:
        if context in seen:
            raise _refuse(RefusalCode.P5, component, ForgeReason.CONTEXT_TWICE)

        seen[context] = status

    if not required.names <= seen.keys():
        raise _refuse(RefusalCode.P5, component, ForgeReason.CONTEXT_MISSING)

    for context, status in seen.items():
        if status not in KNOWN_STATUSES:
            raise _refuse(RefusalCode.P5, component, ForgeReason.UNKNOWN_STATUS)

        green = status == GREEN or (status == SKIPPED and context not in required.names)
        if not green:
            raise _refuse(RefusalCode.P5, component, ForgeReason.CONTEXT_NOT_GREEN)


# -- the composition ----------------------------------------------------------------


class _Checked:
    """No module but this one names this type. `ForgeVerified` takes a value
    of it first, so only `verify_forge_release` makes one."""


_CHECKED: Final = _Checked()


@dataclass(frozen=True)
class ForgeVerified:
    """A release from the Forgejo instance after P1 to P5 passed: what the
    ledger records, and the signed entry of the tag for P6."""

    made: InitVar[_Checked]
    verified: Verified
    entry: StatementTag

    def __post_init__(self, made: _Checked) -> None:
        if made is not _CHECKED:
            raise TypeError("only verify_forge_release makes a ForgeVerified")


def _get(api: ForgeGetFn, path: ForgePath, code: RefusalCode, component: str) -> ForgeReply:
    """One API read. A read that gets no reply (`OSError`) refuses with the
    code of the check: the check fails closed. Each other error is a fault
    of the step, and the ledger gets its type (executor rule 9)."""
    try:
        return api(path)
    except OSError:
        raise _refuse(code, component, ForgeReason.NOT_REACHED) from None


def _commit(code: RefusalCode, component: str, sha: str) -> ObjectId:
    try:
        return ObjectId(sha)
    except ForgeValueError:
        raise _refuse(code, component, ProvenanceReason.BAD_COMMIT) from None


def _release_tag(component: str, tag: str) -> TagName:
    try:
        name = TagName(tag)
    except ForgeValueError:
        raise _refuse(RefusalCode.P1, component, ProvenanceReason.BAD_TAG) from None

    if name.text.rpartition("-v")[0] != component:
        raise _refuse(RefusalCode.P1, component, ProvenanceReason.BAD_TAG)

    return name


def _merge_checks(api: ForgeGetFn, component: str, forge: ForgeRepo, commit: ObjectId) -> Verified:
    """P3 to P5 for one commit."""
    reply = _get(api, compare_path(forge, commit), RefusalCode.P3, component)
    check_on_main(component, read_compare(component, reply))

    reply = _get(api, commit_pull_path(forge, commit), RefusalCode.P4, component)
    pull = read_commit_pull(component, reply)
    check_merge(component, pull, commit)

    reply = _get(api, commit_status_path(forge, pull.head_sha), RefusalCode.P5, component)
    check_green(component, read_commit_status(component, reply), required_for(forge.repo))

    return Verified(component=component, pr=pull.number, head_sha=pull.head_sha.text)


def verify_forge_release(
    api: ForgeGetFn,
    run: RunFn,
    component: str,
    forge: ForgeRepo,
    tag: str,
    sha: str,
    work: Path,
) -> ForgeVerified:
    """P1 to P5 for one component at one tag, in the order of the picture
    at the top. It raises at the first check that does not pass. `forge`
    is the catalog repository on the forge (`forge_repo`), and `work` is
    the work tree of the request. P6 is `check_digests`, after the copy of
    step 2."""
    release_tag = _release_tag(component, tag)
    commit = _commit(RefusalCode.P2, component, sha)
    origin = expected_origin(forge, commit)

    reply = _get(api, release_path(forge, release_tag), RefusalCode.P1, component)
    check_release(component, read_release(component, reply), release_tag)

    reply = _get(api, tag_refs_path(forge, release_tag), RefusalCode.P2, component)
    ref = read_tag_refs(component, reply, release_tag)
    reply = _get(api, tag_object_path(forge, ref.tag_object), RefusalCode.P2, component)
    tag_object = read_tag_object(component, reply, release_tag, ref.tag_object)
    check_tag_commit(component, tag_object, commit)

    message = decode_message(component, tag_object.message)
    statement = read_statement(check_signature(run, component, message, work))
    entry = check_statement(component, statement, origin, release_tag)

    verified = _merge_checks(api, component, forge, commit)

    return ForgeVerified(_CHECKED, verified, entry)


def verify_forge_commit(api: ForgeGetFn, component: str, forge: ForgeRepo, sha: str) -> Verified:
    """P3 to P5 for a commit that has no tag: the registry commit of an
    `mcp-servers` release (note section 5, last paragraph)."""
    commit = _commit(RefusalCode.P3, component, sha)

    return _merge_checks(api, component, forge, commit)


# -- P6 -------------------------------------------------------------------------------


def signed_names(component: str) -> frozenset[str]:
    """The asset names that the statement signs for `component`: each asset
    of `artifacts.asset_limits` but the bundles, which retire (FR8). The
    kind is the kind of the catalog row. Discovery refuses a manifest of
    another kind. A name of no catalog row has no asset."""
    row = CATALOG_BY_NAME.get(component)
    if row is None:
        return frozenset()

    limits = asset_limits(component, row.kind)

    return frozenset(one.name for one in limits if not one.name.endswith(BUNDLE_SUFFIX))


def check_digests(release: ForgeVerified, digests: Mapping[str, str]) -> None:
    """P6: each asset of the tag in the signed statement has a copy with the
    same SHA-256 (note section 5).

    The expected names are `signed_names` of the component of `release`,
    so no caller names the assets of another component. `digests` maps the
    name of each copy of step 2 to its SHA-256 in hex. The member list and
    the disk rules after the signature stay in `provenance.check_artifact`
    and `check_files`.

    1. The statement names another set of assets: `SIGNATURE_REFUSED`.
    2. Step 2 made no copy of an asset: `NO_ARTIFACT`.
    3. A copy has another SHA-256: `SIGNATURE_REFUSED`. A digest that is
       not 64 lower-case hex digits is never equal to a signed one.
    """
    component = release.verified.component
    signed = {asset.name: asset.sha256 for asset in release.entry.assets}
    if frozenset(signed) != signed_names(component):
        raise _refuse(RefusalCode.P6, component, ArtifactRefusal.SIGNATURE_REFUSED)

    for asset, digest in signed.items():
        copy = digests.get(asset)
        if copy is None:
            raise _refuse(RefusalCode.P6, component, ArtifactRefusal.NO_ARTIFACT)

        if copy != digest:
            raise _refuse(RefusalCode.P6, component, ArtifactRefusal.SIGNATURE_REFUSED)
