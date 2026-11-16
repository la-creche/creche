//! The tests of the public interface of creche-ci-signer. They read the
//! sanitized recordings of Forgejo 17.0.1 in `tests/fixtures/` and the
//! shared vectors in `vectors/`. One test binary holds them all, so the
//! helpers of `common` serve each module.
#![cfg(test)]

mod common;
mod eligibility;
mod exchange;
mod issue;
mod pairs;
mod recorded;
mod sanitized;
mod vectors;
