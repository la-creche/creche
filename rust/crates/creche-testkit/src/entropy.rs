//! Random bytes that a test knows.
//!
//! Each body here is a stub. `AGENTS.md` of this crate lists the stubs and
//! the packet that writes them.

use creche_runtime::entropy::{Entropy, EntropyError};

/// A source that fills bytes from a count. A test then knows each id and
/// each token that the code mints.
#[derive(Debug)]
pub struct CountingEntropy(());

impl CountingEntropy {
    /// A source whose count starts at zero.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    #[must_use]
    pub fn new() -> Self {
        todo!()
    }
}

impl Default for CountingEntropy {
    fn default() -> Self {
        Self::new()
    }
}

impl Entropy for CountingEntropy {
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
        todo!()
    }
}
