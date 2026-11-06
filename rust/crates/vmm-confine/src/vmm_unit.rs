//! The systemd unit of the vmm (decision CH60 rule 1). Each VM scope binds
//! to it, so systemd stops every VM scope when the vmm unit stops or dies.
//!
//! The vmm reads its unit from `/proc/self/cgroup`, with no config key. The
//! unit is the service that holds the vmm under the user manager
//! `user@<uid>.service`, because `systemd-run --user` puts each VM scope in
//! that manager. A vmm in no service unit of a user manager gets a
//! `UnitError`, and lifecycle then refuses every VM launch.

use std::fmt;
use std::io;

use serde::{Deserialize, Serialize};

/// The cgroup file of the running process.
pub const CGROUP: &str = "/proc/self/cgroup";

/// The suffix of a service unit.
const SERVICE: &str = ".service";

/// The most bytes in a unit name. systemd keeps 256 with the NUL.
const NAME_MAX: usize = 255;

/// The name of the service unit of the vmm, such as `creche-vmm.service`.
/// The name has only the characters of a systemd unit name, so it is one
/// word in `-p BindsTo=`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct VmmUnit(String);

/// Why the vmm has no unit for its VM scopes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UnitError {
    /// `/proc/self/cgroup` did not read.
    Read(io::ErrorKind),
    /// The cgroup file has no line of the unified hierarchy (`0::`).
    NoUnifiedLine,
    /// The cgroup path is under no user manager. The value is the path.
    NoUserManager(String),
    /// The cgroup path is in no service of the user manager, for example
    /// in a scope. The value is the path.
    NoService(String),
    /// The value is not the name of a service unit.
    NotServiceName(String),
}

impl fmt::Display for UnitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(kind) => write!(formatter, "reading {CGROUP}: {kind}"),
            Self::NoUnifiedLine => write!(formatter, "{CGROUP} has no 0:: line"),
            Self::NoUserManager(path) => {
                write!(formatter, "the vmm cgroup {path} is under no user manager")
            }
            Self::NoService(path) => {
                write!(formatter, "the vmm cgroup {path} is in no service unit")
            }
            Self::NotServiceName(name) => write!(formatter, "{name:?} is not a service unit name"),
        }
    }
}

impl std::error::Error for UnitError {}

impl VmmUnit {
    /// Checks `name` and makes the unit. A name ends with `.service`, has
    /// a stem before it, has at most 255 bytes and has only ASCII letters,
    /// digits and `:_.\@-`.
    ///
    /// # Errors
    ///
    /// `UnitError::NotServiceName` for any other name.
    pub fn new(name: String) -> Result<Self, UnitError> {
        let allowed = |byte: u8| byte.is_ascii_alphanumeric() || b":_.\\@-".contains(&byte);
        let stem = name.strip_suffix(SERVICE).unwrap_or_default();
        if stem.is_empty() || name.len() > NAME_MAX || !name.bytes().all(allowed) {
            return Err(UnitError::NotServiceName(name));
        }
        Ok(Self(name))
    }

    /// The unit of the running process, from `CGROUP`.
    ///
    /// # Errors
    ///
    /// `UnitError::Read` when the file does not read, else the error of
    /// `from_cgroup`.
    pub fn of_self() -> Result<Self, UnitError> {
        let text =
            std::fs::read_to_string(CGROUP).map_err(|error| UnitError::Read(error.kind()))?;
        Self::from_cgroup(&text)
    }

    /// The unit in `text`, the content of a `/proc/<pid>/cgroup` file. The
    /// `0::` line gives the cgroup path. The unit is the first `.service`
    /// part of the path after the part `user@<uid>.service`, so a slice
    /// between the two and a cgroup that the unit made below itself do not
    /// change it.
    ///
    /// # Errors
    ///
    /// `NoUnifiedLine`, `NoUserManager`, `NoService` or `NotServiceName`.
    pub fn from_cgroup(text: &str) -> Result<Self, UnitError> {
        let path = text
            .lines()
            .find_map(|line| line.strip_prefix("0::"))
            .ok_or(UnitError::NoUnifiedLine)?;
        let mut parts = path.split('/');
        let manager = |part: &str| {
            part.strip_prefix("user@")
                .and_then(|rest| rest.strip_suffix(SERVICE))
                .is_some_and(|uid| !uid.is_empty() && uid.bytes().all(|byte| byte.is_ascii_digit()))
        };
        if !parts.any(manager) {
            return Err(UnitError::NoUserManager(path.to_owned()));
        }
        let unit = parts
            .find(|part| part.ends_with(SERVICE))
            .ok_or_else(|| UnitError::NoService(path.to_owned()))?;
        Self::new(unit.to_owned())
    }

