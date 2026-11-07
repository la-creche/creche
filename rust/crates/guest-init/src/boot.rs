//! The boot of guest init as PID 1 (decision CH62 rule 4).
//!
//! `plan` is the fixed list of boot steps. It is pure, so the unit tests
//! check it on any OS. `boot` runs each step on Linux, writes one console
//! line per step and stops at the first fault (decision CH62 rule 26).
//!
//! The layout:
//! 1. devtmpfs on `/dev` of the initramfs, then devpts and a tmpfs at
//!    `/dev/shm` in it. devtmpfs gives the disk nodes.
//! 2. The root disk ext4, read-only and nodev, at `STAGING`. CH gives the
//!    root disk first, so it is `/dev/vda` (decisions CH8-7, CH8-8, CH50
//!    rule 10).
//! 3. The session disk `/dev/vdb` ext4, read-write, nosuid and nodev, at
//!    the session mount of `creche_wire::guest` (decision CH62 rule 5).
//!    No noexec: tools run code from the workspace.
//! 4. tmpfs, nosuid and nodev, on `/tmp`, `/run`, `/root`, `/var/cache`
//!    and `/var/tmp`, then proc and sysfs, all under `STAGING`.
//! 5. `/dev` moves under `STAGING`. Then the switch root: `STAGING` moves
//!    onto `/` and guest init chroots into it. `pivot_root` does not work
//!    from the initramfs, and guest init stays PID 1.
//! 6. `lo` up, because the planes listen on 127.0.0.1 and the guest has
//!    no network card (decisions CH6-2, CH6-3).
//!
//! A fault runs no fsck: the initramfs holds no e2fsck, and the ext4
//! journal covers a power cut (decision CH62 rule 4).

use std::ffi::CStr;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

use creche_wire::guest::SESSION_DISK_MOUNT;

/// The dir of the initramfs where the root disk mounts before the switch
/// root. The initramfs of `bin/vm-smoke-setup.sh` holds only `/init` and
/// `/dev/console`, so the plan makes it.
const STAGING: &str = "/newroot";

/// The root disk, the first disk of the CH command (decisions CH8-7,
/// CH50 rule 10).
const ROOT_DISK: &str = "/dev/vda";

/// The session disk, the second disk of the CH command (decisions CH8-8,
/// CH50 rule 10).
const SESSION_DISK: &str = "/dev/vdb";

/// The tmpfs mounts of the new root and the mode of each root dir. tmpfs
/// gives mode 1777 when no mode is set, so each mount names its mode: the
/// modes of these dirs in Debian.
///
/// CH62 rule 4 names these five dirs, and CH65 rule 1 sets the Debian mode
/// of each. A change edits only the mode strings.
const TMPFS: [(&str, &CStr); 5] = [
    ("/tmp", c"mode=1777"),
    ("/run", c"mode=0755"),
    ("/root", c"mode=0700"),
    ("/var/cache", c"mode=0755"),
    ("/var/tmp", c"mode=1777"),
];

/// The mount flags of one mount, apart from the switch-root move.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Options {
    pub read_only: bool,
    pub nosuid: bool,
    pub nodev: bool,
    pub noexec: bool,
}

/// devtmpfs and devpts: their nodes are devices, so no nodev.
///
/// CH65 rule 1 sets nosuid and noexec, as Debian mounts `/dev` and
/// `/dev/pts`, since no program runs from them. A change edits only this
/// constant.
const DEVICES: Options = Options {
    read_only: false,
    nosuid: true,
    nodev: false,
    noexec: true,
};

/// proc and sysfs.
///
/// CH65 rule 1 sets nosuid, nodev and noexec, as Debian mounts `/proc` and
/// `/sys`. A change edits only this constant.
const KERNEL: Options = Options {
    read_only: false,
    nosuid: true,
    nodev: true,
    noexec: true,
};

/// Each tmpfs and the session disk (decision CH62 rule 4).
const WRITABLE: Options = Options {
    read_only: false,
    nosuid: true,
    nodev: true,
    noexec: false,
};

