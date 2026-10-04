//! A reader of the vector files under `vectors/data`, for a differential
//! test.
//!
//! A vector is one input and what the Python implementation did with it.
//! `vectors/README.md` holds the file format. `rust/AGENTS.md`, "The
//! differential test", says how a test uses a reader.
//!
//! `creche-contracts` holds a private reader with the same checks. This one
//! is public, so each crate of the workspace can use it.
//!
//! Each body here is a stub, and each type is empty. `AGENTS.md` of this
//! crate lists the stubs and the packet that writes them. That packet gives
//! each type its fields.

use std::error::Error;
use std::fmt;

/// Why the reader cannot give a vector file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VectorsError {
    /// The file under `vectors/data`.
    pub file: String,
    /// What is wrong with the file.
    pub reason: String,
}

impl fmt::Display for VectorsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "vectors/data/{}: {}", self.file, self.reason)
    }
}

impl Error for VectorsError {}

/// One row of `vectors/data/index.json`: a surface, its file and its counts.
#[derive(Debug, Clone)]
pub struct IndexRow(());

/// One vector file: each vector of one surface.
#[derive(Debug, Clone)]
pub struct Surface(());

/// One vector: an input and what the Python code did with it.
#[derive(Debug, Clone)]
pub struct Vector(());

/// The input of a vector, in one of the five forms of `vectors/README.md`.
#[derive(Debug, Clone)]
pub struct Input(());

/// A marker object of `vectors/README.md`: a value that JSON cannot hold as
/// it is.
#[derive(Debug, Clone)]
pub struct Marker(());

/// Each row of `vectors/data/index.json`.
///
/// # Errors
///
/// [`VectorsError`] for an index that the reader cannot read, and for a
/// format that is not 1.
#[expect(
    clippy::todo,
    reason = "skeleton: packet foundation-testkit writes this body"
)]
pub fn index() -> Result<Vec<IndexRow>, VectorsError> {
    todo!()
}

/// The vector file of the surface `name`.
///
/// # Errors
///
/// [`VectorsError`] for a surface that the index does not hold, for a format
/// that is not 1, and for a count of vectors that differs from the index.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-testkit writes this body"
)]
pub fn surface(name: &str) -> Result<Surface, VectorsError> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_names_the_file_and_the_reason() {
        let error = VectorsError {
            file: String::from("index.json"),
            reason: String::from("the format is 2, and the reader takes 1"),
        };

        assert_eq!(
            error.to_string(),
            "vectors/data/index.json: the format is 2, and the reader takes 1"
        );
    }
}