    /// The unit name.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for VmmUnit {
    type Error = UnitError;

    fn try_from(name: String) -> Result<Self, Self::Error> {
        Self::new(name)
    }
}

impl From<VmmUnit> for String {
    fn from(unit: VmmUnit) -> Self {
        unit.0
    }
}

#[cfg(test)]
mod tests {
    use super::{UnitError, VmmUnit};

    const APP: &str = "/user.slice/user-1000.slice/user@1000.service/app.slice";

    fn unit(name: &str) -> VmmUnit {
        VmmUnit::new(name.to_owned()).unwrap()
    }

    #[test]
    fn a_service_of_the_user_manager_is_the_unit() {
        let text = format!("0::{APP}/creche-vmm.service\n");
        assert_eq!(VmmUnit::from_cgroup(&text), Ok(unit("creche-vmm.service")));
    }

    #[test]
    fn a_cgroup_below_the_service_keeps_the_service() {
        let text = format!("0::{APP}/app-x.slice/creche-vmm.service/payload\n");
        assert_eq!(VmmUnit::from_cgroup(&text), Ok(unit("creche-vmm.service")));
    }

    #[test]
    fn a_hybrid_file_uses_its_unified_line() {
        let text = format!("1:name=systemd:/user.slice\n0::{APP}/creche-vmm.service\n");
        assert_eq!(VmmUnit::from_cgroup(&text), Ok(unit("creche-vmm.service")));
    }

    #[test]
    fn a_scope_has_no_unit() {
        let path = format!("{APP}/run-r1234.scope");
        let text = format!("0::{path}\n");
        assert_eq!(VmmUnit::from_cgroup(&text), Err(UnitError::NoService(path)));
        let path = "/user.slice/user-1000.slice/user@1000.service/init.scope";
        let text = format!("0::{path}\n");
        let error = UnitError::NoService(path.to_owned());
        assert_eq!(VmmUnit::from_cgroup(&text), Err(error));
    }

    #[test]
    fn a_login_session_or_a_system_service_has_no_unit() {
        for path in [
            "/user.slice/user-1000.slice/session-3.scope",
            "/system.slice/creche-vmm.service",
            "/",
        ] {
            let text = format!("0::{path}\n");
            let error = UnitError::NoUserManager(path.to_owned());
            assert_eq!(VmmUnit::from_cgroup(&text), Err(error), "{path}");
        }
    }

    #[test]
    fn a_file_with_no_unified_line_has_no_unit() {
        let text = "12:pids:/user.slice\n1:name=systemd:/user.slice\n";
        assert_eq!(VmmUnit::from_cgroup(text), Err(UnitError::NoUnifiedLine));
        assert_eq!(VmmUnit::from_cgroup(""), Err(UnitError::NoUnifiedLine));
    }

    #[test]
    fn a_unit_name_is_one_service_word() {
        for name in [
            "",
            ".service",
            "creche-vmm",
            "creche-vmm.scope",
            "a b.service",
            "a=b.service",
            "a\n.service",
        ] {
            let error = UnitError::NotServiceName(name.to_owned());
            assert_eq!(VmmUnit::new(name.to_owned()), Err(error), "{name:?}");
        }
        let long = format!("{}.service", "a".repeat(248));
        assert!(VmmUnit::new(long).is_err());
        let longest = format!("{}.service", "a".repeat(247));
        assert!(VmmUnit::new(longest).is_ok());
        assert_eq!(unit("x@1.service").name(), "x@1.service");
    }

    #[test]
    fn a_plan_with_a_bad_unit_name_does_not_parse() {
        assert!(serde_json::from_str::<VmmUnit>(r#""a b.service""#).is_err());
        let parsed: VmmUnit = serde_json::from_str(r#""creche-vmm.service""#).unwrap();
        assert_eq!(parsed, unit("creche-vmm.service"));
    }
}
