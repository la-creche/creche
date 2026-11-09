"""The client of the vmm host API (decisions CH9, CH20, CH25).

The vmm owns each VM, one per session (decision CH1). caregiver starts and
stops none of them. It makes one call: when it deletes a family, it asks
the vmm to drop every session of that family (decision CH21-10, contract
05 §4.4).

    caregiver --- POST /drop over vmm.sock ---> vmm

The transport is HTTP/1.1 over the Unix socket `vmm.sock` of decision
CH20. Every client runs as the operator, so the mode of the socket
directory is the guard: no token, no group. The env key
`CAREGIVER_VMM_SOCKET` names the socket. The unit sets it to
`%t/vmm/vmm.sock`, the path of decision CH20.

A route constant of `creche_wire.routes` pairs the request, the reply and
the error types (decision CH25), so pyright refuses a wrong model here.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol

import httpx
from creche_wire import (
    DropError,
    DropFamily,
    Dropped,
    DropTarget,
    FamilyDrop,
    FamilyName,
    ProtocolMismatch,
    read,
)
from creche_wire.routes import (
    DROP,
    DROP_CLIENT_TIMEOUT_S,
    HOST_API_BODY_CAP,
    HOST_API_HEADER,
    HOST_API_PROTOCOL,
    MISMATCH_STATUS,
)

#: The env key that names `vmm.sock` (decision CH20).
#:
#: Decision CH20 fixes the path `/run/user/<uid>/vmm/vmm.sock`. The env
#: catalog cannot spell a uid, so `default.env` holds no key for it. The
#: unit sets this key from `%t`, outside the catalog (decision CH84 rule
#: 1). To make it a catalog key, the catalog grammar needs a form for the
#: runtime dir of an account.
VMM_SOCKET_ENV: Final = "CAREGIVER_VMM_SOCKET"

#: Contract 05 §3.3: a client of the vmm got the mismatch reply on each of
#: its tries. caregiver raises the same code as attendance (decision CH82
#: rule 5).
#:
#: Contract 05 §3.3.1 says that caregiver raises this code for its own vmm
#: call. The one call is the drop of a deleted family, whose status
#: document goes with the family. The code is the `code` of the
#: `VmmError`, the log line of the delete names it, and no status document
#: carries it (decision CH84 rule 2).
VMM_PROTOCOL_MISMATCH: Final = "vmm_protocol_mismatch"

#: Decision CH25 rule 1: after a mismatch, retry a few times, then report
#: the named fault. `wire/AGENTS.md` leaves the numbers to caregiver. These
#: are the numbers of attendance (decision CH33 rule 10): 3 requests in
#: all, 2 s apart.
MISMATCH_TRIES: Final = 3
MISMATCH_PAUSE_S: Final = 2.0

#: The host part of the URL. The transport dials the socket, so it names
#: nothing.
SOCKET_BASE_URL: Final = "http://vmm"

#: An HTTP status of success.
_SUCCESS: Final = range(200, 300)


#: The code of a vmm that did not answer.
UNREACHABLE: Final = "unreachable"

#: The code of an answer that does not read as the type of the route.
UNREADABLE: Final = "unreadable"


class VmmError(RuntimeError):
    """The vmm did not drop what caregiver asked it to drop.

    `code` is the code of the refusal: a code of the route error, the code
    `vmm_protocol_mismatch`, or `unreachable` for a vmm that did not
    answer. ENOENT, ECONNREFUSED and EOF are `unreachable`, a transient
    fault (decision CH20): the next try of the delete sends the call
    again."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class VmmClient(Protocol):
    """The one call of caregiver to the vmm."""

    def drop_family(self, family: str) -> None:
        """Drop every session of `family`: its VMs, snapshots, session
        disks and rows (decision CH21-10). A family with no rows is done
        too. Raises `VmmError` when the vmm did not answer `Dropped`."""
        ...


def socket_from(environ: Mapping[str, str]) -> Path | None:
    """The path of `vmm.sock` that `VMM_SOCKET_ENV` names, or `None` when
    the key is absent or empty."""
    value = environ.get(VMM_SOCKET_ENV, "").strip()
    return Path(value) if value else None


class HttpVmmClient:
    """The vmm on `vmm.sock`. `httpx.Client` is safe to share between the
    threads of the loop, and this class keeps nothing else."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def over_socket(cls, socket: Path) -> HttpVmmClient:
        """The vmm on its Unix socket. A drop stops each VM in 35 s at most,
        so the client waits `DROP_CLIENT_TIMEOUT_S` for the reply (decision
        CH33 rule 11)."""
        client = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), timeout=DROP_CLIENT_TIMEOUT_S
        )
        return cls(client)

    def drop_family(self, family: str) -> None:
        target = DropTarget(FamilyDrop(family=DropFamily(family=FamilyName(family))))
        response = self._post(DROP.path, DROP.encode(target))
        if response.status_code in _SUCCESS:
            _read(Dropped, response)
            return

        refusal = _read(DropError, response)
        raise VmmError(refusal.root.code, f"the vmm refused the drop of {family}")

    def _post(self, path: str, body: bytes) -> httpx.Response:
        """One request, sent again after each mismatch reply, up to
        `MISMATCH_TRIES` requests in all. A mismatch on the last one is the
        fault `vmm_protocol_mismatch` (decisions CH25 rule 1, CH82 rule
        5)."""
        for _ in range(MISMATCH_TRIES - 1):
            response = self._send(path, body)
            if response.status_code != MISMATCH_STATUS:
                return response

            _read(ProtocolMismatch, response)
            time.sleep(MISMATCH_PAUSE_S)

        response = self._send(path, body)
        if response.status_code != MISMATCH_STATUS:
            return response

        mismatch = _read(ProtocolMismatch, response)
        raise VmmError(
            VMM_PROTOCOL_MISMATCH,
            f"the vmm speaks host API protocol {mismatch.protocol}, "
            f"and caregiver speaks {HOST_API_PROTOCOL}",
        )

    def _send(self, path: str, body: bytes) -> httpx.Response:
        """One request. A vmm that does not answer is `unreachable`."""
        headers = {HOST_API_HEADER: str(HOST_API_PROTOCOL), "Content-Type": "application/json"}
        try:
            return self._client.post(f"{SOCKET_BASE_URL}{path}", content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise VmmError(UNREACHABLE, f"POST {path}: {type(exc).__name__}") from exc


def _read[M: DropError | Dropped | ProtocolMismatch](model: type[M], response: httpx.Response) -> M:
    """The body of `response` as `model`, under the body cap of the host
    API. A body that does not read is `VmmError`, never a default."""
    try:
        return read(model, response.content, cap=HOST_API_BODY_CAP)
    except ValueError as exc:
        # `read` raises `NotStrictJson` and `pydantic.ValidationError`, and
        # each is a `ValueError`.
        raise VmmError(
            UNREADABLE,
            f"HTTP {response.status_code}: the body is no {model.__name__}: {type(exc).__name__}",
        ) from exc
