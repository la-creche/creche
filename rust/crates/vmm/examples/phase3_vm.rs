//! The vmm of the phase 3 proof (plan PR V10, decision CH62 rule 29, gap
//! Q54-29 option A). It is an example in no release row (decision CH58
//! rule 1). `tests/kvm_guest.rs` runs it as a transient user service, so
//! each VM scope binds to its unit, as `launch_chain` runs in
//! `creche-vmm-confine/tests/kvm_vm.rs` (decision CH60 rule 1).
//!
//! `serve <version program> <session> <family>` reads the vmm config from
//! its env with `Config::read`, as `vmm serve` does. It composes the parts
//! of the compose step: `guest_init::write_initramfs`, `image_root::read`,
//! the fault book, the rows, the spawner, `ChTools` and one `ColdBoot`
//! with its plane bridges, family-state reply, retire and restore. It
//! creates the row of `<session>` in `<family>` with origin `door`. Like
//! the compose step, it first runs the leftover sweep (decision CH19 (4))
//! and the owner check of CH and ch-remote (decision CH67 rule 1). The
//! version check runs `<version program>`, not `VMM_CH_PROGRAM`: the
//! upstream CH of the runner may print its git tag, and the CI-built CH
//! with the `CH_VERSION` string comes in phase 4 (decision CH7-4, fact
//! 78). It binds no `vmm.sock`.
//!
//! It prints `ready`, then reads one step per stdin line and answers one
//! stdout line:
//!
//! - `start`: `StartVm::start` of the row, a cold boot or a restore. It
//!   answers `started <ms> <JSON>`, or `start failed <fault>`. The JSON
//!   is the facade state that `ControlState` holds after the start: the
//!   one of the ready, or of a `facade` line that came after it.
//! - `facade`: `facade <JSON>`, the facade state that `ControlState` holds
//!   now, or `facade none`.
//! - `sleep`: `ColdBoot::sleep_idle` at `IDLE_TIMEOUT` and 1 s past now,
//!   so the idle gate passes with no wait. It answers `slept <count>`.
//! - `stop`: `StopVm::stop`. It answers `stopped <ms>` or `stop failed
//!   <error>`.
//!
//! At the end of stdin it exits 0. `launch` and `launch-stage <plan>` are
//! the launch step and the launch stage of the spawner, as in `vmm`.

use std::ffi::OsString;
use std::io::{self, BufRead as _, Write as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant, SystemTime};

use creche_vmm::config::{CH, CH_REMOTE, Config};
use creche_vmm::control::ControlState;
use creche_vmm::egress::log::Log;
use creche_vmm::guest_init::write_initramfs;
use creche_vmm::handlers::acp::StartVm as _;
use creche_vmm::handlers::drop::StopVm as _;
use creche_vmm::image_root;
use creche_vmm::relay::Relays;
use creche_vmm::restore::ChTools;
use creche_vmm::root_owned::check_program;
use creche_vmm::shutdown::RUNTIME_END;
use creche_vmm::start::{ColdBoot, StartConfig};
use creche_vmm_confine::faults::{FaultBook, FaultDir};
use creche_vmm_confine::leftovers::stop_all;
use creche_vmm_confine::spawner::{SELF_EXE, Spawner};
use creche_vmm_confine::vmm_unit::VmmUnit;
use creche_vmm_confine::{launch, stage};
use creche_vmm_store::ch_remote::ChRemote;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::key::check_cloud_hypervisor;
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::IDLE_TIMEOUT;
use creche_vmm_store::snapshot::Snapshotter;
use creche_wire::SessionRow;
use tokio::runtime::{Builder, Runtime};

/// The bound of the version check, as in the compose step.
const VERSION_WAIT: Duration = Duration::from_secs(10);

/// How far past now `sleep` reads the idle gate.
const PAST_IDLE: Duration = IDLE_TIMEOUT.saturating_add(Duration::from_secs(1));

type Failure = Box<dyn std::error::Error>;

/// The `ColdBoot` of the vmm.
type Hook = ColdBoot<Spawner, ChTools>;

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let result = match args.as_slice() {
        [mode] if mode == launch::MODE => launch::run()
            .map(|never| match never {})
            .map_err(Failure::from),
        [mode, plan] if mode == stage::MODE => stage::run(std::slice::from_ref(plan))
            .map(|never| match never {})
            .map_err(Failure::from),
        [mode, version, session, family] if mode == "serve" => {
            serve(Path::new(version), session, family)
        }
        _ => Err(
            "usage: phase3_vm launch | launch-stage <plan> | serve <version program> <session> <family>"
                .into(),
        ),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("phase3_vm: {error}");
            ExitCode::from(78)
        }
    }
}

