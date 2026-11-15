//! The protocol of host port 1105, the git platform spool (decision CH3,
//! phase 12 PR2). The guest and the `git-platform` upstream of chaperone
//! move git bundles through two folders of the host spool. A bundle never
//! travels in an MCP result, because one result is capped at 1 MiB.
//!
//! Only the vmm and the guest read this protocol, so it has no serde and
//! no schema entry, as the port numbers of `ports.rs` (decision CH14).
//!
//! # The spool on the host
//!
//! 1. `push/` holds each bundle that a guest puts. The vmm writes it and
//!    deletes it. The upstream reads it through its group and pushes it.
//! 2. `fetch/` holds each bundle that the upstream fetched. The upstream
//!    writes it and deletes it after `FETCH_TTL` (15 minutes). The vmm
//!    reads it through its group and sends it to the guest.
//! 3. Each file name is a `BundleId`: `<family>-<ulid>.bundle`. The family
//!    is the family of the VM that the vmm serves, never a value of the
//!    guest bytes. The upstream takes the family from the app id of the
//!    call, so a family reaches only its own bundles on both sides.
//!
//! # A link
//!
//! The guest dials host port 1105 and sends one request. The vmm sends one
//! reply, then closes the link. A link carries exactly one request.
//!
//! 1. A request is one line of ASCII that ends in one LF. The line has at
//!    most `LINE_CAP` bytes, the LF not counted. A line holds no CR.
//! 2. `put <size>` puts one bundle. `<size>` is the byte count of the
//!    bundle in decimal: 1 to `BUNDLE_CAP`, with no sign and no leading
//!    zero. Exactly `<size>` bytes of the bundle follow the LF.
//! 3. `get <id>` gets one bundle of `fetch/`. `<id>` is a `BundleId` of the
//!    family of the VM, as the `fetch` tool of the upstream returns it.
//! 4. A good put gets the reply `ok <id>`, one line with the new
//!    `BundleId` of `push/`. Pass that id to the `push` tool of the
//!    upstream.
//! 5. A good get gets the reply `ok <size>`, one line with the byte count
//!    in decimal, then exactly `<size>` bytes of the bundle.
//! 6. A refused request gets the reply `err <reason>`, one line. `Refusal`
//!    holds each reason word. No bundle byte follows.
//!
//! Each reply line ends in one LF and has at most `LINE_CAP` bytes, the LF
//! not counted. The vmm bounds the wait for the request line with
//! `LINE_WAIT` and the wait for the bundle bytes of a put with
//! `BODY_WAIT`. A put that ends before `<size>` bytes, or that passes the
//! wait, leaves no file in `push/`.
//!
//! # Examples
//!
//! ```text
//! guest: put 1234\n<1234 bytes>
//! vmm:   ok creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle\n
//!
//! guest: get creche-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle\n
//! vmm:   ok 5678\n<5678 bytes>
//!
//! guest: get code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle\n
//! vmm:   err other_family\n
//! ```

use std::fmt;
use std::str::FromStr;
use std::time::Duration;

use crate::{FamilyName, ULID_LEN, is_ulid};

/// The most bytes of one bundle, put or got. It equals `MAX_BUNDLE_BYTES`
/// of the upstream (`agent_mcp/git_platform/spool.py`). The creche pack is
/// about 20 MiB.
pub const BUNDLE_CAP: u64 = 64 * 1024 * 1024;

/// The most bytes of a request line or a reply line, the LF not counted.
/// The longest line is `get ` and a `BundleId` of a family name of 31
/// characters: 69 bytes.
pub const LINE_CAP: usize = 128;

/// The bound of the wait for the whole request line.
pub const LINE_WAIT: Duration = Duration::from_secs(10);

/// The bound of the wait for all the bundle bytes of one put. At 1 MiB/s,
/// a bundle at the cap takes 64 s.
pub const BODY_WAIT: Duration = Duration::from_secs(120);

/// How long a fetched bundle stays in `fetch/`: `FETCH_TTL_S` of the
/// upstream. The upstream deletes an older file at each of its calls.
pub const FETCH_TTL: Duration = Duration::from_secs(15 * 60);

