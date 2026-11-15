//! The names and ids of the Forgejo API, each parsed at the boundary.
//!
//! A value of each type passed its rule once, so the code that holds one
//! needs no check of its own. The rules keep each name to the unreserved
//! characters of RFC 3986, section 2.3, so a name goes into a request
//! path or query with no escape. A job handle goes on the argv of
//! `forgejo-runner one-job --handle` in a later PR, so it is a UUID and
//! nothing else.

use std::collections::BTreeSet;
use std::fmt;
use std::str::FromStr;

use serde::Deserialize;

/// The most characters of an organization name. Forgejo allows 40.
const ORG_NAME_MAX: usize = 40;

/// The most characters of a runner label.
const LABEL_MAX: usize = 255;

/// The most characters of a runner name. Forgejo stores it in a column
/// of 255 characters.
const RUNNER_NAME_MAX: usize = 255;

/// The length of a UUID as text: 8-4-4-4-12 hex digits.
const UUID_LEN: usize = 36;

/// The offsets of the four hyphens of a UUID.
const UUID_HYPHENS: [usize; 4] = [8, 13, 18, 23];

/// Whether `text` is 1 to `max` characters of ASCII letters, digits, `.`,
/// `_` or `-`, with a letter or a digit first. The first rule refuses `.`
/// and `..`, so a name is never a path step out of its parent.
fn is_name(text: &str, max: usize) -> bool {
    let mut characters = text.chars();

    text.len() <= max
        && characters
            .next()
            .is_some_and(|first| first.is_ascii_alphanumeric())
        && characters.all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
}

/// Whether `text` is a UUID in lowercase hex: 8-4-4-4-12. Forgejo writes
/// each runner uuid and each job handle in this form.
fn is_uuid(text: &str) -> bool {
    text.len() == UUID_LEN
        && text.chars().enumerate().all(|(offset, character)| {
            if UUID_HYPHENS.contains(&offset) {
                character == '-'
            } else {
                matches!(character, '0'..='9' | 'a'..='f')
            }
        })
}

/// The kind of a text that failed its rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NameKind {
    /// An organization name.
    Org,
    /// A runner label.
    Label,
    /// A runner name.
    Runner,
    /// A job handle.
    JobHandle,
    /// A runner uuid.
    RunnerUuid,
}

/// Why a text is not a name or a UUID of its kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NameError(NameKind);

impl NameError {
    /// The kind of the text that failed.
    #[must_use]
    pub fn kind(self) -> NameKind {
        self.0
    }
}

impl fmt::Display for NameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let rule = match self.0 {
            NameKind::Org => {
                "an organization name is 1 to 40 ASCII letters, digits, '.', '_' or '-', with a letter or a digit first"
            }
            NameKind::Label => {
                "a label is 1 to 255 ASCII letters, digits, '.', '_' or '-', with a letter or a digit first"
            }
            NameKind::Runner => {
                "a runner name is 1 to 255 ASCII letters, digits, '.', '_' or '-', with a letter or a digit first"
            }
            NameKind::JobHandle => "a job handle is a lowercase UUID: 8-4-4-4-12 hex digits",
            NameKind::RunnerUuid => "a runner uuid is a lowercase UUID: 8-4-4-4-12 hex digits",
        };
        formatter.write_str(rule)
    }
}

impl std::error::Error for NameError {}

/// A text type with one rule. Serde reads it from a JSON string through
/// the same rule.
macro_rules! checked_text {
    ($(#[$doc:meta])* $name:ident, $kind:expr, $check:expr) => {
        $(#[$doc])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Deserialize)]
        #[serde(try_from = "String")]
        pub struct $name(String);

        impl $name {
            /// The value as text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = NameError;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                if !$check(text) {
                    return Err(NameError($kind));
                }
                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = NameError;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                text.parse()
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

checked_text!(
    /// The name of the Forgejo organization that owns the runners. It is
    /// one step of each request path.
    OrgName,
    NameKind::Org,
    |text| is_name(text, ORG_NAME_MAX)
);

checked_text!(
    /// One runner label, for example `ubuntu-24.04` or `kvm`. A job names
    /// its labels in `runs-on`.
    Label,
    NameKind::Label,
    |text| is_name(text, LABEL_MAX)
);

checked_text!(
    /// The name of a runner that the daemon registers.
    RunnerName,
    NameKind::Runner,
    |text| is_name(text, RUNNER_NAME_MAX)
);

checked_text!(
    /// The handle of one waiting job. `forgejo-runner one-job --handle`
    /// takes it and asks Forgejo for that one job.
    JobHandle,
    NameKind::JobHandle,
    is_uuid
);

checked_text!(
    /// The uuid that Forgejo gives a runner at registration.
    RunnerUuid,
    NameKind::RunnerUuid,
    is_uuid
);

/// Why a list of labels is not a label set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EmptyLabels;

impl fmt::Display for EmptyLabels {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a label set holds at least one label")
    }
}

impl std::error::Error for EmptyLabels {}

/// A set of one or more runner labels, each once, in sorted order. The
/// jobs list takes it as its filter, and each waiting job carries one.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "Vec<Label>")]
pub struct Labels(BTreeSet<Label>);

impl Labels {
    /// The set of `labels`. A label given twice counts once.
    ///
    /// # Errors
    ///
    /// `EmptyLabels` when `labels` holds no label.
    pub fn new(labels: impl IntoIterator<Item = Label>) -> Result<Self, EmptyLabels> {
        let set: BTreeSet<Label> = labels.into_iter().collect();
        if set.is_empty() {
            return Err(EmptyLabels);
        }
        Ok(Self(set))
    }

    /// Each label, in sorted order.
    pub fn iter(&self) -> impl Iterator<Item = &Label> {
        self.0.iter()
    }

    /// Whether the set holds `label`.
    #[must_use]
    pub fn contains(&self, label: &Label) -> bool {
        self.0.contains(label)
    }

    /// The labels as the `labels` query of the jobs list: comma separated,
    /// for example `kvm,ubuntu-24.04`. A label holds no comma.
    pub(crate) fn query(&self) -> String {
        let names: Vec<&str> = self.0.iter().map(Label::as_str).collect();
        names.join(",")
    }
}

impl TryFrom<Vec<Label>> for Labels {
    type Error = EmptyLabels;

    fn try_from(labels: Vec<Label>) -> Result<Self, Self::Error> {
        Self::new(labels)
    }
}

/// Why a number is not an id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ZeroId;

impl fmt::Display for ZeroId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a Forgejo id is a positive integer")
    }
}

impl std::error::Error for ZeroId {}

/// A positive integer id of Forgejo. Serde reads it from a JSON integer.
macro_rules! forgejo_id {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Deserialize)]
        #[serde(try_from = "u64")]
        pub struct $name(u64);

        impl $name {
            /// The id as a number.
            #[must_use]
            pub fn get(self) -> u64 {
                self.0
            }
        }

        impl TryFrom<u64> for $name {
            type Error = ZeroId;

            fn try_from(id: u64) -> Result<Self, Self::Error> {
                if id == 0 {
                    return Err(ZeroId);
                }
                Ok(Self(id))
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(formatter, "{}", self.0)
            }
        }
    };
}

