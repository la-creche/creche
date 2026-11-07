//! The program `vmm serve` and its config (plan PR L20). It needs no KVM.
//! A missing or bad key exits 78 and names the key (owner item 86), and so
//! does an untrusted CH program (decision CH67 rule 1). Each test waits
//! for the vmm to exit, so no test leaves a vmm behind. The wait has a
//! bound, so a vmm that serves fails the test and does not hang it. Each
//! run has a hard `RLIMIT_NOFILE` below the fd need of the vmm, so each
//! test also proves that its check refuses the start before the fd raise
//! (decision CH67 rule 4). The ignored `kvm_` test passes every start
//! check on root-owned files, so the short hard limit itself refuses the
//! start, before the bind of `vmm.sock`. The job kvm runs it, and its
//! runner may use sudo (decision CH36 G20).

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use creche_vmm::config::{
    BUDGET, CH, CH_REMOTE, CHAPERONE, EGRESS_LOG, GIT, GUEST_INIT, LITELLM, MIRROR_ROOT, RAM_CAP,
    STATE_ROOT,
};
use creche_vmm::egress::proxy;
use creche_vmm::image_root::DIGESTS_FILE;
use creche_vmm::{family_state, mirror, planes};
use creche_vmm_confine::vmm_unit::VmmUnit;

/// The exit status of a refused start.
const EX_CONFIG: i32 = 78;

/// A program that does nothing and exits 0.
const TRUE: &str = "/usr/bin/true";

/// Each key with a good value. The paths need not exist: the config
/// checks their form only.
const GOOD: [(&str, &str); 21] = [
    ("VMM_STATE_ROOT", "/srv/vmm/state"),
    ("RUNTIME_DIRECTORY", "/run/user/1000/vmm"),
    ("VMM_FAMILY_ROOT", "/srv/families"),
    ("VMM_FAULTS_ROOT", "/srv/faults"),
    ("VMM_IMAGE_ROOT", "/opt/components/disks"),
    ("VMM_CH_PROGRAM", "/opt/components/vmm/bin/cloud-hypervisor"),
    ("VMM_CH_REMOTE_PROGRAM", "/opt/components/vmm/bin/ch-remote"),
    (
        "VMM_GUEST_INIT_PROGRAM",
        "/opt/components/vmm/bin/guest-init",
    ),
    ("AGENT_SYSTEMD_RUN_PROGRAM", "/usr/bin/systemd-run"),
    ("AGENT_SYSTEMCTL_PROGRAM", "/usr/bin/systemctl"),
    ("VMM_ZSTD_PROGRAM", "/usr/bin/zstd"),
    ("VMM_MKE2FS_PROGRAM", "/usr/sbin/mke2fs"),
    ("VMM_FALLOCATE_PROGRAM", "/usr/bin/fallocate"),
    ("VMM_RAM_CAP_GIB", "80"),
    ("VMM_FREE_FLOOR_GIB", "50"),
    ("VMM_SNAPSHOT_BUDGET_GIB", "150"),
    ("VMM_LITELLM_ADDR", "192.0.2.10:4000"),
    ("VMM_CHAPERONE_ADDR", "192.0.2.10:8300"),
    ("VMM_EGRESS_LOG", "/srv/vmm/egress.jsonl"),
    ("VMM_MIRROR_ROOT", "/srv/agents/code"),
    ("VMM_GIT_PROGRAM", "/usr/bin/git"),
];

/// The CH fixture of the version check (gap G10). Each scratch dir gets a
/// copy, so the `kvm_` test can make root the owner of the copy.
const CH_FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/cloud-hypervisor"
);

/// Each program key whose file the start checks, with its file in `bin/`
/// (decision CH67 rule 1).
const PROGRAMS: [(&str, &str); 3] = [
    (CH, "cloud-hypervisor"),
    (CH_REMOTE, "ch-remote"),
    (GUEST_INIT, "guest-init"),
];

