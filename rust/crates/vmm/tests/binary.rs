//! The program `vmm serve` and its config (plan PR L20). It needs no KVM.
//! A missing or bad key exits 78 and names the key (owner item 86). A good
//! config on temp paths serves until SIGTERM.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::config::{BUDGET, RAM_CAP, STATE_ROOT};
use creche_wire::{HOST_API_HEADER, HOST_API_PROTOCOL, SessionPage};

/// The exit status of a refused start.
const EX_CONFIG: i32 = 78;

/// A program that does nothing and exits 0.
const TRUE: &str = "/usr/bin/true";

/// The longest wait of one step of the serve test.
const WAIT: Duration = Duration::from_secs(10);

/// The frozen mismatch reply. Never change it to make a change pass.
const MISMATCH: &str = r#"{"code":"protocol_mismatch","protocol":1}"#;

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

/// The plan proof of L20 with no KVM: `vmm serve` on temp paths answers
/// an empty list page and a 412 on a bad protocol header, and exits 0 on
/// SIGTERM (decisions CH19 (4), CH25 rule 1, CH26 rule 2). The CH fixture
/// passes the version check, and `true` plays every other program: it
/// lists no leftover scope.
#[test]
fn serve_lists_refuses_and_exits_on_sigterm() {
    // `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    // it (`SOCKET_PATH_MAX`), and the temp dir of macOS is too long.
    let dir = Path::new("/tmp").join(format!("creche-vmm-{}-serve", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    for sub in ["state", "run", "families", "faults"] {
        std::fs::create_dir_all(dir.join(sub)).unwrap();
    }
    let path = |sub: &str| dir.join(sub).to_str().unwrap().to_owned();
    let ch = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/cloud-hypervisor"
    );
    let (state, run, families, faults) =
        (path("state"), path("run"), path("families"), path("faults"));
    let mut env = GOOD.to_vec();
    for one in &mut env {
        one.1 = match one.0 {
            "VMM_STATE_ROOT" => &state,
            "RUNTIME_DIRECTORY" => &run,
            "VMM_FAMILY_ROOT" => &families,
            "VMM_FAULTS_ROOT" => &faults,
            "VMM_CH_PROGRAM" => ch,
            key if key.ends_with("_GIB") => one.1,
            _ => TRUE,
        };
    }
    let mut vmm = Command::new(env!("CARGO_BIN_EXE_vmm"))
        .arg("serve")
        .env_clear()
        .envs(env.iter().copied())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let socket = dir.join("run/vmm.sock");

    let list = r#"{"cursor":null,"cwd":null,"family":"chat"}"#;
    let header = format!("{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n");
    let (status, body) = ask(&socket, "list", &header, list);
    assert_eq!(status, 200, "{body}");
    let page: SessionPage = serde_json::from_str(&body).unwrap();
    assert_eq!((page.next, page.rows.len()), (None, 0));

    let bad = format!("{HOST_API_HEADER}: 2\r\n");
    let (status, body) = ask(&socket, "list", &bad, list);
    assert_eq!(status, 412);
    assert_eq!(body, MISMATCH);

    let term = Command::new("kill")
        .args(["-TERM", &vmm.id().to_string()])
        .status()
        .unwrap();
    assert!(term.success());
    let status = wait(&mut vmm);
    let mut stderr = String::new();
    vmm.stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    assert_eq!(status.code(), Some(0), "{stderr}");
    assert_eq!(stderr, "");
    std::fs::remove_dir_all(dir).unwrap();
}

/// The status and the body of the answer to `POST /<route>` with
/// `headers` and `body`. It waits until the vmm binds its socket.
#[cfg(test)]
fn ask(socket: &Path, route: &str, headers: &str, body: &str) -> (u16, String) {
    let deadline = Instant::now() + WAIT;
    let mut stream = loop {
        match UnixStream::connect(socket) {
            Err(_) if Instant::now() < deadline => sleep(Duration::from_millis(10)),
            connected => break connected.unwrap(),
        }
    };
    stream.set_read_timeout(Some(WAIT)).unwrap();
    write!(
        stream,
        "POST /{route} HTTP/1.1\r\nhost: vmm\r\n{headers}content-type: application/json\r\n\
         connection: close\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    )
    .unwrap();
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    let (head, body) = text.split_once("\r\n\r\n").unwrap();
    (head.get(9..12).unwrap().parse().unwrap(), body.to_owned())
}

/// The exit status of `vmm`, within `WAIT`.
#[cfg(test)]
fn wait(vmm: &mut Child) -> ExitStatus {
    let deadline = Instant::now() + WAIT;
    loop {
        if let Some(status) = vmm.try_wait().unwrap() {
            return status;
        }
        assert!(Instant::now() < deadline, "the vmm did not exit on SIGTERM");
        sleep(Duration::from_millis(10));
    }
}