/// The word of a put request.
pub const PUT: &str = "put";

/// The word of a get request.
pub const GET: &str = "get";

/// The word of a good reply.
pub const OK: &str = "ok";

/// The word of a refused reply.
pub const ERR: &str = "err";

/// The end of each bundle id.
const SUFFIX: &str = ".bundle";

/// The characters of Crockford base32, in value order.
const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// The bits of a ULID that hold the time in milliseconds.
const TIME_BITS: u32 = 48;

/// The bits of a ULID that are random.
const RANDOM_BITS: u32 = 80;

/// The bits of one Crockford character.
const CHAR_BITS: u32 = 5;

/// Why the vmm refused a request. Each variant is one reason word of an
/// `err` reply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// The request line is not `put <size>` or `get <id>`, passed
    /// `LINE_CAP`, or did not come whole within `LINE_WAIT`.
    BadRequest,
    /// The family holds no grant of the operation: `push` for a put,
    /// `fetch` for a get. A grant file that does not read grants nothing.
    NotGranted,
    /// The size of a put, or the size of the file of a get, passes
    /// `BUNDLE_CAP`.
    TooLarge,
    /// The put ended before its size, or passed `BODY_WAIT`.
    ShortBody,
    /// The id of a get is not a `BundleId`.
    BadId,
    /// The id of a get names another family.
    OtherFamily,
    /// `fetch/` holds no regular file of the id.
    NotFound,
    /// The host spool failed: a folder is missing, or a read or a write
    /// failed.
    SpoolFault,
}

impl Refusal {
    /// The reason word of the reply.
    #[must_use]
    pub const fn word(self) -> &'static str {
        match self {
            Self::BadRequest => "bad_request",
            Self::NotGranted => "not_granted",
            Self::TooLarge => "too_large",
            Self::ShortBody => "short_body",
            Self::BadId => "bad_id",
            Self::OtherFamily => "other_family",
            Self::NotFound => "not_found",
            Self::SpoolFault => "spool_fault",
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.word())
    }
}

/// Why a text is not a bundle id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BundleIdError;

impl fmt::Display for BundleIdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a bundle id is <family>-<ulid>.bundle")
    }
}

impl std::error::Error for BundleIdError {}

/// The name of one bundle file of the spool: `<family>-<ulid>.bundle`. It
/// is `BUNDLE_ID` of the upstream: a family name, a hyphen, a ULID of 26
/// characters of upper-case Crockford base32 whose first character is 0
/// to 7, and `.bundle`. The name holds no `/` and never starts with a dot.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct BundleId {
    text: String,
    family: FamilyName,
}

impl BundleId {
    /// The id of a new bundle of `family`, from the time `millis` and 80
    /// random bits `random`. The caller takes both: the vmm takes the
    /// wall time and `/dev/urandom`. A time past 48 bits keeps its low 48.
    #[must_use]
    pub fn mint(family: &FamilyName, millis: u64, random: [u8; 10]) -> Self {
        let random = random
            .iter()
            .fold(0_u128, |value, &byte| (value << 8) | u128::from(byte));
        let time = u128::from(millis) & ((1 << TIME_BITS) - 1);
        let value = (time << RANDOM_BITS) | random;
        let ulid: String = (0..ULID_LEN)
            .rev()
            .map(|index| {
                let shift = u32::try_from(index).unwrap_or(0).saturating_mul(CHAR_BITS);
                let digit = usize::try_from((value >> shift) & 0x1F).unwrap_or(0);
                char::from(CROCKFORD.get(digit).copied().unwrap_or(b'0'))
            })
            .collect();
        Self {
            text: format!("{family}-{ulid}{SUFFIX}"),
            family: family.clone(),
        }
    }

    /// The id as text, which is the file name.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The family that the id names.
    #[must_use]
    pub const fn family(&self) -> &FamilyName {
        &self.family
    }
}

