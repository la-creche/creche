//! The program `vmm serve` and its config (plan PR L20). It needs no KVM.
//! A missing or bad key exits 78 and names the key (owner item 86). The
//! serve step after the config waits for plan PR L7, so a good config
//! still exits 78 for now.

use std::process::{Command, Output};

use creche_vmm::config::{BUDGET, RAM_CAP, STATE_ROOT};

/// The exit status of a refused start.
const EX_CONFIG: i32 = 78;

/// Each key with a good value. The paths need not exist: the config
/// checks their form only.
const GOOD: [(&str, &str); 14] = [
    ("VMM_STATE_ROOT", "/srv/vmm/state"),
    ("RUNTIME_DIRECTORY", "/run/user/1000/vmm"),
    ("VMM_FAMILY_ROOT", "/srv/families"),
    ("VMM_FAULTS_ROOT", "/srv/faults"),
    ("VMM_CH_PROGRAM", "/opt/components/vmm/bin/cloud-hypervisor"),
    ("VMM_CH_REMOTE_PROGRAM", "/opt/components/vmm/bin/ch-remote"),
    ("AGENT_SYSTEMD_RUN_PROGRAM", "/usr/bin/systemd-run"),
    ("AGENT_SYSTEMCTL_PROGRAM", "/usr/bin/systemctl"),
    ("VMM_ZSTD_PROGRAM", "/usr/bin/zstd"),
    ("VMM_MKE2FS_PROGRAM", "/usr/sbin/mke2fs"),
    ("VMM_FALLOCATE_PROGRAM", "/usr/bin/fallocate"),
    ("VMM_RAM_CAP_GIB", "80"),
    ("VMM_FREE_FLOOR_GIB", "50"),
    ("VMM_SNAPSHOT_BUDGET_GIB", "150"),
];

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

#[test]
fn a_good_config_reaches_the_server_that_is_not_built() {
    let (output, stderr) = serve(&GOOD);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, "vmm: the host API server is not built yet\n");
}
