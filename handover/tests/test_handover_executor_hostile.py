"""Every §3.2 rule, proved by a hostile fixture (`stage7-releases.md` §3.2).

Root treats every byte under `requests/` as hostile. Contract 06 §11's
live-state document is not among them: root BUILDS it, so there is no file
for an operator-side writer to plant. What is left of that section here is the
one door a document still comes through —
`handover resolve --state <file>`, a fixture handed to the pure
resolver — and the property the stamps give root whatever a document says.

One test per rule, named for the rule. A test that changes two things at once
proves whichever check happens to run first, which is the failure mode
`handover/AGENTS.md` names for the fixtures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.live_state import Readers, build_state
from handover.executor.request import MAX_REQUEST_BYTES, parse_request, request_id_of
from handover.executor.spool import (
    REQUESTS_DIR,
    NotAFile,
    Spool,
    read_capped,
)
from handover.state import parse_state
from handover_executor_fixtures import (
    REQUEST_ID,
    fake_readers,
    live_state_body,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_live_state,
    write_request,
)


def _raw(body: dict[str, object]) -> bytes:
    return json.dumps(body).encode("utf-8")


# -- §2.3: the request carries intent, and nothing else --------------------


def test_a_request_with_an_unknown_key_is_refused() -> None:
    """Rule 2: the key set is CLOSED. An unknown key is a refusal, not a
    field root ignores — an ignored field is a field somebody adds later."""
    body = request_body({"chaperone": "2.1.0"}) | {"install_to": "/etc"}
    with pytest.raises(Refusal) as raised:
        parse_request(_raw(body), REQUEST_ID)

    assert raised.value.code is RefusalCode.REQUEST


def test_a_request_cannot_name_a_path_a_command_or_a_hash() -> None:
    """Rule 7: no such field exists, so each of these is the same refusal."""
    for smuggled in ("build", "sha", "manifest_sha256", "verify", "url"):
        body = request_body({"chaperone": "2.1.0"}) | {smuggled: "/bin/sh"}
        with pytest.raises(Refusal):
            parse_request(_raw(body), REQUEST_ID)


def test_a_request_body_naming_another_id_is_refused() -> None:
    """The file name is what the id must equal: root acts on one id, and a
    body that names a second one is a request it would ledger elsewhere."""
    body = request_body({"chaperone": "2.1.0"}, request_id="01K5J8M2Q7V3X9R4T6N0B8C2DF")
    with pytest.raises(Refusal):
        parse_request(_raw(body), REQUEST_ID)


def test_a_request_under_an_id_that_is_no_ulid_is_refused() -> None:
    """Each field of a request has its pattern, and the id is a field. A
    caller that gives the parser another id gets the refusal."""
    body = request_body({"chaperone": "2.1.0"}, request_id="not-a-ulid")
    with pytest.raises(Refusal) as raised:
        parse_request(_raw(body), "not-a-ulid")

    assert raised.value.detail == "field 'id' is malformed"


def test_a_version_with_a_trailing_newline_is_refused() -> None:
    """Rule 3: every pattern ends in `\\Z`. A `$` also matches before a
    trailing newline, and these values become git tags and argv words."""
    with pytest.raises(Refusal):
        parse_request(_raw(request_body({"chaperone": "2.1.0\n"})), REQUEST_ID)


def test_a_component_name_with_a_traversal_is_refused() -> None:
    with pytest.raises(Refusal):
        parse_request(_raw(request_body({"../../etc/chaperone": "2.1.0"})), REQUEST_ID)


def test_a_request_naming_more_than_eight_components_is_refused() -> None:
    """§2.3: at most 8 entries, the catalog minus `registry-data`."""
    many = {f"c{index}": "1.0.0" for index in range(9)}
    with pytest.raises(Refusal):
        parse_request(_raw(request_body(many)), REQUEST_ID)


def test_a_request_larger_than_the_cap_is_refused_before_the_parse() -> None:
    """Rule 2: the cap is applied to the BYTES, never after a parse that
    already allocated whatever the file asked for."""
    with pytest.raises(Refusal) as raised:
        parse_request(b"{" + b" " * MAX_REQUEST_BYTES, REQUEST_ID)

    assert "larger than" in raised.value.detail


def test_a_lower_case_ulid_file_name_is_not_a_request() -> None:
    """Contract 06 §9 fixes upper-case Crockford base32 for every ULID."""
    assert request_id_of(f"{REQUEST_ID.lower()}.json") is None
    assert request_id_of(f"{REQUEST_ID}.json") == REQUEST_ID


def test_a_name_that_is_not_json_is_not_a_request() -> None:
    """A writer's in-flight `.<id>.tmp` neither matches the path unit's glob
    nor is picked up here."""
    assert request_id_of(f".{REQUEST_ID}.json.tmp") is None
    assert request_id_of(f"{REQUEST_ID}.txt") is None


def test_a_rollback_without_a_target_is_refused() -> None:
    body = request_body({"chaperone": "2.1.0"}, kind="rollback")
    with pytest.raises(Refusal):
        parse_request(_raw(body), REQUEST_ID)


def test_a_release_carrying_a_rollback_target_is_refused() -> None:
    body = request_body({"chaperone": "2.1.0"}) | {"rollback_of": REQUEST_ID}
    with pytest.raises(Refusal):
        parse_request(_raw(body), REQUEST_ID)


# -- §3.2 rules 1, 2 and 5: what the spool will not read -------------------


def test_a_symlink_in_requests_is_never_followed(tmp_path: Path) -> None:
    """Rule 5. `O_NOFOLLOW` refuses it, so root never reads the target —
    which is how a request could otherwise name `/etc/shadow`."""
    root = make_spool_dirs(tmp_path)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(request_body({"chaperone": "2.1.0"})), encoding="utf-8")
    link = root / REQUESTS_DIR / f"{REQUEST_ID}.json"
    link.symlink_to(secret)

    spool = Spool(str(root), this_uid())
    try:
        with pytest.raises(NotAFile):
            spool.read_request(link.name, REQUEST_ID)
    finally:
        spool.close()


def test_a_fifo_in_requests_does_not_hang_root(tmp_path: Path) -> None:
    """Rule 5. `O_NONBLOCK` is why root does not sit on a FIFO forever."""
    root = make_spool_dirs(tmp_path)
    os.mkfifo(root / REQUESTS_DIR / f"{REQUEST_ID}.json")

    spool = Spool(str(root), this_uid())
    try:
        with pytest.raises(NotAFile):
            spool.read_request(f"{REQUEST_ID}.json", REQUEST_ID)
    finally:
        spool.close()


def test_a_directory_named_like_a_request_is_refused(tmp_path: Path) -> None:
    root = make_spool_dirs(tmp_path)
    (root / REQUESTS_DIR / f"{REQUEST_ID}.json").mkdir()

    spool = Spool(str(root), this_uid())
    try:
        with pytest.raises(NotAFile):
            spool.read_request(f"{REQUEST_ID}.json", REQUEST_ID)
    finally:
        spool.close()


def test_a_file_owned_by_another_uid_is_refused(tmp_path: Path) -> None:
    """Rule 2: owned by the operator. A test cannot chown, so it asks for an owner
    the file cannot have — the check is the same one either way."""
    root = make_spool_dirs(tmp_path)
    write_request(root, REQUEST_ID, request_body({"chaperone": "2.1.0"}))

    spool = Spool(str(root), this_uid() + 1)
    try:
        with pytest.raises(NotAFile):
            spool.read_request(f"{REQUEST_ID}.json", REQUEST_ID)
    finally:
        spool.close()


def test_a_file_past_the_cap_is_refused_by_the_reader(tmp_path: Path) -> None:
    root = make_spool_dirs(tmp_path)
    (root / REQUESTS_DIR / f"{REQUEST_ID}.json").write_bytes(b"x" * (MAX_REQUEST_BYTES + 1))

    spool = Spool(str(root), this_uid())
    try:
        with pytest.raises(Refusal) as raised:
            spool.read_request(f"{REQUEST_ID}.json", REQUEST_ID)
    finally:
        spool.close()

    assert raised.value.code is RefusalCode.REQUEST


def test_read_capped_stops_at_the_cap(tmp_path: Path) -> None:
    """The size check is re-made after the read: a file that GREW between
    the `fstat` and the last `read` is still refused."""
    (tmp_path / "grew").write_bytes(b"y" * 100)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert read_capped(fd, "grew", 100, None) == b"y" * 100
        with pytest.raises(Refusal):
            read_capped(fd, "grew", 99, None)
    finally:
        os.close(fd)


# -- §3.2 rule 4: root never copies a requester's bytes --------------------


def test_running_is_reserialized_from_the_validated_fields(tmp_path: Path) -> None:
    """Rule 4. The file root writes is built from the dataclass, so a value
    that passed no pattern cannot reach `running/` or the ledger."""
    root = make_spool_dirs(tmp_path)
    write_request(root, REQUEST_ID, request_body({"chaperone": "2.1.0"}))

    spool = Spool(str(root), this_uid())
    try:
        request = spool.read_request(f"{REQUEST_ID}.json", REQUEST_ID)
        spool.start(request)
    finally:
        spool.close()

    written = json.loads((root / "running" / f"{REQUEST_ID}.json").read_text(encoding="utf-8"))
    assert set(written) == {
        "id",
        "kind",
        "components",
        "rollback_of",
        "requested_by",
        "requester_session",
        "ts",
    }
    assert written["components"] == {"chaperone": "2.1.0"}
    assert not (root / REQUESTS_DIR / f"{REQUEST_ID}.json").exists()


def test_a_quarantined_name_keeps_nothing_hostile(tmp_path: Path) -> None:
    """Rule 5: renamed WHOLE into `rejected/`, never traversed, and the new
    name is root's. A name with a slash or a newline is dropped entirely."""
    root = make_spool_dirs(tmp_path)
    (root / REQUESTS_DIR / "a b\n.json").write_bytes(b"{}")

    spool = Spool(str(root), this_uid())
    try:
        landed = spool.quarantine("a b\n.json", 1758153600)
    finally:
        spool.close()

    assert "unsafe-name" in landed
    assert (root / "rejected" / landed).is_file()
    assert not list((root / REQUESTS_DIR).iterdir())


