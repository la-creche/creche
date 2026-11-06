//! The config of the vmm (plan PR L20): each key, each fault, and the
//! defaults of `env/src/creche_env/default.env`.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::Path;

use creche_vmm::config::{
    BUDGET, CH, CH_REMOTE, Config, ConfigError, FALLOCATE, FAMILY_ROOT, FAULTS_ROOT, FREE_FLOOR,
    Fault, IMAGE_ROOT, MKE2FS, RAM_CAP, RUN_ROOT, STATE_ROOT, SYSTEMCTL, SYSTEMD_RUN, ZSTD,
};
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_store::retention;

/// The catalog defaults, beside this crate in the repository.
const DEFAULT_ENV: &str = include_str!("../../../../env/src/creche_env/default.env");

/// Each path key of the config, in the order that `Config` reads them.
const PATHS: [&str; 12] = [
    STATE_ROOT,
    RUN_ROOT,
    FAMILY_ROOT,
    FAULTS_ROOT,
    IMAGE_ROOT,
    CH,
    CH_REMOTE,
    SYSTEMD_RUN,
    SYSTEMCTL,
    ZSTD,
    MKE2FS,
    FALLOCATE,
];

/// Each size key of the config.
const SIZES: [&str; 3] = [RAM_CAP, FREE_FLOOR, BUDGET];

/// An env with a good value for each key.
fn good() -> HashMap<&'static str, OsString> {
    let paths = PATHS.map(|key| (key, OsString::from(format!("/srv/{key}"))));
    let sizes = [(RAM_CAP, "80"), (FREE_FLOOR, "50"), (BUDGET, "150")]
        .map(|(key, value)| (key, OsString::from(value)));
    paths.into_iter().chain(sizes).collect()
}

fn read(env: &HashMap<&'static str, OsString>) -> Result<Config, ConfigError> {
    Config::from_lookup(|key| env.get(key).cloned())
}

/// The fault of `env` with `key` set to `value`.
#[cfg(test)]
fn fault_of(key: &'static str, value: &str) -> ConfigError {
    let mut env = good();
    env.insert(key, OsString::from(value));
    read(&env).unwrap_err()
}

#[test]
fn a_good_env_gives_each_path_and_size() {
    let config = read(&good()).unwrap();
    assert_eq!(config.state.path(), Path::new("/srv/VMM_STATE_ROOT"));
    assert_eq!(
        config.run.vmm_socket(),
        Path::new("/srv/RUNTIME_DIRECTORY/vmm.sock")
    );
    assert_eq!(config.families.path(), Path::new("/srv/VMM_FAMILY_ROOT"));
    assert_eq!(config.faults_root, Path::new("/srv/VMM_FAULTS_ROOT"));
    assert_eq!(config.image_root, Path::new("/srv/VMM_IMAGE_ROOT"));
    assert_eq!(config.ch.path(), Path::new("/srv/VMM_CH_PROGRAM"));
    assert_eq!(
        config.ch_remote.path(),
        Path::new("/srv/VMM_CH_REMOTE_PROGRAM")
    );
    assert_eq!(
        config.systemd_run.path(),
        Path::new("/srv/AGENT_SYSTEMD_RUN_PROGRAM")
    );
    assert_eq!(
        config.systemctl.path(),
        Path::new("/srv/AGENT_SYSTEMCTL_PROGRAM")
    );
    assert_eq!(config.zstd.path(), Path::new("/srv/VMM_ZSTD_PROGRAM"));
    assert_eq!(config.mke2fs.path(), Path::new("/srv/VMM_MKE2FS_PROGRAM"));
    assert_eq!(
        config.fallocate.path(),
        Path::new("/srv/VMM_FALLOCATE_PROGRAM")
    );
    assert_eq!(config.limits, Limits::DESIGN);
    assert_eq!(config.budget, retention::BUDGET);
}

#[test]
fn each_missing_key_is_named() {
    for key in PATHS.into_iter().chain(SIZES) {
        let mut env = good();
        env.remove(key);
        let expected = ConfigError {
            key,
            fault: Fault::Missing,
        };
        assert_eq!(read(&env).unwrap_err(), expected, "{key}");
    }
}

#[test]
fn an_empty_value_is_missing() {
    for key in PATHS.into_iter().chain(SIZES) {
        assert_eq!(fault_of(key, "").fault, Fault::Missing, "{key}");
    }
}

#[test]
fn each_path_is_absolute_with_no_parent_component() {
    for key in PATHS {
        assert_eq!(fault_of(key, "srv/x").fault, Fault::NotAbsolute, "{key}");
        assert_eq!(
            fault_of(key, "/srv/../x").fault,
            Fault::ParentComponent,
            "{key}"
        );
    }
}

