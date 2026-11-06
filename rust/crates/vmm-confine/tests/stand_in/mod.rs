//! The vmm unit of the kvm_ tests (decisions CH60 rule 1, CH61). Each VM
//! scope binds to the unit of its vmm (`BindsTo=` and `Before=`, CH61 rule
//! 2), so each test runs a stand-in for the vmm as a transient user
//! service: `systemd-run --user --pipe --collect --unit=<unique name>`.
//! With `--pipe` the service reads the stdin and writes the stdout of its
//! `systemd-run`, so the test talks to it through the `Child`.
//!
//! Each stand-in copies the lines of `systemd/creche-vmm.service` that
//! touch the launch chain and its stop (`UNIT_LINES`). So the launch
//! stage runs with no_new_privs set before it writes the exec attribute,
//! SIGTERM goes to the main process alone, a crash puts the unit into
//! auto-restart, the `ExecStopPost=` step ends each VM scope that is left
//! after each stop, and systemd removes the run root at each stop, a
//! restart and a crash included, as in production.
//!
//! The step runs the real `vmm kill-scopes` (decision CH63). The runner
//! has no vmm at `/opt/components/vmm/bin/vmm`, so a stand-in swaps that
//! path for the `vmm` that cargo built for this test run, and nothing
//! else. This crate cannot name the bin of `creche-vmm` (that crate
//! depends on this one), so `built_vmm` finds it beside the deps dir of
//! the test binary, as `kvm_slot.rs` finds its example. The kvm job runs
//! `cargo test` over the workspace, and cargo builds the bin `vmm` for
//! the tests of `creche-vmm` before it runs any test. A missing `vmm`
//! fails the start of the stand-in. The stand-in sets the one key of the
//! mode, `AGENT_SYSTEMCTL_PROGRAM`, as the env file of the unit does.
//!
//! That step kills every `creche-vm-*` scope of the user manager, because
//! one vmm owns every VM (CH61 rule 1). Each stop also removes every file
//! of the run root. So one test at a time in a test binary writes in the
//! run root: each such test takes `OneVmm` first and holds it to its
//! end. `StandIn::start` needs it. A test with no stand-in launches no VM.
//!
//! A gated stand-in gets an `ExecStop=` that waits for its gate file. A
//! stop of its unit then lasts until the test opens the gate, as the
//! shutdown of the vmm lasts until the vmm exits. A drop opens the gate,
//! stops the unit and reaps its `systemd-run`, so a failed test leaves no
//! stand-in. The `ExecStopPost=` step of that stop ends each VM scope.
#![cfg(test)]

use std::ffi::OsStr;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, MutexGuard, PoisonError};
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
/// a failed test never opens. It is not the 90 s of the vmm unit, which
/// bounds the shutdown of the vmm.
const STOP_TIMEOUT: &str = "TimeoutStopSec=60";

/// The lines of `systemd/creche-vmm.service` that each stand-in copies
/// (decisions CH60 rule 1, CH61 rule 1):
///
/// 1. The `Restart` lines put a unit whose main process died into
///    auto-restart, 5 s before the next start. Exit 78 ends the restarts,
///    so a restarted `hold`, which reads EOF, exits 78 and stays down.
/// 2. `KillMode=mixed` sends the SIGTERM of a stop to the main process
///    alone.
/// 3. `ExecStopPost=` kills each VM scope that is left after a stop, a
///    restart or a crash of the unit, with `vmm kill-scopes` (decision
///    CH63). A stand-in runs it as `stop_post` gives it.
/// 4. `NoNewPrivileges=yes` sets no_new_privs before the launch stage
///    writes the exec attribute (decision CH55).
/// 5. `RuntimeDirectory=vmm` makes the run root `/run/user/<uid>/vmm`
///    with `RuntimeDirectoryMode=0700` at each start. systemd removes it at
///    each stop, a restart and a crash included, so the kvm tests prove
///    that each serial log in the state root outlives it (decision CH59
///    rule 2).
///
/// The lines keep the order of the unit. The test
/// `the_stand_in_copies_the_vmm_unit` fails when a line with one of these
/// keys differs from the unit.
const UNIT_LINES: [&str; 10] = [
    "RuntimeDirectory=vmm",
    "RuntimeDirectoryMode=0700",
    "Restart=always",
    "RestartSec=5s",
    "RestartSteps=5",
    "RestartMaxDelaySec=120s",
    "RestartPreventExitStatus=78",
    "KillMode=mixed",
    STOP_POST,
    "NoNewPrivileges=yes",
];

