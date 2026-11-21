//! The pure part of step 2 of `check`: the parse of the cgroup path of a
//! slot unit, and the choice of the unit folder that has the cgroup id of
//! the peer. It reads nothing, so the tests of each OS cover it.

use std::fmt;

/// The cgroup path of the slice of the job VMs, from the cgroup root.
/// systemd makes it from the slice name `ci-runner-vms.slice` of the slot
/// units: each `-` of the name opens one more level.
pub const SLICE_PATH: &str = "/ci.slice/ci-runner.slice/ci-runner-vms.slice";

/// The start of the name of each slot unit:
/// `ci-runner-slot<n>@<instance>.service`.
pub const UNIT_PREFIX: &str = "ci-runner-slot";

/// The end of the name of each slot unit.
const UNIT_SUFFIX: &str = ".service";

/// The mark between the slot number and the instance of a unit name.
const INSTANCE_MARK: char = '@';

/// The number of one slot of the runner host, from 0. The service takes
/// it from the `open` line of the session (design section 6.2), never
/// from the peer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Slot(u8);

impl Slot {
    /// The slot `n`.
    #[must_use]
    pub const fn new(n: u8) -> Self {
        Self(n)
    }

    /// The number of the slot.
    #[must_use]
    pub const fn get(self) -> u8 {
        self.0
    }

    /// The slot of the digits of a unit name: no sign, no leading zero
    /// but in `0`, and at most 255.
    fn parse(digits: &str) -> Option<Self> {
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        if digits.len() > 1 && digits.starts_with('0') {
            return None;
        }
        digits.parse().ok().map(Self)
    }
}

impl From<u8> for Slot {
    fn from(n: u8) -> Self {
        Self(n)
    }
}

impl fmt::Display for Slot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// One unit of a slot: `ci-runner-slot<n>@<instance>.service` directly
/// under `SLICE_PATH`. systemd chooses the instance of a socket-activated
/// unit, so any instance with no `/` is one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotUnit {
    slot: Slot,
    instance: String,
}

impl SlotUnit {
    /// The unit of the cgroup path `path`, from the cgroup root, for
    /// example `/ci.slice/ci-runner.slice/ci-runner-vms.slice/ci-runner-slot0@7-12331-2592268_2675647-973.service`.
    ///
    /// # Errors
    ///
    /// The first part of the path that is not the path of a slot unit.
    /// A path whose last part alone matches, for example one under
    /// `/user.slice/`, is `PathFault::Prefix`.
    pub fn parse(path: &str) -> Result<Self, PathFault> {
        let name = path
            .strip_prefix(SLICE_PATH)
            .and_then(|rest| rest.strip_prefix('/'))
            .ok_or(PathFault::Prefix)?;
        let (digits, rest) = name
            .strip_prefix(UNIT_PREFIX)
            .and_then(|rest| rest.split_once(INSTANCE_MARK))
            .ok_or(PathFault::Template)?;
        let slot = Slot::parse(digits).ok_or(PathFault::Number)?;
        let instance = rest.strip_suffix(UNIT_SUFFIX).ok_or(PathFault::Suffix)?;
        if instance.is_empty() || instance.contains('/') {
            return Err(PathFault::Instance);
        }
        Ok(Self {
            slot,
            instance: instance.to_owned(),
        })
    }

    /// The slot of the unit.
    #[must_use]
    pub const fn slot(&self) -> Slot {
        self.slot
    }

    /// The instance of the unit, the part between `@` and `.service`.
    #[must_use]
    pub fn instance(&self) -> &str {
        &self.instance
    }
}

impl fmt::Display for SlotUnit {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{UNIT_PREFIX}{}{INSTANCE_MARK}{}{UNIT_SUFFIX}",
            self.slot, self.instance
        )
    }
}

/// Why a cgroup path is no path of a slot unit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathFault {
    /// The path does not start with `SLICE_PATH` and one `/`.
    Prefix,
    /// The last part does not start with `ci-runner-slot`, or has no `@`.
    Template,
    /// The slot number is empty, has a sign or a leading zero, or is
    /// above 255.
    Number,
    /// The last part does not end with `.service`, or the path has a part
    /// after it.
    Suffix,
    /// The instance is empty or holds a `/`.
    Instance,
    /// The name of the folder is not UTF-8.
    Utf8,
}