forgejo_id!(
    /// The id of one job of a workflow run.
    JobId
);

forgejo_id!(
    /// The id of one runner. The remove request takes it.
    RunnerId
);

#[cfg(test)]
mod tests {
    use super::{
        EmptyLabels, JobHandle, Label, Labels, NameKind, OrgName, RunnerId, RunnerName, RunnerUuid,
        ZeroId,
    };

    #[test]
    fn a_name_takes_only_unreserved_characters() {
        for text in ["kvm", "ubuntu-24.04", "ubuntu_latest", "a", "9"] {
            assert!(text.parse::<Label>().is_ok(), "{text}");
        }
        for text in [
            "",
            ".",
            "..",
            "-kvm",
            "_kvm",
            "kvm,docker",
            "kvm docker",
            "a/b",
            "a?b",
            "a%2c",
            "ubuntu:24.04",
            "über",
            "kvm\n",
        ] {
            assert_eq!(
                text.parse::<Label>().map_err(|error| error.kind()),
                Err(NameKind::Label),
                "{text:?}"
            );
        }
    }

    #[test]
    fn each_name_kind_has_its_own_cap() {
        assert!("a".repeat(40).parse::<OrgName>().is_ok());
        assert!("a".repeat(41).parse::<OrgName>().is_err());
        assert!("a".repeat(255).parse::<Label>().is_ok());
        assert!("a".repeat(256).parse::<Label>().is_err());
        assert!("a".repeat(255).parse::<RunnerName>().is_ok());
        assert!("a".repeat(256).parse::<RunnerName>().is_err());
    }

    #[test]
    fn a_handle_is_a_lowercase_uuid() {
        let handle = "2f923f44-2d31-4d08-ad46-e04a543826e0";
        assert_eq!(handle.parse::<JobHandle>().unwrap().as_str(), handle);
        assert!(handle.parse::<RunnerUuid>().is_ok());
        for text in [
            "",
            "2F923F44-2D31-4D08-AD46-E04A543826E0",
            "2f923f442d314d08ad46e04a543826e0",
            "2f923f44-2d31-4d08-ad46-e04a543826e",
            "2f923f44-2d31-4d08-ad46-e04a543826e0a",
            "2f923f44_2d31-4d08-ad46-e04a543826e0",
            "2f923f44-2d31-4d08-ad46-e04a543826g0",
            "--handle-2d31-4d08-ad46-e04a543826e0",
        ] {
            assert_eq!(
                text.parse::<JobHandle>().map_err(|error| error.kind()),
                Err(NameKind::JobHandle),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_label_set_is_never_empty_and_holds_each_label_once() {
        assert_eq!(Labels::new([]), Err(EmptyLabels));

        let labels =
            Labels::new(["ubuntu-24.04", "kvm", "kvm"].map(|text| text.parse::<Label>().unwrap()))
                .unwrap();
        assert_eq!(labels.query(), "kvm,ubuntu-24.04");
        assert_eq!(labels.iter().count(), 2);
        assert!(labels.contains(&"kvm".parse().unwrap()));
        assert!(!labels.contains(&"docker".parse().unwrap()));
    }

    #[test]
    fn serde_reads_each_type_through_its_rule() {
        let labels: Labels = serde_json::from_str(r#"["kvm","kvm"]"#).unwrap();
        assert_eq!(labels.query(), "kvm");
        for text in [r"[]", r#"["kvm,x"]"#, r#""kvm""#, "null"] {
            assert!(serde_json::from_str::<Labels>(text).is_err(), "{text}");
        }

        assert_eq!(serde_json::from_str::<RunnerId>("3").unwrap().get(), 3);
        for text in ["0", "-1", "1.5", r#""3""#] {
            assert!(serde_json::from_str::<RunnerId>(text).is_err(), "{text}");
        }
        assert_eq!(RunnerId::try_from(0), Err(ZeroId));
    }
}
