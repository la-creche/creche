"""The readers of `executor/forge_api.py` on the recorded replies of Forgejo
17.0.1 (`docs/design/forgejo-release-path.md`, section 3 M2 to M6 and
section 5). Each reply takes its status from `index.tsv`.

A test that needs a reply that no recording holds changes a recorded reply
in test code, and its docstring says what it changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.forge_api import (
    API_MAX_BYTES,
    SURFACE,
    ForgeReason,
    ForgeReply,
    ObjectId,
    read_commit_pull,
    read_commit_status,
    read_compare,
    read_release,
    read_tag_object,
    read_tag_refs,
)
from handover.executor.forge_provenance import (
    RequiredContexts,
    check_green,
    check_merge,
    check_on_main,
)
from handover.executor.release_statement import (
    StatementReason,
    decode_message,
    written_message,
)
from handover_frp_fixtures import (
    MERGE_COMMIT,
    PR_HEAD,
    TAG_OBJECT,
    changed,
    create_reply,
    recorded,
    reply,
)

from handover import notices

COMPONENT = "vmm"


def _refused(call: Any, *args: Any) -> Refusal:
    with pytest.raises(Refusal) as refused:
        call(*args)

    return refused.value


def _reason(refusal: Refusal) -> tuple[RefusalCode, str]:
    return refusal.code, refusal.detail


# -- P1 ---------------------------------------------------------------------------


def test_p1_reads_the_release_and_no_author() -> None:
    """The read-back names the operator as author, the create reply names
    the Actions user. Both give the same facts, so P1 reads no author
    (FR2, M2)."""
    read_back = read_release(COMPONENT, reply("p3-release-by-tag.json"))
    created = read_release(COMPONENT, ForgeReply(200, create_reply().body))

    assert read_back == created
    assert (read_back.tag_name, read_back.draft, read_back.prerelease) == ("rec-ann", False, False)
    assert recorded("p3-release-by-tag.json")["author"]["login"] == "operator"
    assert b'"login":"forgejo-actions"' in create_reply().body


def test_p1_refuses_a_missing_release_and_a_bad_shape() -> None:
    """A 404 is no Release. The second case changes `draft` to a text."""
    missing = ForgeReply(404, b'{"message":"Not Found"}')
    assert _reason(_refused(read_release, COMPONENT, missing)) == (
        RefusalCode.P1,
        ForgeReason.NO_RELEASE.value,
    )

    def text_draft(value: dict[str, Any]) -> None:
        value["draft"] = "false"

    bad = changed("p3-release-by-tag.json", text_draft)
    assert _refused(read_release, COMPONENT, bad).detail == ForgeReason.BAD_SHAPE.value


# -- P2 ---------------------------------------------------------------------------


def test_p2_takes_the_one_exact_ref() -> None:
    one = read_tag_refs(COMPONENT, reply("p2-git-refs-tag.json"), "rec-ann")
    assert one.tag_object == ObjectId(TAG_OBJECT)

    prefix = reply("p2-git-refs-prefix.json")
    assert read_tag_refs(COMPONENT, prefix, "rec-ann").tag_object == ObjectId(TAG_OBJECT)
    refused = _refused(read_tag_refs, COMPONENT, prefix, "rec-")
    assert _reason(refused) == (RefusalCode.P2, ForgeReason.NO_EXACT_REF.value)


def test_p2_refuses_a_lightweight_tag() -> None:
    measure = reply("p2-git-refs-prefix-measure.json")
    annotated = read_tag_refs(COMPONENT, measure, "measure-git-2")
    assert annotated.tag_object == ObjectId("67bf558bde4e906d9fad07f83665c25009f6c2ba")

    light = _refused(read_tag_refs, COMPONENT, measure, "measure-rel-2")
    assert light.detail == ForgeReason.LIGHTWEIGHT_TAG.value
    none = _refused(read_tag_refs, COMPONENT, measure, "measure")
    assert none.detail == ForgeReason.NO_EXACT_REF.value


def test_p2_refuses_a_missing_tag_and_two_exact_refs() -> None:
    """The second case is built in test code: the recorded list of one ref
    with that ref twice. No recording holds it."""
    missing = _refused(read_tag_refs, COMPONENT, reply("p2-git-refs-missing.json"), "no-such-tag")
    assert _reason(missing) == (RefusalCode.P2, ForgeReason.NO_TAG.value)

    def twice(value: list[Any]) -> None:
        value.append(value[0])

    doubled = changed("p2-git-refs-tag.json", twice)
    two = _refused(read_tag_refs, COMPONENT, doubled, "rec-ann")
    assert two.detail == ForgeReason.TWO_EXACT_REFS.value


def test_p2_reads_the_tag_object() -> None:
    found = read_tag_object(
        COMPONENT, reply("p2-git-tags-sha.json"), "rec-ann", ObjectId(TAG_OBJECT)
    )
    assert found.commit == ObjectId(MERGE_COMMIT)

    # The message of the tags API has no final LF. The tag object adds one.
    written = recorded("p2-tags-get.json")["message"]
    assert written_message(COMPONENT, found.message) == written

    other = _refused(
        read_tag_object,
        COMPONENT,
        reply("p2-git-tags-sha.json"),
        "rec-tag-push",
        ObjectId(TAG_OBJECT),
    )
    assert other.detail == ForgeReason.OTHER_TAG.value
    object_id = ObjectId("f47ab8c8187139e69d39d2906d3ac402258a2786")
    wrong = _refused(
        read_tag_object, COMPONENT, reply("p2-git-tags-sha.json"), "rec-ann", object_id
    )
    assert wrong.detail == ForgeReason.OTHER_TAG_OBJECT.value


def test_p2_refuses_a_tag_of_a_tag() -> None:
    """The recorded tag object with `object.type` changed to `tag`."""

    def nested(value: dict[str, Any]) -> None:
        value["object"]["type"] = "tag"

    bad = changed("p2-git-tags-sha.json", nested)
    refused = _refused(read_tag_object, COMPONENT, bad, "rec-ann", ObjectId(TAG_OBJECT))
    assert refused.detail == ForgeReason.NOT_A_COMMIT.value


def test_p1_reads_the_message_of_the_tag_object_only() -> None:
    """The recorded probe message has two lines, so it is no statement. The
    message of the tags API has no final LF, so root refuses it as a tag
    object message."""
    git_message = recorded("p2-git-tags-sha.json")["message"]
    lines = _refused(decode_message, COMPONENT, git_message)
    assert _reason(lines) == (RefusalCode.P1, StatementReason.LINES.value)

    tags_message = recorded("p2-tags-get.json")["message"]
    suffix = _refused(decode_message, COMPONENT, tags_message)
    assert suffix.detail == StatementReason.SUFFIX.value


# -- P3 ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["p5-compare-ancestor.json", "p5-compare-main-itself.json"])
def test_p3_passes_an_ancestor_and_main(name: str) -> None:
    check_on_main(COMPONENT, read_compare(COMPONENT, reply(name)))


@pytest.mark.parametrize("name", ["p5-compare-ahead.json", "p5-compare-diverged.json"])
def test_p3_refuses_a_commit_off_main(name: str) -> None:
    facts = read_compare(COMPONENT, reply(name))
    refused = _refused(check_on_main, COMPONENT, facts)
    assert _reason(refused) == (RefusalCode.P3, ForgeReason.NOT_ON_MAIN.value)


def test_p3_refuses_a_missing_commit() -> None:
    refused = _refused(read_compare, COMPONENT, reply("p5-compare-missing.json"))
    assert _reason(refused) == (RefusalCode.P3, ForgeReason.NOT_ON_MAIN.value)


# -- P4 ---------------------------------------------------------------------------


def test_p4_passes_the_merge_commit() -> None:
    pull = read_commit_pull(COMPONENT, reply("p1-commit-pull-merge-sha.json"))
    check_merge(COMPONENT, pull, ObjectId(MERGE_COMMIT))
    assert (pull.number, pull.head_sha) == (2, ObjectId(PR_HEAD))


def test_p4_refuses_the_head_of_the_pull_request() -> None:
    refused = _refused(read_commit_pull, COMPONENT, reply("p1-commit-pull-pr-head.json"))
    assert _reason(refused) == (RefusalCode.P4, ForgeReason.NOT_A_MERGE.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("merged", False), ("base", {"ref": "other"}), ("merge_commit_sha", PR_HEAD)],
)
def test_p4_refuses_each_changed_field(field: str, value: object) -> None:
    """The recorded merge reply with one field changed in test code."""

    def change(body: dict[str, Any]) -> None:
        body[field] = value

    pull = read_commit_pull(COMPONENT, changed("p1-commit-pull-merge-sha.json", change))
    refused = _refused(check_merge, COMPONENT, pull, ObjectId(MERGE_COMMIT))
    assert refused.detail == ForgeReason.NOT_A_MERGE.value


# -- P5 ---------------------------------------------------------------------------

PROBE = "probe / facts (push)"


def test_p5_passes_the_required_context() -> None:
    statuses = read_commit_status(COMPONENT, reply("p4-commit-status-main.json"))
    check_green(COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))


def test_p5_refuses_a_missing_required_context() -> None:
    statuses = read_commit_status(COMPONENT, reply("p4-commit-status-main.json"))
    required = RequiredContexts(frozenset({"gate / gate (pull_request)"}))
    refused = _refused(check_green, COMPONENT, statuses, required)
    assert _reason(refused) == (RefusalCode.P5, ForgeReason.CONTEXT_MISSING.value)


def test_p5_refuses_a_failed_context() -> None:
    statuses = read_commit_status(COMPONENT, reply("p4-commit-status.json"))
    refused = _refused(check_green, COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))
    assert refused.detail == ForgeReason.CONTEXT_NOT_GREEN.value


def _without_failure(body: dict[str, Any]) -> None:
    body["statuses"] = [one for one in body["statuses"] if one["status"] != "failure"]
    body["total_count"] = len(body["statuses"])


def test_p5_takes_skipped_only_when_not_required() -> None:
    """The recorded reply with the failed entry dropped in test code."""
    statuses = read_commit_status(COMPONENT, changed("p4-commit-status.json", _without_failure))
    check_green(COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))

    skipped = RequiredContexts(frozenset({"status / skipped (push)"}))
    refused = _refused(check_green, COMPONENT, statuses, skipped)
    assert refused.detail == ForgeReason.CONTEXT_NOT_GREEN.value


def test_p5_refuses_a_count_past_the_list() -> None:
    """The recorded reply with `total_count` raised by one in test code."""

    def more(body: dict[str, Any]) -> None:
        body["total_count"] += 1

    statuses = read_commit_status(COMPONENT, changed("p4-commit-status-main.json", more))
    refused = _refused(check_green, COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))
    assert refused.detail == ForgeReason.STATUS_PAGE.value


def test_p5_reads_no_state() -> None:
    """The recorded reply with the top-level `state` changed to `success`
    and each entry's `state` too. The failed entry still refuses."""

    def green_state(body: dict[str, Any]) -> None:
        body["state"] = "success"
        for one in body["statuses"]:
            one["state"] = "success"

    statuses = read_commit_status(COMPONENT, changed("p4-commit-status.json", green_state))
    refused = _refused(check_green, COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))
    assert refused.detail == ForgeReason.CONTEXT_NOT_GREEN.value


