//! When a VM may sleep: the vmm snapshots it and stops its Cloud
//! Hypervisor (decisions CH2, CH19 (6)).
//!
//! The gate is a pure predicate over what lifecycle knows of one VM. It
//! reads no file and no clock. Lifecycle passes in the facade state of
//! the last `ControlReady`, the turn and door link state and the last use
//! of the VM.
//!
//! The facade is up only in `running` (decision CH32 rule 6). A
//! `starting` facade did not open its local link, so guest init cannot
//! see its turns, and a snapshot could hold a turn that nobody sees. No
//! line reports a change of the facade state yet. So until the phase 3
//! guest init PR adds one, a turn event after the last `ControlReady`
//! also counts as up (decision CH43 rule 8). A VM after a cold boot
//! reports `not_started` or `starting`, and without that rule it would
//! never sleep.
//!
//! Two answers use the gate:
//!
//! 1. `may_sleep`: the idle sleep of decision CH2. The facade is up, no
//!    turn runs, no door link is open, and the VM is idle past
//!    `IDLE_TIMEOUT`.
//! 2. `may_evict`: the eviction of decision CH8-6, under RAM pressure.
//!    The facade is up and no turn runs. The idle time does not count,
//!    because admission snapshots an idle VM sooner than the timeout. An
//!    open door link does not count either: the vmm closes it first, and
//!    the client reconnects with `session/load` (decision CH19 (6)).
//!    Admission (`creche_vmm_confine::admission::decide`, PR C9) orders
//!    the evictable VMs by `last_use`, least recently used first.

use std::time::{Duration, Instant};

use creche_wire::FacadeState;

/// How long a VM stays idle before it sleeps.
// Decision CH36 G12 option A: 5 minutes, a named constant. The phase 8
// measure resets it (decision CH2 asks to measure the idle timeout).
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(5 * 60);

/// What lifecycle knows of one running VM.
#[derive(Debug, Clone, Copy)]
pub struct Activity<'a> {
    /// The facade state of the last `ControlReady` of the current control
    /// link, or `None` before the first one
    /// (`creche_vmm::control::ControlState::facade`).
    pub facade: Option<&'a FacadeState>,
    /// True when a turn event arrived after the last `ControlReady`
    /// (decision CH43 rule 8).
    pub turn_seen: bool,
    /// True when a turn runs (`ControlState::turn_running`). The gate
    /// reads this, not the flag inside `FacadeState::Running`, because
    /// turn events change only this one.
    pub turn_running: bool,
    /// True when a door link (the ACP Upgrade) of the VM is open.
    pub door_open: bool,
    /// The last use of the VM. The smallest is the least recently used.
    // CONTRACT-QUESTION: no decision row names the events that set the
    // last use. The narrowest reading: the VM start or restore, each turn
    // event, and each open and each close of a door link set it.
    pub last_use: Instant,
}

impl Activity<'_> {
    /// Whether the facade is up (decisions CH32 rule 6, CH43 rule 8).
    #[must_use]
    pub fn up(&self) -> bool {
        match self.facade {
            Some(FacadeState::Running(_)) => true,
            Some(_) => self.turn_seen,
            None => false,
        }
    }

    /// Whether the VM may sleep at `now` (decisions CH2, CH19 (6)).
    #[must_use]
    pub fn may_sleep(&self, now: Instant) -> bool {
        self.may_evict()
            && !self.door_open
            && now.saturating_duration_since(self.last_use) > IDLE_TIMEOUT
    }

    /// Whether admission may evict the VM when the RAM cap needs room
    /// (decisions CH8-6, CH19 (6)).
    // CONTRACT-QUESTION: no decision row says whether an idle VM with no
    // door link goes before an older one with a door link open. The
    // narrowest reading: one order by `last_use` alone, as admission has.
    #[must_use]
    pub fn may_evict(&self) -> bool {
        self.up() && !self.turn_running
    }
}