impl fmt::Display for PathFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            Self::Prefix => "the path is not directly under the slice of the job VMs",
            Self::Template => "the name does not start with ci-runner-slot<n>@",
            Self::Number => "the slot number of the name is not a number from 0 to 255",
            Self::Suffix => "the name does not end with .service",
            Self::Instance => "the instance of the name is empty or holds a /",
            Self::Utf8 => "the name is not UTF-8",
        };
        f.write_str(text)
    }
}

/// The id of one cgroup v2 folder: its inode number.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct CgroupId(pub(crate) u64);

impl CgroupId {
    /// The number of the id.
    #[must_use]
    pub const fn get(self) -> u64 {
        self.0
    }
}

impl fmt::Display for CgroupId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// The inode number of the root folder of the cgroup v2 hierarchy. The
/// kernel gives the root the first id of the hierarchy, and a cgroup2
/// mount of the initial cgroup namespace shows the root (measured on
/// Linux 7.0). A mount made in another cgroup namespace shows the root of
/// that namespace, a folder with another inode.
#[cfg(any(target_os = "linux", test))]
pub(crate) const CGROUP_ROOT_INODE: u64 = 1;

/// One sub-folder of the slice folder: its name, its inode number, and
/// whether it lies on another mount than the cgroup root, so that a file
/// system is mounted on it.
#[cfg(any(target_os = "linux", test))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SliceEntry {
    pub(crate) name: std::ffi::OsString,
    pub(crate) inode: u64,
    pub(crate) other_mount: bool,
}

/// Requires that the file system at the cgroup root is cgroup v2
/// (`cgroup2`) and that its folder there is the root of the hierarchy
/// (`inode`). Only then do the sub-folders of the slice path name the
/// units that systemd made. A cgroup namespace of the service, a
/// bind mount of a sub-folder, a tmpfs or a FUSE file system there each
/// show another tree, in which a process can make a folder with any name.
///
/// # Errors
///
/// `NotCgroup2` when the file system is not cgroup v2, `NotCgroupRoot`
/// when the folder is another folder of the hierarchy.
#[cfg(any(target_os = "linux", test))]
pub(crate) fn cgroup_root(cgroup2: bool, inode: u64) -> Result<(), CgroupFault> {
    if !cgroup2 {
        return Err(CgroupFault::NotCgroup2);
    }
    if inode != CGROUP_ROOT_INODE {
        return Err(CgroupFault::NotCgroupRoot(inode));
    }
    Ok(())
}

/// Why the cgroup of the peer is no unit of the slot (step 2 of `check`).
#[derive(Debug)]
pub enum CgroupFault {
    /// `PIDFD_GET_INFO` failed. A kernel before 6.13 gives `ENOTTY` or
    /// `EINVAL`.
    Info(std::io::Error),
    /// The kernel set no `PIDFD_INFO_CGROUPID` in the returned mask.
    NoId,
    /// `/sys/fs/cgroup` could not be opened or read.
    Root(std::io::Error),
    /// The file system at `/sys/fs/cgroup` is not cgroup v2. A tmpfs or a
    /// FUSE file system there gives this.
    NotCgroup2,
    /// The folder at `/sys/fs/cgroup` is not the root of the cgroup v2
    /// hierarchy but the folder with this inode. A cgroup namespace of the
    /// service (`ProtectControlGroups=strict` or `private`, or a
    /// container) or a bind mount of a sub-folder gives this.
    NotCgroupRoot(u64),
    /// The slice folder could not be opened or listed. It is missing
    /// (`ENOENT`) on a host with no slot unit.
    Slice(std::io::Error),
    /// A file system is mounted on a part of the slice path or on a
    /// sub-folder of the slice folder. The folder there is then the
    /// mounted folder, not the folder that systemd made.
    Mounted,
    /// No sub-folder of the slice folder has the id. The peer runs in no
    /// unit of the slice: for example in a unit under `/user.slice/`, in
    /// the daemon, or in another service.
    NotInSlice,
    /// Two sub-folders of the slice folder have the id. A cgroup file
    /// system gives each folder its own id, so the host is not the host
    /// that the check knows.
    TwoFolders,
    /// The folder with the id has a name that is no slot unit.
    Unit(PathFault),
    /// The folder with the id is a unit of this other slot.
    OtherSlot(Slot),
}

