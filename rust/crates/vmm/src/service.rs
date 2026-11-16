//! The service VMs of the vmm (browser design 10.4 gaps 1 and 2, the
//! attachments design note). A service VM boots a root disk of
//! `ServiceImage`, not a flavor. It has no family, no row and no facade
//! of a family. There are two service VMs:
//!
//! 1. The browser VM runs Chrome and agent-browser for the MCP upstream
//!    `browser`.
//! 2. The scanner VM checks and rebuilds each file that a door client
//!    attaches to a prompt. It has no egress.
//!
//! ```text
//! chaperone ─stdio─ browser-relay (uid mcp-browser)
//!                       │ VMM_BROWSER_SOCKET
//!                       ▼
//!                      vmm ── `link` ── guest port 1201 ── browser facade
//!                       │                                   "mcp" line
//!                       └─ host port 1104: public names, port 443 only
//!
//! attendance ── VMM_SCANNER_SOCKET ──► vmm ── `link` ── guest port 1201
//!                                       │      ── scanner facade
//!                                       │         "scan" line
//!                                       └─ no host port
//! ```
//!
//! 1. The VM of a service image runs in the VM table of `ColdBoot`, with
//!    the reserved session id `vm_id`. So the slots, admission, the stop
//!    path and the shutdown serve it as they serve a family VM. It has no
//!    snapshot key, so admission never evicts it, the idle tick never puts
//!    it to sleep, and the shutdown stops it.
//! 2. The create route refuses `vm_id` with `conflict`, so no row ever
//!    names it. An Upgrade and a drop then find no row of it.
//! 3. Each start makes a new session disk, and the guest keeps its state
//!    on its tmpfs. So nothing of one VM start reaches the next one
//!    (decision V4).
//! 4. `link` serves the socket of `VMM_BROWSER_SOCKET` (decision V1) and
//!    the socket of `VMM_SCANNER_SOCKET`.
//! 5. `egress` gives the host listener of each image: the egress proxy
//!    for the browser, and none for the scanner.

pub mod link;

use std::fmt;

use creche_vmm_confine::limits::VmLimits;
use creche_wire::{FamilyCpus, FamilyMemoryMib, FamilyName, ServiceImage, SessionId};
use serde::Serialize;

/// The reserved session id of the browser VM. The version digit (the
/// first digit of the third group) is 0. So no random id of version 4
/// is ever this id.
const BROWSER_ID: &str = "00000000-0000-0000-0000-000000000001";

/// The vCPUs of the browser VM.
const BROWSER_CPUS: u64 = 2;

/// The guest RAM of the browser VM, in MiB. Chrome with a few tabs fits.
const BROWSER_MEMORY_MIB: u64 = 2048;

/// The reserved session id of the scanner VM, in the scheme of
/// `BROWSER_ID`: version digit 0, and the next number.
const SCANNER_ID: &str = "00000000-0000-0000-0000-000000000002";

/// The vCPUs of the scanner VM (attachments design note, section 5).
const SCANNER_CPUS: u64 = 2;

/// The guest RAM of the scanner VM, in MiB (attachments design note,
/// section 5).
const SCANNER_MEMORY_MIB: u64 = 3072;

/// The host listener of a service VM.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Egress {
    /// The egress proxy on host port 1104, with the rule
    /// `rule::decide_public` (browser design 10.2 item 4).
    Public,
    /// No host listener. The deny check holds the bound set empty.
    Closed,
}

/// The owner of a VM: the family of its session, or its service image.
/// A line of the egress log names it as `"family": <name>` or
/// `"service": <image>`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Owner {
    /// A session VM of this family.
    Family(FamilyName),
    /// The VM of this service image.
    Service(ServiceImage),
}

impl fmt::Display for Owner {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Family(family) => write!(formatter, "{family}"),
            Self::Service(image) => write!(formatter, "the {} service", image.name()),
        }
    }
}

/// Why a service VM did not give a link.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceError(pub String);

impl fmt::Display for ServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ServiceError {}

