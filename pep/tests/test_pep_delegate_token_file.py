"""The delegate door reads its bearer at CALL time (contract 04 §7.3).

Why this file exists: `attendance` writes `door-delegate.token` when IT starts.
The PEP is a system unit and can start first, so a token read once at boot is
empty forever and every rotation needs `sudo systemctl restart agent-pep`.
The operator makes one root visit, not one per rotation.

No live service and no socket: every case drives `HttpDelegateDoor` through an
httpx mock transport that reports back which bearer it saw.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from agent_pep.app import PepConfig, build_delegate_door
from agent_pep.delegate import (
    MIN_DOOR_TOKEN_BYTES,
    CachedTokenFile,
    DelegateRequest,
    DelegateStatus,
    DoorConfig,
    DoorTokenError,
    HttpDelegateDoor,
)

FIRST = "a" * MIN_DOOR_TOKEN_BYTES
SECOND = "b" * MIN_DOOR_TOKEN_BYTES
TOKEN_NAME = "door-delegate.token"

#: What the mock transport records, so a case can assert on the bearer the
#: door actually presented rather than on the door's internal state.
seen: list[str] = []


def write_token(path: Path, value: str) -> None:
    """A token file the way `attendance` writes it: 0640, group readable."""
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o640)


def recording_handler(request: httpx.Request) -> httpx.Response:
    seen.append(request.headers.get("authorization", ""))
    return httpx.Response(200, json={"status": "ok", "session_id": "job-1", "content": "hi"})


def door(config: DoorConfig) -> HttpDelegateDoor:
    transport = httpx.MockTransport(recording_handler)  # pyright: ignore[reportArgumentType]
    return HttpDelegateDoor(config, transport=transport)


def request() -> DelegateRequest:
    return DelegateRequest(
        caller_family="chat",
        target_family="vault-oracle",
        delegation_id="01K5J9QWB2M4N6Q8S0V2W4Y6A8",
        claimed_session_id=None,
        message="where is the boiler note",
    )


def call(built: HttpDelegateDoor) -> DelegateStatus:
    return asyncio.run(built.call(request())).status


@pytest.fixture(autouse=True)
def clear_seen() -> None:
    seen.clear()


# ---- the cache itself ------------------------------------------------------


def test_a_file_written_after_start_is_read(tmp_path: Path) -> None:
    """The exact live failure: the PEP starts, `attendance` writes the file
    afterwards, and the next call must find it."""
    path = tmp_path / TOKEN_NAME
    source = CachedTokenFile(path)

    with pytest.raises(DoorTokenError):
        source.value()

    write_token(path, FIRST)
    assert source.value() == FIRST


def test_a_rotated_file_is_re_read(tmp_path: Path) -> None:
    path = tmp_path / TOKEN_NAME
    write_token(path, FIRST)
    source = CachedTokenFile(path)
    assert source.value() == FIRST

    # A rotation inside one clock tick still moves st_size or st_ino, and a
    # replace moves the inode. Change the length so no timer resolution can
    # hide the change.
    write_token(path, SECOND + SECOND)
    assert source.value() == SECOND + SECOND


def test_a_replaced_file_is_re_read(tmp_path: Path) -> None:
    """`os.replace` is how every writer in this repo rotates a secret. The
    inode moves even when size and mtime do not."""
    path = tmp_path / TOKEN_NAME
    write_token(path, FIRST)
    source = CachedTokenFile(path)
    assert source.value() == FIRST

    other = tmp_path / "next.token"
    write_token(other, SECOND)
    os.utime(other, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
    os.replace(other, path)
    assert source.value() == SECOND


def test_an_unchanged_file_is_not_re_read(tmp_path: Path) -> None:
    """The cache is the reason this can sit on the per-call path at all: an
    unchanged file costs one stat, not one read. Proved by changing the
    CONTENT while holding the stat triple still — same length, same inode,
    mtime put back — and seeing the old value come out."""
    path = tmp_path / TOKEN_NAME
    write_token(path, FIRST)
    source = CachedTokenFile(path)
    assert source.value() == FIRST

    before = path.stat()
    write_token(path, SECOND)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert source.value() == FIRST


def test_a_vanished_file_stops_serving_the_cache(tmp_path: Path) -> None:
    """A revoked token must stop working. A stat that fails is never served
    from the cache."""
    path = tmp_path / TOKEN_NAME
    write_token(path, FIRST)
    source = CachedTokenFile(path)
    assert source.value() == FIRST

    path.unlink()
    with pytest.raises(DoorTokenError):
        source.value()


def test_a_short_file_still_refuses(tmp_path: Path) -> None:
    """Contract 02 §3 rule 7's 32 byte floor, applied at call time now."""
    path = tmp_path / TOKEN_NAME
    write_token(path, "tiny")
    with pytest.raises(DoorTokenError):
        CachedTokenFile(path).value()