@pytest.mark.parametrize(
    ("status", "reason"),
    [("pending", ForgeReason.CONTEXT_NOT_GREEN), ("queued", ForgeReason.UNKNOWN_STATUS)],
)
def test_p5_refuses_each_other_status(status: str, reason: ForgeReason) -> None:
    """The recorded green reply with the status of one context changed."""

    def change(body: dict[str, Any]) -> None:
        body["statuses"][1]["status"] = status

    statuses = read_commit_status(COMPONENT, changed("p4-commit-status-main.json", change))
    refused = _refused(check_green, COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))
    assert refused.detail == reason.value


def test_p5_refuses_a_context_twice() -> None:
    """The recorded green reply with its first entry twice."""

    def twice(body: dict[str, Any]) -> None:
        body["statuses"].append(body["statuses"][0])
        body["total_count"] += 1

    statuses = read_commit_status(COMPONENT, changed("p4-commit-status-main.json", twice))
    refused = _refused(check_green, COMPONENT, statuses, RequiredContexts(frozenset({PROBE})))
    assert refused.detail == ForgeReason.CONTEXT_TWICE.value


# -- strict JSON ------------------------------------------------------------------


def test_a_reply_that_is_not_strict_records_a_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded body with a key twice is no strict JSON text. The reader
    refuses it as no JSON and records one notice."""
    seen: list[tuple[str, str]] = []

    def record(surface: str, rule: object, *_: object, **__: object) -> None:
        seen.append((surface, str(rule)))

    monkeypatch.setattr(notices, "record", record)
    body = reply("p3-release-by-tag.json").body.replace(b'{"id":1013,', b'{"id":1013,"id":1,', 1)
    refused = _refused(read_release, COMPONENT, ForgeReply(200, body))

    assert _reason(refused) == (RefusalCode.P1, ForgeReason.NOT_JSON.value)
    assert seen == [(SURFACE, "duplicate_key")]


def test_a_reply_past_the_cap_refuses() -> None:
    body = reply("p5-compare-ancestor.json").body + b" " * API_MAX_BYTES
    refused = _refused(read_compare, COMPONENT, ForgeReply(200, body))
    assert refused.detail == ForgeReason.TOO_LARGE.value


def test_a_status_that_is_not_measured_refuses() -> None:
    refused = _refused(read_commit_status, COMPONENT, ForgeReply(500, b"{}"))
    assert _reason(refused) == (RefusalCode.P5, ForgeReason.HTTP_STATUS.value)
