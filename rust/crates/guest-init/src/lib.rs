//! The guest init: PID 1 of each Cloud Hypervisor VM (decisions CH9,
//! CH7-5).
//!
//! As PID 1 it runs the boot plan of `boot` (decision CH62 rule 4): the
//! mounts, the switch root into the root disk and `lo` up. Then it reads
//! the image env with `env_file` (decision CH62 rule 7), listens on
//! control link 1200 and on the local link, writes `guest-init: up` and
//! serves both with `control` (decisions CH13, CH28, CH32, CH62 rule 10).
//! The first hello starts the facade, and `supervisor` keeps one for the
//! VM life (decision CH19 rule 1). The facade sends its turn events on
//! the local link, and `local_link` relays them on 1200 (decision CH21).
//! PID 1 never exits on its own:
//! the guest kernel has `PANIC_TIMEOUT=0`, so an exit of PID 1 hangs the
//! VM (fact 53). A stop and each fault power the VM off instead (decision
//! CH19 rule 3).
//!
//! Off Linux, and on Linux in a process that is not PID 1, `run` starts
//! nothing and returns exit status 78 (decision CH38).

use std::process::ExitCode;

#[cfg(any(test, target_os = "linux"))]
mod boot;
#[cfg(any(test, target_os = "linux"))]
mod control;
#[cfg(any(test, target_os = "linux"))]
mod env_file;
#[cfg(any(test, target_os = "linux"))]
mod lo;
#[cfg(any(test, target_os = "linux"))]
mod local_link;
#[cfg(target_os = "linux")]
mod power;
#[cfg(any(test, target_os = "linux"))]
mod supervisor;

/// The exit status of a refused start (owner item 86), the same value as
/// `EX_CONFIG` in `rust/crates/vmm/src/main.rs`.
const EX_CONFIG: u8 = 78;

/// Runs the guest init. As PID 1 on Linux it never returns while the VM
/// runs. Each other process gets exit status 78.
#[must_use]
pub fn run() -> ExitCode {
    #[cfg(target_os = "linux")]
    if rustix::process::getpid().is_init() {
        return as_pid_1();
    }
    ExitCode::from(EX_CONFIG)
}

/// The life of PID 1. A stop, a boot fault, a refused env file, a
/// listen fault and a panic all end in `power::off` (decisions CH19 rule
/// 3, CH62 rule 7).
///
/// The panic hook covers a panic in every thread, the accept thread and
/// the reader threads of `control` included: the default hook writes the
/// panic text to the console, then the hook powers off. Only a failed
/// power-off returns from it. The panic then goes on as it would with no
/// hook, and the vmm stop chain ends the hung VM (plan risk 9).
#[cfg(target_os = "linux")]
fn as_pid_1() -> ExitCode {
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        default(info);
        say("panicked");
        let _ = power::off();
    }));
    say(&life());
    power::off()
}

/// Boots, listens on control link 1200 and on the local link and serves
/// both. It returns the console line of the stop or of the fault.
#[cfg(target_os = "linux")]
fn life() -> String {
    // The facade gets the env (decision CH49 rule 1).
    let env = match start() {
        Ok(env) => env,
        Err(fault) => return fault,
    };
    // The listeners open before `up`, so a dial after `up` reaches them,
    // and the facade, which starts later, finds its link.
    let listener = match control::listen() {
        Ok(listener) => listener,
        Err(error) => return format!("control listen failed: {error}"),
    };
    let local = match local_link::listen() {
        Ok(local) => local,
        Err(error) => return format!("local link listen failed: {error}"),
    };
    say("up");
    control::serve(listener, local, env)
}

/// Boots, then reads the env file of the switched root. Both run before
/// any listener, so a refused file powers off before the vmm can dial
/// (decision CH62 rule 7). The error is the console line of the fault.
#[cfg(target_os = "linux")]
fn start() -> Result<env_file::Env, String> {
    boot::boot().map_err(|fault| fault.to_string())?;
    let env = env_file::read(std::path::Path::new(env_file::PATH))
        .map_err(|refusal| format!("env refused: {refusal}"))?;
    say(&format!("env read: {} names", env.len()));
    Ok(env)
}

/// Writes one `guest-init: <text>` line to the console (decision CH62
/// rule 26). The kernel opens `/dev/console` as fd 0, 1 and 2 of PID 1,
/// and `console=ttyS0` sends it to the serial file of the VM. A failed
/// write has no other place to go, so `say` drops it.
#[cfg(target_os = "linux")]
fn say(text: &str) {
    use std::io::Write as _;
    let _ = writeln!(std::io::stderr(), "guest-init: {text}");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A test process is not PID 1, so `run` refuses on every OS. As PID 1,
    /// for example in a container, `run` would boot, so the test stops.
    #[test]
    fn run_refuses_when_not_pid_1() {
        #[cfg(target_os = "linux")]
        assert!(
            !rustix::process::getpid().is_init(),
            "the test runs as PID 1"
        );
        assert_eq!(run(), ExitCode::from(EX_CONFIG));
    }
}