/// The kernel, the base disk and the python disk, each with the SHA-256
/// of its bytes.
const IMAGES: [(&str, &str, &str); 3] = [
    (
        "vmlinux",
        "",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    (
        "base.ext4",
        "abc",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ),
    (
        "python.ext4",
        "hello world",
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
    ),
];

/// A scratch dir under `/tmp` that its drop removes, also after a failed
/// assert. `/tmp`, not `temp_dir`: the run root bounds the socket paths
/// under it (`SOCKET_PATH_MAX`), and the temp dir of macOS is too long.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Scratch {
    /// The empty dirs of a vmm start.
    fn new(name: &str) -> Self {
        let dir = Path::new("/tmp").join(format!("creche-vmm-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        for sub in ["state", "run", "families", "faults", "images", "bin"] {
            std::fs::create_dir_all(dir.join(sub)).unwrap();
        }
        Self(dir)
    }

    /// The text of the path `sub` in the dir.
    fn path(&self, sub: &str) -> String {
        self.0.join(sub).to_str().unwrap().to_owned()
    }

    /// `GOOD` with each root key on its dir and each key of `PROGRAMS` on
    /// its file in `bin/`. The egress log is `egress.jsonl` in the dir, so
    /// its open passes, as in `src/compose/tests.rs`. The mirror root is
    /// `mirror/` in the dir, which no start step opens. Each other program
    /// key is `true`, which lists no leftover scope and passes the owner
    /// check of `VMM_GIT_PROGRAM` (decision CH69 rule 17).
    fn env(&self) -> Vec<(&'static str, String)> {
        GOOD.iter()
            .map(|&(key, good)| {
                let value = match key {
                    "VMM_STATE_ROOT" => self.path("state"),
                    "RUNTIME_DIRECTORY" => self.path("run"),
                    "VMM_FAMILY_ROOT" => self.path("families"),
                    "VMM_FAULTS_ROOT" => self.path("faults"),
                    "VMM_IMAGE_ROOT" => self.path("images"),
                    "VMM_EGRESS_LOG" => self.path("egress.jsonl"),
                    "VMM_MIRROR_ROOT" => self.path("mirror"),
                    key if key.ends_with("_GIB") || key.ends_with("_ADDR") => good.to_owned(),
                    key => match PROGRAMS.iter().find(|program| program.0 == key) {
                        Some((_, file)) => self.path(&format!("bin/{file}")),
                        None => TRUE.to_owned(),
                    },
                };
                (key, value)
            })
            .collect()
    }

    /// The files that the start checks, as `Scratch::new` and `trust` of
    /// `src/compose/tests.rs` make them: the images, a `digests.json`
    /// and the three programs of `PROGRAMS`, each owned by root (decisions
    /// CH57 rule 1, CH67 rule 1). That test module is a `cfg(test)` part
    /// of the lib, so this test crate cannot import it. So this copies only
    /// the files and the sudo that `vmm serve` needs.
    fn trusted(&self) {
        for (file, bytes, _) in IMAGES {
            std::fs::write(self.0.join("images").join(file), bytes).unwrap();
        }
        let entry = |(file, _, sha256): (&str, &str, &str)| serde_json::json!({ "file": file, "sha256": sha256 });
        let digests = serde_json::json!({
            "kernel": entry(IMAGES[0]),
            "base": entry(IMAGES[1]),
            "python": entry(IMAGES[2]),
        });
        let digests_file = self.0.join("images").join(DIGESTS_FILE);
        std::fs::write(&digests_file, digests.to_string()).unwrap();
        std::fs::write(self.0.join("bin/guest-init"), b"guest-init").unwrap();
        place_program(Path::new(CH_FIXTURE), &self.0.join("bin/cloud-hypervisor"));
        let remote = self.0.join("bin/ch-remote.src");
        std::fs::write(&remote, "#!/bin/sh\nexit 1\n").unwrap();
        place_program(&remote, &self.0.join("bin/ch-remote"));

        sudo(&["chown", "0:0"], &digests_file);
        for (_, file) in PROGRAMS {
            sudo(&["chown", "0:0"], &self.0.join("bin").join(file));
        }
    }
}

/// Copies `source` to `dest` as an executable through a `cp` child, as
/// `src/compose/tests.rs` does. The write fd of the copy then lives only
/// in that child, so no fork of a parallel test can hold it open for
/// writing, and the exec of `dest` never fails with ETXTBSY.
#[cfg(test)]
fn place_program(source: &Path, dest: &Path) {
    let status = Command::new("cp").arg(source).arg(dest).status().unwrap();
    assert!(status.success(), "cp {} failed", source.display());
    std::fs::set_permissions(dest, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// Runs `command` on `path` with the sudo of the runner, such as a
/// `chown 0:0`. Decision CH36 G20 allows sudo on GitHub's throwaway
/// runners only, never on the host, so it refuses outside GitHub Actions.
#[cfg(test)]
fn sudo(command: &[&str], path: &Path) {
    assert_eq!(
        std::env::var("GITHUB_ACTIONS").as_deref(),
        Ok("true"),
        "sudo runs only on a GitHub runner (decision CH36 G20)"
    );
    let status = Command::new("sudo")
        .arg("-n")
        .args(command)
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success(), "sudo {command:?}: {status}");
}

/// A hard `RLIMIT_NOFILE` below the fd need of the vmm (102720), and
/// below the hard limit of each host that runs the tests.
const SHORT_NOFILE: &str = "1024";

/// The longest wait for the exit of `vmm serve`. A refused start exits
/// in well under a second. A vmm that passes its start serves until
/// SIGTERM, so the wait ends the test instead of a hang.
const SERVE_WAIT: Duration = Duration::from_secs(60);

/// The time between two checks for the exit of the vmm.
const POLL: Duration = Duration::from_millis(50);

/// Runs `vmm serve` with only the keys of `env` and gives its stderr. A
/// shell sets the soft and the hard `RLIMIT_NOFILE` to `SHORT_NOFILE`,
/// then execs the vmm. A vmm that runs past `SERVE_WAIT` gets SIGKILL,
/// and the test panics with its stderr.
#[cfg(test)]
fn serve(env: &[(&str, &str)]) -> (Output, String) {
    let mut child = Command::new("/bin/sh")
        .args([
            "-c",
            "ulimit -n \"$1\" && exec \"$0\" serve",
            env!("CARGO_BIN_EXE_vmm"),
            SHORT_NOFILE,
        ])
        .env_clear()
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + SERVE_WAIT;
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            let stderr = String::from_utf8_lossy(&output.stderr);
            panic!("vmm serve ran past {SERVE_WAIT:?}: {stderr}");
        }
        std::thread::sleep(POLL);
    }
    let output = child.wait_with_output().unwrap();
    let stderr = String::from_utf8(output.stderr.clone()).unwrap();
    (output, stderr)
}

