//! SPIKE, never merges (decision CH51, draft branch `spike/c3-slot-pool`).
//! The AppArmor profiles of the CH slot pool.
//!
//! The pool has `SLOTS` hard links of the installed CH binary, one at
//! `/opt/components/vmm/bin/ch-slot/<k>/cloud-hypervisor` for each slot
//! `k`. Each link has its own profile `creche-ch-slot-<k>`. AppArmor
//! attaches a profile by the path of the exec, so two names of one inode
//! get two labels. Each VM takes a free slot, and `vm/<id>` is a symlink
//! to `slot/<k>`. AppArmor checks the resolved path of a socket, so a CH in
//! slot `k` reaches only the sockets under `<run root>/slot/<k>/`.
//!
//! `render` makes the profile of one slot from the rules of
//! `apparmor/cloud-hypervisor.profile`. It keeps each rule line and drops
//! the comments. It changes exactly four kinds of line:
//!
//! 1. The header: the name `creche-ch-slot-<k>` and the link path.
//! 2. The rule of the binary: the link path in place of the install path.
//! 3. The signal rule between CH processes: `peer=creche-ch-slot-<k>`, so
//!    a CH signals and receives only within its own label.
//! 4. Each run dir rule: `/vmm/slot/<k>/` in place of `/vmm/vm/*/`.
//!
//! A base profile that lacks one of these lines, or a result that still
//! names the base label, the install path or `vm/*/`, is an error. So an
//! edit of the base profile that the generator does not read fails the
//! render, and no slot profile keeps a rule of every VM.
//!
//! `render_probe` adds one rule to the profile of one slot: `<probe> mr`
//! for a static probe program. A process under the slot label then can
//! exec the probe, and the signal test of the spike runs as that label.
//! No other rule changes.

use std::fmt::{self, Write as _};

/// The base profile of CH (decision CH8-1).
pub const BASE: &str = include_str!("../apparmor/cloud-hypervisor.profile");

/// The pool size. 80 GiB of host RAM ÷ 2 GiB per VM is 40 VMs, so 64 slots
/// cover every VM that admission lets in (item 45 C3, option 2).
pub const SLOTS: u16 = 64;

/// The label of the base profile.
const BASE_NAME: &str = "creche-cloud-hypervisor";

/// The install path that the base profile attaches to (gap G28).
const BASE_BINARY: &str = "/opt/components/vmm/bin/cloud-hypervisor";

/// The parent dir of the slot links.
const LINK_ROOT: &str = "/opt/components/vmm/bin/ch-slot";

/// The run dir part of each run dir rule of the base profile.
const BASE_RUN_DIR: &str = "/vmm/vm/*/";

/// The one ABI line, which a file of many profiles holds once.
const ABI: &str = "abi <abi/4.0>,";

/// The signal rule between CH processes in the base profile.
const BASE_SIGNAL: &str = "signal (send, receive) peer=creche-cloud-hypervisor,";

/// The run dir rules of the base profile: `api.sock`, its lock,
/// `vsock.sock`, the `vsock.sock_*` listeners and `serial.log`.
const RUN_DIR_RULES: usize = 5;

/// One slot of the pool, below `SLOTS`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slot(u16);

impl Slot {
    /// The slot `number`, or `None` at or above `SLOTS`.
    #[must_use]
    pub fn new(number: u16) -> Option<Self> {
        (number < SLOTS).then_some(Self(number))
    }

    /// Every slot, from 0.
    pub fn all() -> impl Iterator<Item = Self> {
        (0..SLOTS).map(Self)
    }

    /// The slot number.
    #[must_use]
    pub fn number(self) -> u16 {
        self.0
    }

    /// The profile name and label: `creche-ch-slot-<k>`.
    #[must_use]
    pub fn name(self) -> String {
        format!("creche-ch-slot-{}", self.0)
    }

    /// The link of the slot: `<bin>/ch-slot/<k>/cloud-hypervisor`.
    #[must_use]
    pub fn binary(self) -> String {
        format!("{LINK_ROOT}/{}/cloud-hypervisor", self.0)
    }

    /// The run dir part of each run dir rule: `/vmm/slot/<k>/`.
    #[must_use]
    pub fn run_dir(self) -> String {
        format!("/vmm/slot/{}/", self.0)
    }
}

/// Why a render failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RenderError {
    /// The base profile has `found` lines of one kind, not `want`.
    Count {
        /// The kind of line.
        what: &'static str,
        /// The lines of that kind in the base profile.
        found: usize,
        /// The lines of that kind that the generator reads.
        want: usize,
    },
    /// A line of the result still names the base label, the install path
    /// or the run dir of every VM.
    Left(String),
    /// The probe path is not absolute, or holds a space, a comma or a glob.
    Probe(String),
}

impl fmt::Display for RenderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Count { what, found, want } => write!(
                formatter,
                "the base profile has {found} {what} lines, the generator reads {want}"
            ),
            Self::Left(line) => write!(formatter, "a slot rule still names the base: {line}"),
            Self::Probe(path) => write!(formatter, "not a plain absolute probe path: {path}"),
        }
    }
}

