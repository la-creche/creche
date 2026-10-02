"""The family's one LiteLLM key (contract 04 §1.2, contract 05 §6,
`docs/rework/spec.md` §5.1). One key per family, alias `family-<name>`,
minted with the one model alias the family's `model.router` names and a
daily budget.

Probe 0b proved `/key/generate`, `/key/update` and delete-by-alias on the
live host (contract 05 §10).

The master key comes from the environment. It never appears on argv, in a
URL or in a log line (invariant 13): every call sends it as a bearer header,
and no method here logs a response body, because a `/key/generate` response
body on success IS the new key."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import httpx

#: Contract 05 §1: "Key alias = family-<name>." Fixed, one per family.
ALIAS_PREFIX: Final = "family-"

#: `model.budget_usd_per_day` renews every day (contract 01 §3.2).
BUDGET_DURATION: Final = "1d"

GENERATE_PATH: Final = "/key/generate"
UPDATE_PATH: Final = "/key/update"
DELETE_PATH: Final = "/key/delete"
INFO_PATH: Final = "/key/info"

REQUEST_TIMEOUT_S: Final = 15

#: Contract 05 §7: the family key's budget renews daily, so the window the
#: spend is measured over is a day.
SPEND_WINDOW: Final = "day"


class LiteLLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Spend:
    """What LiteLLM says this family has spent (contract 05 §7).

    LiteLLM is the authority. The `usage` numbers a supervisor reports are
    advisory, because the sandbox is untrusted (contract 03 §13 rule 7)."""

    spend_usd: float
    budget_usd: float | None


def key_alias(family: str) -> str:
    return f"{ALIAS_PREFIX}{family}"


class LiteLLMKeys(Protocol):
    """`ensure_key`, `update_key`, `rotate_key`, `delete_key`."""

    def ensure_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        """Mint the family's key. Returns the raw key; the caller writes it
        into `creds.json` (contract 03 §12) and never logs it."""
        ...

    def update_key(self, key: str, models: list[str], budget_usd_per_day: float) -> None:
        """`/key/update` on an already-minted key (contract 01 §6.2): up to
        10s worker-cache lag on the model plane, per probe 0b."""
        ...

    def rotate_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        """Mint a replacement (contract 05 §6.3 step 1). Whether a second
        live key may share the alias is unprobed, so `rotate.py` deletes
        the old key first."""
        ...

    def delete_key(self, family: str) -> None:
        """Delete by alias: the caller may not hold the raw key (it never
        persists past the process that minted it, if that process is not
        this one), so alias is the only handle guaranteed to work."""
        ...

    def read_spend(self, key: str) -> Spend:
        """This key's own spend (contract 05 §7).

        The key presents ITSELF as the bearer. `POST /key/info` answers 405
        and a key value may never ride in a URL (invariant 13), so bearing
        the key is the only read left."""
        ...


class HttpLiteLLMKeys:
    def __init__(self, base_url: str, master_key: str, client: httpx.Client | None = None) -> None:
        if not master_key:
            raise LiteLLMError("LITELLM_MASTER_KEY is not set")

        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)

    def _send(
        self,
        what: str,
        method: str,
        path: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Every request leaves through here, so a LiteLLM that is AWAY is a
        `LiteLLMError` like any other refusal, and every caller already turns
        that into a fault the status document carries.

        `sudo systemctl restart ai-stack` takes LiteLLM away for about half a
        minute. A raw `httpx.ConnectError` from a spend read then would reach
        no caller that catches it and leave `serve` through the top of its
        loop. The message names the error's class and nothing the error
        carries: its request holds a bearer (invariant 13)."""
        try:
            return self._client.request(
                method, f"{self._base}{path}", headers=headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise LiteLLMError(f"{what} failed: {type(exc).__name__}") from exc

    def ensure_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        payload: dict[str, Any] = {
            "key_alias": key_alias(family),
            "models": models,
            "max_budget": budget_usd_per_day,
            "budget_duration": BUDGET_DURATION,
        }
        what = f"key mint for {family!r}"
        resp = self._send(what, "POST", GENERATE_PATH, self._headers, payload)
        # Never echo resp.text: on HTTP 200 it IS (or holds) the key just
        # minted, and logging this exception would put it in the journal.
        if resp.status_code != 200:
            raise LiteLLMError(f"key mint failed for {family!r}: HTTP {resp.status_code}")

        body = _json_object(what, resp)
        key = body.get("key", "")
        if not isinstance(key, str) or not key.startswith("sk-"):
            raise LiteLLMError(f"key mint for {family!r} returned no usable key")

        return key

    def update_key(self, key: str, models: list[str], budget_usd_per_day: float) -> None:
        payload: dict[str, Any] = {
            "key": key,
            "models": models,
            "max_budget": budget_usd_per_day,
            "budget_duration": BUDGET_DURATION,
        }
        resp = self._send("key update", "POST", UPDATE_PATH, self._headers, payload)
        if resp.status_code != 200:
            raise LiteLLMError(f"key update failed: HTTP {resp.status_code}")

    def rotate_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        return self.ensure_key(family, models, budget_usd_per_day)

    def delete_key(self, family: str) -> None:
        resp = self._send(
            f"key delete for {family!r}",
            "POST",
            DELETE_PATH,
            self._headers,
            {"key_aliases": [key_alias(family)]},
        )
        if resp.status_code != 200:
            raise LiteLLMError(f"key delete failed for {family!r}: HTTP {resp.status_code}")

    def read_spend(self, key: str) -> Spend:
        """GET, with the family key as its own bearer. The master key is
        not used here: `POST /key/info` answers 405, and passing `?key=` to
        the GET form would put the key in a URL (invariant 13)."""
        resp = self._send("key info", "GET", INFO_PATH, {"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            raise LiteLLMError(f"key info failed: HTTP {resp.status_code}")

        body = _json_object("key info", resp)
        info = body.get("info")
        fields = cast("dict[str, Any]", info) if isinstance(info, dict) else body
        return Spend(
            spend_usd=_number(fields.get("spend")) or 0.0,
            budget_usd=_number(fields.get("max_budget")),
        )


def _json_object(what: str, resp: httpx.Response) -> dict[str, Any]:
    """The body as an object, or a `LiteLLMError` that never echoes it: a
    proxy in front of a LiteLLM that is starting answers 200 with a page,
    and on a mint the body may hold the key."""
    try:
        body: object = resp.json()
    except ValueError:
        raise LiteLLMError(f"{what} answered a body that is not JSON") from None

    if not isinstance(body, dict):
        raise LiteLLMError(f"{what} answered JSON that is not an object")

    return cast("dict[str, Any]", body)


def _number(value: Any) -> float | None:
    """A LiteLLM field that is absent, null or not a number answers None.
    `spend` missing is unknown, not zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value)


class FakeLiteLLMKeys:
    """In memory. `.minted` counts calls, for a test to check `ensure_key`
    is not repeated needlessly; `.deleted` records every alias asked for.

    `HttpLiteLLMKeys` is safe to share between threads because
    `httpx.Client` is. This one has a lock instead: the real families
    converge side by side, so a test's fake is called from
    several threads and `+= 1` is not one instruction."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}  # alias -> fake key
        self.budgets: dict[str, tuple[list[str], float]] = {}  # alias -> (models, budget)
        self.spends: dict[str, float] = {}  # key -> spend so far
        self.minted = 0
        self.deleted: list[str] = []
        self._sequence = 0
        self._mutex = threading.Lock()

    def ensure_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        with self._mutex:
            self._sequence += 1
            alias = key_alias(family)
            key = f"sk-fake-{alias}-{self._sequence}"
            self.keys[alias] = key
            self.budgets[alias] = (list(models), budget_usd_per_day)
            self.minted += 1
            return key

    def update_key(self, key: str, models: list[str], budget_usd_per_day: float) -> None:
        with self._mutex:
            for alias, existing in self.keys.items():
                if existing == key:
                    self.budgets[alias] = (list(models), budget_usd_per_day)
                    return

        raise LiteLLMError(f"update_key: no such key {key!r}")

    def rotate_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        return self.ensure_key(family, models, budget_usd_per_day)

    def delete_key(self, family: str) -> None:
        with self._mutex:
            alias = key_alias(family)
            self.deleted.append(alias)
            self.keys.pop(alias, None)
            self.budgets.pop(alias, None)

    def read_spend(self, key: str) -> Spend:
        with self._mutex:
            for alias, existing in self.keys.items():
                if existing == key:
                    return Spend(self.spends.get(key, 0.0), self.budgets[alias][1])

        raise LiteLLMError("read_spend: no such key")
