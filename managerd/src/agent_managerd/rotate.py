"""`rotate`: a new family key and a new family token (contract 05 §6.3).

Rotation exists because of one accepted cost: a key copied out of a
sandbox stays valid until the family key is rotated (`docs/rework/spec.md` §5.1).
It never replaces a sandbox and never ends a session (§6.3's two "never"s).

## The two halves rotate differently, and the reason is a probe

The **token** overlaps cleanly. The grant file carries a LIST of accepted
digests (contract 04 §2.2), so the new and the old digest both live until
`rotation_grace_s` passes. Every call in that path is proven.

The **key** does not overlap here. Contract 05 §1 fixes one alias per
family, and §6.3's graceful step 1 would put a second live key under it.
Whether LiteLLM allows that is unprobed, and worse: `POST /key/delete`
deletes by ALIAS, so step 5 would take BOTH keys if the duplicate were
allowed. So the key half runs a sequence that needs neither fact:

    brake the old key -> delete the old key -> mint the new one

No two keys ever share the alias, nothing is deleted by a handle that
could match the wrong key, and every call is one probe 0b proved. The cost
is a window of a few HTTP calls in which the family has no key, instead of
§6.3's 300-second overlap. §6.3 already accepts a turn failing with
`model_error` at the end of a graceful rotation; this makes that window
shorter and earlier, not wider.

**What a probe must show to allow the graceful key sequence.** Either
`/key/generate` accepts a second key under an alias a live key already
holds AND `/key/delete` accepts `{"keys": ["<value>"]}` so the old one can
be named exactly, or `/key/update` can move `key_alias` so the new key can
be minted under a suffixed alias and renamed back. One of those two, on
the host's own image. Until then `mode: graceful` and `mode: immediate`
run the same key sequence and `RotateOutcome.note` says so."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from agent_family import FamilyFile, Index

from . import paths, steps
from .clock import now_rfc3339, seconds_from_now
from .credentials import Credentials, mint_token, read_creds, write_creds
from .litellm_keys import LiteLLMError, LiteLLMKeys
from .status import RotationState
from .webhook_tokens import WebhookToken, rotate_webhooks

log = logging.getLogger("agent_managerd.rotate")

#: Contract 05 §6.3 step 5. Above the thin job limit of 120 s on purpose,
#: so a job started under the old epoch finishes under it.
ROTATION_GRACE_S: Final = 300


class Scope(StrEnum):
    """Contract 05 §6.3's `scope`."""

    KEY = "key"
    TOKEN = "token"
    BOTH = "both"


class Mode(StrEnum):
    """Contract 05 §6.3's `mode`. It decides the TOKEN's overlap. The key
    half runs one sequence either way — the module docstring says why."""

    GRACEFUL = "graceful"
    IMMEDIATE = "immediate"


class Reason(StrEnum):
    """Contract 05 §6.3's `reason`. `suspicion` adds the budget brake."""

    SCHEDULED = "scheduled"
    SUSPICION = "suspicion"
    MANUAL = "manual"


class RotateError(RuntimeError):
    """Nothing was rotated, and nothing was lost. A family with no
    credentials has nothing to rotate, and a LiteLLM that refuses the mint
    leaves the old key live."""


@dataclass(frozen=True)
class RotateRequest:
    scope: Scope = Scope.BOTH
    mode: Mode = Mode.GRACEFUL
    reason: Reason = Reason.MANUAL


@dataclass(frozen=True)
class RotateOutcome:
    """What one rotation did. `epoch` is the new one, which `sessiond`
    picks up from the status document on its next read (§6.3 step 3)."""

    epoch: int
    key_rotated: bool
    token_rotated: bool
    token_overlap_until: str | None
    state: RotationState
    note: str
    #: Contract 05 §6.4. The declared webhooks whose bearer moved, by PATH.
    #: The caller republishes the status document from these.
    webhooks: tuple[WebhookToken, ...] = ()


