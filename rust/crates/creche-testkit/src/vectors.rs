//! The reader of the vector files, for a differential test.
//!
//! The crate `creche-vectors` holds the reader and its tests. This module
//! gives each item of that crate under the path `creche_testkit::vectors`,
//! so a crate that has the testkit needs no second dev dependency.
//! `rust/AGENTS.md`, "The differential test", says how a test uses the
//! reader.
//!
//! ```
//! use creche_testkit::vectors::{self, IndexRow, Outcome, Surface};
//!
//! let rows: Vec<IndexRow> = vectors::index()?;
//! let row = rows.first().ok_or("the index holds no surface")?;
//! let surface: Surface = vectors::surface(row.surface())?;
//!
//! assert_eq!(surface.vectors().len(), row.vectors());
//! assert!(row.count(Outcome::Accepted) <= row.vectors());
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```
//!
//! The module has no Python origin, as the reader has none.

pub use creche_vectors::*;
