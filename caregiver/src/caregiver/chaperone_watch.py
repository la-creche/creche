"""Something watches the PEP (contract 05 §3.3 `pep_unreachable`, §2.1's
`pep` block).

Every family depends on the PEP for every tool call, every delegation and
every `enqueue`. A PEP that is down while nothing watches it leaves every
family's document `in_sync` with no fault, and the noticeboard shows a
healthy fleet.

    every <interval>  ->  GET /healthz  ->  200?  -> the fault clears, now
                                         \\-> no  -> failing for > threshold?
                                                    -> the fault is raised

Four rules shape this file.

1. **One probe per interval for the WHOLE fleet**, never one per family.
   Eleven families asking the same question eleven times answers it no
   better.
2. **One flap is not an outage.** `agent-control-deploy` restarts the PEP
   on every deploy and `/healthz` answers again in about 10 to 15 s. The
   fault waits out `PEP_UNREACHABLE_AFTER_S` of unbroken silence.
3. **Nothing escapes.** A probe that raises, hangs or answers rubbish is a
   probe that did not answer 200. `litellm_keys._send` shows the rule this
   package already follows: one unmapped `httpx` error must not end
   `caregiver`.
4. **No address means the watch is OFF, and the document says so.** A
   watch that is silently off is the same bug wearing the fix's clothes.

It is a leaf: `clock.py` for the stamp and `faults.py` for the entry, the
same two `status.py` takes."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

import httpx

from .clock import now_rfc3339
from .faults import FaultEntry

log = logging.getLogger("caregiver.chaperone_watch")

#: Contract 05 §3.3's code. It is the same word the sandbox bridge already
#: puts in a tool error when a call cannot leave the VM
#: (`playpen/bridge/pep.ts`, `CallFailure.Unreachable`), so an operator
#: reading the noticeboard and an operator reading a turn see one name.
PEP_UNREACHABLE: Final = "pep_unreachable"

#: The PEP answers this with 200 when it is up (`chaperone/src/chaperone/app.py`).
#: It holds no secret, so the probe carries no bearer (invariant 13).
HEALTH_PATH: Final = "/healthz"

HTTP_OK: Final = 200

#: How often the PEP is asked. Ten seconds is short enough that nine
#: probes fall inside the 90-second threshold, so a real outage is never
#: missed for want of evidence, and long enough that the whole fleet costs
#: six requests a minute against a path that returns a two-key object.
PEP_PROBE_INTERVAL_S: Final = 10.0

#: How long the PEP must stay silent before the fleet is faulted.
#:
#: Ninety seconds, which is contract 05 §2 rule 5's own number for "long
#: enough that a reader must stop believing". A deploy's restart takes
#: about 10 to 15 s, so this is six times the longest flap measured, and
#: an outage is on every family's document inside two minutes.
#: `--pep-unreachable-after-s` moves it.
PEP_UNREACHABLE_AFTER_S: Final = 90.0

#: The probe's own ceiling. Short on purpose: the probe runs on the loop
#: thread, and a family's pass runs in its own (`loop.Passes`), so the
#: worst this can cost is two seconds of dispatch latency once per
#: interval — and only while the PEP is not answering, which is exactly
#: when a fault matters more than a second of latency.
PEP_PROBE_TIMEOUT_S: Final = 2.0

#: A reading before any probe, so the first poll always runs one.
NEVER: Final = float("-inf")


class PepReach(StrEnum):
    """What the watch can say about the PEP. Three, not two: "nobody
    asked" is a different fact from "it answered"."""

    OFF = "off"
    OK = "ok"
    UNREACHABLE = "unreachable"


class PepProbe(Protocol):
    """One question: did `/healthz` answer 200 just now?

    It answers a bool and raises nothing. Every implementation maps its
    own transport errors, because the caller runs inside the reconcile
    loop and an exception there stops eleven families converging."""

    def healthy(self) -> bool: ...


class HttpPepProbe:
    """`GET <base>/healthz`, with a short timeout and no bearer."""

    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        timeout_s: float = PEP_PROBE_TIMEOUT_S,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}{HEALTH_PATH}"
        self._timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)

    def healthy(self) -> bool:
        """True only on a 200. Every other answer, and every transport
        error, is "it did not answer": a proxy in front of a PEP that is
        starting answers 204 or 503, and neither is the PEP."""
        try:
            answer = self._client.get(self._url, timeout=self._timeout_s)
        except httpx.HTTPError as exc:
            # The class name and nothing the error carries: its request
            # holds a URL this service chose, and the rule is the same one
            # `litellm_keys._send` follows.
            log.debug("chaperone probe: %s", type(exc).__name__)
            return False

        return answer.status_code == HTTP_OK