/// The root disk (decision CH62 rule 4).
const READ_ONLY: Options = Options {
    read_only: true,
    nosuid: false,
    nodev: true,
    noexec: false,
};

/// What a mount mounts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// A block device with an ext4 file system.
    Ext4(&'static str),
    /// A file system of the kernel, such as `proc`. The type is also the
    /// source name.
    Kernel(&'static str),
}

/// One mount of the plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mount {
    /// The name in the console lines.
    pub name: &'static str,
    pub source: Source,
    /// The mount point at the time of the mount.
    pub target: PathBuf,
    pub options: Options,
    /// The file system data, such as a tmpfs mode.
    pub data: Option<&'static CStr>,
}

/// One step of the boot plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    /// Makes a dir with mode 0755, or finds it.
    MakeDir(PathBuf),
    Mount(Mount),
    /// Moves a mount and every mount under it (MS_MOVE).
    Move {
        from: PathBuf,
        to: PathBuf,
    },
    /// Moves `STAGING` onto `/` and chroots into it.
    SwitchRoot,
    /// Brings `lo` up with one RTM_NEWLINK message.
    LoopbackUp,
}

impl Step {
    /// The console line after the step ran.
    pub fn done(&self) -> String {
        match self {
            Self::MakeDir(dir) => format!("made {}", dir.display()),
            Self::Mount(mount) => {
                let mode = if mount.options.read_only { "ro" } else { "rw" };
                format!("{} mounted {mode}", mount.name)
            }
            Self::Move { from, to } => format!("moved {} to {}", from.display(), to.display()),
            Self::SwitchRoot => "root switched".to_owned(),
            Self::LoopbackUp => "lo up".to_owned(),
        }
    }

    /// The step in a fault line.
    fn what(&self) -> String {
        match self {
            Self::MakeDir(dir) => format!("make {}", dir.display()),
            Self::Mount(mount) => format!("{} mount", mount.name),
            Self::Move { from, .. } => format!("move {}", from.display()),
            Self::SwitchRoot => "switch root".to_owned(),
            Self::LoopbackUp => "lo up".to_owned(),
        }
    }
}

/// A step that failed. Its text is the console line of the fault.
#[derive(Debug)]
pub struct Fault {
    what: String,
    error: io::Error,
}

impl Fault {
    fn new(step: &Step, error: io::Error) -> Self {
        Self {
            what: step.what(),
            error,
        }
    }
}

impl fmt::Display for Fault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} failed: {}", self.what, self.error)
    }
}

/// `path` of the new root, under `STAGING`.
fn staged(path: &str) -> PathBuf {
    Path::new(STAGING).join(path.trim_start_matches('/'))
}

/// A mount of the kernel file system `fs` at `target`.
fn kernel(
    fs: &'static str,
    name: &'static str,
    target: PathBuf,
    options: Options,
    data: Option<&'static CStr>,
) -> Step {
    Step::Mount(Mount {
        name,
        source: Source::Kernel(fs),
        target,
        options,
        data,
    })
}

/// The boot plan, in order.
pub fn plan() -> Vec<Step> {
    let mut steps = vec![
        kernel(
            "devtmpfs",
            "/dev",
            "/dev".into(),
            DEVICES,
            Some(c"mode=0755"),
        ),
        Step::MakeDir("/dev/pts".into()),
        kernel("devpts", "/dev/pts", "/dev/pts".into(), DEVICES, None),
        Step::MakeDir("/dev/shm".into()),
        kernel(
            "tmpfs",
            "/dev/shm",
            "/dev/shm".into(),
            WRITABLE,
            Some(c"mode=1777"),
        ),
        Step::MakeDir(STAGING.into()),
        Step::Mount(Mount {
            name: "root",
            source: Source::Ext4(ROOT_DISK),
            target: STAGING.into(),
            options: READ_ONLY,
            data: None,
        }),
        Step::Mount(Mount {
            name: "session",
            source: Source::Ext4(SESSION_DISK),
            target: staged(SESSION_DISK_MOUNT),
            options: WRITABLE,
            data: None,
        }),
    ];
    steps.extend(
        TMPFS
            .into_iter()
            .map(|(name, mode)| kernel("tmpfs", name, staged(name), WRITABLE, Some(mode))),
    );
    steps.extend([
        kernel("proc", "/proc", staged("/proc"), KERNEL, None),
        kernel("sysfs", "/sys", staged("/sys"), KERNEL, None),
        Step::Move {
            from: "/dev".into(),
            to: staged("/dev"),
        },
        Step::SwitchRoot,
        Step::LoopbackUp,
    ]);
    steps
}