def rotate(
    request: RotateRequest,
    family: FamilyFile,
    index: Index,
    *,
    state_root: Path,
    litellm: LiteLLMKeys,
    grace_s: int = ROTATION_GRACE_S,
) -> RotateOutcome:
    """Mint, write, publish. The caller republishes the status document.

    Order: the key first, because it is the half that can fail. A token
    rotation cannot fail — it is one `secrets.token_bytes` call — so doing
    it first would leave a new token written against an old key when
    LiteLLM refused the mint."""
    creds_path = paths.creds_path(state_root, family.name)
    existing = read_creds(creds_path)
    if existing is None:
        raise RotateError(f"{family.name} has no credentials to rotate")

    key = _new_key(request, family, existing, litellm) if _does_key(request) else None
    token = mint_token() if _does_token(request) else None
    fresh = _next(existing, request, key, token, grace_s)
    write_creds(creds_path, fresh)
    steps.write_grants(family, index, state_root, fresh)

    # Contract 05 §6.4: a webhook bearer is a token, so `scope: token` and
    # `scope: both` move it. It has no overlap of its own — one file holds
    # one value — so whatever calls the route is refused until the operator pastes
    # the new value in. That is what rotating a shared secret costs.
    hooks = rotate_webhooks(family, state_root) if _does_token(request) else ()
    log.info(
        "%s: rotated to epoch %d (scope=%s mode=%s reason=%s)",
        family.name,
        fresh.epoch,
        request.scope,
        request.mode,
        request.reason,
    )
    return RotateOutcome(
        epoch=fresh.epoch,
        key_rotated=key is not None,
        token_rotated=token is not None,
        token_overlap_until=fresh.previous_expires_at,
        state=RotationState.ROTATING if fresh.previous_expires_at else RotationState.SETTLED,
        note=_note(request, key is not None),
        webhooks=hooks,
    )


def settle(family: FamilyFile, index: Index, *, state_root: Path) -> bool:
    """Drop an overlap whose grace has run out (contract 05 §6.3 step 5).

    The watch loop calls this: a grace period is a fact over time, and a
    one-shot verb cannot hold one. Answers whether anything changed."""
    creds_path = paths.creds_path(state_root, family.name)
    existing = read_creds(creds_path)
    if existing is None or existing.previous_expires_at is None:
        return False

    if now_rfc3339() < existing.previous_expires_at:
        return False

    settled = replace(existing, previous_pep_token=None, previous_expires_at=None)
    write_creds(creds_path, settled)
    steps.write_grants(family, index, state_root, settled)
    log.info("%s: rotation settled, the previous token is no longer accepted", family.name)
    return True


def _does_key(request: RotateRequest) -> bool:
    return request.scope in (Scope.KEY, Scope.BOTH)


def _does_token(request: RotateRequest) -> bool:
    return request.scope in (Scope.TOKEN, Scope.BOTH)


def _new_key(
    request: RotateRequest, family: FamilyFile, existing: Credentials, litellm: LiteLLMKeys
) -> str:
    """Brake, delete, mint. In that order, and never any other.

    The brake runs only for `suspicion`, and it runs FIRST because it is
    the fast one: a budget drop refuses the next request at once, while a
    deleted key is still served by some LiteLLM worker for up to 10 s
    (contract 05 §6.3, probe 0b)."""
    if request.reason is Reason.SUSPICION:
        _brake(family, existing, litellm)

    try:
        litellm.delete_key(family.name)
        return litellm.rotate_key(
            family.name, [family.model.router], family.model.budget_usd_per_day
        )
    except LiteLLMError as exc:
        # The old key is already gone, so the family has no key at all.
        # Saying so is the only safe answer: a caller that wrote a new
        # `creds.json` here would publish a key that does not exist.
        raise RotateError(f"{family.name}: the new key was not minted: {exc}") from exc


def _brake(family: FamilyFile, existing: Credentials, litellm: LiteLLMKeys) -> None:
    """Lower the leaked key's budget to what it has already spent
    (contract 05 §6.3, "what `immediate` actually buys", point 2).

    A failed read or update is logged and not raised. The brake is an
    accelerator for the delete that follows, never a condition of it: a
    suspicion that cannot brake must still rotate."""
    try:
        spent = litellm.read_spend(existing.litellm_key).spend_usd
        litellm.update_key(existing.litellm_key, [family.model.router], spent)
    except LiteLLMError as exc:
        log.warning("%s: could not brake the old key before deleting it: %s", family.name, exc)


def _next(
    existing: Credentials,
    request: RotateRequest,
    key: str | None,
    token: str | None,
    grace_s: int,
) -> Credentials:
    """The next epoch. An overlap is recorded only when the TOKEN moved and
    the mode is graceful: there is nothing to overlap otherwise."""
    overlapping = token is not None and request.mode is Mode.GRACEFUL
    return Credentials(
        epoch=existing.epoch + 1,
        litellm_key=key if key is not None else existing.litellm_key,
        pep_token=token if token is not None else existing.pep_token,
        written_at=now_rfc3339(),
        previous_pep_token=existing.pep_token if overlapping else None,
        previous_expires_at=seconds_from_now(grace_s) if overlapping else None,
    )


def _note(request: RotateRequest, key_rotated: bool) -> str:
    if not key_rotated or request.mode is Mode.IMMEDIATE:
        return f"scope={request.scope} mode={request.mode}"

    # Honesty in the outcome, not only in the docstring: a caller that
    # asked for a graceful key rotation did not get one.
    return (
        f"scope={request.scope} mode=graceful, but the KEY half ran the immediate sequence, "
        "with no overlap"
    )
