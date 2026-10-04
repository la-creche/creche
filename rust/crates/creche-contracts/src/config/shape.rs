//! A raw type that takes only a mapping.
//!
//! A struct that derives `Deserialize` also takes a sequence: `serde` then
//! fills the fields by position. No Python reader does that. Each one
//! refuses a list where the contract gives a mapping. [`MapOnly`] refuses
//! the sequence form.

use std::fmt;
use std::marker::PhantomData;

use serde::de::value::MapAccessDeserializer;
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// A `T` that deserializes from a mapping and from no other form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct MapOnly<T>(pub(super) T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for MapOnly<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct OnlyMap<T>(PhantomData<T>);

        impl<'de, T: Deserialize<'de>> Visitor<'de> for OnlyMap<T> {
            type Value = MapOnly<T>;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a mapping")
            }

            fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Self::Value, A::Error> {
                T::deserialize(MapAccessDeserializer::new(map)).map(MapOnly)
            }
        }

        deserializer.deserialize_map(OnlyMap(PhantomData))
    }
}

impl<T: Serialize> Serialize for MapOnly<T> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.0.serialize(serializer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, PartialEq, Deserialize, Serialize)]
    struct Row {
        command: String,
        #[serde(default)]
        args: Vec<String>,
    }

    #[test]
    fn a_derived_struct_takes_a_sequence() {
        let row: Row = serde_json::from_str(r#"["run", ["-v"]]"#).unwrap();

        assert_eq!(row.command, "run");
    }

    #[test]
    fn a_map_only_struct_takes_a_mapping_and_no_sequence() {
        let row: MapOnly<Row> = serde_json::from_str(r#"{"command": "run"}"#).unwrap();

        assert_eq!(row.0.command, "run");
        assert_eq!(
            serde_json::to_string(&row).unwrap(),
            r#"{"command":"run","args":[]}"#
        );
        for text in [r#"["run", ["-v"]]"#, r#""run""#, "null", "7", "true"] {
            assert!(
                serde_json::from_str::<MapOnly<Row>>(text).is_err(),
                "{text}"
            );
        }
    }
}
