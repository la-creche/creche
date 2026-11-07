//! The program `vmm serve` and its config (plan PR L20). It needs no KVM.
//! A missing or bad key exits 78 and names the key (owner item 86), and so
//! does an untrusted CH program (decision CH67 rule 1). Each test waits
//! for the vmm to exit, so no test leaves a vmm behind.

use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{BUDGET, CHAPERONE, GUEST_INIT, LITELLM, RAM_CAP, STATE_ROOT};

/// The exit status of a refused start.
const EX_CONFIG: i32 = 78;

/// A program that does nothing and exits 0.
const TRUE: &str = "/usr/bin/true";

/// Each key with a good value. The paths need not exist: the config
/// checks their form only.
const GOOD: [(&str, &str); 18] = [
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
];

/// A scratch dir that its drop removes, also after a failed assert.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Runs `vmm serve` with only the keys of `env` and gives its stderr.
#[cfg(test)]
fn serve(env: &[(&str, &str)]) -> (Output, String) {
    let output = Command::new(env!("CARGO_BIN_EXE_vmm"))
        .arg("serve")
        .env_clear()
        .envs(env.iter().copied())
        .output()
        .unwrap();
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

/// A good config on temp paths runs the leftover sweep (`true` lists no
/// scope). Then the vmm refuses a missing CH program and one that root
/// does not own, and exits 78 (decision CH67 rule 1, owner item 86). A
/// test cannot make a root-owned file, so the serve proof of plan PR L20
/// runs on the compose step (`src/compose/tests.rs`).
#[test]
fn an_untrusted_ch_program_exits_78() {
    // `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    // it (`SOCKET_PATH_MAX`), and the temp dir of macOS is too long.
    let scratch =
        Scratch(Path::new("/tmp").join(format!("creche-vmm-{}-binary", std::process::id())));
    let dir = &scratch.0;
    let _ = std::fs::remove_dir_all(dir);
    for sub in ["state", "run", "families", "faults", "images", "bin"] {
        std::fs::create_dir_all(dir.join(sub)).unwrap();
    }
    let path = |sub: &str| dir.join(sub).to_str().unwrap().to_owned();
    let (state, run, families, faults, images, ch) = (
        path("state"),
        path("run"),
        path("families"),
        path("faults"),
        path("images"),
        path("bin/cloud-hypervisor"),
    );
    let mut env = GOOD.to_vec();
    for one in &mut env {
        one.1 = match one.0 {
            "VMM_STATE_ROOT" => &state,
            "RUNTIME_DIRECTORY" => &run,
            "VMM_FAMILY_ROOT" => &families,
            "VMM_FAULTS_ROOT" => &faults,
            "VMM_IMAGE_ROOT" => &images,
            "VMM_CH_PROGRAM" => &ch,
            key if key.ends_with("_GIB") || key.ends_with("_ADDR") => one.1,
            _ => TRUE,
        };
    }

    let (output, stderr) = serve(&env);
    assert_eq!(output.status.code(), Some(EX_CONFIG), "{stderr}");
    assert!(
        stderr.starts_with("vmm: VMM_CH_PROGRAM: the program cannot be read: "),
        "{stderr}"
    );

    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/cloud-hypervisor"
        ),
        &ch,
    )
    .unwrap();
    let uid = std::fs::metadata(&ch).unwrap().uid();
    let (output, stderr) = serve(&env);
    if uid != 0 {
        assert_eq!(output.status.code(), Some(EX_CONFIG), "{stderr}");
        let expected =
            format!("vmm: VMM_CH_PROGRAM: the program is owned by uid {uid}, not by root\n");
        assert_eq!(stderr, expected);
    }
}
