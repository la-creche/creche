"""The per-family grant file and its live re-read (contract 04 §1)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from chaperone.family_grants import (
    MAX_GRANT_FILE_BYTES,
    FamilyStore,
    parse_grants,
    token_digest,
)
from chaperone.faults import FaultWriter
from chaperone_family_helpers import FAMILY_TOKEN, OTHER_FAMILY_TOKEN, make_grants, write_grants
from chaperone_family_helpers import write_grants_raw as write_raw


def _store(tmp_path: Path) -> tuple[FamilyStore, Path, FaultWriter]:
    grants_dir = tmp_path / "grants"
    grants_dir.mkdir()
    faults = FaultWriter(tmp_path / "faults" / "pep")
    return FamilyStore(grants_dir, faults), grants_dir, faults


def _fault(tmp_path: Path, family: str) -> dict[str, object]:
    raw = (tmp_path / "faults" / "pep" / f"{family}.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _faults_of(tmp_path: Path, family: str) -> list[dict[str, object]]:
    entries = _fault(tmp_path, family)["faults"]
    assert isinstance(entries, list)
    return entries


def test_no_directory_resolves_nothing(tmp_path: Path) -> None:
    store = FamilyStore(None, FaultWriter(None))
    assert store.lookup(FAMILY_TOKEN) is None
    assert store.served_rev("chat") is None


def test_a_family_token_resolves(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())

    found = store.lookup(FAMILY_TOKEN)
    assert found is not None
    assert found.family == "chat"
    assert store.served_rev("chat") == "01K5J9QW3R7T0ZP4YB2H6N8M1D"


def test_the_current_grants_read_by_family_name(tmp_path: Path) -> None:
    """What a held approval gate re-reads (contract 04 §1.5.3). A family the
    PEP has never served reads as nothing, the same as a deleted file."""
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())

    current = store.current("chat")
    assert current is not None
    assert current.rev == "01K5J9QW3R7T0ZP4YB2H6N8M1D"
    assert store.current("vault-oracle") is None


def test_a_deleted_file_has_no_current_grants(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    path = write_grants(grants_dir, make_grants())
    assert store.current("chat") is not None

    path.unlink()
    assert store.current("chat") is None


def test_an_empty_or_unknown_token_resolves_to_nothing(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())

    assert store.lookup("") is None
    assert store.lookup("not-the-token") is None


def test_both_rotation_digests_resolve(tmp_path: Path) -> None:
    """Contract 04 §2.3: the overlap lets a call in flight finish."""
    store, grants_dir, _ = _store(tmp_path)
    write_grants(
        grants_dir,
        make_grants(token_sha256=[token_digest(FAMILY_TOKEN), token_digest(OTHER_FAMILY_TOKEN)]),
    )

    assert store.lookup(FAMILY_TOKEN) is not None
    assert store.lookup(OTHER_FAMILY_TOKEN) is not None


def test_a_rewrite_between_two_calls_applies(tmp_path: Path) -> None:
    """Invariant 9: an addition and a removal both land on the next call."""
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    first = store.lookup(FAMILY_TOKEN)
    assert first is not None
    assert first.tools == {"kagi": ("kagi_search_fetch",)}

    write_grants(
        grants_dir,
        make_grants(rev="rev-2", tools={"kagi": ["kagi_search_fetch", "kagi_extract"]}),
    )
    second = store.lookup(FAMILY_TOKEN)
    assert second is not None
    assert second.tools == {"kagi": ("kagi_search_fetch", "kagi_extract")}

    write_grants(grants_dir, make_grants(rev="rev-3", tools={}))
    third = store.lookup(FAMILY_TOKEN)
    assert third is not None
    assert third.tools == {}


def test_a_deleted_file_revokes_and_faults(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    path = write_grants(grants_dir, make_grants())
    assert store.lookup(FAMILY_TOKEN) is not None

    path.unlink()
    assert store.lookup(FAMILY_TOKEN) is None
    assert store.served_rev("chat") is None

    entry = _faults_of(tmp_path, "chat")[0]
    assert entry["code"] == "grants_stale"
    assert entry["blocks_turns"] is True
    # The last revision the PEP parsed, per §1.6 rule 2.
    assert entry["rev"] == "01K5J9QW3R7T0ZP4YB2H6N8M1D"


def test_a_malformed_file_fails_closed_and_faults(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_raw(grants_dir, "chat", "{not json")

    assert store.lookup(FAMILY_TOKEN) is None
    entry = _faults_of(tmp_path, "chat")[0]
    assert entry["code"] == "grants_stale"
    # Never parsed one, so the revision is null.
    assert entry["rev"] is None


def test_an_unknown_version_fails_closed(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    document = json.loads(json.dumps(make_grants().model_dump()))
    document["version"] = 3
    write_raw(grants_dir, "chat", json.dumps(document))

    assert store.lookup(FAMILY_TOKEN) is None
    entry = _faults_of(tmp_path, "chat")[0]
    message = entry["message"]
    assert isinstance(message, str)
    assert "unknown version 3" in message


def test_a_repaired_file_clears_the_fault(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_raw(grants_dir, "chat", "{not json")
    assert store.lookup(FAMILY_TOKEN) is None
    assert _faults_of(tmp_path, "chat") != []

    write_grants(grants_dir, make_grants())
    assert store.lookup(FAMILY_TOKEN) is not None
    assert _faults_of(tmp_path, "chat") == []


def test_a_stem_that_is_not_a_family_name_is_ignored(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_raw(grants_dir, "Not_A_Family", json.dumps(make_grants().model_dump()))

    assert store.lookup(FAMILY_TOKEN) is None
    assert not (tmp_path / "faults" / "pep" / "Not_A_Family.json").exists()


def test_an_oversized_file_fails_closed(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    document = json.loads(json.dumps(make_grants().model_dump()))
    document["model_alias"] = "a" * (MAX_GRANT_FILE_BYTES + 1)
    write_raw(grants_dir, "chat", json.dumps(document))

    assert store.lookup(FAMILY_TOKEN) is None
    message = _faults_of(tmp_path, "chat")[0]["message"]
    assert isinstance(message, str)
    assert "exceeds the cap" in message


def test_an_unreadable_file_fails_closed(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    path = write_grants(grants_dir, make_grants())
    path.chmod(0o000)
    try:
        assert store.lookup(FAMILY_TOKEN) is None
        message = _faults_of(tmp_path, "chat")[0]["message"]
        assert isinstance(message, str)
        assert "unreadable" in message
    finally:
        path.chmod(0o640)


def test_a_file_naming_another_family_is_refused(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    document = json.loads(json.dumps(make_grants().model_dump()))
    write_raw(grants_dir, "vault", json.dumps(document))

    assert store.lookup(FAMILY_TOKEN) is None
    message = _faults_of(tmp_path, "vault")[0]["message"]
    assert isinstance(message, str)
    assert "names family 'chat'" in message


def test_an_unreadable_directory_is_survivable(tmp_path: Path) -> None:
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    grants_dir.chmod(0o000)
    try:
        assert store.lookup(FAMILY_TOKEN) is None
    finally:
        grants_dir.chmod(0o750)


def test_a_directory_that_raises_on_listing_is_survivable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pathlib.Path.glob` swallows a permission error and yields nothing, so
    the handler is driven directly. It exists for the filesystems that do
    raise, where the alternative is an unhandled error inside `/call`."""
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())

    def refuse(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("EIO")

    monkeypatch.setattr(Path, "glob", refuse)
    assert store.lookup(FAMILY_TOKEN) is None


def test_a_dangling_link_is_absent_not_a_crash(tmp_path: Path) -> None:
    """The name matches the glob and the `stat` then fails. Contract 04 §1.4
    row 3: absent denies every call."""
    store, grants_dir, _ = _store(tmp_path)
    (grants_dir / "chat.json").symlink_to(grants_dir / "gone.json")

    assert store.lookup(FAMILY_TOKEN) is None
    message = _faults_of(tmp_path, "chat")[0]["message"]
    assert message == "grants/chat.json: absent"


def test_parse_rejects_shapes_that_are_not_objects() -> None:
    assert parse_grants(b"[]", "chat")[0] is None
    assert parse_grants(b"\xff\xfe", "chat")[0] is None
    assert parse_grants(b'{"version": true}', "chat")[0] is None
    assert parse_grants(b'{"version": "2"}', "chat")[0] is None
    assert parse_grants(b'{"version": 2}', "chat")[0] is None


def test_a_grant_file_without_limits_caps_delegations_at_two(tmp_path: Path) -> None:
    """Contract 04 §1.2: the cap now comes from the caller family's file, and
    `caregiver` writes it into every grant file. A file written before that
    field existed carries no `limits` block, and this PEP must read it as 2
    rather than as no cap at all (contract 01 §3.6.1)."""
    store, grants_dir, _ = _store(tmp_path)
    body = make_grants().model_dump()
    del body["limits"]
    write_raw(grants_dir, "chat", json.dumps(body))

    grants = store.lookup(FAMILY_TOKEN)

    assert grants is not None
    assert grants.limits.max_inflight_delegations == 2


def test_the_inflight_cap_is_read_from_the_file(tmp_path: Path) -> None:
    """A rewritten file decides the next call, so a raised cap needs no
    restart (contract 04 §1.4)."""
    store, grants_dir, _ = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    assert store.lookup(FAMILY_TOKEN) is not None

    limits = {"pep_rpm": 60, "max_inflight_delegations": 3, "max_open_gates": 10}
    write_grants(grants_dir, make_grants(limits=limits))
    grants = store.lookup(FAMILY_TOKEN)

    assert grants is not None
    assert grants.limits.max_inflight_delegations == 3


def test_unchanged_files_are_not_reparsed(tmp_path: Path) -> None:
    """The stat guard: a second lookup uses the cached parse."""
    store, grants_dir, _ = _store(tmp_path)
    path = write_grants(grants_dir, make_grants())
    first = store.lookup(FAMILY_TOKEN)

    # Corrupt the bytes without moving the inode, size or mtime; the store is
    # entitled to serve its cache until one of those changes.
    stats = path.stat()
    with path.open("r+b") as handle:
        handle.write(b"{")
    os.utime(path, ns=(stats.st_atime_ns, stats.st_mtime_ns))
    second = store.lookup(FAMILY_TOKEN)
    assert second is first
