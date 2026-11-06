//! The sleep gate over each facade state and each condition (decisions
//! CH2, CH19 (6), CH32 rule 6, CH43 rule 8, CH8-6).

use std::num::NonZeroU32;
use std::time::{Duration, Instant};

use creche_vmm_store::sleep_gate::{Activity, IDLE_TIMEOUT};
use creche_wire::{
    FacadeExited, FacadeNotStarted, FacadeRunning, FacadeStarting, FacadeState, StartCount,
};

const MS: Duration = Duration::from_millis(1);

// `cfg(test)` lets the helper use `expect` (rust/clippy.toml).
#[cfg(test)]
fn starts() -> StartCount {
    StartCount::try_from(1).expect("1 is a start count")
}

fn pid() -> NonZeroU32 {
    NonZeroU32::MIN.saturating_add(1)
}

/// One facade state of each variant, in the order of the type.
fn every_state() -> [FacadeState; 4] {
    [
        FacadeState::NotStarted(FacadeNotStarted {}),
        FacadeState::Starting(FacadeStarting {
            last_turn_end: None,
            pid: pid(),
            starts: starts(),
        }),
        FacadeState::Running(FacadeRunning {
            last_turn_end: None,
            pid: pid(),
            starts: starts(),
            turn_running: false,
        }),
        FacadeState::Exited(FacadeExited {
            last_turn_end: None,
            starts: starts(),
        }),
    ]
}

/// A VM that may sleep when its facade state is `running`: no turn, no
/// door link, last used at `last_use`.
fn quiet(facade: Option<&FacadeState>, last_use: Instant) -> Activity<'_> {
    Activity {
        facade,
        turn_seen: false,
        turn_running: false,
        door_open: false,
        last_use,
    }
}

#[test]
fn idle_timeout_is_five_minutes() {
    assert_eq!(IDLE_TIMEOUT, Duration::from_secs(300));
}

#[test]
fn only_running_is_up_without_a_turn_event() {
    let last_use = Instant::now();
    let late = last_use + IDLE_TIMEOUT + MS;
    for state in &every_state() {
        let vm = quiet(Some(state), last_use);
        let running = matches!(state, FacadeState::Running(_));
        assert_eq!(vm.up(), running, "{state:?}");
        assert_eq!(vm.may_sleep(late), running, "{state:?}");
        assert_eq!(vm.may_evict(), running, "{state:?}");
    }
}

#[test]
fn a_turn_event_after_the_ready_makes_each_state_up() {
    let last_use = Instant::now();
    let late = last_use + IDLE_TIMEOUT + MS;
    for state in &every_state() {
        let vm = Activity {
            turn_seen: true,
            ..quiet(Some(state), last_use)
        };
        assert!(vm.up(), "{state:?}");
        assert!(vm.may_sleep(late), "{state:?}");
        assert!(vm.may_evict(), "{state:?}");
    }
}

#[test]
fn no_ready_is_never_up() {
    let last_use = Instant::now();
    let vm = Activity {
        turn_seen: true,
        ..quiet(None, last_use)
    };
    assert!(!vm.up());
    assert!(!vm.may_sleep(last_use + IDLE_TIMEOUT + MS));
    assert!(!vm.may_evict());
}

#[test]
fn a_running_turn_blocks_sleep_and_eviction() {
    let [_, _, running, _] = every_state();
    let last_use = Instant::now();
    let vm = Activity {
        turn_running: true,
        ..quiet(Some(&running), last_use)
    };
    assert!(!vm.may_sleep(last_use + IDLE_TIMEOUT + MS));
    assert!(!vm.may_evict());
}

#[test]
fn the_turn_flag_inside_running_is_not_read() {
    // A turn end event clears `turn_running` and leaves the facade state
    // of the ready as it was (decision CH43 rule 8).
    let running = FacadeState::Running(FacadeRunning {
        last_turn_end: None,
        pid: pid(),
        starts: starts(),
        turn_running: true,
    });
    let last_use = Instant::now();
    let vm = quiet(Some(&running), last_use);
    assert!(vm.may_sleep(last_use + IDLE_TIMEOUT + MS));
    assert!(vm.may_evict());
}

#[test]
fn an_open_door_link_blocks_sleep_but_not_eviction() {
    let [_, _, running, _] = every_state();
    let last_use = Instant::now();
    let vm = Activity {
        door_open: true,
        ..quiet(Some(&running), last_use)
    };
    assert!(!vm.may_sleep(last_use + IDLE_TIMEOUT + MS));
    assert!(vm.may_evict());
}

#[test]
fn sleep_waits_past_the_idle_timeout() {
    let [_, _, running, _] = every_state();
    let last_use = Instant::now();
    let vm = quiet(Some(&running), last_use);
    assert!(!vm.may_sleep(last_use));
    assert!(!vm.may_sleep(last_use + IDLE_TIMEOUT));
    assert!(vm.may_sleep(last_use + IDLE_TIMEOUT + MS));
    // A clock read before the last use counts as no idle time.
    assert!(!vm.may_sleep(last_use.checked_sub(MS).unwrap_or(last_use)));
}

#[test]
fn eviction_does_not_wait_for_the_idle_timeout() {
    let [_, _, running, _] = every_state();
    let vm = quiet(Some(&running), Instant::now());
    assert!(vm.may_evict());
}
