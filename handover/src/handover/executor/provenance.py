"""Step 3, the provenance predicate (`stage7-releases.md` §2.4).

Root deploys a component at a tag only when GitHub itself says, right now,
that the tag is a Release CI cut, on the SHA the resolver computed, on
`main`, as the merge of a pull request whose CI runs were green.

| Check | Question |
|---|---|
| P1 | The Release exists, is not a draft or prerelease, author `github-actions[bot]`. |
| P2 | The tag's commit equals the resolved SHA. |
| P3 | The SHA is reachable from `main`. |
| P4 | The SHA is a pull-request merge commit. |
| P5 | That pull request's head has CI runs, all completed and successful. |

Plus: no component goes backwards unless `kind: rollback` (§2.4 step 3).

They read the tag shape `<component>-vX.Y.Z` (contract 06 §2) and were
measured on the host on 2026-09-17. Two facts from that run are easy to
miss:

1. P5 asks the **Actions** API, not `commits/{sha}/check-runs`. A
   fine-grained PAT cannot hold a "Checks" permission at all, so on a
   private repo that endpoint is closed to the only kind of token root may
   carry. Every check in these repos is an Actions workflow run, and
   "Actions: read" is grantable.
2. `[` is not a character a user login may hold, so `github-actions[bot]`
   is a name only CI can present. A PAT can push a tag. It cannot be the
   bot.

**The predicate fails closed.** A transport error or an undocumented answer
is a refusal, never a pass. There is no pin check, because no pin is
written by hand (contract 06 §3.3).

Nothing here reads the spool, the ledger or a checkout. The API reader is
injected, so every test answers it from a fixture.
"""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from ..errors import Refusal, RefusalCode
from ..site import github_owner, github_repo
from .source import newest_tagged_version

#: What `git/matching-refs/tags/<prefix>` answers with in front of a name.
TAG_REF_PREFIX: Final = "refs/tags/"
API_HOST: Final = "api.github.com"
API_VERSION: Final = "2022-11-28"
API_TIMEOUT_S: Final = 20.0
API_MAX_BYTES: Final = 2 << 20
USER_AGENT: Final = "handover"

MAIN: Final = "main"

#: Only CI can be this author.
BOT_LOGIN: Final = "github-actions[bot]"
BOT_TYPE: Final = "Bot"

ON_MAIN: Final = ("identical", "behind")
RUN_COMPLETED: Final = "completed"
RUN_SUCCESS: Final = "success"

#: One page. A head SHA with more runs than this is not one root can vouch
#: for, so it refuses rather than reading a second page it cannot bound.
RUNS_MAX: Final = 100

HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404

SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
VERSION_RE: Final = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


class Accept(StrEnum):
    JSON = "application/vnd.github+json"


@dataclass(frozen=True)
class ApiReply:
    status: int
    body: object


ApiGetFn = Callable[[str, Accept], ApiReply]


@dataclass(frozen=True)
class Verified:
    """What the checks learned on the way, for the ledger."""

    component: str
    pr: int
    head_sha: str


def _refuse(code: RefusalCode, component: str, detail: str) -> Refusal:
    return Refusal(code, component, detail)


def github_api(token: str) -> ApiGetFn:
    """The real reader. The token rides in a header, never in argv or a URL
    (invariant 13)."""

    def get(path: str, accept: Accept) -> ApiReply:
        conn = http.client.HTTPSConnection(API_HOST, timeout=API_TIMEOUT_S)
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": accept.value,
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": USER_AGENT,
                },
            )
            response = conn.getresponse()
            raw = response.read(API_MAX_BYTES + 1)
        finally:
            conn.close()

        if len(raw) > API_MAX_BYTES:
            raise ValueError("answer larger than API_MAX_BYTES")

        text = raw.decode("utf-8")
        if response.status != HTTP_OK:
            return ApiReply(response.status, text)

        return ApiReply(response.status, json.loads(text))

    return get


def _get(api: ApiGetFn, path: str, code: RefusalCode, component: str) -> ApiReply:
    try:
        return api(path, Accept.JSON)
    except Exception as exc:
        detail = f"api.github.com unreachable: {type(exc).__name__}"
        raise _refuse(code, component, detail) from None


def _ok(reply: ApiReply, code: RefusalCode, component: str, what: str) -> object:
    if reply.status != HTTP_OK:
        raise _refuse(code, component, f"{what}: HTTP {reply.status}")

    return reply.body


