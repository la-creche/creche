"""The PEP clears its own faults without traffic (contract 04 §1.6 rule 7).

`grants_stale` has `blocks_turns: true` (contract 05 §3.3), and `attendance`
answers `family_degraded` to every turn of a family that carries one. If only
a CALL re-read a good grant file, those two rules would close a loop:

    grant file missing for one second
      -> the PEP raises grants_stale
      -> attendance refuses every turn of that family
      -> no call reaches the PEP
      -> nothing re-reads the grant file
      -> the fault never clears

A family broken for one second would then stay down until somebody called the
PEP by hand. That breaks invariant 9: a permission change needs one action and
no manual apply.

This module is the traffic-free half. It re-reads the grant file of the
families that carry a fault, and nothing else: on a healthy fleet it costs no
`stat` at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Final

log = logging.getLogger("chaperone.fault_sweep")

#: Two bounds pick it. Above: contract 04 §1.6 rule 4 rewrites an open fault
#: every 30 s, so a slower sweep would let the refresh, not the clear, decide
#: how long a family stays blocked. Below: `caregiver`'s reconcile loop looks
#: every 2 s (`caregiver.loop.POLL_INTERVAL_S`), and a sweep faster than
#: that would re-`stat` the same unchanged file several times per look. 5 s
#: sits between them, so a family unblocks within one look of the grant file
#: landing. `PEP_FAULT_SWEEP_INTERVAL_S` overrides it per host.
FAULT_SWEEP_INTERVAL_S: Final = 5.0

#: What one pass answers: the families whose fault it cleared.
Sweep = Callable[[], frozenset[str]]


async def fault_sweep_loop(sweep: Sweep, interval_s: float) -> None:
    """Run `sweep` every `interval_s` until the task is cancelled.

    Every failure is logged and the next pass still runs. This loop holds no
    call and decides nothing, so a sweep that raises must not take down the
    process that holds every upstream credential. `Exception` and not
    `BaseException`: a cancellation is how the lifespan stops this task.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            cleared = sweep()
        except Exception:
            log.exception("fault sweep failed")
            continue

        if cleared:
            log.info("fault sweep cleared grants_stale for %s", ", ".join(sorted(cleared)))
