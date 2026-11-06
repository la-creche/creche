//! The vmm unit of the kvm_ tests (decision CH60 rule 1). Each VM scope
//! binds to the unit of its vmm (`BindsTo=` and `Before=`), so each test
//! runs a stand-in for the vmm as a transient user service:
//! `systemd-run --user --pipe --collect --unit=<unique name>`. With
//! `--pipe` the service reads the stdin and writes the stdout of its
//! `systemd-run`, so the test talks to it through the `Child`.
//!
//! A gated stand-in gets an `ExecStop=` that waits for its gate file. A
//! stop of its unit then lasts until the test opens the gate, as the
//! shutdown of the vmm lasts until the vmm exits. A drop opens the gate,
//! stops the unit and reaps its `systemd-run`, so a failed test leaves no
//! stand-in. systemd stops each VM scope bound to it in that stop.
#![cfg(test)]

use std::ffi::OsStr;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::launch::Launch;
use creche_vmm_confine::vmm_unit::VmmUnit;

/// The host programs of the ubuntu-24.04 runner.
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";
const SYSTEMCTL: &str = "/usr/bin/systemctl";

/// The longest wait for a unit to reach a state.
const STATE_WAIT: Duration = Duration::from_secs(30);

/// The `TimeoutStopSec` of a stand-in. It bounds the wait of a gate that
/// a failed test never opens.
const STOP_TIMEOUT: &str = "TimeoutStopSec=60";

/// The unit of the stand-in `name` of this test process. The name never
/// starts with `creche-vm-`, so `leftovers::stop_all` never stops it.
pub fn unit(name: &str) -> VmmUnit {
    VmmUnit::new(format!("kvm-vmm-{name}-{}.service", std::process::id())).unwrap()
}

/// The `ActiveState` of the user unit `name`, such as `active`. systemd
/// gives `inactive` for a unit that is not loaded.
pub fn state_of(name: &str) -> String {
    let output = Command::new(SYSTEMCTL)
        .args(["--user", "show", "--property=ActiveState", "--value", "--"])
        .arg(name)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(output.status.success(), "systemctl show: {output:?}");
    String::from_utf8(output.stdout)
        .unwrap()
        .trim_end()
        .to_owned()
}

/// Polls the `ActiveState` of `name` until it is `state`. Fails after
/// `STATE_WAIT`.
pub fn until_state(name: &str, state: &str) {
    let deadline = Instant::now() + STATE_WAIT;
    loop {
        let now = state_of(name);
        if now == state {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "{name} is {now}, not {state}, after {STATE_WAIT:?}"
        );
        sleep(Duration::from_millis(20));
    }
}

/// A vmm stand-in that runs as a transient user service.
pub struct StandIn {
    unit: VmmUnit,
    /// The gate file of a gated stand-in. A test that writes it lets the
    /// stop of the unit go on.
    pub gate: PathBuf,
    child: Child,
}

impl StandIn {
    /// Starts `command` as the service `unit` and waits until the unit is
    /// active. With `gated`, the stop of the unit runs `example await
    /// <gate>` first, where `example` is the first word of `command`.
    pub fn start<S: AsRef<OsStr>>(unit: &VmmUnit, gated: bool, command: &[S]) -> Self {
        let gate = std::env::temp_dir().join(format!("{}.gate", unit.name()));
        let _ = fs::remove_file(&gate);
        let mut run = Command::new(SYSTEMD_RUN);
        run.args(["--user", "--pipe", "--quiet", "--collect"])
            .arg(format!("--unit={}", unit.name()))
            .args(["-p", STOP_TIMEOUT]);
        if gated {
            let example = Path::new(command.first().unwrap());
            let stop = format!("ExecStop={} await {}", example.display(), gate.display());
            run.arg("-p").arg(stop);
        }
        let child = run
            .arg("--")
            .args(command)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stand_in = Self {
            unit: unit.clone(),
            gate,
            child,
        };
        until_state(unit.name(), "active");
        stand_in
    }

    /// The example in its role `hold` as the service of the unit of
    /// `launch`, with `launch` on its stdin. Returns the stand-in and the
    /// pid of the launch, which becomes CH.
    pub fn hold(example: &Path, launch: &Launch, gated: bool) -> (Self, u32) {
        let command = [example.as_os_str(), OsStr::new("hold")];
        let mut stand_in = Self::start(launch.unit(), gated, &command);
        let plan = serde_json::to_vec(launch).unwrap();
        let mut stdin = stand_in.child.stdin.take().unwrap();
        stdin.write_all(&plan).unwrap();
        drop(stdin);
        // The chain writes to the output pipe of the spawner, so the one
        // line on the stdout of the stand-in is the pid.
        let mut line = String::new();
        BufReader::new(stand_in.child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        let pid = line.trim().parse().unwrap_or_else(|_| {
            panic!("no pid from the stand-in: {line:?}");
        });
        (stand_in, pid)
    }

    /// The `ActiveState` of the unit.
    pub fn state(&self) -> String {
        state_of(self.unit.name())
    }

    /// Runs `systemctl --user <args> -- <unit>` and gives whether it
    /// worked.
    pub fn systemctl(&self, args: &[&str]) -> bool {
        Command::new(SYSTEMCTL)
            .arg("--user")
            .args(args)
            .arg("--")
            .arg(self.unit.name())
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    /// Sends SIGKILL to the main process of the unit, as a crash of the
    /// vmm ends it.
    pub fn kill(&self) {
        let kill = ["kill", "--kill-whom=main", "--signal=SIGKILL"];
        assert!(self.systemctl(&kill), "no SIGKILL");
    }
}

impl Drop for StandIn {
    /// Opens the gate, stops the unit and waits for that stop, then reaps
    /// `systemd-run`. A drop never panics.
    fn drop(&mut self) {
        let _ = fs::write(&self.gate, b"");
        let _ = self.systemctl(&["stop"]);
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.gate);
    }
}
