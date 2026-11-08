//! The reader of the env file of the vmm unit (decisions CH76 rules 2 and
//! 6, contract 06 §4 rule 7). It reads the `vmm.env` that `bin/fleet-up
//! up` writes: a `#` header line and the keys in the order of `VMM_KEYS`.
//! It refuses each line that systemd would read in another way.

use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;

use creche_vmm::env_file::{CAP, EnvFile, EnvFileError, Problem, read};

/// A scratch file that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

/// Reads `bytes` as an env file.
#[cfg(test)]
fn parse(name: &str, bytes: &[u8]) -> Result<EnvFile, EnvFileError> {
    let scratch = write(name, bytes);
    read(&scratch.0)
}

/// A scratch file of `bytes`.
#[cfg(test)]
fn write(name: &str, bytes: &[u8]) -> Scratch {
    let path =
        std::env::temp_dir().join(format!("creche-vmm-env-file-{}-{name}", std::process::id()));
    let scratch = Scratch(path);
    fs::write(&scratch.0, bytes).unwrap();
    scratch
}

/// The problem of the refused line `number`.
#[cfg(test)]
fn problem(name: &str, bytes: &[u8], number: usize) -> Problem {
    match parse(name, bytes) {
        Err(EnvFileError::Line {
            number: found,
            problem,
        }) if found == number => problem,
        other => panic!("{name}: {other:?}"),
    }
}

/// The header of `env_set` in `bin/fleet-up`, then keys out of byte order,
/// as `VMM_KEYS` lists them.
#[test]
fn the_fleet_up_header_and_key_order_read() {
    let text = "# Written by bin/fleet-up, mode 0600. fleet-up owns each key below.\n\
                VMM_STATE_ROOT=/home/op/.local/state/creche-vmm\n\
                VMM_IMAGE_ROOT=/opt/components/disks\n\
                VMM_CH_PROGRAM=/opt/components/vmm/bin/cloud-hypervisor\n\
                AGENT_SYSTEMCTL_PROGRAM=/usr/bin/systemctl\n\
                VMM_LITELLM_ADDR=10.0.0.39:4000\n\
                VMM_EMPTY=\n\
                VMM_EQUALS=a=b\n";
    let env = parse("fleet-up", text.as_bytes()).unwrap();
    let value = |key| env.lookup(key);
    assert_eq!(
        value("VMM_CH_PROGRAM"),
        Some(OsString::from("/opt/components/vmm/bin/cloud-hypervisor"))
    );
    assert_eq!(
        value("AGENT_SYSTEMCTL_PROGRAM"),
        Some(OsString::from("/usr/bin/systemctl"))
    );
    assert_eq!(
        value("VMM_LITELLM_ADDR"),
        Some(OsString::from("10.0.0.39:4000"))
    );
    assert_eq!(value("VMM_EMPTY"), Some(OsString::new()));
    assert_eq!(value("VMM_EQUALS"), Some(OsString::from("a=b")));
    assert_eq!(value("VMM_ZSTD_PROGRAM"), None);
    assert_eq!(parse("empty", b"").unwrap().lookup("VMM_IMAGE_ROOT"), None);
}

/// systemd strips a quote pair and reads a backslash as an escape, so the
/// hook would read another value than the unit. Each one refuses, and the
/// refusal names the key and not the value.
#[test]
fn a_quoted_value_is_refused() {
    for (name, line) in [
        ("double", "VMM_IMAGE_ROOT=\"/opt/components/disks\"\n"),
        ("single", "VMM_IMAGE_ROOT='/opt/components/disks'\n"),
        ("backslash", "VMM_IMAGE_ROOT=/opt/components/disks\\\n"),
        ("lead", "VMM_IMAGE_ROOT= /opt/components/disks\n"),
        ("trail", "VMM_IMAGE_ROOT=/opt/components/disks\t\n"),
        ("cr", "VMM_IMAGE_ROOT=/opt/components/disks\r\n"),
        ("nul", "VMM_IMAGE_ROOT=/opt/\0disks\n"),
    ] {
        let text = format!("# header\n{line}");
        let found = problem(name, text.as_bytes(), 2);
        assert_eq!(
            found,
            Problem::Unread("VMM_IMAGE_ROOT".to_owned()),
            "{name}"
        );
        let error = parse(name, text.as_bytes()).unwrap_err().to_string();
        assert!(error.contains("VMM_IMAGE_ROOT"), "{error}");
        assert!(!error.contains("/opt"), "{error}");
    }
}

#[test]
fn a_line_that_is_no_key_is_refused() {
    assert_eq!(problem("blank", b"A=1\n\nB=2\n", 2), Problem::NoEquals);
    assert_eq!(problem("bare", b"A=1\nB\n", 2), Problem::NoEquals);
    assert_eq!(problem("export", b"export A=1\n", 1), Problem::BadName);
    assert_eq!(problem("indent", b" A=1\n", 1), Problem::BadName);
    assert_eq!(problem("digit", b"1A=1\n", 1), Problem::BadName);
    assert_eq!(problem("semicolon", b"; a comment\n", 1), Problem::NoEquals);
    assert_eq!(problem("no-name", b"=1\n", 1), Problem::BadName);
    assert_eq!(problem("no-lf", b"A=1\nB=2", 2), Problem::NoLineEnd);
    assert_eq!(
        problem("twice", b"A=1\nB=2\nA=3\n", 3),
        Problem::Twice("A".to_owned())
    );
}

/// A file of exactly `CAP` bytes reads, and one byte more refuses.
#[test]
fn the_cap_holds() {
    let cap = usize::try_from(CAP).unwrap();
    let line = |length: usize| format!("A={}\n", "x".repeat(length - 3));
    assert!(parse("at-cap", line(cap).as_bytes()).is_ok());
    let error = parse("past-cap", line(cap + 1).as_bytes()).unwrap_err();
    assert!(matches!(error, EnvFileError::OverCap), "{error:?}");
    let missing = std::env::temp_dir().join("creche-vmm-env-file-none");
    assert!(matches!(read(&missing), Err(EnvFileError::Read(_))));
}