impl std::error::Error for RenderError {}

/// The rule lines of `base`: trimmed, with no blank line and no comment.
fn rule_lines(base: &str) -> impl Iterator<Item = &str> {
    let lines = base.lines().map(str::trim);
    lines.filter(|line| !line.is_empty() && !line.starts_with('#'))
}

/// The header of the base profile.
fn base_header() -> String {
    format!("profile {BASE_NAME} {BASE_BINARY} {{")
}

/// Fails unless `base` has exactly `want` lines for which `kind` holds.
fn count(
    base: &str,
    what: &'static str,
    want: usize,
    kind: impl Fn(&str) -> bool,
) -> Result<(), RenderError> {
    let found = rule_lines(base).filter(|line| kind(line)).count();
    if found == want {
        Ok(())
    } else {
        Err(RenderError::Count { what, found, want })
    }
}

/// The profile block of `slot`, made from the rules of `base`, with one
/// more rule line for each entry of `extra`. The block ends with a new
/// line and has no ABI line.
///
/// # Errors
///
/// `Count` when `base` does not have exactly one header, one binary rule,
/// one ABI line, one signal rule between CH processes and five run dir
/// rules. `Left` when a line of the result still names the base.
fn render_with(base: &str, slot: Slot, extra: &[String]) -> Result<String, RenderError> {
    let header = base_header();
    let binary_rule = format!("{BASE_BINARY} mr,");
    count(base, "header", 1, |line| line == header)?;
    count(base, "binary", 1, |line| line == binary_rule)?;
    count(base, "abi", 1, |line| line == ABI)?;
    count(base, "signal", 1, |line| line == BASE_SIGNAL)?;
    count(base, "run dir", RUN_DIR_RULES, |line| {
        line.contains(BASE_RUN_DIR)
    })?;
    count(base, "end", 1, |line| line == "}")?;

    let mut block = String::new();
    for line in rule_lines(base) {
        let rule = if line == ABI {
            continue;
        } else if line == header {
            format!("profile {} {} {{", slot.name(), slot.binary())
        } else if line == binary_rule {
            format!("  {} mr,", slot.binary())
        } else if line == BASE_SIGNAL {
            format!("  signal (send, receive) peer={},", slot.name())
        } else if line == "}" {
            let mut end = String::new();
            for rule in extra {
                // Writing to a String does not fail.
                let _ = writeln!(end, "  {rule}");
            }
            end.push('}');
            end
        } else {
            format!("  {}", line.replace(BASE_RUN_DIR, &slot.run_dir()))
        };
        let left = [BASE_NAME, BASE_BINARY, BASE_RUN_DIR, "/vm/"];
        if left.iter().any(|word| rule.contains(word)) {
            return Err(RenderError::Left(rule));
        }
        block.push_str(&rule);
        block.push('\n');
    }
    Ok(block)
}

/// The profile of `slot` alone, from the rules of `base`.
///
/// # Errors
///
/// `Count` or `Left`, as `render_set` says.
pub fn render(base: &str, slot: Slot) -> Result<String, RenderError> {
    render_with(base, slot, &[])
}

/// One profile file for `slots`: a comment, the ABI line once, then one
/// block for each slot.
///
/// # Errors
///
/// `Count` when `base` does not have exactly one header, one binary rule,
/// one ABI line, one signal rule between CH processes and five run dir
/// rules. `Left` when a line of the result still names the base label, the
/// install path or the run dir of every VM.
pub fn render_set(base: &str, slots: impl Iterator<Item = Slot>) -> Result<String, RenderError> {
    let mut file = file_head();
    for slot in slots {
        file.push('\n');
        file.push_str(&render(base, slot)?);
    }
    Ok(file)
}

/// One profile file for the probe variant of `slot`: the rules of the
/// slot plus `<probe> mr`. The probe must be a static program, so it maps
/// no library.
///
/// # Errors
///
/// `Probe` when `probe` is not absolute or holds a space, a comma or a
/// glob, else the errors of `render_set`.
pub fn render_probe(base: &str, slot: Slot, probe: &str) -> Result<String, RenderError> {
    let plain = |char: char| char.is_ascii_alphanumeric() || "/._-".contains(char);
    if !probe.starts_with('/') || !probe.chars().all(plain) {
        return Err(RenderError::Probe(probe.to_owned()));
    }
    let mut file = file_head();
    file.push('\n');
    file.push_str(&render_with(base, slot, &[format!("{probe} mr,")])?);
    Ok(file)
}

/// The comment and the ABI line of a profile file.
fn file_head() -> String {
    format!(
        "# SPIKE (decision CH51): the CH slot pool profiles, made by\n\
         # rust/crates/vmm-confine/examples/ch_slot_profiles.rs from\n\
         # rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile.\n\
         # Do not edit. Agents never load it on the host.\n\
         \n\
         {ABI}\n"
    )
}
