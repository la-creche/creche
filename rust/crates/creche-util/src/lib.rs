//! The shared helpers of the workspace: each helper that two crates use, or
//! two modules that hold two contracts.
//!
//! Each function here is a pure function: its result depends on its
//! arguments only. It follows a published rule, and its doc comment names
//! that rule. The crate has no dependency, and no function here checks a rule
//! of a contract. The file `AGENTS.md` of the crate holds the rules for what
//! the crate takes.
//!
//! | Module | Holds |
//! |---|---|
//! | [`sha256`] | the SHA-256 digest of FIPS 180-4 |
//! | [`hex`] | bytes as lower-case hex text |
//! | [`pytext`] | the white space rules of `str` in Python |

pub mod hex;
pub mod pytext;
pub mod sha256;
