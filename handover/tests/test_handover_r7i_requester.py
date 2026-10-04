"""The requester half of §2.1: somebody who can ask for a release.

`stage7-releases.md` §2.3 says what a request carries and §3.2 says what root
does to one it does not believe. Without a requester the operator hand-writes a
ULID-named JSON file and learns every §3.2 rule from a ledger entry after the
fact.

The rule these tests hold: **a requester refuses what the executor would
refuse, with the executor's own reason.** They share `parse_request`, so a new
rule in `executor/request.py` reaches the requester with no second edit.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.request import MAX_REQUEST_BYTES, Kind, Request, parse_request
from handover.executor.spool import (
    MAX_PENDING_PER_REQUESTER,
    REQUESTS_DIR,
    NotAFile,
    Spool,
)
from handover.requester import (
    REQUEST_FILE_MODE,
    RequesterError,
    file_request,
    new_ulid,
    plan_request,
)
from handover_executor_fixtures import make_spool_dirs

NOW = 1758153590.0

#: A uid this process is not. `Spool`'s first argument is the operator's, and these
#: tests run as neither the operator nor chaperone.
_ANOTHER_UID = os.getuid() + 1


def _requests(tmp_path: Path) -> Path:
    directory = tmp_path / REQUESTS_DIR
    directory.mkdir(parents=True)

    return directory


def _plan(
    components: dict[str, str],
    *,
    kind: str = "release",
    rollback_of: str | None = None,
    requested_by: str = "human",
    requester_session: str | None = None,
) -> Request:
    return plan_request(
        components,
        kind=kind,
        rollback_of=rollback_of,
        requested_by=requested_by,
        requester_session=requester_session,
        now=NOW,
    )


# -- the id ----------------------------------------------------------------


def test_the_minted_id_is_one_the_executor_accepts() -> None:
    """Contract 06 §9: 26 upper-case Crockford characters. The executor's own
    pattern is the judge, because the file name must match the body."""
    for step in range(64):
        minted = new_ulid(NOW + step)
        body = {
            "id": minted,
            "kind": "release",
            "components": {"chaperone": "2.1.0"},
            "rollback_of": None,
            "requested_by": "human",
            "requester_session": None,
            "ts": NOW,
        }
        assert parse_request(json.dumps(body).encode("utf-8"), minted).id == minted


def test_two_ids_minted_in_one_millisecond_differ() -> None:
    """80 bits of randomness, not a counter: two requests filed in one
    millisecond must not collide on a name `file_request` refuses to replace."""
    assert len({new_ulid(NOW) for _ in range(256)}) == 256


def test_the_id_sorts_by_time() -> None:
    """`Spool.pending` sorts by name, so a later request must sort later."""
    assert new_ulid(NOW) < new_ulid(NOW + 1.0)


@pytest.mark.parametrize(
    "given",
    [
        pytest.param("not-a-ulid", id="words"),
        pytest.param("01k5j8m2q7v3x9r4t6n0b8c2de", id="lower-case"),
        pytest.param("01K5J8M2Q7V3X9R4T6N0B8C2D", id="25-characters"),
        pytest.param("01K5J8M2Q7V3X9R4T6N0B8C2DE\n", id="a-final-line-feed"),
    ],
)
def test_a_given_id_that_is_no_ulid_is_refused(given: str) -> None:
    """The id is the name of the file that `file_request` writes, so it has
    the grammar of a ULID, as an id that the requester mints has."""
    with pytest.raises(Refusal) as raised:
        plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW, request_id=given)

    assert raised.value.code is RefusalCode.REQUEST
    assert raised.value.detail == "field 'id' is malformed"


def test_a_given_ulid_is_the_id_of_the_request() -> None:
    given = "01K5J8M2Q7V3X9R4T6N0B8C2DE"
    request = plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW, request_id=given)

    assert request.id == given


# -- what the requester refuses before the tap -----------------------------


def test_a_component_name_the_executor_refuses_is_refused_here() -> None:
    """§3.2 rule 2's pattern, reached through the executor's own parser."""
    with pytest.raises(Refusal) as raised:
        _plan({"chaperone; rm -rf /": "2.1.0"})

    assert raised.value.code is RefusalCode.REQUEST
    assert "components" in raised.value.detail


def test_a_version_that_is_not_a_version_is_refused() -> None:
    with pytest.raises(Refusal) as raised:
        _plan({"chaperone": "2.1"})

    assert raised.value.code is RefusalCode.REQUEST


def test_more_components_than_the_cap_are_refused() -> None:
    """§2.3: at most eight entries."""
    with pytest.raises(Refusal):
        _plan({f"comp{index}": "latest" for index in range(9)})


def test_a_rollback_with_no_target_is_refused() -> None:
    with pytest.raises(Refusal) as raised:
        _plan({"chaperone": "2.1.0"}, kind="rollback")

    assert "rollback_of" in raised.value.detail


def test_a_release_carrying_a_rollback_target_is_refused() -> None:
    with pytest.raises(Refusal):
        _plan({"chaperone": "2.1.0"}, rollback_of="01K5J8M2Q7V3X9R4T6N0B8C2DE")


def test_a_requested_by_that_is_not_a_name_is_refused() -> None:
    with pytest.raises(Refusal):
        _plan({"chaperone": "2.1.0"}, requested_by="Agent Control")


def test_a_session_id_with_a_newline_is_refused() -> None:
    """§3.2 rule 3: `fullmatch`, so a trailing newline is not a session id."""
    with pytest.raises(Refusal):
        _plan({"chaperone": "2.1.0"}, requester_session="tui-01K5J7Z9R0P2M4C6H8K1N3V5W7\n")


def test_the_planned_request_is_under_the_executor_s_byte_cap() -> None:
    """Eight components, the longest names the patterns allow, still under
    §3.2 rule 2's 4096 bytes — so a legal request never files a file root
    refuses on size."""
    longest = {f"{'c' * 29}{index:02d}": "latest" for index in range(8)}
    planned = plan_request(
        longest,
        requested_by="a" * 31,
        requester_session="s" * 128,
        now=NOW,
    )
    assert len(longest) == 8
    assert len(json.dumps(planned.as_dict()).encode("utf-8")) <= MAX_REQUEST_BYTES


# -- the file ---------------------------------------------------------------


def test_the_file_is_named_after_the_id_and_parses_back(tmp_path: Path) -> None:
    directory = _requests(tmp_path)
    planned = plan_request({"attendance": "latest"}, requested_by="human", now=NOW)
    path = file_request(planned, str(directory))

    assert Path(path).name == f"{planned.id}.json"
    raw = Path(path).read_bytes()
    assert parse_request(raw, planned.id).wanted() == {"attendance": "latest"}


def test_no_temporary_name_is_left_behind(tmp_path: Path) -> None:
    """§2.2: the path unit is a `PathExistsGlob`. A leftover name that ends
    in `.json` re-fires the unit; a leftover dot-name is litter root never
    drains, because it does not match the glob."""
    directory = _requests(tmp_path)
    file_request(
        plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW), str(directory)
    )

    assert [one.name for one in directory.iterdir() if one.name.startswith(".")] == []


def test_the_file_is_closed_to_group_and_other(tmp_path: Path) -> None:
    """Nothing but the writer and root reads a request."""
    directory = _requests(tmp_path)
    path = file_request(
        plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW), str(directory)
    )
    mode = stat.S_IMODE(os.stat(path).st_mode)

    assert mode == REQUEST_FILE_MODE
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_a_full_directory_is_refused_with_the_executor_s_rate_code(tmp_path: Path) -> None:
    """§3.2 rule 8. The executor ledgers `rate` after the fact; a requester
    that can see the directory says so first, so the operator never spends a tap on
    a request that was refused before step 1."""
    directory = _requests(tmp_path)
    for index in range(MAX_PENDING_PER_REQUESTER):
        (directory / f"{new_ulid(NOW + index)}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(Refusal) as raised:
        file_request(
            plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW), str(directory)
        )

    assert raised.value.code is RefusalCode.RATE


def test_a_requests_directory_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    """The requester opens its one directory `O_NOFOLLOW`, for the same
    reason root does: the path is under a directory other accounts reach."""
    directory = _requests(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(directory)

    with pytest.raises(RequesterError):
        file_request(plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW), str(link))


def test_a_missing_spool_is_an_error_and_not_a_refusal(tmp_path: Path) -> None:
    """A refusal is what root would say about the request. A missing spool is
    this host's fault, and a caller must be able to tell them apart."""
    with pytest.raises(RequesterError):
        file_request(
            plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW),
            str(tmp_path / "absent"),
        )