impl fmt::Display for CgroupFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Info(error) => write!(f, "PIDFD_GET_INFO failed: {error}"),
            Self::NoId => f.write_str("the kernel gave no cgroup id"),
            Self::Root(error) => write!(f, "/sys/fs/cgroup could not be read: {error}"),
            Self::NotCgroup2 => f.write_str("the file system at /sys/fs/cgroup is not cgroup v2"),
            Self::NotCgroupRoot(inode) => write!(
                f,
                "/sys/fs/cgroup shows the cgroup folder with the inode {inode}, not the root of the hierarchy"
            ),
            Self::Slice(error) => write!(f, "the slice folder could not be listed: {error}"),
            Self::Mounted => f.write_str(
                "a file system is mounted on the slice path or on a sub-folder of the slice",
            ),
            Self::NotInSlice => {
                f.write_str("the cgroup of the peer is no sub-folder of the slice of the job VMs")
            }
            Self::TwoFolders => f.write_str("two sub-folders of the slice have the cgroup id"),
            Self::Unit(fault) => write!(f, "the cgroup of the peer is no slot unit: {fault}"),
            Self::OtherSlot(slot) => write!(f, "the cgroup of the peer is a unit of slot {slot}"),
        }
    }
}

/// The unit of slot `slot` whose folder in `entries` has the id `id`.
///
/// # Errors
///
/// `Mounted` when a file system is mounted on any entry, `NotInSlice`
/// when no entry has the id, `TwoFolders` when two have it, `Unit` when
/// the name of that entry is no slot unit, and `OtherSlot` when it is a
/// unit of another slot.
#[cfg(any(target_os = "linux", test))]
pub(crate) fn unit_of(
    id: CgroupId,
    entries: &[SliceEntry],
    slot: Slot,
) -> Result<SlotUnit, CgroupFault> {
    if entries.iter().any(|entry| entry.other_mount) {
        return Err(CgroupFault::Mounted);
    }
    let mut found = entries.iter().filter(|entry| entry.inode == id.0);
    let entry = found.next().ok_or(CgroupFault::NotInSlice)?;
    if found.next().is_some() {
        return Err(CgroupFault::TwoFolders);
    }
    let name = entry
        .name
        .to_str()
        .ok_or(CgroupFault::Unit(PathFault::Utf8))?;
    let unit = SlotUnit::parse(&format!("{SLICE_PATH}/{name}")).map_err(CgroupFault::Unit)?;
    if unit.slot != slot {
        return Err(CgroupFault::OtherSlot(unit.slot));
    }
    Ok(unit)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::{
        CGROUP_ROOT_INODE, CgroupFault, CgroupId, PathFault, SLICE_PATH, SliceEntry, Slot,
        SlotUnit, cgroup_root, unit_of,
    };

    /// The instance of the slot unit of the host proof of 2026-11-18.
    const PROOF_INSTANCE: &str = "7-12331-2592268_2675647-973";

    fn path(name: &str) -> String {
        format!("{SLICE_PATH}/{name}")
    }

    #[test]
    fn the_unit_of_the_host_proof_parses() {
        let unit =
            SlotUnit::parse(&path(&format!("ci-runner-slot0@{PROOF_INSTANCE}.service"))).unwrap();
        assert_eq!(unit.slot(), Slot::new(0));
        assert_eq!(unit.instance(), PROOF_INSTANCE);
        assert_eq!(
            unit.to_string(),
            format!("ci-runner-slot0@{PROOF_INSTANCE}.service")
        );
    }

    #[test]
    fn an_instance_with_dashes_and_underscores_parses() {
        // The instance of S3 of the second spike run (design section 13.3).
        let unit =
            SlotUnit::parse(&path("ci-runner-slot5@0-163868-1786815_10115136-0.service")).unwrap();
        assert_eq!(unit.slot(), Slot::new(5));
        assert_eq!(unit.instance(), "0-163868-1786815_10115136-0");
        let unit = SlotUnit::parse(&path("ci-runner-slot17@m2.service")).unwrap();
        assert_eq!(unit.slot(), Slot::new(17));
    }

    #[test]
    fn the_same_last_part_under_the_user_slice_is_refused() {
        // The path of the user unit of measurement M2(d), with the
        // automatic slice of the user manager.
        let m2 = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-ci\\x2drunner\\x2dslot0.slice/ci-runner-slot0@x.service";
        assert_eq!(SlotUnit::parse(m2), Err(PathFault::Prefix));
        assert_eq!(
            SlotUnit::parse("/user.slice/ci-runner-slot0@x.service"),
            Err(PathFault::Prefix)
        );
        assert_eq!(
            SlotUnit::parse(&format!(
                "/user.slice{SLICE_PATH}/ci-runner-slot0@x.service"
            )),
            Err(PathFault::Prefix)
        );
    }

    #[test]
    fn the_daemon_and_other_units_are_refused() {
        for daemon in [
            "/ci.slice/ci-runner.slice/ci-runner.service",
            "/system.slice/ci-runner.service",
            "/",
            "",
            SLICE_PATH,
            "/ci.slice/ci-runner.slice/ci-runner-vms.slicex/ci-runner-slot0@x.service",
        ] {
            assert_eq!(SlotUnit::parse(daemon), Err(PathFault::Prefix), "{daemon}");
        }
    }

    #[test]
    fn a_slash_in_the_instance_is_refused() {
        assert_eq!(
            SlotUnit::parse(&path("ci-runner-slot0@a/b.service")),
            Err(PathFault::Instance)
        );
        assert_eq!(
            SlotUnit::parse(&path("ci-runner-slot0@x.service/y.service")),
            Err(PathFault::Instance)
        );
        assert_eq!(
            SlotUnit::parse(&path("ci-runner-slot0@.service")),
            Err(PathFault::Instance)
        );
    }

    #[test]
    fn a_path_with_an_extra_trailing_part_is_refused() {
        for extra in [
            "ci-runner-slot0@x.service/sub",
            "ci-runner-slot0@x.service/",
            "ci-runner-slot0@x.service/cgroup.procs",
            "ci-runner-slot0@x.socket",
        ] {
            assert_eq!(
                SlotUnit::parse(&path(extra)),
                Err(PathFault::Suffix),
                "{extra}"
            );
        }
    }

    #[test]
    fn another_name_or_a_bad_number_is_refused() {
        for (name, fault) in [
            ("ci-runner-slot0.service", PathFault::Template),
            ("ci-runner-vm0@x.service", PathFault::Template),
            ("/ci-runner-slot0@x.service", PathFault::Template),
            ("ci-runner-slot@x.service", PathFault::Number),
            ("ci-runner-slot00@x.service", PathFault::Number),
            ("ci-runner-slot01@x.service", PathFault::Number),
            ("ci-runner-slot+1@x.service", PathFault::Number),
            ("ci-runner-slot256@x.service", PathFault::Number),
            ("ci-runner-slot0x@x.service", PathFault::Number),
        ] {
            assert_eq!(SlotUnit::parse(&path(name)), Err(fault), "{name}");
        }
        assert_eq!(
            SlotUnit::parse(&path("ci-runner-slot255@x.service"))
                .unwrap()
                .slot(),
            Slot::new(255)
        );
    }

    fn entry(name: &str, inode: u64) -> SliceEntry {
        SliceEntry {
            name: OsString::from(name),
            inode,
            other_mount: false,
        }
    }

    #[test]
    fn only_the_root_of_a_cgroup2_mount_is_accepted() {
        assert!(cgroup_root(true, CGROUP_ROOT_INODE).is_ok());
        // A tmpfs or a FUSE file system that reports the inode 1.
        assert!(matches!(
            cgroup_root(false, CGROUP_ROOT_INODE),
            Err(CgroupFault::NotCgroup2)
        ));
        // The mount of a cgroup namespace rooted at the scratch cgroup of
        // the proof of review finding 1, inode 24328.
        assert!(matches!(
            cgroup_root(true, 24328),
            Err(CgroupFault::NotCgroupRoot(24328))
        ));
    }

    #[test]
    fn a_mount_on_any_sub_folder_of_the_slice_is_refused() {
        // A mount on the folder of the unit shows the inode of the mounted
        // folder. So the entry with the id may be that mount.
        let mut mounted = entries();
        if let Some(first) = mounted.first_mut() {
            first.other_mount = true;
        }
        assert!(matches!(
            unit_of(CgroupId(26232), &mounted, Slot::new(0)),
            Err(CgroupFault::Mounted)
        ));
        assert!(matches!(
            unit_of(CgroupId(28495), &mounted, Slot::new(1)),
            Err(CgroupFault::Mounted)
        ));
    }

    /// The sub-folders of a slice folder with two runs, as M2(e) saw them.
    fn entries() -> Vec<SliceEntry> {
        vec![
            entry(&format!("ci-runner-slot0@{PROOF_INSTANCE}.service"), 26232),
            entry("ci-runner-slot1@proof.service", 28495),
        ]
    }

    #[test]
    fn the_folder_of_the_id_gives_the_unit_of_the_slot() {
        let unit = unit_of(CgroupId(26232), &entries(), Slot::new(0)).unwrap();
        assert_eq!(unit.instance(), PROOF_INSTANCE);
        let unit = unit_of(CgroupId(28495), &entries(), Slot::new(1)).unwrap();
        assert_eq!(unit.slot(), Slot::new(1));
    }

    #[test]
    fn the_unit_of_another_slot_is_refused() {
        // M2(e): the real CH of slot 0, checked against slot 1.
        assert!(matches!(
            unit_of(CgroupId(26232), &entries(), Slot::new(1)),
            Err(CgroupFault::OtherSlot(slot)) if slot == Slot::new(0)
        ));
    }

    #[test]
    fn an_id_outside_the_slice_is_refused() {
        // M2(d) and M2(g): the user unit got 28874, the daemon cgroup 10812.
        for id in [28874, 10812] {
            assert!(matches!(
                unit_of(CgroupId(id), &entries(), Slot::new(0)),
                Err(CgroupFault::NotInSlice)
            ));
        }
        assert!(matches!(
            unit_of(CgroupId(26232), &[], Slot::new(0)),
            Err(CgroupFault::NotInSlice)
        ));
    }

    #[test]
    fn two_folders_with_the_id_are_refused() {
        let mut two = entries();
        two.push(entry("ci-runner-slot0@other.service", 26232));
        assert!(matches!(
            unit_of(CgroupId(26232), &two, Slot::new(0)),
            Err(CgroupFault::TwoFolders)
        ));
    }

    #[test]
    fn a_folder_of_the_id_with_another_name_is_refused() {
        let others = vec![
            entry("ci-runner-slot0@x.scope", 7),
            entry("cgroup-of-a-test", 8),
        ];
        assert!(matches!(
            unit_of(CgroupId(7), &others, Slot::new(0)),
            Err(CgroupFault::Unit(PathFault::Suffix))
        ));
        assert!(matches!(
            unit_of(CgroupId(8), &others, Slot::new(0)),
            Err(CgroupFault::Unit(PathFault::Template))
        ));
    }

    #[cfg(unix)]
    #[test]
    fn a_folder_name_that_is_not_utf8_is_refused() {
        use std::os::unix::ffi::OsStringExt;
        let others = vec![SliceEntry {
            name: OsString::from_vec(b"ci-runner-slot0@\xff.service".to_vec()),
            inode: 9,
            other_mount: false,
        }];
        assert!(matches!(
            unit_of(CgroupId(9), &others, Slot::new(0)),
            Err(CgroupFault::Unit(PathFault::Utf8))
        ));
    }
}