def test_the_error_never_carries_the_value(tmp_path: Path) -> None:
    path = tmp_path / TOKEN_NAME
    write_token(path, "tiny")
    with pytest.raises(DoorTokenError) as caught:
        CachedTokenFile(path).value()
    assert "tiny" not in str(caught.value)


# ---- the door presents what the file holds now -----------------------------


def test_the_door_presents_the_current_token(tmp_path: Path) -> None:
    path = tmp_path / TOKEN_NAME
    write_token(path, FIRST)
    built = door(DoorConfig(token="", token_source=CachedTokenFile(path)))

    assert call(built) is DelegateStatus.OK
    write_token(path, SECOND + SECOND)
    assert call(built) is DelegateStatus.OK

    assert seen == [f"Bearer {FIRST}", f"Bearer {SECOND + SECOND}"]


def test_an_unreadable_file_fails_the_call_closed(tmp_path: Path) -> None:
    """Fail closed (pep/AGENTS.md rule 1): no request leaves the PEP, and the
    caller is told the call failed."""
    built = door(DoorConfig(token="", token_source=CachedTokenFile(tmp_path / TOKEN_NAME)))

    assert call(built) is DelegateStatus.FAILED
    assert seen == []


def test_the_failure_never_names_the_path(tmp_path: Path) -> None:
    """A host path is not the sandbox's business. The detail goes to the
    journal; the caller gets a fixed sentence."""
    built = door(DoorConfig(token="", token_source=CachedTokenFile(tmp_path / TOKEN_NAME)))
    reply = asyncio.run(built.call(request()))
    assert str(tmp_path) not in (reply.error or "")


def test_a_fixed_token_still_works() -> None:
    """The value form stays, because every other test in this package builds
    a door with one and no file exists in those cases."""
    built = door(DoorConfig(token=FIRST))
    assert call(built) is DelegateStatus.OK
    assert seen == [f"Bearer {FIRST}"]


# ---- configuration ---------------------------------------------------------


def _config(tmp_path: Path, **fields: object) -> PepConfig:
    base = {
        "audit_dir": tmp_path / "audit",
        "rework_dir": tmp_path / "rework",
        "attendance_socket": tmp_path / "sessiond.sock",
    }
    return PepConfig(**{**base, **fields})  # pyright: ignore[reportArgumentType]


def test_a_token_file_alone_builds_a_door(tmp_path: Path) -> None:
    """The door exists because it is CONFIGURED, not because the file is
    readable right now. That is the whole fix: the PEP may start first."""
    built = _config(tmp_path, delegate_token_file=tmp_path / TOKEN_NAME)
    assert build_delegate_door(built) is not None


def test_no_token_and_no_file_builds_no_door(tmp_path: Path) -> None:
    assert build_delegate_door(_config(tmp_path)) is None


def test_a_token_file_without_a_socket_builds_no_door(tmp_path: Path) -> None:
    base = PepConfig(
        audit_dir=tmp_path / "audit",
        rework_dir=tmp_path / "rework",
        delegate_token_file=tmp_path / TOKEN_NAME,
    )
    assert build_delegate_door(base) is None


def test_the_built_door_reads_the_file_late(tmp_path: Path) -> None:
    """End to end through `build_delegate_door`: configure, start, THEN write
    the file, and the first call still carries the right bearer."""
    path = tmp_path / TOKEN_NAME
    built = build_delegate_door(_config(tmp_path, delegate_token_file=path))
    assert built is not None

    write_token(path, FIRST)
    # The socket in the config names nothing that listens, so the call fails
    # at the transport. What matters is that it got that far: an unreadable
    # token would have refused before any connection attempt.
    reply = asyncio.run(built.call(request()))
    assert reply.status is DelegateStatus.FAILED
    assert "token" not in (reply.error or "")
