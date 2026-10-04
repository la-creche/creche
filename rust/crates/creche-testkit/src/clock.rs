//! A clock that a test moves by hand.
//!
//! Each body here is a stub. `AGENTS.md` of this crate lists the stubs and
//! the packet that writes them.

use std::time::{Duration, SystemTime};

use creche_runtime::clock::{Clock, Monotonic};

/// A clock that stands still until the test moves it.
///
/// The wall time and the monotonic time move together with
/// [`FixedClock::advance`]. [`FixedClock::set`] moves only the wall time, as
/// an operator does who sets the clock of the host.
#[derive(Debug)]
pub struct FixedClock(());

impl FixedClock {
    /// A clock that shows the wall time `at`. Its monotonic time starts at
    /// zero.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    #[must_use]
    pub fn new(at: SystemTime) -> Self {
        todo!()
    }

    /// Sets the wall time to `at`. The monotonic time does not move.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub fn set(&self, at: SystemTime) {
        todo!()
    }

    /// Moves the wall time and the monotonic time forward by `by`.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub fn advance(&self, by: Duration) {
        todo!()
    }
}

impl Clock for FixedClock {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    fn now(&self) -> SystemTime {
        todo!()
    }

    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    fn monotonic(&self) -> Monotonic {
        todo!()
    }
}

/// A clock that reads the paused time of `tokio`.
///
/// A test that pauses the time of `tokio` moves this clock and each timer
/// together, with `tokio::time::advance`.
#[derive(Debug)]
pub struct PausedClock(());

impl PausedClock {
    /// A clock that shows the wall time `at` now. Call it inside a runtime
    /// whose time is paused.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    #[must_use]
    pub fn new(at: SystemTime) -> Self {
        todo!()
    }
}

impl Clock for PausedClock {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    fn now(&self) -> SystemTime {
        todo!()
    }

    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    fn monotonic(&self) -> Monotonic {
        todo!()
    }
}