#[test]
fn a_run_root_too_long_for_its_sockets_is_refused() {
    let long = format!("/{}", "r".repeat(80));
    let error = fault_of(RUN_ROOT, &long);
    assert_eq!(error.key, RUN_ROOT);
    assert!(matches!(error.fault, Fault::SocketPathTooLong(_)));
}

#[test]
fn a_run_root_that_lists_paths_is_refused() {
    // Gap G5 option A: one run root. systemd joins the paths of more than
    // one RuntimeDirectory= entry with `:`.
    let error = fault_of(RUN_ROOT, "/run/user/1000/creche-vmm:/run/user/1000/other");
    let expected = ConfigError {
        key: RUN_ROOT,
        fault: Fault::ListOfPaths,
    };
    assert_eq!(error, expected);
    assert_eq!(
        error.to_string(),
        "RUNTIME_DIRECTORY: the key holds a `:`, so it lists more than one path"
    );
}

#[test]
fn a_size_is_decimal_digits_only() {
    for key in SIZES {
        for bad in ["8O", "-1", "+1", " 80", "1.5", "0x10", "80G"] {
            assert_eq!(fault_of(key, bad).fault, Fault::NotGib, "{key} {bad}");
        }
    }
}

#[test]
fn a_size_past_u64_is_too_large() {
    for key in SIZES {
        // 2^34 GiB is 2^64 bytes, one past u64::MAX.
        assert_eq!(fault_of(key, "17179869184").fault, Fault::TooLarge, "{key}");
        assert_eq!(
            fault_of(key, &"9".repeat(30)).fault,
            Fault::TooLarge,
            "{key}"
        );
    }
    let mut env = good();
    env.insert(BUDGET, OsString::from("17179869183"));
    assert_eq!(read(&env).unwrap().budget, 17_179_869_183 * GIB);
}

#[test]
fn only_the_floor_may_be_zero() {
    assert_eq!(fault_of(RAM_CAP, "0").fault, Fault::Zero);
    assert_eq!(fault_of(BUDGET, "0").fault, Fault::Zero);
    let mut env = good();
    env.insert(FREE_FLOOR, OsString::from("0"));
    assert_eq!(read(&env).unwrap().limits.free_floor, 0);
}

#[test]
fn a_fault_names_the_key_and_never_the_value() {
    let text = fault_of(ZSTD, "zstd-secret").to_string();
    assert_eq!(text, "VMM_ZSTD_PROGRAM: the key is not an absolute path");
    assert!(!fault_of(RAM_CAP, "secret").to_string().contains("secret"));
}

/// The value of `key` in `default.env`, before its references.
fn default_of(key: &str) -> Option<&'static str> {
    DEFAULT_ENV
        .lines()
        .find_map(|line| line.strip_prefix(key)?.strip_prefix('='))
}

#[test]
fn the_catalog_holds_a_default_for_each_key_but_the_run_root() {
    for key in PATHS.into_iter().chain(SIZES) {
        let found = default_of(key);
        if key == RUN_ROOT {
            assert_eq!(found, None, "systemd sets {key}");
        } else {
            assert!(found.is_some_and(|value| !value.is_empty()), "{key}");
        }
    }
}

#[test]
fn the_catalog_defaults_are_the_design_values() {
    // Decision CH50 rule 11: the state root is the StateDirectory of the
    // user unit.
    let state = "${AGENT_OPERATOR_HOME}/.local/state/creche-vmm";
    assert_eq!(default_of(STATE_ROOT), Some(state));
    // Each other path key of the catalog. The family root and the faults
    // root are a contract with caregiver: paths.py names FAMILIES_DIR
    // "families" (CH36 G6) and FAULTS_DIR "faults" (CH36 G5). Decision
    // CH57 rule 1 sets the image root. Gap G9 checked /usr/bin/zstd on
    // Hyperion.
    let paths = [
        (FAMILY_ROOT, "${CAREGIVER_STATE_ROOT}/families"),
        (FAULTS_ROOT, "${CAREGIVER_STATE_ROOT}/faults"),
        (IMAGE_ROOT, "${AGENT_INSTALL_ROOT}/disks"),
        (CH, "${AGENT_INSTALL_ROOT}/vmm/bin/cloud-hypervisor"),
        (CH_REMOTE, "${AGENT_INSTALL_ROOT}/vmm/bin/ch-remote"),
        (ZSTD, "/usr/bin/zstd"),
        (MKE2FS, "/usr/sbin/mke2fs"),
        (FALLOCATE, "/usr/bin/fallocate"),
    ];
    for (key, expected) in paths {
        assert_eq!(default_of(key), Some(expected), "{key}");
    }
    // Gap G14: the design values of the cap, the floor and the budget.
    let mut env = good();
    for key in SIZES {
        env.insert(key, OsString::from(default_of(key).unwrap()));
    }
    let config = read(&env).unwrap();
    assert_eq!(config.limits, Limits::DESIGN);
    assert_eq!(config.budget, retention::BUDGET);
}
