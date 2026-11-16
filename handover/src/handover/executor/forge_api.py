"""The replies of the Forgejo API that checks P1 to P5 read
(`docs/design/forgejo-release-path.md`, section 3 M2 to M6 and section 5).

This module is the one module that knows a Forgejo path or a field name of
a Forgejo reply. Each reader takes the raw reply and gives typed facts, or
it raises one `Refusal` with the code of its check and a reason of
`ForgeReason`. Each reader checks in this order:

1. The status. A 404 has its own reason where the note gives one.
2. The byte cap, `API_MAX_BYTES`.
3. Strict JSON (`strictjson.loads`). A text that is not strict records one
   notice with `SURFACE`, as `provenance.py` does.
4. Each field that the check reads, and its type. `bool` is no integer.
   A key that the check does not read is ignored.

The HTTP client is not here. It is part of the wiring PR, because it needs
HTTPS on the Forgejo instance (FR11, M9). A `ForgeGetFn` gives the raw
reply of one `ForgePath`, and only the builders below make a `ForgePath`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import Any, Final, cast

from .. import notices, strictjson
from ..catalog import Repo
from ..errors import Refusal, RefusalCode
from .release_statement import COMMIT_SHA_RE, OWNER_RE, RELEASE_TAG_RE, REPO_NAME_RE

HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404

#: The most bytes of one reply, as for the GitHub API (`provenance.py`).
API_MAX_BYTES: Final = 2 << 20

#: The surface word of a notice for a reply that is not strict JSON. It is
#: the word of `provenance.SURFACE`, written again here so that this module
#: does not import the GitHub reader.
SURFACE: Final = "code_host.answer"

#: What the refs of a tag start with.
TAG_REF_PREFIX: Final = "refs/tags/"

#: The `object.type` of a ref to an annotated tag, and of a ref to a commit
#: (a lightweight tag). Measured on Forgejo 17.0.1 (note section 5, P2).
TYPE_TAG: Final = "tag"
TYPE_COMMIT: Final = "commit"

#: The branch that P3 compares with (note section 5, P3).
MAIN: Final = "main"


@dataclass(frozen=True)
class ForgeReply:
    """One raw reply: the HTTP status and the body bytes."""

    status: int
    body: bytes


class ForgeValueError(ValueError):
    """A value has no shape that a Forgejo path may hold."""


@dataclass(frozen=True)
class ForgeRepo:
    """A repository of the catalog on the Forgejo instance: the owner of
    the site and the catalog repository. The name on the forge is the
    value of `repo`, so no value pairs one repository of the catalog with
    the name of another. The owner and the name have the grammar of the
    signer crate (`names.rs`). The statement names the repository as
    `owner/name`."""

    owner: str
    repo: Repo

    # Decided by the operator on 2026-11-16: the repository name on the
    # forge equals the catalog name. Only the owner comes from the site
    # file (FR11). A change is one site read of the name inside `name`,
    # keyed by `repo`, so the pair stays bound.
    def __post_init__(self) -> None:
        # A text with the value of a `Repo` is no `Repo`: the type, not the
        # text, binds the name.
        try:
            bound = Repo(self.repo) is self.repo
        except ValueError:
            bound = False

        if not bound:
            raise ForgeValueError("not a repository of the catalog")

        if OWNER_RE.fullmatch(self.owner) is None or REPO_NAME_RE.fullmatch(self.name) is None:
            raise ForgeValueError("not an owner and a repository name")

    @property
    def name(self) -> str:
        return self.repo.value

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class ObjectId:
    """A git object id: 40 lower-case hex digits, a commit or a tag object."""

    text: str

    def __post_init__(self) -> None:
        if COMMIT_SHA_RE.fullmatch(self.text) is None:
            raise ForgeValueError("not a git object id")


@dataclass(frozen=True)
class TagName:
    """A release tag `<component>-vX.Y.Z`, in the grammar of the signer."""

    text: str

    def __post_init__(self) -> None:
        if RELEASE_TAG_RE.fullmatch(self.text) is None:
            raise ForgeValueError("not a release tag")


class _Built:
    """No module but this one names this type. `ForgePath` takes a value of
    it first, so only the builders below make one."""


_BUILT: Final = _Built()


@dataclass(frozen=True)
class ForgePath:
    """A path below the API base `/api/v1` of the Forgejo instance. Each
    part of it passed a pattern."""

    made: InitVar[_Built]
    text: str

    def __post_init__(self, made: _Built) -> None:
        if made is not _BUILT:
            raise TypeError("only the builders of forge_api make a ForgePath")


def _repo_path(repo: ForgeRepo, rest: str) -> ForgePath:
    return ForgePath(_BUILT, f"/repos/{repo.owner}/{repo.name}/{rest}")


def release_path(repo: ForgeRepo, tag: TagName) -> ForgePath:
    """P1: `GET /repos/{o}/{r}/releases/tags/{tag}`."""
    return _repo_path(repo, f"releases/tags/{tag.text}")


def tag_refs_path(repo: ForgeRepo, tag: TagName) -> ForgePath:
    """P2: `GET /repos/{o}/{r}/git/refs/tags/{tag}`, a prefix match."""
    return _repo_path(repo, f"git/refs/tags/{tag.text}")


def tag_object_path(repo: ForgeRepo, tag_object: ObjectId) -> ForgePath:
    """P2: `GET /repos/{o}/{r}/git/tags/{sha}`."""
    return _repo_path(repo, f"git/tags/{tag_object.text}")


def compare_path(repo: ForgeRepo, commit: ObjectId) -> ForgePath:
    """P3: `GET /repos/{o}/{r}/compare/main...{sha}`."""
    return _repo_path(repo, f"compare/{MAIN}...{commit.text}")


def commit_pull_path(repo: ForgeRepo, commit: ObjectId) -> ForgePath:
    """P4: `GET /repos/{o}/{r}/commits/{sha}/pull`."""
    return _repo_path(repo, f"commits/{commit.text}/pull")


def commit_status_path(repo: ForgeRepo, commit: ObjectId) -> ForgePath:
    """P5: `GET /repos/{o}/{r}/commits/{head}/status`."""
    return _repo_path(repo, f"commits/{commit.text}/status")


#: One API read. The HTTP client of the wiring PR gives it. A read that
#: gets no reply raises `OSError`. Each other error is a fault of the step.
ForgeGetFn = Callable[[ForgePath], ForgeReply]


class ForgeReason(StrEnum):
    """The reasons of a refusal from a reply, a closed list. Each value is a
    fixed text. No text of a reply reaches a refusal."""

    NOT_REACHED = "the Forgejo instance was not reached"
    HTTP_STATUS = "the reply has an unexpected HTTP status"
    TOO_LARGE = "the reply is larger than the cap"
    NOT_JSON = "the reply is no strict JSON text"
    BAD_SHAPE = "the reply does not have the measured shape"
    NO_RELEASE = "the repository has no Release for the tag"
    OTHER_TAG = "the reply names another tag"
    DRAFT_OR_PRERELEASE = "the Release is a draft or a prerelease"
    NO_TAG = "the repository has no such tag"
    NO_EXACT_REF = "no ref is exactly the tag"
    TWO_EXACT_REFS = "two refs are exactly the tag"
    LIGHTWEIGHT_TAG = "the tag is a lightweight tag"
    OTHER_TAG_OBJECT = "the reply names another tag object"
    NOT_A_COMMIT = "the tag does not name a commit"
    OTHER_COMMIT = "the tag names another commit than the resolved one"
    NOT_ON_MAIN = "the commit is not reachable from main"
    NOT_A_MERGE = "the commit is not the merge of a pull request into main"
    STATUS_PAGE = "the status count is not the count of the listed statuses"
    CONTEXT_TWICE = "a status context is listed twice"
    CONTEXT_MISSING = "a required status context is missing"
    CONTEXT_NOT_GREEN = "a status context is not green"
    UNKNOWN_STATUS = "a status context has an unknown status"


def _refuse(code: RefusalCode, component: str, reason: ForgeReason) -> Refusal:
    return Refusal(code, component, reason.value)


def _value(
    code: RefusalCode, component: str, reply: ForgeReply, missing: ForgeReason | None
) -> object:
    """The JSON value of a 200 reply, after the status and the cap."""
    if reply.status == HTTP_NOT_FOUND and missing is not None:
        raise _refuse(code, component, missing)

    if reply.status != HTTP_OK:
        raise _refuse(code, component, ForgeReason.HTTP_STATUS)

    if len(reply.body) > API_MAX_BYTES:
        raise _refuse(code, component, ForgeReason.TOO_LARGE)

    try:
        return strictjson.loads(reply.body, cap=API_MAX_BYTES)
    except strictjson.NotStrictJson as error:
        notices.record(SURFACE, error.rule)
        raise _refuse(code, component, ForgeReason.NOT_JSON) from None


class _Shape:
    """The field reads of one reader. A field of the wrong type refuses
    with the reason `BAD_SHAPE` and the code of the check."""

    def __init__(self, code: RefusalCode, component: str) -> None:
        self._code = code
        self._component = component

    def fail(self) -> Refusal:
        return _refuse(self._code, self._component, ForgeReason.BAD_SHAPE)

    def obj(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise self.fail()

        return cast("dict[str, Any]", value)

    def items(self, value: object) -> list[object]:
        if not isinstance(value, list):
            raise self.fail()

        return cast("list[object]", value)

    def text(self, value: object) -> str:
        if not isinstance(value, str):
            raise self.fail()

        return value

    def flag(self, value: object) -> bool:
        if not isinstance(value, bool):
            raise self.fail()

        return value

    def whole(self, value: object) -> int:
        if type(value) is not int:
            raise self.fail()

        return value

    def object_id(self, value: object) -> ObjectId:
        try:
            return ObjectId(self.text(value))
        except ForgeValueError:
            raise self.fail() from None


# -- P1: the Release ----------------------------------------------------------


@dataclass(frozen=True)
class ReleaseFacts:
    """The three fields of a Release that P1 reads. P1 reads no author
    (FR2): the create reply and a later read name two authors (M2)."""

    tag_name: str
    draft: bool
    prerelease: bool


def read_release(component: str, reply: ForgeReply) -> ReleaseFacts:
    """`GET /repos/{o}/{r}/releases/tags/{tag}`. A 404 is no Release."""
    shape = _Shape(RefusalCode.P1, component)
    body = shape.obj(_value(RefusalCode.P1, component, reply, ForgeReason.NO_RELEASE))

    return ReleaseFacts(
        tag_name=shape.text(body.get("tag_name")),
        draft=shape.flag(body.get("draft")),
        prerelease=shape.flag(body.get("prerelease")),
    )


# -- P2: the tag ----------------------------------------------------------------


@dataclass(frozen=True)
class AnnotatedRef:
    """The one ref that is exactly the tag. It names an annotated tag."""

    tag_object: ObjectId


def read_tag_refs(component: str, reply: ForgeReply, want_tag: TagName) -> AnnotatedRef:
    """`GET /repos/{o}/{r}/git/refs/tags/{tag}`. The path is a prefix match
    and the reply is a list, also for one tag (M3). Only an entry whose
    `ref` is exactly `refs/tags/{want_tag}` counts. It must be one entry,
    and it must name an annotated tag. A 404 is no tag."""
    # CONTRACT-QUESTION: forgejo-release-path.md section 3 M3 and M8 measure
    # the prefix match, not the page size of the list. A prefix such as
    # `handover-v0.1.9` also matches `handover-v0.1.90` to `-v0.1.99`. The
    # reading here reads one page. When a page drops the exact entry, P2
    # refuses with NO_EXACT_REF (fail closed). A change costs a page loop.
    shape = _Shape(RefusalCode.P2, component)
    listed = shape.items(_value(RefusalCode.P2, component, reply, ForgeReason.NO_TAG))
    exact = f"{TAG_REF_PREFIX}{want_tag.text}"
    entries = [shape.obj(item) for item in listed]
    found = [one for one in entries if shape.text(one.get("ref")) == exact]
    if not found:
        raise _refuse(RefusalCode.P2, component, ForgeReason.NO_EXACT_REF)

    if len(found) > 1:
        raise _refuse(RefusalCode.P2, component, ForgeReason.TWO_EXACT_REFS)

    target = shape.obj(found[0].get("object"))
    kind = shape.text(target.get("type"))
    if kind == TYPE_COMMIT:
        raise _refuse(RefusalCode.P2, component, ForgeReason.LIGHTWEIGHT_TAG)

    if kind != TYPE_TAG:
        raise shape.fail()

    return AnnotatedRef(shape.object_id(target.get("sha")))


@dataclass(frozen=True)
class TagObject:
    """An annotated tag object: the commit it names and its message, with
    the LF that this reply adds (M3)."""

    commit: ObjectId
    message: str


def read_tag_object(
    component: str, reply: ForgeReply, want_tag: TagName, tag_object: ObjectId
) -> TagObject:
    """`GET /repos/{o}/{r}/git/tags/{sha}`. The reply must name the tag
    object of the ref and the tag, and that object must name a commit."""
    # CONTRACT-QUESTION: forgejo-release-path.md section 5, P2, names only
    # `object.sha` and `message` of this reply. The reading here also
    # requires `sha` equal to the object of the ref and `tag` equal to the
    # name. Both fields are recorded. A change drops two comparisons.
    shape = _Shape(RefusalCode.P2, component)
    body = shape.obj(_value(RefusalCode.P2, component, reply, None))
    if shape.text(body.get("sha")) != tag_object.text:
        raise _refuse(RefusalCode.P2, component, ForgeReason.OTHER_TAG_OBJECT)

    if shape.text(body.get("tag")) != want_tag.text:
        raise _refuse(RefusalCode.P2, component, ForgeReason.OTHER_TAG)

    target = shape.obj(body.get("object"))
    if shape.text(target.get("type")) != TYPE_COMMIT:
        raise _refuse(RefusalCode.P2, component, ForgeReason.NOT_A_COMMIT)

    return TagObject(
        commit=shape.object_id(target.get("sha")),
        message=shape.text(body.get("message")),
    )


# -- P3: on main ------------------------------------------------------------------


@dataclass(frozen=True)
class CompareFacts:
    """`total_commits` and the count of the `commits` list."""

    total_commits: int
    commits: int


def read_compare(component: str, reply: ForgeReply) -> CompareFacts:
    """`GET /repos/{o}/{r}/compare/main...{sha}`. A 404 is a commit that the
    repository does not hold (M4)."""
    shape = _Shape(RefusalCode.P3, component)
    body = shape.obj(_value(RefusalCode.P3, component, reply, ForgeReason.NOT_ON_MAIN))

    return CompareFacts(
        total_commits=shape.whole(body.get("total_commits")),
        commits=len(shape.items(body.get("commits"))),
    )


# -- P4: the merge ------------------------------------------------------------------


@dataclass(frozen=True)
class PullFacts:
    """The fields of the pull request of a commit that P4 reads."""

    number: int
    merged: bool
    base_ref: str
    merge_commit_sha: str | None
    head_sha: ObjectId


def read_commit_pull(component: str, reply: ForgeReply) -> PullFacts:
    """`GET /repos/{o}/{r}/commits/{sha}/pull`, one object. A 404 is a
    commit that is no merge of a pull request (M5)."""
    shape = _Shape(RefusalCode.P4, component)
    body = shape.obj(_value(RefusalCode.P4, component, reply, ForgeReason.NOT_A_MERGE))
    merge = body.get("merge_commit_sha")

    return PullFacts(
        number=shape.whole(body.get("number")),
        merged=shape.flag(body.get("merged")),
        base_ref=shape.text(shape.obj(body.get("base")).get("ref")),
        merge_commit_sha=None if merge is None else shape.text(merge),
        head_sha=shape.object_id(shape.obj(body.get("head")).get("sha")),
    )


# -- P5: the statuses -----------------------------------------------------------------


@dataclass(frozen=True)
class CommitStatuses:
    """`total_count`, and the context and the status of each listed entry,
    in the order of the reply."""

    total_count: int
    entries: tuple[tuple[str, str], ...]


def read_commit_status(component: str, reply: ForgeReply) -> CommitStatuses:
    """`GET /repos/{o}/{r}/commits/{head}/status`. Each entry names its
    state in `status` (M6). The reader never reads `state`, of the reply or
    of an entry: the combined `state` is not the rule of P5."""
    shape = _Shape(RefusalCode.P5, component)
    body = shape.obj(_value(RefusalCode.P5, component, reply, None))
    entries = tuple(
        (shape.text(one.get("context")), shape.text(one.get("status")))
        for one in (shape.obj(item) for item in shape.items(body.get("statuses")))
    )

    return CommitStatuses(total_count=shape.whole(body.get("total_count")), entries=entries)