#[test]
fn an_empty_env_exits_78_and_names_the_first_key() {
    let (output, stderr) = serve(&[]);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, format!("vmm: {STATE_ROOT}: the key has no value\n"));
}

#[test]
fn a_bad_key_exits_78_and_names_the_key() {
    let mut env = GOOD.to_vec();
    for one in &mut env {
        if one.0 == BUDGET {
            one.1 = "0";
        }
    }
    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, format!("vmm: {BUDGET}: the key is 0\n"));

    let env: Vec<_> = GOOD
        .iter()
        .copied()
        .filter(|one| one.0 != RAM_CAP)
        .collect();
    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, format!("vmm: {RAM_CAP}: the key has no value\n"));
}

/// Decision CH62 rule 17: the vmm needs its guest init program, so an env
/// with no `VMM_GUEST_INIT_PROGRAM` refuses the start.
#[test]
fn a_missing_guest_init_key_exits_78() {
    let env: Vec<_> = GOOD
        .iter()
        .copied()
        .filter(|one| one.0 != GUEST_INIT)
        .collect();
    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(
        stderr,
        format!(
            "vmm: {GUEST_INIT}: the key has no value
"
        )
    );
}

/// Decision CH62 rule 20: the vmm needs the address of each plane, so an
/// env with no `VMM_LITELLM_ADDR` or no `VMM_CHAPERONE_ADDR` refuses the
/// start.
#[test]
fn a_missing_plane_key_exits_78() {
    for key in [LITELLM, CHAPERONE] {
        let env: Vec<_> = GOOD.iter().copied().filter(|one| one.0 != key).collect();
        let (output, stderr) = serve(&env);
        assert_eq!(output.status.code(), Some(EX_CONFIG));
        assert_eq!(stderr, format!("vmm: {key}: the key has no value\n"));
    }
}

/// Decisions CH59 rule 1 and CH69 rule 10: the vmm needs the path of the
/// egress log, so an env with no `VMM_EGRESS_LOG` refuses the start.
#[test]
fn a_missing_egress_log_key_exits_78() {
    let env: Vec<_> = GOOD
        .iter()
        .copied()
        .filter(|one| one.0 != EGRESS_LOG)
        .collect();
    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, format!("vmm: {EGRESS_LOG}: the key has no value\n"));
}

/// The egress log is a path key: a relative path refuses the start.
#[test]
fn a_relative_egress_log_exits_78() {
    let mut env = GOOD.to_vec();
    for one in &mut env {
        if one.0 == EGRESS_LOG {
            one.1 = "egress.jsonl";
        }
    }
    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    let expected = format!("vmm: {EGRESS_LOG}: the key is not an absolute path\n");
    assert_eq!(stderr, expected);
}