/// The `ExecStopPost=` line of the vmm unit (decisions CH61 rule 1,
/// CH63).
const STOP_POST: &str = "ExecStopPost=/opt/components/vmm/bin/vmm kill-scopes";

/// The installed vmm that `STOP_POST` runs.
const INSTALLED_VMM: &str = "/opt/components/vmm/bin/vmm";

/// The one key of `vmm kill-scopes`, as the env file of the unit sets it.
pub const SYSTEMCTL_KEY: &str = "AGENT_SYSTEMCTL_PROGRAM";

/// The `vmm` that cargo built for this test run, beside the deps dir of
/// the test binary. Fails when it is missing.
pub fn built_vmm() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    let vmm = debug.join("vmm");
    assert!(
        vmm.is_file(),
        "{} is missing: run `cargo test` over the workspace, which builds the bin vmm",
        vmm.display()
    );
    vmm
}

/// The `ExecStopPost=` line of a stand-in: `STOP_POST` with the built
/// vmm in place of the installed one.
fn stop_post() -> String {
    let vmm = built_vmm();
    STOP_POST.replacen(INSTALLED_VMM, vmm.to_str().unwrap(), 1)
}

/// The lock of the run root of this test binary. The `ExecStopPost=` step
/// of a stand-in kills the VM scopes of every test, and its stop removes
/// the run root of every test. So a second test waits here until the
/// first one has ended.
static ONE_VMM: Mutex<()> = Mutex::new(());

/// The hold of `ONE_VMM` by one test. The test takes it before it writes
/// in the run root and keeps it until its last drop, so no stop of
/// another stand-in removes its files and no other `ExecStopPost=` step
/// kills its VMs.
pub struct OneVmm {
    _guard: MutexGuard<'static, ()>,
}

impl OneVmm {
    /// Waits for `ONE_VMM`. A test that panicked while it held the lock
    /// still stopped its stand-in and removed its files in its drops, so a
    /// poisoned lock is free too.
    pub fn take() -> Self {
        Self {
            _guard: ONE_VMM.lock().unwrap_or_else(PoisonError::into_inner),
        }
    }
}

/// The unit file of the vmm, from the root of the repository.
const VMM_UNIT_FILE: &str = "systemd/creche-vmm.service";

/// The unit of the stand-in `name` of this test process. The name never
/// starts with `creche-vm-`, so `leftovers::stop_all` never stops it.
pub fn unit(name: &str) -> VmmUnit {
    VmmUnit::new(format!("kvm-vmm-{name}-{}.service", std::process::id())).unwrap()
}

/// The `ActiveState` of the user unit `name`, such as `active`. systemd
/// gives `inactive` for a unit that is not loaded.
pub fn state_of(name: &str) -> String {
    property_of(name, "ActiveState")
}