impl FromStr for BundleId {
    type Err = BundleIdError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let stem = text.strip_suffix(SUFFIX).ok_or(BundleIdError)?;
        // The ULID has a fixed length, so the last hyphen before it ends
        // the family, which may hold hyphens itself.
        let split = stem.len().checked_sub(ULID_LEN).ok_or(BundleIdError)?;
        let (head, ulid) = (stem.get(..split), stem.get(split..));
        let (Some(head), Some(ulid)) = (head, ulid) else {
            return Err(BundleIdError);
        };
        let family = head.strip_suffix('-').ok_or(BundleIdError)?;
        let first_fits = ulid.starts_with(|first| matches!(first, '0'..='7'));
        if !is_ulid(ulid) || !first_fits {
            return Err(BundleIdError);
        }
        let family = family.parse().map_err(|_| BundleIdError)?;
        Ok(Self {
            text: text.to_owned(),
            family,
        })
    }
}

impl fmt::Display for BundleId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.text)
    }
}

#[cfg(test)]
mod tests {
    use super::{BundleId, LINE_CAP, Refusal};
    use crate::FamilyName;

    fn family(name: &str) -> FamilyName {
        name.parse().unwrap()
    }

    #[test]
    fn a_minted_id_reads_back_with_its_family() {
        let id = BundleId::mint(&family("creche"), 1_700_000_000_000, [0xFF; 10]);
        let read: BundleId = id.as_str().parse().unwrap();
        assert_eq!(read, id);
        assert_eq!(read.family(), &family("creche"));
        assert!(id.as_str().starts_with("creche-01HF"));
        assert!(id.as_str().ends_with("ZZZZZZZZZZZZZZZZ.bundle"));
    }

    /// The upstream's ULID of the time 0 and no random bit is all zeros,
    /// and the highest 48-bit time starts with 7.
    #[test]
    fn the_ulid_has_the_layout_of_the_upstream() {
        let zero = BundleId::mint(&family("code"), 0, [0; 10]);
        assert_eq!(zero.as_str(), "code-00000000000000000000000000.bundle");
        let last = BundleId::mint(&family("code"), u64::MAX, [0; 10]);
        assert_eq!(last.as_str(), "code-7ZZZZZZZZZ0000000000000000.bundle");
    }

    #[test]
    fn a_family_with_hyphens_reads() {
        let id: BundleId = "my-fam-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle".parse().unwrap();
        assert_eq!(id.family(), &family("my-fam"));
    }

    #[test]
    fn every_other_form_is_refused() {
        let refused = [
            "",
            ".bundle",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5E",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle.part",
            ".creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle.part",
            "creche_01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "creche01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "Creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "creche-81JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "creche-01jc9z5t3x4r2n1m0k8h7g6f5e.bundle",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5I.bundle",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5.bundle",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5EE.bundle",
            "../creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "a/creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle",
            "creche-01JC9Z5T3X4R2N1M0K8H7G6F5E.bundle\n",
        ];
        for text in refused {
            assert!(text.parse::<BundleId>().is_err(), "{text:?}");
        }
    }

    /// The longest request line fits the cap.
    #[test]
    fn the_longest_line_fits_the_cap() {
        let name = format!("a{}", "b".repeat(30));
        let id = BundleId::mint(&family(&name), u64::MAX, [0xFF; 10]);
        let line = format!("get {id}");
        assert_eq!(line.len(), 69);
        assert!(line.len() <= LINE_CAP);
    }

    #[test]
    fn each_reason_has_one_word() {
        let all = [
            Refusal::BadRequest,
            Refusal::NotGranted,
            Refusal::TooLarge,
            Refusal::ShortBody,
            Refusal::BadId,
            Refusal::OtherFamily,
            Refusal::NotFound,
            Refusal::SpoolFault,
        ];
        let words: Vec<&str> = all.iter().map(|reason| reason.word()).collect();
        assert_eq!(
            words,
            [
                "bad_request",
                "not_granted",
                "too_large",
                "short_body",
                "bad_id",
                "other_family",
                "not_found",
                "spool_fault",
            ]
        );
    }
}
