"""The family token and `creds.json` (contract 04 section 2, contract 03
section 12, contract 05 section 6.2).

The token names the family and nothing narrower. `caregiver` mints it, hands
it to the sandbox through a mounted file, and keeps only its digest in the
grant file (contract 04 section 2.2). It never appears on argv, in a URL or
in a log line (invariant 13)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .atomic import atomic_write

#: Contract 04 section 2.2 rule 1: "at least 256 bits from the system
#: random source, base32, no padding."
TOKEN_BYTES: Final = 32

#: Contract 03 section 12.2: creds.json is mode 0600, readable only by the
#: user caregiver runs as until the sandbox mounts it read-only.
CREDS_FILE_MODE: Final = 0o600


def mint_token() -> str:
    """256 bits, base32, no padding (contract 04 section 2.2 rule 1)."""
    return base64.b32encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii").rstrip("=")


def token_sha256(token: str) -> str:
    """What the grant file stores. The PEP never sees the token itself
    (contract 04 section 2.2 rule 2)."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Credentials:
    """One family's current authority, as written to `creds.json`
    (contract 03 section 12, contract 05 section 6.2 step 3).

    `epoch` increases on every write. The playpen refuses a turn whose
    `env_epoch` is newer than what it can read here (contract 03 section
    12.5), which is what makes a rotation visible without a new sandbox."""

    epoch: int
    litellm_key: str
    pep_token: str
    written_at: str
    #: The token the previous epoch used, and when it stops being accepted
    #: (contract 05 §6.3 graceful, step 5). The grant file carries BOTH
    #: digests until then, so a turn that started under the old epoch
    #: finishes. It lives here and not in a second file because the grant
    #: file is rebuilt from `creds.json` on every reconcile pass, and a
    #: fact split over two files is a fact that can disagree with itself.
    previous_pep_token: str | None = None
    previous_expires_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "litellm_key": self.litellm_key,
            "pep_token": self.pep_token,
            "written_at": self.written_at,
            "previous_pep_token": self.previous_pep_token,
            "previous_expires_at": self.previous_expires_at,
        }

    def accepted_tokens(self, now: str) -> tuple[str, ...]:
        """Every token the PEP should still accept. The current one always,
        and the previous one only while its overlap lasts. Both stamps are
        fixed-width UTC, so `<` orders them without parsing."""
        if self.previous_pep_token is None or self.previous_expires_at is None:
            return (self.pep_token,)

        if now >= self.previous_expires_at:
            return (self.pep_token,)

        return (self.pep_token, self.previous_pep_token)


def write_creds(path: Path, creds: Credentials) -> None:
    """Atomic write, mode 0600 (contract 03 section 12.2)."""
    body = json.dumps(creds.as_json(), indent=2).encode("utf-8") + b"\n"
    atomic_write(path, body, mode=CREDS_FILE_MODE)


def read_creds(path: Path) -> Credentials | None:
    """`None` when no credentials exist yet (a new family) or the file is
    unreadable. Never raises: a corrupt `creds.json` is a fault for the
    caller to report, not a crash (invariant 19's spirit applied here)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(body, dict):
        return None

    fields = cast("dict[str, Any]", body)
    try:
        return Credentials(
            epoch=int(fields["epoch"]),
            litellm_key=str(fields["litellm_key"]),
            pep_token=str(fields["pep_token"]),
            written_at=str(fields["written_at"]),
            previous_pep_token=_optional(fields.get("previous_pep_token")),
            previous_expires_at=_optional(fields.get("previous_expires_at")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional(value: Any) -> str | None:
    """A field written by an older epoch, or never written at all, reads as
    "no overlap" rather than as a reason to refuse the whole file."""
    return value if isinstance(value, str) and value else None