def test_a_planted_file_with_the_same_name_is_never_replaced(tmp_path: Path) -> None:
    """`link`, not `rename`: replacing a name that already exists would let a
    second writer's request vanish with nothing to say so."""
    directory = _requests(tmp_path)
    planned = plan_request({"chaperone": "2.1.0"}, requested_by="human", now=NOW)
    (directory / f"{planned.id}.json").write_text("planted", encoding="utf-8")

    with pytest.raises(RequesterError):
        file_request(planned, str(directory))

    assert (directory / f"{planned.id}.json").read_text(encoding="utf-8") == "planted"


# -- who root accepts a file from -------------------------------------------


def test_root_reads_a_file_from_either_requester(tmp_path: Path) -> None:
    """§3.1 names the operator and the `agent-control` family. The operator types
    `handover request`; the family's path is the PEP's `release`
    verb, and the PEP runs as `chaperone`, so the file it writes is chaperone-owned.

    A check of one uid would leave the verb's file unread and the second
    requester a path that cannot work.
    """
    root = make_spool_dirs(tmp_path)
    planned = plan_request({"chaperone": "2.1.0"}, requested_by="agent-control", now=NOW)
    file_request(planned, str(root / REQUESTS_DIR))

    spool = Spool(str(root), _ANOTHER_UID, also_owned_by=frozenset({os.getuid()}))
    try:
        assert spool.read_request(f"{planned.id}.json", planned.id).id == planned.id
    finally:
        spool.close()


def test_root_refuses_a_file_from_a_third_account(tmp_path: Path) -> None:
    """The set is root's own configuration, so a fourth account that can
    write into `requests/` still files nothing."""
    root = make_spool_dirs(tmp_path)
    planned = plan_request({"chaperone": "2.1.0"}, requested_by="agent-control", now=NOW)
    file_request(planned, str(root / REQUESTS_DIR))

    spool = Spool(str(root), _ANOTHER_UID, also_owned_by=frozenset({_ANOTHER_UID + 1}))
    try:
        with pytest.raises(NotAFile):
            spool.read_request(f"{planned.id}.json", planned.id)
    finally:
        spool.close()


def test_the_kind_survives_the_round_trip(tmp_path: Path) -> None:
    directory = _requests(tmp_path)
    target = "01K5J8M2Q7V3X9R4T6N0B8C2DE"
    planned = plan_request(
        {"chaperone": "2.1.0"}, requested_by="human", now=NOW, kind="rollback", rollback_of=target
    )
    path = file_request(planned, str(directory))
    back = parse_request(Path(path).read_bytes(), planned.id)

    assert back.kind is Kind.ROLLBACK
    assert back.rollback_of == target
