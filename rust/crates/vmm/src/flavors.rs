//! The flavors of the wire enum `SandboxFlavor`, and `PerFlavor`, one
//! value for each of them (decision CH58 rule 4).
//!
//! The vmm holds no list of the flavors. `all` takes the names from the
//! serde derive of the enum: the derive passes the name of each variant
//! to `Deserializer::deserialize_enum`, and a probe deserializer keeps
//! them. So a new flavor of the wire crate needs no vmm change.

use std::fmt;

use creche_wire::SandboxFlavor;
use serde::de::value::{Error as ValueError, StrDeserializer};
use serde::de::{Deserialize, Deserializer, Error as _, Visitor};
use serde::forward_to_deserialize_any;

/// Why a set of values is not one value for each flavor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlavorError {
    /// The serde derive of `SandboxFlavor` gave no list of its names, or
    /// gave a name that does not read back as a flavor.
    NoList,
    /// No value names this flavor.
    Missing(&'static str),
    /// Two or more values name this flavor.
    Duplicate(&'static str),
}

impl fmt::Display for FlavorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoList => {
                formatter.write_str("the wire flavor enum gives no list of its flavors")
            }
            Self::Missing(name) => write!(formatter, "no entry for the flavor `{name}`"),
            Self::Duplicate(name) => write!(formatter, "two entries for the flavor `{name}`"),
        }
    }
}

impl std::error::Error for FlavorError {}

/// Each flavor with its wire name, in the order of the enum.
///
/// # Errors
///
/// `FlavorError::NoList` when the serde derive of `SandboxFlavor` gives
/// no list of names, or a name that does not read back as a flavor.
pub fn all() -> Result<Vec<(&'static str, SandboxFlavor)>, FlavorError> {
    let mut probe = Probe(None);
    // The probe always fails the read. It only keeps the names.
    let _ = SandboxFlavor::deserialize(&mut probe);
    let names = probe.0.ok_or(FlavorError::NoList)?;
    names
        .iter()
        .map(|&name| {
            SandboxFlavor::deserialize(StrDeserializer::<ValueError>::new(name))
                .map(|flavor| (name, flavor))
                .map_err(|_| FlavorError::NoList)
        })
        .collect()
}

/// A deserializer that keeps the variant names of an enum and reads no
/// value.
struct Probe(Option<&'static [&'static str]>);

impl<'de> Deserializer<'de> for &mut Probe {
    type Error = ValueError;

    fn deserialize_any<V: Visitor<'de>>(self, _visitor: V) -> Result<V::Value, Self::Error> {
        Err(ValueError::custom("the probe reads only an enum"))
    }

    fn deserialize_enum<V: Visitor<'de>>(
        self,
        _name: &'static str,
        variants: &'static [&'static str],
        _visitor: V,
    ) -> Result<V::Value, Self::Error> {
        self.0 = Some(variants);
        Err(ValueError::custom("the probe reads only the variant names"))
    }

    forward_to_deserialize_any! {
        bool i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 f32 f64 char str string
        bytes byte_buf option unit unit_struct newtype_struct seq tuple
        tuple_struct map struct identifier ignored_any
    }
}

/// One value for each flavor of the wire enum, in the order of the enum.
/// Only `new` and `every` make one, and each checks the whole set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PerFlavor<T>(Vec<(SandboxFlavor, T)>);

impl<T> PerFlavor<T> {
    /// Takes `values` when they hold each flavor exactly once.
    ///
    /// # Errors
    ///
    /// `FlavorError::Missing` or `FlavorError::Duplicate` for the first
    /// flavor that `values` lacks or repeats. `FlavorError::NoList` when
    /// the flavor list fails, or when it lacks a flavor of `values`.
    pub fn new(mut values: Vec<(SandboxFlavor, T)>) -> Result<Self, FlavorError> {
        let flavors = all()?;
        let mut ordered = Vec::with_capacity(flavors.len());
        for (name, flavor) in flavors {
            let mut matches = values
                .iter()
                .enumerate()
                .filter(|(_, (named, _))| *named == flavor)
                .map(|(index, _)| index);
            let index = matches.next().ok_or(FlavorError::Missing(name))?;
            if matches.next().is_some() {
                return Err(FlavorError::Duplicate(name));
            }
            let (_, value) = values.swap_remove(index);
            ordered.push((flavor, value));
        }
        if values.is_empty() {
            Ok(Self(ordered))
        } else {
            Err(FlavorError::NoList)
        }
    }

