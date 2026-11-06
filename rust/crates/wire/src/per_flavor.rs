//! `PerFlavor`, one value for each flavor of `SandboxFlavor` (decision
//! CH58 rule 4). It is a Rust helper next to the enum, not a wire type: it
//! has no serde and no schema.
//!
//! A new flavor fails to compile here until `PerFlavor` holds it: the
//! match of `get` and `get_mut` names each variant, and `try_every` and
//! `iter` name each field. So no user of `PerFlavor` holds a list of the
//! flavors, and `get` cannot miss one.

use std::convert::Infallible;

use crate::SandboxFlavor;

/// One value for each flavor of `SandboxFlavor`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PerFlavor<T> {
    base: T,
    python: T,
}

impl<T> PerFlavor<T> {
    /// The value `value` gives for each flavor, in the order of the enum.
    pub fn every(mut value: impl FnMut(SandboxFlavor) -> T) -> Self {
        let Ok(every) = Self::try_every(|flavor| Ok::<_, Infallible>(value(flavor)));
        every
    }

    /// The value `value` gives for each flavor, in the order of the enum.
    ///
    /// # Errors
    ///
    /// The first error of `value`.
    pub fn try_every<E>(mut value: impl FnMut(SandboxFlavor) -> Result<T, E>) -> Result<Self, E> {
        Ok(Self {
            base: value(SandboxFlavor::Base)?,
            python: value(SandboxFlavor::Python)?,
        })
    }

    /// The value of `flavor`.
    #[must_use]
    pub const fn get(&self, flavor: SandboxFlavor) -> &T {
        match flavor {
            SandboxFlavor::Base => &self.base,
            SandboxFlavor::Python => &self.python,
        }
    }

    /// The value of `flavor`, to change.
    pub const fn get_mut(&mut self, flavor: SandboxFlavor) -> &mut T {
        match flavor {
            SandboxFlavor::Base => &mut self.base,
            SandboxFlavor::Python => &mut self.python,
        }
    }

    /// Each flavor with its value, in the order of the enum.
    pub fn iter(&self) -> impl Iterator<Item = (SandboxFlavor, &T)> {
        let Self { base, python } = self;
        [(SandboxFlavor::Base, base), (SandboxFlavor::Python, python)].into_iter()
    }

    /// The set with `map` applied to each value, in the order of the enum.
    ///
    /// # Errors
    ///
    /// The first error of `map`.
    pub fn try_map<U, E>(
        &self,
        mut map: impl FnMut(SandboxFlavor, &T) -> Result<U, E>,
    ) -> Result<PerFlavor<U>, E> {
        PerFlavor::try_every(|flavor| map(flavor, self.get(flavor)))
    }
}

#[cfg(test)]
mod tests {
    use super::PerFlavor;
    use crate::SandboxFlavor;

    /// `every`, `get` and `iter` agree on the value of each flavor, and
    /// `iter` gives each flavor once, in the order of the enum.
    #[test]
    fn every_get_and_iter_agree() {
        let every = PerFlavor::every(|flavor| flavor);
        let listed: Vec<_> = every.iter().collect();
        assert_eq!(
            listed,
            [
                (SandboxFlavor::Base, &SandboxFlavor::Base),
                (SandboxFlavor::Python, &SandboxFlavor::Python),
            ]
        );
        for (flavor, value) in every.iter() {
            assert_eq!(every.get(flavor), value);
        }
    }

    #[test]
    fn get_mut_changes_only_its_flavor() {
        let mut values = PerFlavor::every(|_| 0_u8);
        *values.get_mut(SandboxFlavor::Python) = 1;
        assert_eq!(*values.get(SandboxFlavor::Base), 0);
        assert_eq!(*values.get(SandboxFlavor::Python), 1);
    }

    #[test]
    fn try_map_keeps_each_flavor_or_gives_the_first_error() {
        let every = PerFlavor::every(|_| 1_u8);
        let doubled = every.try_map(|_, value| Ok::<_, ()>(value * 2)).unwrap();
        assert!(doubled.iter().all(|(_, value)| *value == 2));
        let failed = every.try_map(|flavor, _| Err::<u8, _>(flavor));
        assert_eq!(failed, Err(SandboxFlavor::Base));
    }

    /// `name` is the wire name of the serde derive, and it reads back as
    /// its own flavor.
    #[test]
    fn name_is_the_wire_name() {
        for (flavor, _) in PerFlavor::every(|_| ()).iter() {
            let wire = serde_json::to_value(flavor).unwrap();
            assert_eq!(wire, flavor.name());
            assert_eq!(
                serde_json::from_value::<SandboxFlavor>(wire).unwrap(),
                flavor
            );
        }
    }
}