/// Decisions CH59 rule 1 and CH69 rule 17: the vmm needs the mirror root
/// and its git, so an env with no `VMM_MIRROR_ROOT` or no
/// `VMM_GIT_PROGRAM` refuses the start.
#[test]
fn a_missing_mirror_key_exits_78() {
    for key in [MIRROR_ROOT, GIT] {
        let env: Vec<_> = GOOD.iter().copied().filter(|one| one.0 != key).collect();
        let (output, stderr) = serve(&env);
        assert_eq!(output.status.code(), Some(EX_CONFIG));
        assert_eq!(stderr, format!("vmm: {key}: the key has no value\n"));
    }
}

/// Each mirror key is a path key: a relative path refuses the start
/// (decision CH69 rule 17).
#[test]
fn a_relative_mirror_key_exits_78() {
    for (key, relative) in [(MIRROR_ROOT, "srv/agents/code"), (GIT, "git")] {
        let mut env = GOOD.to_vec();
        for one in &mut env {
            if one.0 == key {
                one.1 = relative;
            }
        }
        let (output, stderr) = serve(&env);
        assert_eq!(output.status.code(), Some(EX_CONFIG));
        let expected = format!("vmm: {key}: the key is not an absolute path\n");
        assert_eq!(stderr, expected);
    }
}

/// A good config on temp paths runs the leftover sweep (`true` lists no
/// scope). Then the vmm refuses a missing CH program and one that root
/// does not own, and exits 78 (decision CH67 rule 1, owner item 86). The
/// short hard fd limit does not hide either fault, because the fd raise
/// runs after the start checks (decision CH67 rule 4). A test cannot make
/// a root-owned file without sudo, so the serve proof of plan PR L20 runs
/// on the compose step (`src/compose/tests.rs`).
#[test]
fn an_untrusted_ch_program_exits_78() {
    let scratch = Scratch::new("binary");
    let owned = scratch.env();
    let env: Vec<_> = owned
        .iter()
        .map(|(key, value)| (*key, value.as_str()))
        .collect();
    let ch = scratch.path("bin/cloud-hypervisor");

    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG), "{stderr}");
    assert!(
        stderr.starts_with("vmm: VMM_CH_PROGRAM: the program cannot be read: "),
        "{stderr}"
    );

    std::fs::copy(CH_FIXTURE, &ch).unwrap();
    let uid = std::fs::metadata(&ch).unwrap().uid();
    let (output, stderr) = serve(&env);
    if uid != 0 {
        assert_eq!(output.status.code(), Some(EX_CONFIG), "{stderr}");
        let expected =
            format!("vmm: VMM_CH_PROGRAM: the program is owned by uid {uid}, not by root\n");
        assert_eq!(stderr, expected);
    }
}

/// Decision CH67 rule 4: with every start check passed, a hard
/// `RLIMIT_NOFILE` below the fd need refuses the start with exit 78 and
/// names both numbers. The raise runs before the bind, so no `vmm.sock`
/// exists. A raise after the bind would leave `vmm.sock` in the run root.
/// A vmm in no service unit prints the unit line of `parts` first. The
/// vmm runs in the cgroup of this test, so `VmmUnit::of_self` here gives
/// the same answer (decision CH60 rule 1).
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_short_hard_fd_limit_refuses_before_the_bind() {
    let scratch = Scratch::new("nofile");
    scratch.trusted();
    let owned = scratch.env();
    let env: Vec<_> = owned
        .iter()
        .map(|(key, value)| (*key, value.as_str()))
        .collect();

    let (output, stderr) = serve(&env);
    let need = planes::fd_need() + family_state::fd_need() + mirror::fd_need() + proxy::fd_need();
    assert_eq!(need, 102_720);
    assert_eq!(output.status.code(), Some(EX_CONFIG), "{stderr}");
    let unit = match VmmUnit::of_self() {
        Ok(_) => String::new(),
        Err(error) => format!("vmm: {error}, so every VM launch refuses\n"),
    };
    let nofile =
        format!("vmm: the hard RLIMIT_NOFILE is {SHORT_NOFILE}, and the vmm needs {need} fds\n");
    assert_eq!(stderr, format!("{unit}{nofile}"));
    assert!(!scratch.0.join("run/vmm.sock").exists());
}
