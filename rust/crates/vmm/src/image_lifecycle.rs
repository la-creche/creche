//! The read and write gate of digest-store image use.

use std::sync::{Arc, PoisonError, RwLock, RwLockReadGuard, RwLockWriteGuard};

/// Starts hold a read gate through image selection, checks and launch.
/// Imports and collection hold the write gate.
#[derive(Debug, Clone, Default)]
pub(crate) struct ImageLifecycle(Arc<RwLock<()>>);

impl ImageLifecycle {
    /// A start gate.
    pub(crate) fn read(&self) -> RwLockReadGuard<'_, ()> {
        self.0.read().unwrap_or_else(PoisonError::into_inner)
    }

    /// An import or collection gate.
    pub(crate) fn write(&self) -> RwLockWriteGuard<'_, ()> {
        self.0.write().unwrap_or_else(PoisonError::into_inner)
    }
}
