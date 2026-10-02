"""One bearer token per declared webhook trigger (contract 05 §6.4).

Invariant 16: a family file is the one place a family changes, and
`managerd` converges. A family file that declares

    triggers:
      - webhook: boiler-alert

must therefore open a live route with no second, hand-made act. Before
this module, `door-trigger` answered 404 on every call to that route until
an operator wrote the token by hand, and it said so in one log line and
nowhere else.

```
  family.yaml declares a webhook
        |
        v
  managerd mints <state>/triggers/webhooks/<family>/<name>.token, 0600
        |                                      |
        |                                      `--> status.json names the PATH
        v
  door-trigger reads it on its next refresh and serves the route
```

Three rules, each with its reason:

1. **An existing token is kept.** `ensure_webhooks` runs on every apply and
   every reconcile pass. Minting each time would break whatever holds the
   bearer — Home Assistant, Node-RED — several times an hour.
2. **A withdrawn webhook loses its file.** The credential exists because the
   family file declares the trigger. When the declaration goes, the reach
   goes with it (invariant 9, a removal applies at once).
3. **No value is ever logged or published.** Only the path is (invariant 13).
   The status document is 0644 exactly so that rule is easy to check.
"""

from __future__ import annotations

import base64
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_family import FamilyFile

from . import paths
from .atomic import atomic_write

#: `door-trigger`'s `MIN_WEBHOOK_TOKEN_BYTES` is 32 and contract 02 §3 rule
#: 7 sets the same floor for a door's own token. 32 raw bytes is 43 base64
#: characters, which clears it.
WEBHOOK_TOKEN_BYTES: Final = 32

#: `door-trigger`'s `tokens.py` refuses any file with a group or other bit
#: and treats its route as unconfigured, so this mode is load-bearing.
WEBHOOK_TOKEN_MODE: Final = 0o600

#: The directory holds nothing but bearer tokens, so it gets the same reach.
WEBHOOKS_DIR_MODE: Final = 0o700


def mint_webhook_token() -> str:
    """32 bytes from the system random source, base64, no padding."""
    raw = secrets.token_bytes(WEBHOOK_TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class WebhookToken:
    """One declared webhook and where its bearer lives (contract 05 §6.4)."""

    name: str
    path: Path

    def as_json(self) -> dict[str, Any]:
        """The path, never the value. The operator pastes it into Home Assistant."""
        return {"name": self.name, "token_path": str(self.path)}


def ensure_webhooks(family: FamilyFile, state_root: Path) -> tuple[WebhookToken, ...]:
    """Mint what is missing, keep what exists, remove what is withdrawn."""
    return _converge(family, state_root, keep_existing=True)


def rotate_webhooks(family: FamilyFile, state_root: Path) -> tuple[WebhookToken, ...]:
    """Contract 05 §6.3. A new bearer for every webhook this family declares."""
    return _converge(family, state_root, keep_existing=False)


def forget_webhooks(state_root: Path, family_name: str) -> None:
    """Every bearer of a deleted family, gone before its sandboxes are
    (invariant 13: credentials die before processes)."""
    shutil.rmtree(paths.webhooks_family_dir(state_root, family_name), ignore_errors=True)


def _converge(
    family: FamilyFile, state_root: Path, *, keep_existing: bool
) -> tuple[WebhookToken, ...]:
    declared = _declared_webhooks(family)
    folder = paths.webhooks_family_dir(state_root, family.name)
    _drop_withdrawn(folder, declared)

    if not declared:
        return ()

    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(WEBHOOKS_DIR_MODE)
    published: list[WebhookToken] = []

    for name in declared:
        path = paths.webhook_token_path(state_root, family.name, name)
        _write_token(path, keep_existing=keep_existing)
        published.append(WebhookToken(name=name, path=path))

    return tuple(published)


def _declared_webhooks(family: FamilyFile) -> list[str]:
    """Contract 01 §3.13. A trigger carries a cron OR a webhook, never both."""
    triggers = family.triggers or ()
    return [one.webhook for one in triggers if one.webhook is not None]


def _write_token(path: Path, *, keep_existing: bool) -> None:
    # Rule 1. A readable file of the right mode is this family's live
    # bearer, and something on the LAN already holds its value.
    if keep_existing and path.is_file():
        return

    atomic_write(path, mint_webhook_token().encode("ascii") + b"\n", mode=WEBHOOK_TOKEN_MODE)


def _drop_withdrawn(folder: Path, declared: list[str]) -> None:
    """Rule 2. Only this directory, and only files this module writes."""
    if not folder.is_dir():
        return

    for entry in folder.iterdir():
        if entry.suffix != paths.WEBHOOK_TOKEN_SUFFIX or entry.stem in declared:
            continue

        entry.unlink(missing_ok=True)
