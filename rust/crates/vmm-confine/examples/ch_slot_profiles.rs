//! SPIKE (decision CH51): prints the AppArmor profiles of the CH slot pool
//! on stdout. `bin/vm-smoke-setup.sh` runs it on the runner and loads the
//! output with sudo. Agents never load it on the host.
//!
//! - `set <n>` prints one file with the profiles of slots 0 to n-1.
//! - `probe <k> <path>` prints one file with the profile of slot k plus
//!   `<path> mr`, the probe variant of the signal test.
//!
//! `creche_vmm_confine::slot_profile` holds the render rules.

use std::io::{self, Write};
use std::process::ExitCode;

use creche_vmm_confine::slot_profile::{BASE, SLOTS, Slot, render_probe, render_set};

type Failure = Box<dyn std::error::Error>;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    let result = match words.as_slice() {
        ["set", count] => set(count),
        ["probe", slot, path] => probe(slot, path),
        _ => Err("usage: ch_slot_profiles set <n> | probe <k> <path>".into()),
    };
    match result.and_then(|text| Ok(io::stdout().write_all(text.as_bytes())?)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("ch_slot_profiles: {error}");
            ExitCode::FAILURE
        }
    }
}

/// The profiles of slots 0 to `count` - 1.
fn set(count: &str) -> Result<String, Failure> {
    let count: u16 = count.parse()?;
    if count == 0 || count > SLOTS {
        return Err(format!("the count must be 1 to {SLOTS}").into());
    }
    Ok(render_set(BASE, Slot::all().take(usize::from(count)))?)
}

/// The probe variant of `slot`.
fn probe(slot: &str, path: &str) -> Result<String, Failure> {
    let slot = Slot::new(slot.parse()?).ok_or("no such slot")?;
    Ok(render_probe(BASE, slot, path)?)
}
