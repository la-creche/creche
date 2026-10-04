//! Random bytes that a test knows.
//!
//! Code of a service takes a `&dyn Entropy` and never reads the random
//! device itself. A test gives [`CountingEntropy`], and the test then knows
//! each id and each token that the code mints.
//!
//! This type has no Python origin. No Python test helper gives a service its
//! random bytes.

use std::sync::{Mutex, PoisonError};

use creche_runtime::entropy::{Entropy, EntropyError};

/// A source that fills bytes from a count. A test then knows each id and
/// each token that the code mints.
///
/// The first byte that the source gives is 0, the next one is 1, and each
/// later byte is one more. The count goes from 255 back to 0. The count
/// continues from one call of `fill` to the next call, so two mints never
/// get the same bytes.
///
/// The type has no Python origin.
///
/// ```
/// use std::sync::Mutex;
///
/// use creche_runtime::entropy::Entropy;
/// use creche_testkit::entropy::CountingEntropy;
///
/// let entropy = CountingEntropy::new();
/// let mut first = [0xff_u8; 4];
/// let mut second = [0xff_u8; 2];
/// entropy.fill(&mut first)?;
/// entropy.fill(&mut second)?;
///
/// assert_eq!(first, [0, 1, 2, 3]);
/// assert_eq!(second, [4, 5]);
/// # Ok::<(), creche_runtime::entropy::EntropyError>(())
/// ```
///
/// Code outside this module builds the source only with
/// [`CountingEntropy::new`], so the count of a new source is zero:
///
/// ```compile_fail,E0451
/// use std::sync::Mutex;
///
/// use creche_runtime::entropy::Entropy;
/// use creche_testkit::entropy::CountingEntropy;
///
/// let entropy = CountingEntropy {
///     next: Mutex::new(7),
/// };
/// ```
#[derive(Debug)]
pub struct CountingEntropy {
    /// The byte that the source gives next.
    next: Mutex<u8>,
}

impl CountingEntropy {
    /// A source whose count starts at zero.
    #[must_use]
    pub fn new() -> Self {
        Self {
            next: Mutex::new(0),
        }
    }
}

impl Default for CountingEntropy {
    fn default() -> Self {
        Self::new()
    }
}

impl Entropy for CountingEntropy {
    /// Fills `bytes` from the count. The function gives no error.
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
        // A test that panicked with the lock leaves a poisoned lock. The
        // count under it is valid after each statement, so the function
        // takes it.
        let mut next = self.next.lock().unwrap_or_else(PoisonError::into_inner);

        for byte in bytes {
            *byte = *next;
            *next = next.wrapping_add(1);
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    #[test]
    fn the_bytes_count_up_from_zero() {
        let entropy = CountingEntropy::new();
        let mut bytes = [0xff_u8; 10];
        entropy.fill(&mut bytes).unwrap();

        assert_eq!(bytes, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    }

    #[test]
    fn the_count_continues_from_one_fill_to_the_next() {
        let entropy = CountingEntropy::default();
        let mut first = [0_u8; 10];
        let mut second = [0_u8; 10];
        entropy.fill(&mut first).unwrap();
        entropy.fill(&mut second).unwrap();

        assert_eq!(first, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
        assert_eq!(second, [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]);
    }

    #[test]
    fn the_count_goes_from_255_back_to_zero() {
        let entropy = CountingEntropy::new();
        let mut bytes = [0_u8; 258];
        entropy.fill(&mut bytes).unwrap();

        assert_eq!(bytes[254..], [254, 255, 0, 1]);
    }

    #[test]
    fn an_empty_fill_moves_no_count() {
        let entropy = CountingEntropy::new();
        let mut one = [0xff_u8; 1];
        entropy.fill(&mut []).unwrap();
        entropy.fill(&mut one).unwrap();

        assert_eq!(one, [0]);
    }

    #[test]
    fn two_sources_give_the_same_bytes() {
        let mut first = [0_u8; 16];
        let mut second = [0_u8; 16];
        CountingEntropy::new().fill(&mut first).unwrap();
        CountingEntropy::new().fill(&mut second).unwrap();

        assert_eq!(first, second);
    }

    #[test]
    fn a_source_is_usable_behind_a_trait_object_in_two_threads() {
        let entropy: Arc<dyn Entropy> = Arc::new(CountingEntropy::new());
        let shared = Arc::clone(&entropy);

        let seen = std::thread::spawn(move || {
            let mut bytes = [0_u8; 3];
            shared.fill(&mut bytes).unwrap();

            bytes
        })
        .join()
        .unwrap();
        let mut next = [0_u8; 1];
        entropy.fill(&mut next).unwrap();

        assert_eq!(seen, [0, 1, 2]);
        assert_eq!(next, [3]);
        assert!(format!("{entropy:?}").starts_with("CountingEntropy"));
    }

    #[test]
    fn a_source_is_usable_after_a_panic_under_its_lock() {
        let entropy = Arc::new(CountingEntropy::new());
        let held = Arc::clone(&entropy);
        let panicked = std::thread::spawn(move || {
            let _next = held.next.lock().unwrap();
            panic!("the test poisons the lock");
        })
        .join();
        let mut bytes = [0xff_u8; 2];

        assert!(panicked.is_err());
        assert!(entropy.next.is_poisoned());

        entropy.fill(&mut bytes).unwrap();

        assert_eq!(bytes, [0, 1]);
    }
}
