//! The config of the vmm (plan PR L20): each key, each fault, and the
//! defaults of `env/src/creche_env/default.env`. The plane addresses come
//! from plan PR V6. Also the config of `vmm
//! kill-scopes` (decision CH63): one key, with the check of `Config`.

use std::collections::HashMap;
use std::ffi::OsString;
use std::net::SocketAddr;
use std::path::Path;

use creche_vmm::config::{
    BUDGET, CH, CH_REMOTE, CHAPERONE, Config, ConfigError, EGRESS_LOG, FALLOCATE, FAMILY_ROOT,
    FAULTS_ROOT, FREE_FLOOR, Fault, GIT, GUEST_INIT, IMAGE_ROOT, KillScopesConfig, LITELLM,
    MIRROR_ROOT, MKE2FS, RAM_CAP, RUN_ROOT, STATE_ROOT, SYSTEMCTL, SYSTEMD_RUN, ZSTD,
};
use creche_vmm::planes::Upstreams;
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_store::retention;

/// The catalog defaults, beside this crate in the repository.
const DEFAULT_ENV: &str = include_str!("../../../../env/src/creche_env/default.env");

/// Each path key of the config, in the order that `Config` reads them.
const PATHS: [&str; 16] = [
    STATE_ROOT,
    RUN_ROOT,
    FAMILY_ROOT,
    FAULTS_ROOT,
    IMAGE_ROOT,
    CH,
    CH_REMOTE,
    GUEST_INIT,
    SYSTEMD_RUN,
    SYSTEMCTL,
    ZSTD,
    MKE2FS,
    FALLOCATE,
    EGRESS_LOG,
    MIRROR_ROOT,
    GIT,
];

/// Each size key of the config.
const SIZES: [&str; 3] = [RAM_CAP, FREE_FLOOR, BUDGET];

/// Each address key of the config (decision CH62 rule 20).
const ADDRS: [&str; 2] = [LITELLM, CHAPERONE];

/// Each key of the config.
fn keys() -> impl Iterator<Item = &'static str> {
    PATHS.into_iter().chain(SIZES).chain(ADDRS)
}

/// An env with a good value for each key.
fn good() -> HashMap<&'static str, OsString> {
    let paths = PATHS.map(|key| (key, OsString::from(format!("/srv/{key}"))));
    let sizes = [(RAM_CAP, "80"), (FREE_FLOOR, "50"), (BUDGET, "150")]
        .map(|(key, value)| (key, OsString::from(value)));
    let addrs = [(LITELLM, "192.0.2.10:4000"), (CHAPERONE, "192.0.2.10:8300")]
        .map(|(key, value)| (key, OsString::from(value)));
    paths.into_iter().chain(sizes).chain(addrs).collect()
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
        config.guest_init.path(),
        Path::new("/srv/VMM_GUEST_INIT_PROGRAM")
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
    let planes = Upstreams {
        litellm: "192.0.2.10:4000".parse().unwrap(),
        chaperone: "192.0.2.10:8300".parse().unwrap(),
    };
    assert_eq!(config.planes, planes);
    assert_eq!(config.egress_log, Path::new("/srv/VMM_EGRESS_LOG"));
    assert_eq!(config.mirror_root.path(), Path::new("/srv/VMM_MIRROR_ROOT"));
    assert_eq!(config.git.path(), Path::new("/srv/VMM_GIT_PROGRAM"));
}

#[test]
fn each_missing_key_is_named() {
    for key in keys() {
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
    for key in keys() {
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

/// `vmm kill-scopes` reads only `AGENT_SYSTEMCTL_PROGRAM`, so an env
/// with no other key gives its program.
#[test]
fn kill_scopes_reads_only_the_systemctl_key() {
    let lookup = |key: &str| (key == SYSTEMCTL).then(|| OsString::from("/usr/bin/systemctl"));
    let config = KillScopesConfig::from_lookup(lookup).unwrap();
    assert_eq!(config.systemctl.path(), Path::new("/usr/bin/systemctl"));
}

/// `vmm kill-scopes` checks its key as `Config` checks it: each fault of
/// a program path, with the same error.
#[test]
fn kill_scopes_checks_the_key_as_serve_does() {
    for value in ["", "srv/x", "/srv/../x"] {
        let kill = KillScopesConfig::from_lookup(|_| Some(OsString::from(value))).unwrap_err();
        assert_eq!(kill, fault_of(SYSTEMCTL, value), "{value:?}");
    }
    let missing = KillScopesConfig::from_lookup(|_| None).unwrap_err();
    let expected = ConfigError {
        key: SYSTEMCTL,
        fault: Fault::Missing,
    };
    assert_eq!(missing, expected);
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

/// Decision CH62 rule 20: each plane key is an IP address and a port, as
/// `SocketAddr` parses it. A host name, a URL, a missing port and a port
/// past 65535 each refuse the start.
#[test]
fn plane_addresses_parse_host_and_port() {
    for key in ADDRS {
        for good in ["192.0.2.10:4000", "127.0.0.1:8300", "[2001:db8::1]:4000"] {
            let mut env = self::good();
            env.insert(key, OsString::from(good));
            let planes = read(&env).unwrap().planes;
            let read = if key == LITELLM {
                planes.litellm
            } else {
                planes.chaperone
            };
            assert_eq!(read, good.parse::<SocketAddr>().unwrap(), "{key} {good}");
        }
        for bad in [
            "192.0.2.10",
            "hyperion:4000",
            "http://192.0.2.10:4000",
            "192.0.2.10:",
            ":4000",
            "192.0.2.10:65536",
            " 192.0.2.10:4000",
            "2001:db8::1:4000",
        ] {
            let error = fault_of(key, bad);
            let expected = ConfigError {
                key,
                fault: Fault::NotSocketAddr,
            };
            assert_eq!(error, expected, "{bad}");
        }
    }
    assert_eq!(
        fault_of(LITELLM, "secret:4000").to_string(),
        "VMM_LITELLM_ADDR: the key is not an IP address and a port"
    );
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
    for key in keys() {
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
        // Decision CH62 rule 17: the guest-init file of the vmm row.
        (GUEST_INIT, "${AGENT_INSTALL_ROOT}/vmm/bin/guest-init"),
        (ZSTD, "/usr/bin/zstd"),
        (MKE2FS, "/usr/sbin/mke2fs"),
        (FALLOCATE, "/usr/bin/fallocate"),
        // Decision CH69 rule 10: a file of its own beside the state root,
        // so it amends no state-root layout (decision CH39 rule 3).
        (
            EGRESS_LOG,
            "${AGENT_OPERATOR_HOME}/.local/state/creche-vmm-egress.jsonl",
        ),
        // Decision CH69 rule 17 and fact 13: the mirror of the host and
        // the git of Ubuntu.
        (MIRROR_ROOT, "/srv/agents/code"),
        (GIT, "/usr/bin/git"),
    ];
    for (key, expected) in paths {
        assert_eq!(default_of(key), Some(expected), "{key}");
    }
    // Decision CH62 rule 20 names 10.0.0.39, the LAN address of Hyperion.
    // Rule 3 of env/AGENTS.md keeps a deployment address out of the
    // catalog, so each default takes the LAN address key, as
    // `AGENT_LITELLM_BASE_URL` and `CHAPERONE_LISTEN` do.
    let addrs = [
        (LITELLM, "${AGENT_LAN_ADDRESS}:4000"),
        (CHAPERONE, "${AGENT_LAN_ADDRESS}:8300"),
    ];
    for (key, expected) in addrs {
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
