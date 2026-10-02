"""Mint an id, build §2.3's body, and write ONE file into `requests/`.

`stage7-releases.md` §2.3 fixes the seven fields and §3.2 fixes what root
does to a file it does not believe. Everything here exists so a requester
learns a refusal BEFORE the tap instead of out of `done/<ULID>.json`.

Three rules, each with the reason it is a rule.

1. **The executor's parser is the judge.** `plan_request` serializes the body
   and hands it to `executor.request.parse_request`, the same function root
   runs over the same bytes. Re-implementing the patterns here would give two
   answers to one question, and the one that matters is root's.
2. **A name is never replaced.** `os.link` refuses a name that already
   exists, where `rename` would silently overwrite it. §2.2 says a writer
   renames `.<ulid>.tmp` into `<ulid>.json`; a link plus an unlink has the
   same two observable properties — a reader sees a whole file or none, and
   the temporary name does not match the path unit's `*.json` glob — and it
   refuses a collision instead of eating another writer's request.
3. **The pass is capped before the write, not after.** §3.2 rule 8 caps
   pending requests, and a requester that can see `requests/` can say `rate`
   itself. The executor still enforces its own cap: this one is a courtesy
   and never a control.

Stdlib only, every import at module top. The PEP imports this module, and a
release re-syncs venvs under running processes (contract 06 §1.1).
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from typing import Final

from ..errors import Refusal, RefusalCode
from ..executor.request import MAX_REQUEST_BYTES, REQUEST_SUFFIX, Request, parse_request
from ..executor.spool import MAX_PENDING_PER_REQUESTER, REQUESTS_DIR, SPOOL_ROOT

#: Where a request lands on the host. Built from the executor's own two
#: constants so the path is written down once (`release/AGENTS.md`, pain 1).
REQUESTS_PATH: Final = f"{SPOOL_ROOT}/{REQUESTS_DIR}"

#: §3.2 rule 8, applied to the directory this writer can see (rule 3 in the
#: module docstring). It counts entries, not `requested_by` values: those
#: sit inside the other writer's files, and a count of entries is stricter
#: than rule 8, so no request this side files is one root refuses `rate`.
MAX_PENDING: Final = MAX_PENDING_PER_REQUESTER

#: Nothing but the writer and root reads a request. Root reads it as root.
REQUEST_FILE_MODE: Final = 0o600

#: §3.2 rule 4's shape, applied on this side: a refusal a caller may print is
#: a fixed string, never built from what the caller passed in.
RATE_REASON: Final = f"requests/ already holds {MAX_PENDING} requests"

TMP_PREFIX: Final = "."
TMP_SUFFIX: Final = ".tmp"

#: Contract 06 §9 and contract 02 §2: 26 characters of Crockford base32,
#: upper case, with I, L, O and U absent.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CHAR_BITS: Final = 5
_ULID_CHARS: Final = 26
_RANDOM_BYTES: Final = 10
_RANDOM_BITS: Final = _RANDOM_BYTES * 8

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

#: §2.3's `kind` values. Spelled here rather than imported as an enum so a
#: caller that holds a plain string from a JSON schema needs no conversion.
RELEASE_KIND: Final = "release"


class RequesterError(Exception):
    """This host cannot file a request: no spool, a path that is not a
    directory, a name already taken.

    Deliberately NOT a `Refusal`. A refusal is what root would say about the
    request itself, and a caller that cannot tell the two apart reports a
    broken directory as a bad request.
    """


def new_ulid(now: float, entropy: Callable[[int], bytes] = os.urandom) -> str:
    """48 bits of milliseconds, then 80 bits of randomness (contract 02 §2).

    The time half sorts: `Spool.pending` orders by file name, so a later
    request must sort after an earlier one. The random half is what keeps two
    requests filed inside one millisecond from colliding on a name
    `file_request` refuses to replace.
    """
    value = (int(now * 1000) << _RANDOM_BITS) | int.from_bytes(entropy(_RANDOM_BYTES), "big")
    first = (_ULID_CHARS * _CHAR_BITS) - _CHAR_BITS

    return "".join(
        _ALPHABET[(value >> shift) & 0b11111] for shift in range(first, -_CHAR_BITS, -_CHAR_BITS)
    )


def plan_request(
    components: Mapping[str, str],
    *,
    requested_by: str,
    now: float,
    kind: str = RELEASE_KIND,
    rollback_of: str | None = None,
    requester_session: str | None = None,
    request_id: str | None = None,
) -> Request:
    """§2.3's body, validated by the executor's own parser.

    Every refusal out of here is one root would give for the same bytes, with
    root's own detail string, so a caller can print it unchanged.
    """
    minted = request_id if request_id is not None else new_ulid(now)
    body: dict[str, object] = {
        "id": minted,
        "kind": kind,
        "components": dict(components),
        "rollback_of": rollback_of,
        "requested_by": requested_by,
        "requester_session": requester_session,
        "ts": now,
    }
    raw = _encode(body)
    if len(raw) > MAX_REQUEST_BYTES:
        raise Refusal(RefusalCode.REQUEST, "request", f"is larger than {MAX_REQUEST_BYTES} bytes")

    return parse_request(raw, minted)


def file_request(request: Request, requests_dir: str = REQUESTS_PATH) -> str:
    """Write one request. Answers the path it landed at.

    The whole body is re-serialized from the validated `Request`, never from
    the caller's own dictionary, so a field that failed a pattern cannot ride
    along beside the one that passed.
    """
    name = request.id + REQUEST_SUFFIX
    fd = _open_requests(requests_dir)
    try:
        _refuse_a_full_directory(fd)
        _write_once(fd, name, _encode(request.as_dict()))
    finally:
        os.close(fd)

    return f"{requests_dir}/{name}"


def pending_count(requests_dir: str = REQUESTS_PATH) -> int:
    """How many requests are waiting. Read by the CLI before it prints."""
    fd = _open_requests(requests_dir)
    try:
        return _pending(fd)
    finally:
        os.close(fd)


def _encode(body: dict[str, object]) -> bytes:
    return json.dumps(body, sort_keys=False).encode("utf-8")


def _open_requests(requests_dir: str) -> int:
    """One descriptor, `O_NOFOLLOW`, and every later call relative to it.

    The same rule root applies (`spool.py`), for the same reason: the path
    runs through directories other accounts reach, so a component of it may
    be swapped for a symlink between two calls.
    """
    try:
        return os.open(requests_dir, _DIR_FLAGS)
    except OSError as exc:
        raise RequesterError(f"{requests_dir}: {exc.strerror or 'cannot be opened'}") from None


def _pending(dir_fd: int) -> int:
    return sum(1 for name in os.listdir(dir_fd) if name.endswith(REQUEST_SUFFIX))


def _refuse_a_full_directory(dir_fd: int) -> None:
    if _pending(dir_fd) >= MAX_PENDING:
        raise Refusal(RefusalCode.RATE, "request", RATE_REASON)


def _write_once(dir_fd: int, name: str, payload: bytes) -> None:
    """Whole file or none, and never on top of a name that exists."""
    tmp = f"{TMP_PREFIX}{name}{TMP_SUFFIX}"
    _unlink_quiet(dir_fd, tmp)
    try:
        fd = os.open(tmp, _CREATE_FLAGS, REQUEST_FILE_MODE, dir_fd=dir_fd)
    except OSError as exc:
        raise RequesterError(f"cannot create {tmp}: {exc.strerror}") from None

    try:
        # A umask the caller's process inherited must not widen or narrow it.
        os.fchmod(fd, stat.S_IMODE(REQUEST_FILE_MODE))
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        raise RequesterError(f"cannot create {name}: {exc.strerror}") from None
    finally:
        _unlink_quiet(dir_fd, tmp)


def _unlink_quiet(dir_fd: int, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dir_fd)