def _obj(value: object, code: RefusalCode, component: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse(code, component, f"{what}: not the documented shape")

    return cast("dict[str, Any]", value)


def _items(value: object, code: RefusalCode, component: str, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise _refuse(code, component, f"{what}: not the documented shape")

    return cast("list[Any]", value)


def _repo_path(repo: str) -> str:
    """`/repos/<owner>/<repo>`. The owner is the site's (`site.py`): this
    code names nobody's account. So is the repository's name on GitHub,
    which need not be the catalog's name for it."""
    return f"/repos/{github_owner()}/{github_repo(repo)}"


def check_release(api: ApiGetFn, component: str, repo: str, tag: str) -> None:
    """P1: a published Release, authored by CI."""
    reply = _get(api, f"{_repo_path(repo)}/releases/tags/{tag}", RefusalCode.P1, component)
    if reply.status == HTTP_NOT_FOUND:
        raise _refuse(RefusalCode.P1, component, f"{repo} has no Release for {tag}")

    body = _obj(_ok(reply, RefusalCode.P1, component, "release"), RefusalCode.P1, component, "rel")
    if body.get("tag_name") != tag:
        raise _refuse(RefusalCode.P1, component, "the Release names another tag")

    if body.get("draft") is not False or body.get("prerelease") is not False:
        raise _refuse(RefusalCode.P1, component, f"{tag} is a draft or a prerelease")

    author = _obj(body.get("author"), RefusalCode.P1, component, "release author")
    if author.get("login") != BOT_LOGIN or author.get("type") != BOT_TYPE:
        raise _refuse(RefusalCode.P1, component, f"{tag} was not released by {BOT_LOGIN}")


def tag_commit(api: ApiGetFn, component: str, repo: str, tag: str) -> str:
    """P2's first half: the commit the tag names on GitHub NOW. CI cuts
    lightweight tags; an annotated one is dereferenced once."""
    reply = _get(api, f"{_repo_path(repo)}/git/ref/tags/{tag}", RefusalCode.P2, component)
    if reply.status == HTTP_NOT_FOUND:
        raise _refuse(RefusalCode.P2, component, f"{repo} has no tag {tag}")

    ref = _obj(_ok(reply, RefusalCode.P2, component, "tag ref"), RefusalCode.P2, component, "ref")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise _refuse(RefusalCode.P2, component, "the ref answer names another tag")

    target = _obj(ref.get("object"), RefusalCode.P2, component, "tag ref")
    if target.get("type") == "tag":
        path = f"{_repo_path(repo)}/git/tags/{target.get('sha')}"
        reply = _get(api, path, RefusalCode.P2, component)
        body = _ok(reply, RefusalCode.P2, component, "tag object")
        target = _obj(
            _obj(body, RefusalCode.P2, component, "tag object").get("object"),
            RefusalCode.P2,
            component,
            "tag object",
        )

    sha = target.get("sha")
    if target.get("type") != "commit" or not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise _refuse(RefusalCode.P2, component, f"{tag} does not name a commit")

    return sha


def check_tag_sha(api: ApiGetFn, component: str, repo: str, tag: str, sha: str) -> None:
    """P2: the tag's commit is the resolved SHA — a moved tag refuses."""
    actual = tag_commit(api, component, repo, tag)
    if actual != sha:
        raise _refuse(RefusalCode.P2, component, f"{tag} is another commit than the manifest's")


def check_on_main(api: ApiGetFn, component: str, repo: str, sha: str) -> None:
    """P3: reachable from `main`."""
    path = f"{_repo_path(repo)}/compare/{MAIN}...{sha}"
    reply = _get(api, path, RefusalCode.P3, component)
    body = _ok(reply, RefusalCode.P3, component, "compare")
    compare = _obj(body, RefusalCode.P3, component, "compare")
    if compare.get("status") not in ON_MAIN:
        raise _refuse(RefusalCode.P3, component, f"the SHA is not reachable from {MAIN}")


def _is_merge_of(pull: dict[str, Any], sha: str) -> bool:
    base = pull.get("base")
    base_ref = cast("dict[str, Any]", base).get("ref") if isinstance(base, dict) else None

    return (
        isinstance(pull.get("merged_at"), str)
        and base_ref == MAIN
        and pull.get("merge_commit_sha") == sha
    )


def merged_pr(api: ApiGetFn, component: str, repo: str, sha: str) -> tuple[int, str]:
    """P4: the SHA IS the merge of a pull request into `main` — not merely a
    commit some pull request contains."""
    path = f"{_repo_path(repo)}/commits/{sha}/pulls"
    reply = _get(api, path, RefusalCode.P4, component)
    listed = _items(_ok(reply, RefusalCode.P4, component, "pulls"), RefusalCode.P4, component, "p")
    pulls = [_obj(one, RefusalCode.P4, component, "pull") for one in listed]
    merges = [one for one in pulls if _is_merge_of(one, sha)]
    if not merges:
        raise _refuse(RefusalCode.P4, component, f"the SHA is not the merge of a PR into {MAIN}")

    pull = merges[0]
    number = pull.get("number")
    head = _obj(pull.get("head"), RefusalCode.P4, component, "pull head").get("sha")
    if isinstance(number, bool) or not isinstance(number, int):
        raise _refuse(RefusalCode.P4, component, "pull: not the documented shape")

    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        raise _refuse(RefusalCode.P4, component, "pull head: not the documented shape")

    return number, head


def check_runs_green(api: ApiGetFn, component: str, repo: str, head_sha: str) -> None:
    """P5: the pull request's head has CI runs, and every one succeeded."""
    path = f"{_repo_path(repo)}/actions/runs?head_sha={head_sha}&per_page={RUNS_MAX}"
    reply = _get(api, path, RefusalCode.P5, component)
    body = _ok(reply, RefusalCode.P5, component, "workflow runs")
    answer = _obj(body, RefusalCode.P5, component, "runs")
    listed = _items(answer.get("workflow_runs"), RefusalCode.P5, component, "runs")
    runs = [_obj(one, RefusalCode.P5, component, "run") for one in listed]
    total = answer.get("total_count")

    if not runs:
        raise _refuse(RefusalCode.P5, component, "the PR head has no CI runs")

    if total != len(runs):
        raise _refuse(RefusalCode.P5, component, f"{total} CI runs, {len(runs)} listed")

    red = [
        str(one.get("name"))
        for one in runs
        if one.get("head_sha") != head_sha
        or one.get("status") != RUN_COMPLETED
        or one.get("conclusion") != RUN_SUCCESS
    ]
    if red:
        raise _refuse(RefusalCode.P5, component, f"not success: {len(red)} run(s)")


def verify(api: ApiGetFn, component: str, repo: str, tag: str, sha: str) -> Verified:
    """P1 to P5 for one component; raises at the first failing check."""
    check_release(api, component, repo, tag)
    check_tag_sha(api, component, repo, tag, sha)
    check_on_main(api, component, repo, sha)
    number, head_sha = merged_pr(api, component, repo, sha)
    check_runs_green(api, component, repo, head_sha)

    return Verified(component=component, pr=number, head_sha=head_sha)


#: One page of matching refs. GitHub sorts them alphabetically, so a FULL
#: page means the newest may be on a page root did not read. It refuses
#: there rather than naming the highest of a prefix it cannot bound.
REFS_MAX: Final = 100

#: What `latest` costs to answer, and why the refusal below is `state` and
#: not a `P` code: this read builds contract 06 §11's document at step 2.
#: The predicate itself runs at step 3 over whatever it names.
TOO_MANY_TAGS: Final = "more tags than one page: name the version instead of 'latest'"


def newest_version(api: ApiGetFn, component: str, repo: str) -> str | None:
    """The highest version among `<component>-v*` tags, or None for none.

    Root reads tags from the same authority it reads `facts.sha` from. The
    corpus clone is the other candidate and `executor/live_state.py` carries
    the argument: a tag planted in an operator-owned clone would otherwise name
    a version GitHub has no Release for, and every `request <name>` with no
    version would refuse at step 2 for ever.
    """
    prefix = f"{component}-v"
    path = f"{_repo_path(repo)}/git/matching-refs/tags/{prefix}?per_page={REFS_MAX}"
    reply = _get(api, path, RefusalCode.STATE, component)
    if reply.status == HTTP_NOT_FOUND:
        return None

    body = _ok(reply, RefusalCode.STATE, component, "matching refs")
    listed = _items(body, RefusalCode.STATE, component, "refs")
    if len(listed) >= REFS_MAX:
        raise _refuse(RefusalCode.STATE, component, TOO_MANY_TAGS)

    names = [_ref_name(one) for one in listed]

    return newest_tagged_version([one for one in names if one], component)


def _ref_name(value: object) -> str:
    """`refs/tags/<name>` to `<name>`. An answer that is not that shape is
    not a tag root can read, and it is dropped rather than refused: the
    caller's question is "which is newest", not "is every ref documented"."""
    if not isinstance(value, dict):
        return ""

    ref = cast("dict[str, Any]", value).get("ref")
    if not isinstance(ref, str) or not ref.startswith(TAG_REF_PREFIX):
        return ""

    return ref.removeprefix(TAG_REF_PREFIX)


def _version_of(value: str) -> tuple[int, int, int] | None:
    matched = VERSION_RE.fullmatch(value)
    if matched is None:
        return None

    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def check_monotonic(component: str, to_version: str, live: str | None) -> None:
    """A release never goes backwards; only a gated rollback may.

    Equal is fine: P2 already tied the tag to one commit, so an equal
    version is the same commit, redeployed. The comparison rests on what is
    INSTALLED, never on the ledger, which the group can write.
    """
    if live is None:
        return

    wanted, deployed = _version_of(to_version), _version_of(live)
    if wanted is None or deployed is None:
        detail = "cannot compare the target with the installed version"
        raise _refuse(RefusalCode.MONOTONIC, component, detail)

    if wanted < deployed:
        raise _refuse(RefusalCode.MONOTONIC, component, f"{to_version} is older than {live}")