/// The reserved session id of the VM of `image`.
///
/// # Errors
///
/// A `ServiceError` if the id does not read. A unit test reads each id.
pub fn vm_id(image: ServiceImage) -> Result<SessionId, ServiceError> {
    let text = match image {
        ServiceImage::Browser => BROWSER_ID,
        ServiceImage::Scanner => SCANNER_ID,
    };
    text.parse()
        .map_err(|error| ServiceError(format!("the id of {}: {error:?}", image.name())))
}

/// Whether `id` is the reserved session id of a service VM.
#[must_use]
pub fn is_reserved(id: &SessionId) -> bool {
    [BROWSER_ID, SCANNER_ID].contains(&id.as_str())
}

/// The host listener of the VM of `image`. The scanner VM gets none: the
/// attachments design note gives it no network device and no egress rule.
#[must_use]
pub const fn egress(image: ServiceImage) -> Egress {
    match image {
        ServiceImage::Browser => Egress::Public,
        ServiceImage::Scanner => Egress::Closed,
    }
}

/// The vCPUs and the guest RAM of the VM of `image`.
///
/// # Errors
///
/// A `ServiceError` if a size is outside the range of a VM shape. A unit
/// test reads each size.
pub fn limits(image: ServiceImage) -> Result<VmLimits, ServiceError> {
    let (cpus, memory) = match image {
        ServiceImage::Browser => (BROWSER_CPUS, BROWSER_MEMORY_MIB),
        ServiceImage::Scanner => (SCANNER_CPUS, SCANNER_MEMORY_MIB),
    };
    let size = |error| ServiceError(format!("the size of {}: {error}", image.name()));
    let cpus = FamilyCpus::try_from(cpus).map_err(|error| size(error.to_string()))?;
    let memory = FamilyMemoryMib::try_from(memory).map_err(|error| size(error.to_string()))?;
    Ok(VmLimits::new(cpus, memory))
}

#[cfg(test)]
mod tests {
    use creche_wire::{PerService, ServiceImage};

    use super::{Egress, Owner, egress, is_reserved, limits, vm_id};

    /// Each service image has an id that reads, is reserved and is no id
    /// of version 4, and sizes that fit a VM shape. No two images share
    /// an id.
    #[test]
    fn each_image_has_a_reserved_id_and_its_sizes() {
        let mut ids = Vec::new();
        for (image, ()) in PerService::every(|_| ()).iter() {
            let id = vm_id(image).unwrap();
            assert!(is_reserved(&id));
            assert_eq!(id.as_str().as_bytes()[14], b'0');
            assert!(!ids.contains(&id), "{}", image.name());
            ids.push(id);
            limits(image).unwrap();
        }
        let browser = limits(ServiceImage::Browser).unwrap();
        assert_eq!(browser.vcpus(), 2);
        assert_eq!(browser.guest_ram_mib(), 2048);
        let scanner = limits(ServiceImage::Scanner).unwrap();
        assert_eq!(scanner.vcpus(), 2);
        assert_eq!(scanner.guest_ram_mib(), 3072);
    }

    /// The browser VM gets the egress proxy, and the scanner VM gets no
    /// host listener (attachments design note, section 5).
    #[test]
    fn only_the_browser_has_egress() {
        assert_eq!(egress(ServiceImage::Browser), Egress::Public);
        assert_eq!(egress(ServiceImage::Scanner), Egress::Closed);
    }

    #[test]
    fn a_random_id_is_not_reserved() {
        let id = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        assert!(!is_reserved(&id));
    }

    /// The owner of a line of the egress log is one key.
    #[test]
    fn the_owner_is_one_key_of_a_line() {
        let family = Owner::Family("code".parse().unwrap());
        let service = Owner::Service(ServiceImage::Browser);
        assert_eq!(
            serde_json::to_value(family).unwrap(),
            serde_json::json!({"family": "code"})
        );
        assert_eq!(
            serde_json::to_value(service).unwrap(),
            serde_json::json!({"service": "browser"})
        );
        let scanner = Owner::Service(ServiceImage::Scanner);
        assert_eq!(scanner.to_string(), "the scanner service");
    }
}