/// Runs the boot plan. Each step that ran writes its console line.
///
/// # Errors
///
/// The first step that fails. The steps before it stay done.
#[cfg(target_os = "linux")]
pub fn boot() -> Result<(), Fault> {
    for step in plan() {
        run(&step).map_err(|error| Fault::new(&step, error))?;
        crate::say(&step.done());
    }
    Ok(())
}

/// Runs one step.
#[cfg(target_os = "linux")]
fn run(step: &Step) -> io::Result<()> {
    use rustix::fs::{Mode, mkdir};
    use rustix::io::Errno;
    use rustix::mount::{mount, mount_move};
    use rustix::process::{chdir, chroot};

    match step {
        // A dir that exists counts: the mount on it fails if it is no dir.
        Step::MakeDir(dir) => match mkdir(dir, Mode::from_raw_mode(0o755)) {
            Ok(()) | Err(Errno::EXIST) => {}
            Err(error) => return Err(error.into()),
        },
        Step::Mount(each) => {
            let (source, fs) = match each.source {
                Source::Ext4(device) => (device, "ext4"),
                Source::Kernel(fs) => (fs, fs),
            };
            mount(source, &each.target, fs, flags(each.options), each.data)?;
        }
        Step::Move { from, to } => mount_move(from, to)?,
        Step::SwitchRoot => {
            chdir(STAGING)?;
            mount_move(".", "/")?;
            chroot(".")?;
            chdir("/")?;
        }
        Step::LoopbackUp => crate::lo::up()?,
    }
    Ok(())
}

/// The flags of the read-only remount of the session disk at power off:
/// the flags of its boot mount, read-only added. A remount sets each
/// flag again, so nosuid and nodev stay.
#[cfg(target_os = "linux")]
pub fn session_remount_flags() -> rustix::mount::MountFlags {
    flags(Options {
        read_only: true,
        ..WRITABLE
    })
}

