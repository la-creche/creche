//! `vmm check-images`, the verify hook of the disks row (contract 06 §4,
//! decision CH76 rule 6). handover runs it from the vmm tree after each
//! disks release, once the new tree is in place. The digests follow of a
//! running vmm (`image_root::follow`, decision CH76 rule 8) imports the
//! new set only after its bytes stay the same for the 300 s of this hook
//! (decision CH77 rule 2). So the follow never imports while the hook
//! runs, and a failed hook restores the previous tree before the follow
//! imports it.
//!
//! 1. It reads the env file of the vmm unit (`crate::env_file`) and
//!    `VMM_IMAGE_ROOT` and `VMM_ZSTD_PROGRAM` from it
//!    (`CheckImagesConfig`).
//! 2. It reads `digests.json` with `image_root::read`, the one reader of
//!    the file (decisions CH57 rule 1, CH58 rules 4 and 5). So the hook
//!    refuses a file that the vmm refuses.
//! 3. `ImageSet::check` hashes each named file as the import reads it,
//!    through `zstd -d` for a `.zst` file, and writes no file. It writes
//!    nothing in the state root.
//!
//! It writes one `verify::Report` line on stdout, one check for the env
//! file, one for `digests.json` and one for each entry. `src/main.rs`
//! exits 0 when every entry matches, else 1.

use std::path::Path;

use crate::config::CheckImagesConfig;
use crate::env_file;
use crate::image_root::{self, DIGESTS_FILE};
use crate::verify::{Check, ENV_FILE, Report};

/// Runs the checks of the module doc with the env file at `env_file`.
/// Blocks.
#[must_use]
pub fn run(env_file: &Path) -> Report {
    let config = env_file::read(env_file)
        .map_err(|error| error.to_string())
        .and_then(|env| {
            CheckImagesConfig::from_lookup(|key| env.lookup(key)).map_err(|error| error.to_string())
        });
    let config = match config {
        Ok(config) => config,
        Err(error) => return Report::new(vec![Check::fail(ENV_FILE, error)]),
    };
    let mut checks = vec![Check::pass(ENV_FILE, env_file.display())];
    let set = match image_root::read(&config.image_root) {
        Ok(set) => set,
        Err(error) => {
            checks.push(Check::fail(DIGESTS_FILE, error));
            return Report::new(checks);
        }
    };
    checks.push(Check::pass(
        DIGESTS_FILE,
        config.image_root.join(DIGESTS_FILE).display(),
    ));
    checks.extend(
        set.check(config.zstd.path())
            .into_iter()
            .map(|entry| match entry.result {
                Ok(()) => Check::pass(entry.key, &entry.file),
                Err(error) => Check::fail(entry.key, format_args!("{}: {error}", entry.file)),
            }),
    );
    Report::new(checks)
}