/// The serve role. It returns at the end of stdin.
fn serve(version: &Path, session: &OsString, family: &OsString) -> Result<(), Failure> {
    let config = Config::read()?;
    for unit in stop_all(&config.systemctl)? {
        eprintln!("phase3_vm: stopped the leftover unit {unit}");
    }
    check_program(CH, config.ch.path())?;
    check_program(CH_REMOTE, config.ch_remote.path())?;
    let ch_version = check_cloud_hypervisor(version, VERSION_WAIT)?;
    let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
    let digests = image_root::read(&config.image_root)?.import(&images)?;
    let initramfs = write_initramfs(config.guest_init.path(), &images)?.digest();
    let faults = FaultBook::start(FaultDir::open(&config.faults_root)?, SystemTime::now())?;
    let mut rows = Rows::open(config.state.clone())?;
    let create = serde_json::json!({
        "created_at": 1_793_577_600_000_u64,
        "cwd": "/root",
        "family": family.to_str().ok_or("the family is not UTF-8")?,
        "id": session.to_str().ok_or("the session is not UTF-8")?,
        "origin": {"door": {}},
    });
    let row = rows.create(serde_json::from_value(create)?)?;
    let remote = || ChRemote::new(config.ch_remote.path().to_owned());
    let api = ChTools {
        remote: remote(),
        snapshotter: Snapshotter::new(remote(), config.fallocate.path().to_owned()),
    };
    let start = StartConfig {
        images,
        disks: SessionDisks::new(&config.state, config.mke2fs.path().to_owned()),
        digests,
        initramfs,
        state: config.state,
        run: config.run,
        families: config.families,
        ch: config.ch,
        systemd_run: config.systemd_run,
        vmm_unit: VmmUnit::of_self(),
        limits: config.limits,
        budget: config.budget,
        ch_version,
        planes: config.planes,
        egress_log: Arc::new(Log::open(config.egress_log)?),
    };
    let control = Arc::new(Mutex::new(ControlState::default()));
    let hook = ColdBoot::new(
        start,
        Arc::new(Spawner::start(PathBuf::from(SELF_EXE))?),
        Arc::new(api),
        Relays::default(),
        Arc::new(Mutex::new(rows)),
        Arc::clone(&control),
        Arc::new(Mutex::new(faults)),
    );
    // The runtime of `vmm serve` (decision CH36, gap G21).
    let runtime = Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()?;
    answer("ready")?;
    for line in io::stdin().lock().lines() {
        let reply = step(&runtime, &hook, (&row, &control), &line?)?;
        answer(&reply)?;
    }
    runtime.shutdown_timeout(RUNTIME_END);
    Ok(())
}

/// Runs the step `line` on `hook` and gives its answer.
fn step(
    runtime: &Runtime,
    hook: &Hook,
    (row, control): (&SessionRow, &Mutex<ControlState>),
    line: &str,
) -> Result<String, Failure> {
    let facade = || -> Result<String, Failure> {
        let control = control.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(match control.facade(&row.id) {
            Some(state) => serde_json::to_string(state)?,
            None => "none".to_owned(),
        })
    };
    let began = Instant::now();
    Ok(match line {
        "start" => match runtime.block_on(hook.start(row)) {
            Ok(_link) => format!("started {} {}", began.elapsed().as_millis(), facade()?),
            Err(fault) => format!("start failed {fault:?}"),
        },
        "facade" => format!("facade {}", facade()?),
        "sleep" => {
            let now = began.checked_add(PAST_IDLE).ok_or("no instant past idle")?;
            let slept = runtime.block_on(hook.sleep_idle(now));
            format!("slept {}", slept.len())
        }
        "stop" => match runtime.block_on(hook.stop(&row.id)) {
            Ok(()) => format!("stopped {}", began.elapsed().as_millis()),
            Err(error) => format!("stop failed {error:?}"),
        },
        other => return Err(format!("no step {other:?}").into()),
    })
}

/// Writes `line` on stdout and flushes it, so the test reads it at once.
fn answer(line: &str) -> io::Result<()> {
    let mut out = io::stdout().lock();
    writeln!(out, "{line}")?;
    out.flush()
}