class FakePepProbe:
    """In memory. `up` moves between polls, `calls` counts them, so a test
    can prove one probe per interval rather than one per family."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.calls = 0

    def healthy(self) -> bool:
        self.calls += 1
        return self.up


class RaisingPepProbe:
    """A probe that breaks its own contract, for the test that proves the
    watch survives one anyway."""

    def __init__(self) -> None:
        self.calls = 0

    def healthy(self) -> bool:
        self.calls += 1
        raise RuntimeError("probe raised")


@dataclass(frozen=True)
class PepReport:
    """One reading of the watch, frozen so a pass may publish the same
    verdict into several documents and they cannot disagree."""

    reach: PepReach
    url: str
    checked_at: str | None = None
    unreachable_since: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Contract 05 §2.1's `pep` block."""
        return {
            "watch": str(self.reach),
            "url": self.url,
            "checked_at": self.checked_at,
            "unreachable_since": self.unreachable_since,
        }

    def fault(self) -> FaultEntry | None:
        """Contract 05 §3.3's entry, or None when there is nothing to
        report. `since` is when the PEP was first seen silent, not when
        the threshold ran out: the outage began at the first failure."""
        if self.reach is not PepReach.UNREACHABLE:
            return None

        return FaultEntry(
            code=PEP_UNREACHABLE,
            blocks_turns=False,
            since=self.unreachable_since or now_rfc3339(),
            source="managerd",
            detail={
                "url": self.url,
                "message": (
                    f"{self.url}{HEALTH_PATH} has not answered {HTTP_OK} since "
                    f"{self.unreachable_since}; no tool call, delegation or enqueue "
                    "can be authorized while it does not"
                ),
            },
        )


def unwatched() -> PepReport:
    """What a caller that does not watch publishes: `off`, honestly.

    `apply-once` and `reconcile-once` are one pass in one process. Neither
    has an interval to probe over, so neither may claim the PEP answered."""
    return PepReport(reach=PepReach.OFF, url="")


class PepWatch:
    """The fleet's one PEP probe, its threshold, and its current verdict.

    A mutex, because `poll` runs on the loop thread while a family's pass
    reads `report` from its own (`loop.Passes`)."""

    def __init__(
        self,
        probe: PepProbe | None,
        *,
        url: str = "",
        interval_s: float = PEP_PROBE_INTERVAL_S,
        threshold_s: float = PEP_UNREACHABLE_AFTER_S,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], str] = now_rfc3339,
    ) -> None:
        self._probe = probe
        self._url = url
        self._interval_s = interval_s
        self._threshold_s = threshold_s
        self._monotonic = monotonic
        self._wall = wall
        self._mutex = threading.Lock()
        self._probed_at = NEVER
        self._checked_at: str | None = None
        self._failing_since: float | None = None
        self._failing_since_wall: str | None = None
        # Optimistic before the first probe, and `checked_at: null` says
        # nobody has answered yet. The alternative is to call a PEP that
        # is up `unreachable` for the first interval after every restart
        # of this service.
        self._reach = PepReach.OFF if probe is None else PepReach.OK

    def poll(self) -> bool:
        """Probe, if the interval has run out. Answers whether the VERDICT
        moved, which is the loop's signal to put every family through a
        pass: a fault that is computed and never published is a fault
        nobody sees, and a restamp carries the old content forward."""
        if self._probe is None:
            return False

        with self._mutex:
            if self._monotonic() - self._probed_at < self._interval_s:
                return False

            self._probed_at = self._monotonic()
            was = self._reach

        healthy = self._ask()
        with self._mutex:
            self._record(healthy)
            return self._reach is not was

    def report(self) -> PepReport:
        with self._mutex:
            return PepReport(
                reach=self._reach,
                url=self._url,
                checked_at=self._checked_at,
                unreachable_since=self._failing_since_wall
                if self._reach is PepReach.UNREACHABLE
                else None,
            )

    def _ask(self) -> bool:
        """Rule 3. A `PepProbe` promises to raise nothing, and this is
        what happens when one breaks that promise: the loop keeps going
        and the journal says which class of error it was."""
        if self._probe is None:
            return False

        try:
            return self._probe.healthy()
        except Exception:
            log.exception("chaperone probe raised; counting it as no answer")
            return False

    def _record(self, healthy: bool) -> None:
        """Called under the mutex. An answer clears the outage at once —
        no turn, no restart and no human is needed — and silence raises
        the fault only once it has lasted longer than the threshold."""
        self._checked_at = self._wall()
        if healthy:
            if self._failing_since is not None:
                log.info("chaperone answered again after %s", self._failing_since_wall)

            self._failing_since = None
            self._failing_since_wall = None
            self._reach = PepReach.OK
            return

        if self._failing_since is None:
            self._failing_since = self._monotonic()
            self._failing_since_wall = self._checked_at

        silent_s = self._monotonic() - self._failing_since
        if silent_s <= self._threshold_s:
            # Rule 2: one flap is not an outage.
            return

        if self._reach is not PepReach.UNREACHABLE:
            log.warning(
                "chaperone has not answered %s for %.0fs; faulting every family",
                HEALTH_PATH,
                silent_s,
            )

        self._reach = PepReach.UNREACHABLE
