"""The family's own two reads, through the PEP, with the family's own token
(contract 01 §3.15 rule 4):

    POST /call {"tool": "<board>__survey_board", "args": {}}   -> fingerprint
    POST /call {"tool": "job_status", "args": {"limit": 200}}  -> live, ended

The token is `pep_token` in the family's `creds.json` (contract 03 §12.2),
the file `caregiver` writes and the sandbox reads. It rides in the
Authorization header and nowhere else (invariant 13). The PEP audits both
calls under the family, with no session.

Every failure answers None, and the gate wakes on it: a check that cannot
see is not quiet."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final, Protocol

import httpx
from agent_family import CALL_SEPARATOR, SURVEY_TOOL, Verb

from ..untrusted import as_object, field_text, is_list
from .board import fingerprint
from .decide import Jobs

_LOG = logging.getLogger(__name__)

_OK: Final = 200

#: Contract 02 §13.4.2's ceiling. Newest first, so a job old enough to fall
#: off the page ended long ago: a turn's own limit is an hour.
JOB_LIMIT: Final = 200

#: Contract 02 §13.4.2's one terminal status. Every other one is live.
ENDED: Final = "ended"

CONNECT_TIMEOUT_S: Final = 5.0
#: A survey reads the whole board from Vikunja. The unit's 120 s start limit
#: must still cover both reads and the firing after them.
READ_TIMEOUT_S: Final = 20.0

#: Contract 03 §12.2: `creds/creds.json` under the family's state directory.
CREDS_PATH: Final = "{family}/creds/creds.json"
TOKEN_FIELD: Final = "pep_token"


class FamilyReads(Protocol):
    """What the gate asks the PEP, as the family."""

    def board(self, server: str) -> str | None:
        """The board's fingerprint, or None."""
        ...

    def jobs(self) -> Jobs | None:
        """The jobs this family enqueued, or None."""
        ...


def family_token(families_dir: Path, family: str) -> str | None:
    """The family's PEP bearer, or None. Never logged, never raised."""
    path = families_dir / CREDS_PATH.format(family=family)
    try:
        body: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        _LOG.warning("quiet: %s: no readable creds.json (%s)", family, type(exc).__name__)
        return None

    return field_text(as_object(body), TOKEN_FIELD) or None


class HttpFamilyReads:
    """`FamilyReads` over the PEP's `/call`. A None token reads nothing."""

    def __init__(self, client: httpx.Client, token: str | None) -> None:
        self._client = client
        self._token = token

    def board(self, server: str) -> str | None:
        answer = self._call(f"{server}{CALL_SEPARATOR}{SURVEY_TOOL}", {})
        if not isinstance(answer, str):
            return None

        # An MCP tool answers text: the survey's own JSON.
        try:
            survey: object = json.loads(answer)
        except (ValueError, RecursionError):
            return None

        return fingerprint(survey)

    def jobs(self) -> Jobs | None:
        rows = as_object(self._call(Verb.JOB_STATUS, {"limit": JOB_LIMIT})).get("jobs")
        if not is_list(rows):
            return None

        live: set[str] = set()
        ended: set[str] = set()
        for row in rows:
            body = as_object(row)
            session = field_text(body, "session")
            status = field_text(body, "status")
            if not session or not status:
                return None

            (ended if status == ENDED else live).add(session)

        return Jobs(live=frozenset(live), ended=frozenset(ended))

    def _call(self, tool: str, args: dict[str, object]) -> object | None:
        """The call's `result`, or None. Logs the status and the PEP's
        reason, never a body: a body can hold the family's own data."""
        if self._token is None:
            return None

        try:
            response = self._client.post(
                "/call",
                json={"tool": tool, "args": args},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
            )
        except httpx.HTTPError as exc:
            _LOG.warning("quiet: %s: the PEP did not answer (%s)", tool, type(exc).__name__)
            return None

        body = _json_object(response.text)
        if response.status_code != _OK or body.get("ok") is not True:
            reason = field_text(body, "reason") or "no reason"
            _LOG.warning("quiet: %s: the PEP answered %d, %s", tool, response.status_code, reason)
            return None

        return body.get("result")


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: an answer that nests too deep is not a ValueError.
        return {}

    return as_object(parsed)