    /// The value `value` gives for each flavor.
    ///
    /// # Errors
    ///
    /// `FlavorError::NoList` when the flavor list fails.
    pub fn every(value: impl Fn(SandboxFlavor) -> T) -> Result<Self, FlavorError> {
        Ok(Self(
            all()?
                .into_iter()
                .map(|(_, flavor)| (flavor, value(flavor)))
                .collect(),
        ))
    }

    /// The value of `flavor`. `new` and `every` hold each flavor, so only
    /// a flavor that the serde derive does not list gives `None`.
    #[must_use]
    pub fn get(&self, flavor: SandboxFlavor) -> Option<&T> {
        self.0
            .iter()
            .find(|(named, _)| *named == flavor)
            .map(|(_, value)| value)
    }

    /// Each flavor with its value, in the order of the enum.
    pub fn iter(&self) -> impl Iterator<Item = (SandboxFlavor, &T)> {
        self.0.iter().map(|(flavor, value)| (*flavor, value))
    }

    /// The set with `map` applied to each value.
    ///
    /// # Errors
    ///
    /// The first error of `map`.
    pub fn try_map<U, E>(
        &self,
        mut map: impl FnMut(SandboxFlavor, &T) -> Result<U, E>,
    ) -> Result<PerFlavor<U>, E> {
        self.0
            .iter()
            .map(|(flavor, value)| map(*flavor, value).map(|mapped| (*flavor, mapped)))
            .collect::<Result<_, _>>()
            .map(PerFlavor)
    }
}

#[cfg(test)]
mod tests {
    use creche_wire::SandboxFlavor;

    use super::{FlavorError, PerFlavor, all};

    /// The probe reads the wire names of the serde derive, and each name
    /// reads back as its own flavor.
    #[test]
    fn all_lists_each_wire_name_once() {
        let flavors = all().unwrap();
        for (name, flavor) in &flavors {
            assert_eq!(serde_json::to_value(flavor).unwrap(), *name);
        }
        for flavor in [SandboxFlavor::Base, SandboxFlavor::Python] {
            assert_eq!(
                flavors
                    .iter()
                    .filter(|(_, listed)| *listed == flavor)
                    .count(),
                1
            );
        }
    }

    #[test]
    fn new_takes_each_flavor_once_in_any_order() {
        let every = PerFlavor::every(|flavor| flavor).unwrap();
        let mut values: Vec<_> = every.iter().map(|(flavor, _)| (flavor, flavor)).collect();
        values.reverse();
        assert_eq!(PerFlavor::new(values).unwrap(), every);
        for (flavor, value) in every.iter() {
            assert_eq!(every.get(flavor), Some(value));
        }
    }

    #[test]
    fn new_refuses_a_missing_or_a_repeated_flavor() {
        let base = (SandboxFlavor::Base, ());
        let python = (SandboxFlavor::Python, ());
        assert_eq!(
            PerFlavor::new(vec![base]),
            Err(FlavorError::Missing("python"))
        );
        assert_eq!(
            PerFlavor::new(vec![base, python, base]),
            Err(FlavorError::Duplicate("base"))
        );
    }

    #[test]
    fn try_map_keeps_each_flavor_or_gives_the_first_error() {
        let every = PerFlavor::every(|_| 1_u8).unwrap();
        let doubled = every.try_map(|_, value| Ok::<_, ()>(value * 2)).unwrap();
        assert!(doubled.iter().all(|(_, value)| *value == 2));
        let failed = every.try_map(|flavor, _| Err::<u8, _>(flavor));
        assert_eq!(failed, Err(SandboxFlavor::Base));
    }
}