/// The value of `property` of the user unit `name`.
fn property_of(name: &str, property: &str) -> String {
    let output = Command::new(SYSTEMCTL)
        .args(["--user", "show", "--value"])
        .arg(format!("--property={property}"))
        .arg("--")
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
    /// active. With `gated_by` the example, the stop of the unit runs
    /// `<example> await <gate>` first. The `OneVmm` of the test outlives
    /// the stand-in: the test makes its `Rig` or `Setup` first, which
    /// holds it, so that drops last.
    pub fn start<S: AsRef<OsStr>>(
        _one: &OneVmm,
        unit: &VmmUnit,
        gated_by: Option<&Path>,
        command: &[S],
    ) -> Self {
        let gate = std::env::temp_dir().join(format!("{}.gate", unit.name()));
        let _ = fs::remove_file(&gate);
        let mut run = Command::new(SYSTEMD_RUN);
        run.args(["--user", "--pipe", "--quiet", "--collect"])
            .arg(format!("--unit={}", unit.name()))
            .arg(format!("--setenv={SYSTEMCTL_KEY}={SYSTEMCTL}"))
            .args(["-p", STOP_TIMEOUT]);
        for line in UNIT_LINES {
            if line == STOP_POST {
                run.arg("-p").arg(stop_post());
            } else {
                run.args(["-p", line]);
            }
        }
        if let Some(example) = gated_by {
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
    ///
    /// Before it writes the launch, it waits until the main process of the
    /// unit has no_new_privs set. Only `NoNewPrivileges=yes` of the unit
    /// sets it there, and every launch inherits it. So the launch stage
    /// writes the exec attribute under no_new_privs, as under
    /// `creche-vmm.service` (decision CH55).
    pub fn hold(one: &OneVmm, example: &Path, launch: &Launch, gated: bool) -> (Self, u32) {
        let command = [example.as_os_str(), OsStr::new("hold")];
        let gated_by = gated.then_some(example);
        let mut stand_in = Self::start(one, launch.unit(), gated_by, &command);
        let main = stand_in.main_pid();
        let deadline = Instant::now() + STATE_WAIT;
        while !no_new_privs(main) {
            assert!(
                Instant::now() < deadline,
                "the stand-in {main} has no no_new_privs after {STATE_WAIT:?}"
            );
            sleep(Duration::from_millis(20));
        }
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

    /// The `SubState` of the unit, such as `auto-restart`.
    pub fn sub_state(&self) -> String {
        property_of(self.unit.name(), "SubState")
    }

    /// The pid of the main process of the unit. Fails when the unit has
    /// none.
    pub fn main_pid(&self) -> u32 {
        let pid: u32 = property_of(self.unit.name(), "MainPID").parse().unwrap();
        assert_ne!(pid, 0, "{} has no main process", self.unit.name());
        pid
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

/// Whether `pid` has no_new_privs set. A pid that is gone has not.
fn no_new_privs(pid: u32) -> bool {
    fs::read_to_string(format!("/proc/{pid}/status")).is_ok_and(|status| {
        status
            .lines()
            .any(|line| line.split_whitespace().eq(["NoNewPrivs:", "1"]))
    })
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

/// Each line of `UNIT_LINES` is a line of the vmm unit, and each line of
/// the unit with one of their keys is in `UNIT_LINES`. So a stand-in
/// runs with the decided lines of the vmm unit (decisions CH60 rule 1,
/// CH61 rule 1).
#[test]
fn the_stand_in_copies_the_vmm_unit() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let path = root.join(VMM_UNIT_FILE);
    let text = fs::read_to_string(&path).unwrap();
    let key = |line: &str| line.split_once('=').map(|(key, _)| key.to_owned());
    let keys: Vec<String> = UNIT_LINES.iter().filter_map(|line| key(line)).collect();
    let unit: Vec<&str> = text
        .lines()
        .filter(|line| key(line).is_some_and(|key| keys.contains(&key)))
        .collect();
    assert_eq!(unit, UNIT_LINES, "{}", path.display());
}

/// The stand-in swaps only the program of the step, and the program is
/// the installed vmm (decision CH63).
#[test]
fn the_stand_in_step_names_the_installed_vmm() {
    let tail = STOP_POST.strip_prefix("ExecStopPost=").unwrap();
    assert_eq!(tail.strip_suffix(" kill-scopes"), Some(INSTALLED_VMM));
}
