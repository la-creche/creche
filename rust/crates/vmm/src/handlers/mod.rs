//! The handlers of the host API routes (decision CH25). Each handler
//! implements `Serve` or `ServeUpgrade` of `creche-wire` for its route.

pub mod acp;
pub mod drop;