# -- contract 06 §11: root builds it, so there is nothing to plant ---------


def test_a_planted_live_state_file_is_never_read(tmp_path: Path) -> None:
    """The planted document, from the other end.

    An operator-side writer plants a document saying `chaperone` is at 9.9.9 and
    that `latest` means 9.9.9. Root builds its own from the
    install trees and never opens the file, so nothing it says reaches the
    resolution.
    """
    roots = (tmp_path / "components",)
    stamp_tree(roots[0], "chaperone", "2.0.3")
    planted = tmp_path / "releases"
    planted.mkdir()
    write_live_state(planted, live_state_body({"chaperone": "9.9.9"}, {"chaperone": "9.9.9"}))

    built = build_state(roots, {"chaperone": "latest"}, fake_readers(latest={"chaperone": "2.1.0"}))

    assert built.state.live["chaperone"] == "2.0.3"
    assert built.state.latest == {"chaperone": "2.1.0"}


def test_the_install_stamp_is_the_only_source_of_live(tmp_path: Path) -> None:
    """The stamp on the install tree is the authority, and now it is the
    ONLY thing `live` is built from. The monotonic rule of §2.4 step 3
    therefore rests on what is installed and on nothing a writer claims."""
    roots = (tmp_path / "components",)
    stamp_tree(roots[0], "chaperone", "2.0.3")

    built = build_state(roots, {"chaperone": "2.1.0"}, fake_readers())

    assert built.state.live["chaperone"] == "2.0.3"
    assert built.state.live["attendance"] is None


def test_a_reader_that_answers_nothing_builds_an_empty_world(tmp_path: Path) -> None:
    """`Readers()` is the fail-closed default: no tag, no commit, no digest.

    A builder nobody wired resolves `latest` to nothing and carries no
    facts, so `resolve` refuses the request instead of inventing a version.
    """
    built = build_state((tmp_path / "components",), {"chaperone": "latest"}, Readers())

    assert built.state.latest == {}
    assert built.state.facts == {}
    assert built.notes == ("no released tag for chaperone: 'latest' names nothing",)


def test_a_state_fixture_naming_an_unknown_component_is_refused() -> None:
    """Contract 06 §11 rule 2, on the one door a document still comes
    through: `--state <file>`. The component list is fixed by the
    contract, so a name it does not list is refused with the code
    `state`."""
    body = live_state_body({"chaperone": "2.0.3"}, {"chaperone": "2.1.0"})
    body["latest"] = {"not-a-component": "1.0.0"}

    with pytest.raises(Refusal) as raised:
        parse_state(json.dumps(body), "live-state.json")

    assert raised.value.code is RefusalCode.STATE
