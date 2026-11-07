//! The AppArmor slot profiles of Cloud Hypervisor (decisions CH8-1, CH51,
//! CH55). Landlock ABI 4 covers neither UDP nor Unix sockets (fact 77), so
//! these profiles close both.
//!
//! The pool has one profile `creche-ch-slot-<k>` for each slot `k` of
//! `creche_vmm_store::layout::SLOTS` (mechanism X, decision CH55). No
//! profile has an attachment path. The launch stage of the vmm writes
//! `exec creche-ch-slot-<k>` to `/proc/self/attr/apparmor/exec` and then
//! execs the one installed CH (plan PR C18). Each VM takes a free slot,
//! and `vm/<id>` is a symlink to `slot/<k>` (PR D16). AppArmor checks the
//! resolved path of a socket, so a CH in slot `k` reaches only the sockets
//! under `<run root>/slot/<k>/`.
//!
//! Every slot gets the same rules, except for three lines:
//!
//! 1. The header names the label `creche-ch-slot-<k>`, with no path.
//! 2. The signal rule between CH processes names only its own label.
//! 3. Each run dir rule names `slot/<k>/` of the run root.
//!
//! `render_file` makes `apparmor/cloud-hypervisor-slots.profile`, the file
//! that David installs (design section 6.1 step 1). Its install path is
//! the decided `/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor`
//! (gap G28 option A of decision CH36). The test `tests/slot_profile.rs`
//! holds the committed file equal to it.

use creche_vmm_store::layout::Slot;

/// The prefix of each slot label (decision CH55).
pub const LABEL_PREFIX: &str = "creche-ch-slot-";

/// The one installed CH binary (gap G28). Each slot profile maps it.
pub const CH_BINARY: &str = "/opt/components/vmm/bin/cloud-hypervisor";

/// The run root `RuntimeDirectory=vmm` of the user unit, for any uid
/// (decision CH20, gap G5).
pub const RUN_ROOT: &str = "/run/user/[0-9]*/vmm/";

/// The state root default (decision CH50 rule 11). `@{HOME}` comes from
/// `include <tunables/global>`.
pub const STATE_ROOT: &str = "@{HOME}/.local/state/creche-vmm/";

/// The committed file that `render_file` makes, relative to the crate.
pub const FILE: &str = "apparmor/cloud-hypervisor-slots.profile";

/// The one ABI line of the file.
const ABI: &str = "abi <abi/4.0>,";

/// The include that defines `@{HOME}` (decision CH50 rule 11).
const TUNABLES: &str = "include <tunables/global>";

/// The comment at the top of the file.
const HEAD: &str = "\
# The AppArmor slot profiles of Cloud Hypervisor (decisions CH8-1, CH51,
# CH55). rust/crates/vmm-confine/src/slot_profile.rs makes this file and
# holds the reason for each rule. Never edit it: run
#   CRECHE_SLOT_PROFILES=write cargo test -p creche-vmm-confine --test slot_profile
# in rust/.
#
# No profile has an attachment path. The launch stage of the vmm writes
# `exec creche-ch-slot-<k>` to /proc/self/attr/apparmor/exec and then
# execs CH, so each CH runs under the label of its slot (decision CH55).
#
# Install (design section 6.1 step 1, gap G28 option A of decision CH36).
# David runs it on the host once, and again after each change of this
# file:
#   sudo install -m 0644 rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile \\
#     /etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
#   sudo apparmor_parser -r /etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
# Check with `sudo aa-status | grep creche-ch-slot`, because the labels
# are the slot labels (decision CH55). Agents never install it on the
# host.
";

/// The label of `slot`: `creche-ch-slot-<k>` (decision CH55).
#[must_use]
pub fn label(slot: Slot) -> String {
    format!("{LABEL_PREFIX}{}", slot.number())
}

/// The profile block of `slot`. It ends with a new line.
#[must_use]
pub fn render(slot: Slot) -> String {
    let label = label(slot);
    let run = format!("{RUN_ROOT}slot/{}/", slot.number());
    let rules = [
        // The binary itself. It is static, so it maps no library.
        format!("{CH_BINARY} mr"),
        // The devices of fact 77, and the stdin that the launch step opens.
        "/dev/kvm rw".to_owned(),
        "/dev/urandom r".to_owned(),
        "/dev/null rw".to_owned(),
        // Unix stream sockets only. No `network` rule, so CH can make no
        // inet socket, TCP or UDP (decision CH8-1). The deny rules name
        // only the permissions that carry an address, so an abstract
        // address is denied both ways and a create still works (C3 spike,
        // PR #454).
        "unix type=stream".to_owned(),
        "deny unix (bind, connect) addr=@**".to_owned(),
        "deny unix (connect, send, receive) peer=(addr=@**)".to_owned(),
        // Signals from the vmm and the user manager, and between the CH
        // processes of this slot only (decision CH51).
        "signal (receive) peer=unconfined".to_owned(),
        format!("signal (send, receive) peer={label}"),
        // The run dir of the slot. `vmm.sock` sits in the run root itself,
        // so no rule names it (decision CH24). One pattern covers every
        // host listener, so a new port needs no reload (decision CH24). A
        // pathname socket needs `w` for a connect or a bind.
        format!("owner {run}api.sock rw"),
        format!("owner {run}api.sock.lock wk"),
        format!("owner {run}vsock.sock rw"),
        format!("owner {run}vsock.sock_* rw"),
        // The state root names of decision CH39 rule 3 under the default
        // root (decision CH50 rule 11). The root disks and kernels are
        // read only (decision CH8-8). `serial.log` sits in the session dir
        // (decision CH59 rule 2) and gets no read (gap G23). The rule
        // matches the log of every session, as the disk rule does. The
        // Landlock ruleset of the VM allows only its own.
        format!("owner {STATE_ROOT}store/* rk"),
        format!("owner {STATE_ROOT}sessions/*/disk.ext4 rwk"),
        format!("owner {STATE_ROOT}sessions/*/serial.log w"),
        format!("owner {STATE_ROOT}sessions/*/snapshots/** rw"),
    ];

    let mut block = format!("profile {label} {{\n");
    for rule in rules {
        block.push_str("  ");
        block.push_str(&rule);
        block.push_str(",\n");
    }
    block.push_str("}\n");
    block
}

/// The whole file: the comment, the ABI line, the include, then one block
/// for each slot, from 0.
#[must_use]
pub fn render_file() -> String {
    let mut file = format!("{HEAD}\n{ABI}\n\n{TUNABLES}\n");
    for slot in Slot::all() {
        file.push('\n');
        file.push_str(&render(slot));
    }
    file
}