/// The kernel mount flags of `options`.
#[cfg(target_os = "linux")]
fn flags(options: Options) -> rustix::mount::MountFlags {
    use rustix::mount::MountFlags;

    [
        (options.read_only, MountFlags::RDONLY),
        (options.nosuid, MountFlags::NOSUID),
        (options.nodev, MountFlags::NODEV),
        (options.noexec, MountFlags::NOEXEC),
    ]
    .into_iter()
    .filter(|(set, _)| *set)
    .fold(MountFlags::empty(), |all, (_, flag)| all | flag)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mounts() -> Vec<Mount> {
        plan()
            .into_iter()
            .filter_map(|step| match step {
                Step::Mount(mount) => Some(mount),
                _ => None,
            })
            .collect()
    }

    fn mount(name: &str) -> Mount {
        mounts()
            .into_iter()
            .find(|mount| mount.name == name)
            .unwrap()
    }

    #[test]
    fn the_plan_mounts_root_read_only() {
        let root = mount("root");
        assert_eq!(root.source, Source::Ext4("/dev/vda"));
        assert_eq!(root.target, Path::new("/newroot"));
        assert!(root.options.read_only);
        assert!(root.options.nodev);
        assert_eq!(Step::Mount(root).done(), "root mounted ro");
    }

    #[test]
    fn the_plan_mounts_the_session_disk_nosuid_nodev() {
        let session = mount("session");
        assert_eq!(session.source, Source::Ext4("/dev/vdb"));
        assert_eq!(session.target, Path::new("/newroot/var/lib/creche/session"));
        assert_eq!(session.options, WRITABLE);
        assert!(!session.options.noexec);
        assert_eq!(Step::Mount(session).done(), "session mounted rw");
    }

    /// Each mount point exists when its mount runs: a dir of the plan, a
    /// dir of the initramfs or a dir of the root disk.
    #[test]
    fn the_plan_lists_each_mount_once_in_order() {
        let names: Vec<_> = mounts().iter().map(|mount| mount.name).collect();
        assert_eq!(
            names,
            [
                "/dev",
                "/dev/pts",
                "/dev/shm",
                "root",
                "session",
                "/tmp",
                "/run",
                "/root",
                "/var/cache",
                "/var/tmp",
                "/proc",
                "/sys",
            ]
        );
        let steps = plan();
        let root = steps
            .iter()
            .position(|step| matches!(step, Step::Mount(mount) if mount.name == "root"))
            .unwrap();
        let made = steps
            .iter()
            .position(|step| *step == Step::MakeDir(STAGING.into()))
            .unwrap();
        assert!(made < root);
        assert_eq!(
            steps.get(steps.len() - 3..).unwrap(),
            [
                Step::Move {
                    from: "/dev".into(),
                    to: "/newroot/dev".into()
                },
                Step::SwitchRoot,
                Step::LoopbackUp,
            ]
        );
    }

    /// The flags and the data of every mount (CH62 rule 4, and the
    /// CH65 rule 1 readings on `TMPFS`, `DEVICES` and `KERNEL`).
    #[test]
    fn the_plan_gives_each_mount_its_options() {
        let expected = [
            ("/dev", DEVICES, Some(c"mode=0755")),
            ("/dev/pts", DEVICES, None),
            ("/dev/shm", WRITABLE, Some(c"mode=1777")),
            ("root", READ_ONLY, None),
            ("session", WRITABLE, None),
            ("/tmp", WRITABLE, Some(c"mode=1777")),
            ("/run", WRITABLE, Some(c"mode=0755")),
            ("/root", WRITABLE, Some(c"mode=0700")),
            ("/var/cache", WRITABLE, Some(c"mode=0755")),
            ("/var/tmp", WRITABLE, Some(c"mode=1777")),
            ("/proc", KERNEL, None),
            ("/sys", KERNEL, None),
        ];
        let got: Vec<_> = mounts()
            .into_iter()
            .map(|mount| (mount.name, mount.options, mount.data))
            .collect();
        assert_eq!(got, expected);
    }

    /// The kernel flags of each set of options. `flags` is the only path
    /// from the plan to `mount(2)`, so this pins what reaches the kernel.
    #[cfg(target_os = "linux")]
    #[test]
    fn each_set_of_options_gives_its_kernel_flags() {
        use rustix::mount::MountFlags;

        assert_eq!(flags(READ_ONLY), MountFlags::RDONLY | MountFlags::NODEV);
        assert_eq!(flags(WRITABLE), MountFlags::NOSUID | MountFlags::NODEV);
        assert_eq!(
            flags(KERNEL),
            MountFlags::NOSUID | MountFlags::NODEV | MountFlags::NOEXEC
        );
        assert_eq!(flags(DEVICES), MountFlags::NOSUID | MountFlags::NOEXEC);
        assert_eq!(
            session_remount_flags(),
            MountFlags::RDONLY | MountFlags::NOSUID | MountFlags::NODEV
        );
    }

    #[test]
    fn each_tmpfs_names_its_mode() {
        for mount in mounts() {
            if mount.source == Source::Kernel("tmpfs") {
                assert!(mount.data.is_some(), "{} has no mode", mount.name);
            }
        }
    }

    #[test]
    fn a_fault_line_names_the_step() {
        let fault = Fault::new(&Step::Mount(mount("root")), io::Error::other("no disk"));
        assert_eq!(fault.to_string(), "root mount failed: no disk");
    }
}
